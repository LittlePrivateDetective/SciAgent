# 云端保活：Cloudflare Worker

演示实例跑在托管沙箱上，**空闲会休眠**。休眠后第一个访客要等 20–30 秒
（实测：04:41 那次定时预热，`retrieval` 21.4 s、合计 28 s）。

仓库里已有两条防线，这个目录是第三条、也是最可靠的一条：

| 方案 | 谁在驱动 | 可靠性 | 需要什么 |
| --- | --- | --- | --- |
| 页面打开时前端静默预热 | 访客自己的浏览器 | 只能救"先看页面再提问"的人 | 无 |
| `.github/workflows/keepalive.yml` | GitHub Actions 定时任务 | ⚠️ 尽力而为，实测新仓库前 30 分钟一次没触发 | 无 |
| **本目录的 Worker** | Cloudflare 边缘节点 | ✅ 准时 | 一个 Cloudflare 账号（你已有，`zp-avatar.cn` 就托管在那儿） |

## 部署（控制台操作，5 分钟，不用装 wrangler）

1. Cloudflare 控制台 → **Workers & Pages** → Create → **Worker** → 起名 `sciagent-proxy` → Deploy
2. 点 **Edit code**，把 `worker.js` 的内容整个粘贴进去 → Deploy
3. **Settings → Variables and Secrets** → 添加明文变量
   - 名称 `SHARECODE`，值就是演示链接里 `?sharecode=` 后面那串
   （不加这个变量，Worker 会在访问时报 403「链接不完整」）
4. **Settings → Triggers → Cron Triggers** → 添加 `*/5 * * * *`
5. **Settings → Domains & Routes → Add → Custom Domain** → `sciagent.zp-avatar.cn`
   （域名已在这个账号下，证书自动签发，几分钟生效）

## 域名

已定：**`sciagent.zp-avatar.cn`**（和项目名一致，写进文档、录视频都用它）。

如果以后想在口播时念一个更短的，可以再给同一个 Worker 加一条自定义域
（`sci.zp-avatar.cn`），两条互不影响、证书各自自动签发；不加也不影响现有链接。

## 部署完怎么验证（3 条命令）

```bash
# 1. 首页 200，且地址栏不会跳到 app.workbuddy.host
curl -sI https://sciagent.zp-avatar.cn | head -3

# 2. 接口不带 sharecode 也应是 200（证明 Worker 补参数生效）
curl -s -o /dev/null -w "%{http_code}\n" https://sciagent.zp-avatar.cn/api/documents

# 3. 问答必须是流式：应该先看到 data: {...} 逐条出来
curl -sN -X POST https://sciagent.zp-avatar.cn/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question":"BERT 的预训练任务是什么","mode":"qa"}'
```

第 2 条返回 403 / 第 3 条返回一整坨 HTML，都说明 Worker 没接上（自定义域没生效或
`SHARECODE` 变量没加）。

## 生效后

| 入口 | 地址 |
| --- | --- |
| 前台 | `https://sciagent.zp-avatar.cn/` |
| 后台 | `https://sciagent.zp-avatar.cn/admin` |

地址栏不再出现 sharecode（Worker 在转发时补上），评委看到的是一个干净的域名。

## 两个注意

- **别在 Worker 里缓存 `/api/*`**：`worker.js` 里统一 `cache-control: no-store`，
  问答是流式的，一旦被边缘缓存，前端"逐字出现"会变成整段蹦出来。
- **提交材料仍然填平台链接**，不填 `sciagent.zp-avatar.cn`：`.cn` 域名在国内公开访问
  严格说要备案，比赛材料里没必要引入这个灰色项。这个域名是给你录视频、给同学
  演示、答辩现场用的。

## 如果没有 Cloudflare 账号

退而求其次：用 [cron-job.org](https://cron-job.org)（免费）加一条 5 分钟任务，
URL 填 `https://sciagent-research.app.workbuddy.host/api/warm?sharecode=<你的码>`，
方法 GET。缺点是多了第三方依赖，且它挂了不会有人告诉你。

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 打开是 403「链接不完整」 | `SHARECODE` 变量没加或写错 | 设置 → 变量和密钥 → 确认名称就是 `SHARECODE`，值不带引号和问号 |
| 页面能开但全是乱码 | 手动设了 `accept-encoding` 又删掉了 `content-encoding`（已在本仓库修正） | 用当前版本的 `worker.js` 重新粘贴部署 |
| 问答不逐字出现，憋一会儿整段蹦出 | 中间有层在缓冲 | 确认 Worker 里没有读 `resp.text()`；`cache-control: no-store` 必须保留 |
| 首页 522 / 1020 | 源站（托管沙箱）挂了或休眠中 | 先在浏览器直接打开托管地址确认它活着，再回来看 Worker |
| 改了 Worker 但表现没变 | 旧版本还在生效 / 浏览器缓存 | 重新部署后强制刷新（Ctrl+F5）；确认自定义域状态是 Active |
| 只在某些网络打不开 | 国内解析 `.cn` 到 Cloudflare 边缘偶发不稳 | 换网络复测；比赛材料仍以平台链接为准 |

## 本地怎么验这份 Worker（不用部署）

```bash
node scripts/worker_smoke.mjs
```

它在 Node 里给 `fetch` / `caches` 打桩，验证：补 sharecode、不覆盖已有值、
保留其它 query 参数、POST 方法不被吞、全站 no-store、保活打的是 `/api/warm`。
粘贴到 Cloudflare 之前先跑一遍，能挡掉大部分手误。
