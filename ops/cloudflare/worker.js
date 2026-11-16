/**
 * SciAgent 演示实例 —— Cloudflare Worker 反向代理 + 定时保活
 *
 * 解决三件事：
 * 1) 给一个好看好记的地址：sciagent.zp-avatar.cn → 平台的 sciagent-research.app.workbuddy.host
 * 2) 每 5 分钟打一次 /api/warm，让托管沙箱不进入休眠（冷启动实测 21–28 秒，偶尔超过 100 秒）
 * 3) 上游睡着时不再把 Cloudflare 的 504 丢给用户，而是回一个会自愈的「唤醒中」页
 *
 * 部署方式（Cloudflare 控制台，不用装 wrangler）：
 *   Workers & Pages → Create → Worker → 粘贴本文件 → Deploy
 *   → Settings → Variables：加 SHARECODE（明文变量即可，别加 Secret 也一样）
 *   → Settings → Trigger events → Cron Triggers：加 `* /5 * * * *`（去掉空格）
 *   → Settings → Domains & Routes → Add Custom Domain：sciagent.zp-avatar.cn
 *
 * 为什么用 Worker 而不是纯 DNS CNAME：
 *   纯 CNAME 只是把域名指过去，没有地方挂定时器，沙箱照样会睡。
 *
 * 为什么必须自己设超时：
 *   上游休眠唤醒偶尔要 100 秒以上，而 Cloudflare 边缘等满 100 秒就返回自己的
 *   504 错误页——页面全英文、只写"网关超时"，用户第一反应是"站点挂了"。
 *   本地实测：13:57 正常 → 14:02:18 返回 504 → 14:03 又正常，中间就是一次长唤醒。
 *   这里改成「单次 20 秒超时 + 重试一次 + 唤醒页」，最坏 45 秒内给用户一个
 *   会说人话、并且会自己重试的页面。
 */

const UPSTREAM = "https://sciagent-research.app.workbuddy.host";

/** 单次尝试的上限。两次加起来仍远低于 Cloudflare 的 100 秒边缘超时 */
const TIMEOUT_GET_MS = 20000;
/** 非 GET（主要是问答与上传）：首字节可能确实慢，给宽一点，但只试一次 */
const TIMEOUT_WRITE_MS = 60000;

/** 把 sharecode 补进去：用户不用记那串码，地址栏也干净 */
function withSharecode(url, sharecode) {
  if (!sharecode) return url;
  if (url.searchParams.has("sharecode")) return url;
  url.searchParams.set("sharecode", sharecode);
  return url;
}

/**
 * 带超时的 fetch。计时器在响应头到达后立刻清掉——否则长连接的 SSE 会在
 * 第 20 秒被中途掐断，"逐字输出"变成"说到一半断掉"，比慢更糟。
 */
async function fetchWithTimeout(url, init, ms) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: ac.signal });
  } finally {
    clearTimeout(timer);
  }
}

