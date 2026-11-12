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
5. **Settings → Domains & Routes → Add → Custom Domain** → `sci.zp-avatar.cn`
   （域名已在这个账号下，证书自动签发，几分钟生效）

## 生效后

| 入口 | 地址 |
| --- | --- |
| 前台 | `https://sci.zp-avatar.cn/` |
| 后台 | `https://sci.zp-avatar.cn/admin` |

地址栏不再出现 sharecode（Worker 在转发时补上），评委看到的是一个干净的域名。

## 两个注意

- **别在 Worker 里缓存 `/api/*`**：`worker.js` 里统一 `cache-control: no-store`，
  问答是流式的，一旦被边缘缓存，前端"逐字出现"会变成整段蹦出来。
- **提交材料仍然填平台链接**，不填 `sci.zp-avatar.cn`：`.cn` 域名在国内公开访问
  严格说要备案，比赛材料里没必要引入这个灰色项。这个域名是给你录视频、给同学
  演示、答辩现场用的。

## 如果没有 Cloudflare 账号

退而求其次：用 [cron-job.org](https://cron-job.org)（免费）加一条 5 分钟任务，
URL 填 `https://sciagent-research.app.workbuddy.host/api/warm?sharecode=<你的码>`，
方法 GET。缺点是多了第三方依赖，且它挂了不会有人告诉你。
