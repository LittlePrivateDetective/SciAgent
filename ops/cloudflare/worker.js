/**
 * SciAgent 演示实例 —— Cloudflare Worker 反向代理 + 定时保活
 *
 * 解决两件事：
 * 1) 给一个好看好记的地址：sci.zp-avatar.cn → 平台的 sciagent-research.app.workbuddy.host
 * 2) 每 5 分钟打一次 /api/warm，让托管沙箱不进入休眠（冷启动实测 21–28 秒）
 *
 * 部署方式（Cloudflare 控制台，不用装 wrangler）：
 *   Workers & Pages → Create → Worker → 粘贴本文件 → Deploy
 *   → Settings → Variables：加 SHARECODE（明文变量即可，别加 Secret 也一样）
 *   → Settings → Triggers → Cron Triggers：加 `* /5 * * * *`（去掉空格）
 *   → Settings → Domains & Routes → Add Custom Domain：sci.zp-avatar.cn
 *
 * 为什么用 Worker 而不是纯 DNS CNAME：
 *   纯 CNAME 只是把域名指过去，没有地方挂定时器，沙箱照样会睡。
 */

const UPSTREAM = "https://sciagent-research.app.workbuddy.host";

/** 把 sharecode 补进去：用户不用记那串码，地址栏也干净 */
function withSharecode(url, sharecode) {
  if (!sharecode) return url;
  if (url.searchParams.has("sharecode")) return url;
  url.searchParams.set("sharecode", sharecode);
  return url;
}

export default {
  async fetch(request, env, ctx) {
    const src = new URL(request.url);
    const dst = new URL(src.pathname + src.search, UPSTREAM);
    withSharecode(dst, env.SHARECODE);

    const headers = new Headers(request.headers);
    // Host 必须交给 fetch 自己按目标域名填，手动带过去会被平台拒
    headers.delete("host");
    headers.set("accept-encoding", "gzip");

    let resp;
    try {
      resp = await fetch(dst.toString(), {
        method: request.method,
        headers,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "manual",
      });
    } catch (err) {
      return new Response(`上游不可达：${err.message}`, {
        status: 502,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
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
      const r = await fetch(url.toString(), { headers: { accept: "application/json" } });
      const body = await r.text();
      console.log(`[warm] ${r.status} ${Date.now() - t0}ms ${body.slice(0, 200)}`);
    } catch (err) {
      console.log(`[warm] 失败 ${Date.now() - t0}ms ${err.message}`);
    }
  },
};
