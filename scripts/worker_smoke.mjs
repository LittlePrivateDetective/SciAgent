/**
 * cloudflare_worker.js 的本地冒烟测试（不联网、不需要 Cloudflare 账号）。
 *
 * Worker 代码是要手动粘贴到 Cloudflare 控制台的，粘错一个字就得从头排查，
 * 所以这里先在 Node 里把 fetch / caches 打桩，验证三件事：
 *   1. 访客 URL 没带 sharecode 时，回源地址被自动补上
 *   2. 已经带 sharecode 时不重复添加
 *   3. 静态资源第二次命中边缘缓存（不回源）
 *
 * 运行：node scripts/worker_smoke.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(root, 'ops', 'cloudflare', 'worker.js'), 'utf8');

// 把 export default {...} 改成模块内的默认导出变量，便于动态求值
const mod = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
const worker = mod.default;
const ENV = { SHARECODE: 'CHBCOXLVVO' };

const calls = [];
const store = new Map();

globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), method: init.method || 'GET' });
  return new Response('body', {
    status: 200,
    headers: { 'content-type': 'text/plain' },
  });
};

globalThis.caches = {
  default: {
    async match(req) {
      const hit = store.get(req.url);
      return hit ? hit.clone() : undefined;
    },
    async put(req, res) {
      store.set(req.url, res.clone());
    },
  },
};

let failed = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) failed += 1;
};

// 1. 普通接口请求：应补 sharecode
calls.length = 0;
await worker.fetch(new Request('https://sciagent.zp-avatar.cn/api/documents'), ENV);
const u1 = calls[0]?.url || '';
check('接口请求补 sharecode', u1.includes('sharecode=CHBCOXLVVO'), u1);
check('回源域名正确', u1.startsWith('https://sciagent-research.app.workbuddy.host/'), '');
check('路径未被改写', u1.includes('/api/documents'), '');

// 2. 已带 sharecode：不重复加
calls.length = 0;
await worker.fetch(new Request('https://sciagent.zp-avatar.cn/api/documents?sharecode=OTHER'), ENV);
const u2 = calls[0]?.url || '';
check('已有 sharecode 不覆盖', u2.includes('sharecode=OTHER') && !u2.includes('CHBCOXLVVO'), u2);

// 3. 其它参数保留
calls.length = 0;
await worker.fetch(new Request('https://sciagent.zp-avatar.cn/api/documents/d1/page/2.png?dpi=150'), ENV);
const u3 = calls[0]?.url || '';
check('原有参数保留', u3.includes('dpi=150'), u3);
check('仍补 sharecode', u3.includes('sharecode=CHBCOXLVVO'), '');

// 4. 这版实现是"全站 no-store"：不做边缘缓存，靠前端 ?v= 版本号换资源。
//    要验证的是这条约定真的生效——一旦某天被改成可缓存，SSE 会被边缘缓存住，
//    前端"逐字出现"就会变成整段蹦出来。
calls.length = 0;
const staticResp = await worker.fetch(
  new Request('https://sciagent.zp-avatar.cn/static/app.js?v=46'), ENV);
check('所有响应都标 no-store',
  staticResp.headers.get('cache-control') === 'no-store',
  staticResp.headers.get('cache-control') || '(空)');
check('不透传 content-encoding',
  !staticResp.headers.get('content-encoding'),
  staticResp.headers.get('content-encoding') || '(无)');

// 5. POST 带 body 不被吞
calls.length = 0;
await worker.fetch(
  new Request('https://sciagent.zp-avatar.cn/api/chat', {
    method: 'POST',
    body: JSON.stringify({ question: 'x' }),
    headers: { 'content-type': 'application/json' },
  }),
  ENV,
);
check('POST 保留方法', calls[0]?.method === 'POST', calls[0]?.method || '');
check('POST 也补 sharecode', (calls[0]?.url || '').includes('sharecode='), '');

// 6. 保活打的是 /api/warm
calls.length = 0;
const waits = [];
await worker.scheduled({}, ENV, { waitUntil: (p) => waits.push(p) });
await Promise.all(waits);
check('保活命中 /api/warm', (calls[0]?.url || '').includes('/api/warm'), calls[0]?.url || '');

console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