/** 上游睡着时的兜底页：会说人话，并且自己重试，不让人以为站点炸了 */
function wakingPage() {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta http-equiv="refresh" content="6" />
<title>SciAgent · 正在唤醒实例</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100dvh; display:grid; place-items:center;
         font:16px/1.7 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:#0f1115; color:#e6e8ee; }
  .box { max-width:520px; padding:40px 32px; text-align:center; }
  h1 { font-size:22px; margin:0 0 12px; font-weight:600; }
  p { margin:8px 0; color:#9aa3b2; }
  .bar { height:3px; width:180px; margin:26px auto 0; border-radius:2px;
         background:#20242e; overflow:hidden; }
  .bar i { display:block; height:100%; width:40%; border-radius:2px;
           background:#4f7cff; animation:run 1.4s ease-in-out infinite; }
  @keyframes run { 0%{transform:translateX(-100%)} 100%{transform:translateX(250%)} }
  a { color:#7b98ff; }
</style>
</head>
<body>
  <div class="box">
    <h1>正在唤醒演示实例</h1>
    <p>这个实例为省资源会休眠，唤醒需要十几秒到一分钟。</p>
    <p>页面会在 6 秒后自动重试，不用手动刷新。</p>
    <div class="bar"><i></i></div>
    <p style="margin-top:26px;font-size:13px">
      等了很久还没进来？<a href="/">点这里立即重试</a>
    </p>
  </div>
</body>
</html>`;
  return new Response(html, {
    status: 503,
    headers: {
      // 交给浏览器自动刷新；同时给 CDN 一个明确的短缓存策略
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "retry-after": "6",
    },
  });
}

export default {
  async fetch(request, env, ctx) {
    const src = new URL(request.url);
    const dst = new URL(src.pathname + src.search, UPSTREAM);
    withSharecode(dst, env.SHARECODE);

    const headers = new Headers(request.headers);
    // Host 必须交给 fetch 自己按目标域名填，手动带过去会被平台拒
    headers.delete("host");
    // 不要手动设 accept-encoding：一旦设了，Workers 就把压缩交给你自己处理，
    // resp.body 是 gzip 字节流；而下面又删掉了 content-encoding 头，
    // 浏览器会把压缩字节当明文解析，页面直接乱码。
    // 交给 Workers 默认策略（自动解压、按需重压）最省事。

    const isRead = ["GET", "HEAD"].includes(request.method);
    const init = {
      method: request.method,
      headers,
      body: isRead ? undefined : request.body,
      redirect: "manual",
    };

    let resp = null;
    let lastErr = null;
    // 读请求代价小、可以安全重发；写请求的 body 是流，重发会拿到空 body，
    // 所以只试一次，失败就直接给唤醒页（用户重试时是全新请求，能成功）。
    const attempts = isRead ? 2 : 1;
    for (let i = 0; i < attempts; i++) {
      try {
        resp = await fetchWithTimeout(
          dst.toString(),
          init,
          isRead ? TIMEOUT_GET_MS : TIMEOUT_WRITE_MS,
        );
        break;
      } catch (err) {
        lastErr = err;
        console.log(`[proxy] 第 ${i + 1}/${attempts} 次失败：${err.message}`);
        if (i + 1 < attempts) {
          // 立刻再试通常还是打不开，等一秒让沙箱把进程拉起来
          await new Promise((r) => setTimeout(r, 1000));
        }
      }
    }

    if (!resp) {
      // 顺手把保活打一下，下次刷新就大概率已经热了
      if (ctx && ctx.waitUntil) {
        const warm = new URL("/api/warm", UPSTREAM);
        withSharecode(warm, env.SHARECODE);
        ctx.waitUntil(
          fetchWithTimeout(warm.toString(), { headers: { accept: "application/json" } }, 15000)
            .catch(() => null),
        );
      }
      console.log(`[proxy] 上游不可达，返回唤醒页：${lastErr && lastErr.message}`);
      return wakingPage();
    }

    const out = new Headers(resp.headers);
    // 流式回答（SSE）不能被缓冲，否则前端"逐字出现"的效果会整段一起蹦出来
    out.set("cache-control", "no-store");
    out.delete("content-encoding");
    return new Response(resp.body, { status: resp.status, headers: out });
  },

  /** Cron Trigger：只打 /api/warm，不打 /api/chat——后者是真问答，会烧 token */
  async scheduled(event, env, ctx) {
    const url = new URL("/api/warm", UPSTREAM);
    withSharecode(url, env.SHARECODE);
    const t0 = Date.now();
    try {
      const r = await fetchWithTimeout(
        url.toString(),
        { headers: { accept: "application/json" } },
        25000,
      );
      const body = await r.text();
      console.log(`[warm] ${r.status} ${Date.now() - t0}ms ${body.slice(0, 200)}`);
    } catch (err) {
      console.log(`[warm] 失败 ${Date.now() - t0}ms ${err.message}`);
    }
  },
};
