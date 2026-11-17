/* SciAgent 前端：Vue3 + Element Plus，单文件无构建链。
 * 设计取舍：不上 React/Vite，因为 8 天工期里构建链是纯风险项；
 * 但保留了"组件化 + 状态驱动"的结构，后续换框架只替换视图层。
 */

/* ---------- 演示令牌透传（对应后台「访问令牌校验」） ----------
 * 后台把 guard_mode 切成 enforce 后，所有请求都要带令牌。这里统一包一层
 * window.fetch，而不是去改二十几处调用点——改漏一处就是"只有那个功能 401"，
 * 而以后新增的接口还会继续漏。
 * 地址栏里的 token 读完立刻从 URL 抹掉：用户截图、复制链接时不该把令牌带出去。
 */
(() => {
  const u = new URL(location.href);
  const fromUrl = u.searchParams.get("token");
  if (fromUrl) {
    try { localStorage.setItem("sciagent_token", fromUrl); } catch (_) { /* 隐私模式下忽略 */ }
    u.searchParams.delete("token");
    history.replaceState({}, "", u.pathname + (u.search ? u.search : "") + u.hash);
  }
  let token = fromUrl || "";
  try { token = token || localStorage.getItem("sciagent_token") || ""; } catch (_) { /* ignore */ }
  if (!token) return;
  const raw = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const headers = new Headers(init.headers || {});
    if (!headers.has("X-Demo-Token")) headers.set("X-Demo-Token", token);
    return raw(input, { ...init, headers });
  };
})();

const { createApp, reactive, nextTick } = Vue;

// ECharts 实例放在组件外：它持有大量 DOM 引用，放进响应式对象会被深度代理拖慢
let _chart = null;

/* ---------- 写作助手的三种出图格式（官方 1.6.2 / 1.6.3） ----------
 * 格式清单与后端 FORMATS 表一一对应；前端先按 kind 过滤，用户就不会选到
 * "雷达图 + Mermaid" 这种后端一定拒绝的组合，省掉一次 400 往返。
 */
const TOPO_FMTS = [
  { k: "mermaid", label: "Mermaid" },
  { k: "dot", label: "Graphviz" },
  { k: "tikz", label: "TikZ" },
  { k: "matplotlib", label: "Matplotlib" },
];
const CHART_FMTS = {
  bar: [
    { k: "mermaid", label: "Mermaid" },
    { k: "tikz", label: "TikZ" },
    { k: "matplotlib", label: "Matplotlib" },
  ],
  line: [
    { k: "mermaid", label: "Mermaid" },
    { k: "tikz", label: "TikZ" },
    { k: "matplotlib", label: "Matplotlib" },
  ],
  radar: [
    { k: "tikz", label: "TikZ（polaraxis）" },
    { k: "matplotlib", label: "Matplotlib" },
  ],
  table: [
    { k: "latex", label: "LaTeX 三线表" },
    { k: "markdown", label: "Markdown" },
  ],
};
const EXT = { mermaid: "mmd", dot: "gv", tikz: "tex", matplotlib: "py", latex: "tex", markdown: "md" };

// 架构图预览的 ECharts 实例。和引用图谱一样放在组件外：
// 它持有 canvas 与大量内部对象，进响应式代理后每次 setOption 都会被深拷贝一遍。
let _topoChart = null;
let _chartChart = null;   // 数据图表预览（自己画，不用 mermaid）

/* ---------- 重型库按需加载 ----------
   mermaid 3.18 MB + echarts 0.98 MB，只在"流程图/图表/写作"面板用到。
   首屏引进来 = 用户点开链接要白等几秒。改成第一次真要用时才注入 <script>，
   同一个库只会加载一次（Promise 缓存）。 */
const _vendorLoading = {};
function loadVendor(name, src) {
  if (_vendorLoading[name]) return _vendorLoading[name];
  _vendorLoading[name] = new Promise((resolve) => {
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = () => resolve(true);
    el.onerror = () => {
      console.warn("按需加载失败：", src);
      delete _vendorLoading[name];   // 允许下次重试（比如临时断网）
      resolve(false);
    };
    document.head.appendChild(el);
  });
  return _vendorLoading[name];
}

async function ensureEcharts() {
  if (window.echarts) return true;
  return loadVendor("echarts", "/static/vendor/echarts.min.js");
}

async function ensureMermaid() {
  if (window.mermaid) return true;
  const ok = await loadVendor("mermaid", "/static/vendor/mermaid.min.js");
  if (ok && window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: false,
      theme: "neutral",
      securityLevel: "loose",
      flowchart: { htmlLabels: true, useMaxWidth: true, curve: "basis" },
    });
  }
  return ok;
}

/* ---------- 极简 Markdown 渲染（够用即止：表格 / 粗体 / 行内代码 / 换行） ---------- */
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function mdToHtml(md) {
  if (!md) return "";
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = esc(md).split("\n");
  let html = "";
  let inTable = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (/^\|.*\|$/.test(line)) {
      const cells = line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue; // 分隔行
      if (!inTable) {
        // 表格必须包一层可横向滚动的容器：右栏只有 360px，6 列表格若按
        // width:100% 挤压，每列只剩 50 多像素，中文会被挤成"一字一行"
        // （实测截图把 1706.03762v1_img.pdf 竖排成一列字符）。
        html += '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
          cells.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
        inTable = true;
      } else {
        html += "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
      }
      continue;
    }
    if (inTable) { html += "</tbody></table></div>"; inTable = false; }
    if (!line) { html += "<br>"; continue; }
    // 标题与分隔线：中译英产出的英文版是 Markdown 文档（# 标题 / ## 小节），
    // 不认 `#` 的话预览区就是一堆带井号的裸文本
    const h = /^(#{1,3})\s+(.+)$/.exec(line);
    if (h) {
      const lv = h[1].length;
      html += `<h${lv}>${h[2].replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</h${lv}>`;
      continue;
    }
    if (/^(-{3,}|_{3,})$/.test(line)) { html += "<hr>"; continue; }
    html += line.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>") + "<br>";
  }
  if (inTable) html += "</tbody></table></div>";

  // 锚点做成可点击徽章：[p3@120,480,468,512]
  return html.replace(
    /\[p(\d+)@(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]/g,
    (m, p, x0, y0, x1, y1) =>
      `<span class="anchor" data-page="${p}" data-bbox="${x0},${y0},${x1},${y1}">p${p} ▸</span>`
  );
}

/* ---------- 句级精确高亮：用词级文本层把一句话还原成"贴着行"的几个小框 ---------- */

// 词形归一：只比字母数字。原文里 "state-of-the-art," 与检索文本里
// "state-of-the-art" 必须归一成同一个 token，否则整句对不上。
function normTok(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

/**
 * 把命中词下标按"行"聚成矩形。
 * words: [{x, y, w, h}]（PDF pt），idxs: 升序的命中下标。
 * 同一行（y 中心相差不超过 0.6 行高）且词间距不大时并成一个框；
 * 间距过大就断开——否则一句话跨栏/跨表格时会把中间无关内容一起涂黄。
 */
function lineRects(words, idxs) {
  const rects = [];
  let cur = null;
  for (const i of idxs) {
    const w = words[i];
    if (!w) continue;
    if (cur) {
      const sameLine = Math.abs(w.y + w.h / 2 - cur.yc) <= Math.max(2.5, cur.h * 0.6);
      const gap = w.x - cur.x1;
      if (sameLine && gap <= Math.max(10, cur.h * 1.1)) {
        cur.x1 = Math.max(cur.x1, w.x + w.w);
        cur.y0 = Math.min(cur.y0, w.y);
        cur.y1 = Math.max(cur.y1, w.y + w.h);
        continue;
      }
    }
    if (cur) rects.push(cur);
    cur = { x0: w.x, x1: w.x + w.w, y0: w.y, y1: w.y + w.h, yc: w.y + w.h / 2, h: w.h };
  }
  if (cur) rects.push(cur);
  return rects
    .filter((r) => r.x1 - r.x0 > 1 && r.y1 - r.y0 > 1)
    .map((r) => ({ x: r.x0, y: r.y0, w: r.x1 - r.x0, h: r.y1 - r.y0 }));
}

/* ---------- 公式渲染（KaTeX，修 D-66） ----------
 * 为什么不在 mdToHtml 里做：那里是纯字符串拼接，拿到的是已经转义的 HTML 片段；
 * KaTeX 要的是原始 TeX 文本，只能等 DOM 落地后再扫一遍。
 * 为什么要防抖：流式时每个 token 都会重建 innerHTML，逐帧调 KaTeX 会把主线程占满
 * （一个 60 字符的公式渲染约 3ms，一段答案里 8 个公式就是 24ms/帧，肉眼可见掉帧）。
 * CDN 没加载、公式语法有错，都必须安静降级成原文，不能影响正文可读性。
 */
const _mathTimers = new WeakMap();

function renderMath(el) {
  if (!el || typeof window.renderMathInElement !== "function") return;
  const txt = el.textContent || "";
  if (txt.indexOf("$") === -1 && txt.indexOf("\\(") === -1) return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      errorColor: "#b91c1c",
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    });
  } catch (e) {
    /* 渲染失败就保持原文，不打断阅读 */
  }
}

const vMath = {
  mounted(el) {
    renderMath(el);
  },
  updated(el) {
    const t = _mathTimers.get(el);
    if (t) clearTimeout(t);
    _mathTimers.set(
      el,
      setTimeout(() => {
        _mathTimers.delete(el);
        renderMath(el);
      }, 260)
    );
  },
};

const app = createApp({
  directives: { math: vMath },
  data() {
    return {
      warmHint: false,          // 冷启动预热中：超过 3 秒还没热完就显示提示条
      // 手机版当前显示哪一栏（docs/reader/qa）。桌面端三栏并排，这个值不起作用，
      // 只在 max-width:760px 的媒体查询里决定显示哪一块。
      mobilePane: "qa",
      info: {},
      // 正在解析入库的文件数：上传 25 页 PDF 要 13 秒，没有 loading 就会被当成"没反应"
      upQueue: 0,
      // 本次问答走哪个后端：cloud / local。只对"这一问"生效（请求头 X-LLM-Route），
      // 不是全局路由开关——全局那份在后台参数中心，动了会换掉所有人的模型。
      askRoute: "cloud",
      docs: [],
      // 勾选的文献 id：既是问答/写作的检索范围，也是批量删除的选中集
      selection: [],
      batchDeleting: false,
      activeDoc: null,
      page: 1,
      pageCount: 1,
      // 连续滚动的页面尺寸固定为"铺满宽度"。
      // 以前还有一个 fit=一页约一屏的档位，但它按屏高算宽度：面板宽 800 左右时
      // 只有 530 宽，正文小到看不清，读者还得再点一次切回来，索性去掉这个档位。
      // 在此基础上再乘一个缩放比例（%）。只改 CSS 宽度不改布局结构，
      // 放大后由阅读区横向滚动承接，文字层靠同一个 scale 自动跟随。
      zoom: 100,
      // 阅读区实测宽度（像素）。frameStyle 用它算页宽，见 measureStage()
      stageW: 0,
      pageSizes: {},
      sections: [],
      showTree: false,
      highlights: [],
      // 点章节树跳转时高亮 1 秒：secFlashPage 是"没有文字层时可退化的整页描边"
      secFlashPage: 0,
      // 页内文本层：words 里每项带预处理好的 style，避免每帧现算 200+ 个词的定位。
      // scale 由图片实际渲染宽度换算，文本层内部一律按 PDF 原始 pt 布局。
      tl: { words: [], sents: [], loaded: false, scale: 1, W: 0, H: 0, page: 0, zhLoading: false },
      // 页图加载失败：原来是浏览器默认的破图标，什么信息都不给。
      // 换成明确的一句"这一页没加载出来 + 重新加载"，评委知道是环境问题还是内容问题。
      imgFailed: false,
      // 连续滚动下每页都可能单独失败；记录到页码上，滚回去还能看到重试入口
      failedPages: {},
      imgNonce: 0,          // 重试用：拼在 URL 上绕开浏览器缓存
      refIndex: {},   // 引用编号 → {index,page,bbox,text}
      // 对话模式：false=快速（直接作答），true=深思（模型先推理再作答）。
      // 默认深思：官方要求展示思考过程，而快速模式下模型不产出推理内容，
      // 界面上就只剩几条管线阶段——那不叫"思考"。觉得慢可以点一下切成快速。
      thinkDeep: true,
      // 写作 Copilot：模型只写论述 + [n]，References 条目全部由服务端从库内元数据拼出
      wr: { idea: "", loading: false, result: null, error: "", tab: "outline" },
      // 架构拓扑图（1.6.2）+ 数据图表与图注（1.6.3）：两块共用一套代码/预览布局
      wv: {
        idea: "",
        tLoading: false, tError: "", topology: null, tFmt: "mermaid", svg: "",
        csv: "",
        cKind: "bar", cFmt: "mermaid", cTitle: "实验结果",
        cLoading: false, cError: "", chart: null, chartSvg: "", chartStale: false,
        sampleCsv: "Model,BLEU,Params\nTransformer,28.4,65\nBERT,34.5,110\nGPT-3,41.0,175",
      },
      sel: { visible: false, loading: false, src: "", zh: "", title: "划词翻译", style: {}, truncated: false, dragging: false, x: 0, y: 0 },
      citePop: { visible: false, index: 0, page: 1, bbox: null, text: "", style: {}, x: 0, y: 0, cites: [], occ: 0 },
      // 悬停翻译（官方 1.3.3.1）：默认关，打开后鼠标停 0.4 秒译整句。
      // cache 是为了"同一句反复悬停不重复请求"——一页读下来常见句子会被来回扫。
      // rects：当前悬停那一句在页面上的矩形（按行拆），用来把原句框出来
      hover: { on: false, visible: false, loading: false, src: "", zh: "", style: {},
               timer: null, cache: {}, si: null, rects: [] },
      // 中文译本（官方 1.3.3.2）：读 translations 缓存拼 Markdown，可预览可下载
      zh: {
        visible: false, loading: false, markdown: "", chars: 0, total: 0,
        translated: 0, missing: 0, verbatim: 0, ratio: 0, title: "", docId: "", truncated: false,
      },
      // 运行模式：cloud / offline。offline 会同时换掉查询向量、向量表、重排和生成，
      // 是"纯本地版"的总开关；和下面只切生成模型的 askRoute 不是一回事。
      offlineMode: false,
      offlineSwitching: false,
      offlineInfo: {},
      // 是否渲染那个开关。云端演示实例（EXPOSE_OFFLINE_SWITCH=0）为 false，
      // 界面就是纯云端形态；本机实例为 true，用来演示断网后的本地推理。
      // 初值必须是 false：给 true 的话，从首屏到 /api/system/info 返回的这一两秒
      // 里「本地 3B」按钮会先画出来再消失——线上要求"任何时刻都不出现本地入口"，
      // 闪一下同样算出现，而且录屏时最容易被截到。
      offlineExpose: false,
      messages: [],
      // 消息自增 id：模板的 :key 用它而不是下标。删中间一条时，下标会让
      // 后面的节点复用前一条的 DOM（气泡里是 v-html），出现"删了 A 却显示 B"。
      msgSeq: 0,
      question: "",
      mode: "qa",   // 保留字段兼容后端；前端不再暴露"对比"开关
      scope: "all",
      // 流式默认开：官方要求能展示思考过程，而检索/通读/推理这几步只有流式
      // 才逐个推得出来（非流式是一次性返回整段，用户只看到一个 loading 圈）。
      // 关掉仍可用：一次性返回，适合网络差时兜底。
      streamMode: true,
      trash: { visible: false, items: [], purging: false },
      // 论文发现与推荐（官方 1.4）：三个 tab 各自独立加载，互不阻塞
      disc: {
        visible: false, tab: "search", q: "", searching: false, searched: false,
        results: [], searchMs: 0,
        refLoading: false, refItems: [], refSelected: [], refTotal: 0, refMatched: 0,
        job: null, jobTimer: null,
        recLoading: false, recItems: [], recTopics: [], recDays: 180, recNote: "",
        importing: "",
      },
      // 整篇中译英（官方 1.6.4）：后台任务 + 轮询，产出 Markdown
      en: {
        visible: false, running: false, done: 0, total: 0, error: "",
        text: "", chars: 0, ms: 0, segments: 0, skipped: 0,
        docId: "", docTitle: "", timer: null,
      },
      loading: false,
      rescanning: false,
      centerView: "reader",
      // 中英对照状态：segs 是"已加载的段落序列"，按页追加
      bi: {
        segs: [],
        loaded: 0,
        loadingMore: false,
        sync: true,
        cur: 0,
        // 鼠标指向的段落：左右两栏同时高亮同一段，这是"指哪看哪"的唯一依据。
        // 之前用滚动位置算高亮，滚动时整段反复闪，反而看不出对应关系。
        hover: -1,
        // 对照区字号缩放（%）。只改 font-size，不改栏宽与段落结构，
        // 否则两侧同一段的相对位置会变，联动算出来的落点就偏了。
        zoom: 100,
        cached: { total: 0, done: 0, ratio: 0 },
        job: { running: false, done: 0, total: 0, error: "" },
        docId: "",
      },
      graph: { nodes: [], edges: [], stats: null, insights: null, clusters: [] },
      // 图谱的文献筛选：applied 为空数组 = 全库（默认）；非空 = 只在这些文献之间建图。
      // 注意不是"隐藏其他节点"，后端会在子图上重算 PageRank / 角色 / 社区。
      graphPick: { visible: false, ids: [], applied: [], loading: false },
      graphLoading: false,
      selectedNode: null,
      dup: { visible: false, message: "", items: [], pendingId: "" },
      survey: { visible: false, html: "", papers: [], loading: false },
      // 未来工作探索（赛题 1.5.4）：后端从各篇 Future Work / Limitations 原句里抽，
      // 前端只负责列出来并标出处。不缓存结果——库一变（新论文、重解析）就该跟着变。
      fw: { visible: false, items: [], method: "", stats: null, loading: false },
      // 推荐问题自带前提，点下去必须先满足前提，否则必错：
      //   前三条问的是 Transformer 原文里的作者/GPU/BLEU，只有对着那一篇才答得对；
      //   最后一条是跨文献对比，只有当库里有 BERT 与 Transformer 两篇时才成立。
      // 实测（4 题 × 2 范围 × 2 模式）：固定用「当前文献」点第 4 条，会在无关文献里
      // 检索并编出"承袭关系"；固定用「全部文献」点前三条，会答成"机构未提及"。
    };
  },
  computed: {
    activeDocId() { return this.activeDoc ? this.activeDoc.id : ""; },
    // 本地模型是否真的可用：开关 + Ollama 端口可达 + 模型名在本机，三条件后端合判。
    // 公网实例答案是 false，这里就不渲染切换按钮——不给点了必错的选项。
    localReady() { return !!(this.info.local && this.info.local.available); },

    // 选中节点的邻居：谁引用了它、它引用了谁。侧卡里把标题列出来，
    // 图上标签一多会互相压着，列表才是能逐条读清的地方。
    gNeighbors() {
      const sel = this.selectedNode;
      if (!sel) return { cites: [], citedBy: [] };
      const byId = {};
      (this.graph.nodes || []).forEach((n) => { byId[n.id] = n; });
      const cites = [], citedBy = [];
      for (const e of this.graph.edges || []) {
        if (e.source === sel.id && byId[e.target]) cites.push(byId[e.target]);
        else if (e.target === sel.id && byId[e.source]) citedBy.push(byId[e.source]);
      }
      return { cites, citedBy };
    },
    // 本地不可用时按钮改成灰色常显，靠这条说明"为什么点不了"。
    // 原来三条件不满足就整块不渲染，用户第一反应是"功能被删了"——已经遇到三次。
    localHint() {
      const l = this.info.local;
      if (!l) return "还在读取本地模型状态";
      if (!l.enabled) return "后台未启用本地模型：把 deploy/.env 的 OLLAMA_ENABLED 改成 1 再重启服务";
      if (!l.reachable) return "连不上 Ollama（" + l.url + "）。双击 Ollama 桌面应用启动后刷新本页";
      if (!(l.models || []).length) return "Ollama 里没有模型：确认 OLLAMA_MODELS 指向 D:////ollama-models";
      return l.reason || "本地模型暂不可用";
    },
    uploading() { return this.upQueue > 0; },
    // 当前生效的模型名：按钮旁边那一行小字。切到本地就显示自训模型名，
    // 让"这一问到底谁答的"在界面上有字面证据，而不是只靠耗时猜。
    activeModelName() {
      if (!this.info.llm) return "";
      if (this.askRoute === "local" && this.info.local) return this.info.local.model;
      return this.info.llm.mock ? "模拟" : this.info.llm.model;
    },
    viewTitle() {
      return { reader: "阅读器", bilingual: "中英对照", graph: "引用图谱", write: "写作助手" }[this.centerView] || "阅读器";
    },
    pageUrl() {
      return this.pageUrlFor(this.page);
    },
    /// 连续滚动要一次列出全部页码：渲染成 1..pageCount 的占位框，图片按需加载。
    pageList() {
      const n = Math.max(1, this.pageCount || 1);
      return Array.from({ length: n }, (_, i) => i + 1);
    },
    enHtml() {
      return mdToHtml(this.en.text || "");
    },
    zhHtml() {
      return mdToHtml(this.zh.markdown || "");
    },
    topoFmts() { return TOPO_FMTS; },
    topoCode() {
      const t = this.wv.topology;
      return t && t.formats && t.formats[this.wv.tFmt] ? t.formats[this.wv.tFmt].code : "";
    },
    chartFmts() { return CHART_FMTS[this.wv.cKind] || CHART_FMTS.bar; },
    chartFmtLabel() {
      const f = this.chartFmts.find((x) => x.k === this.wv.cFmt);
      return f ? f.label : "图表代码";
    },
    // 代码区标题必须描述"页面上这份结果"，而不是当前下拉框：
    // 切换类型/格式后旧结果还在，用下拉值当标题会出现"标题 Mermaid、正文 TikZ"的错位。
    chartResultLabel() {
      const c = this.wv.chart;
      if (!c) return "图表代码";
      const kind = { bar: "柱状", line: "折线", radar: "雷达", table: "表格" }[c.kind] || c.kind;
      const f = (CHART_FMTS[c.kind] || []).find((x) => x.k === c.format);
      return `${kind} · ${f ? f.label : c.format}`;
    },
  },
  mounted() {
    // 冷启动预热：云平台上服务空闲会休眠，唤醒后第一个请求要多等 20–40 秒。
    // 页面一打开就静默打一次，等用户读完标题点"发送"时后端已经是热的。
    // 不 await、不提示、失败也不打扰（本机跑时它就是一次 200ms 的自我检查）。
    const warmTimer = setTimeout(() => { this.warmHint = true; }, 3000);
    fetch("/api/warm")
      .catch(() => {})
      .finally(() => { clearTimeout(warmTimer); this.warmHint = false; });

    // 「快速 / 深思」默认深思（要看得到推理），不再跟随后端的 LLM_THINKING 覆盖：
    // 那个开关控制的是"默认要不要思维链"，是给批处理/脚本用的；界面上的默认值
    // 要服从"评委打开就能看到思考过程"这个目标。用户点了就只对本次生效，
    // 不写 localStorage——否则上次为某道题开了深思，下次所有问题都变慢。
    fetch("/api/system/info").catch(() => {});

    // Mermaid 用 startOnLoad:false —— 让它在切换 tab / 点生成时按需渲染，
    // 否则页面一进来就去扫全站 DOM，白白多一次布局。
    // Mermaid 现在按需加载（见 ensureMermaid），这里不再初始化；
    // 初始化动作跟着首次加载一起做，避免进来就扫全站 DOM。
    // 悬停翻译是"高频花钱"的功能，偏好必须跨会话记住：
    // 否则每次刷新都回到关闭，用户每次都要重新打开
    try {
      this.hover.on = localStorage.getItem("sciagent.hoverTrans") === "1";
    } catch (e) {
      this.hover.on = false;
    }
    this.loadInfo();
    this.loadDocs();
    document.addEventListener("click", async (e) => {
      const wc = e.target.closest(".wcite");
      if (wc) {
        const idx = Number(wc.dataset.i);
        const ref = ((this.wr.result && this.wr.result.references) || [])
          .find((r) => r.index === idx);
        if (ref && ref.doc_id) {
          // 写作页正文的 [n] 点下去就是"去读原文"：直接切阅读器并打开该文献。
          // 不能要求用户先手动点"阅读"——对照/图谱视图下也没有 .write-ref 可滚。
          const d = this.docs.find((x) => x.id === ref.doc_id);
          this.centerView = "reader";
          if (d) await this.openDoc(d);
          else this.onViewChange("reader");
          return;
        }
        this.jumpToRef(idx);
        return;
      }
      const el = e.target.closest(".anchor");
      if (!el) return;
      // 锚点常点在写作/对照/图谱视图里：先切回阅读器并等 DOM 挂载，
      // 否则 jump() 找不到阅读器节点，表现就是"点了没反应"。
      if (this.centerView !== "reader") {
        this.centerView = "reader";
        this.onViewChange("reader");
        await this.$nextTick();
      }
      const [x0, y0, x1, y1] = el.dataset.bbox.split(",").map(Number);
      const page = Number(el.dataset.page);
      const bbox = [x0, y0, x1, y1];
      // 锚点字符串里只有页码和坐标，没有文献 id。原先直接用「当前打开的文献」，
      // 于是点 Transformer 论文的 p7 ▸ 会跳到正在读的 ViT 第 7 页同一块坐标上——
      // 高亮跑到完全无关的段落，用户截图反馈的"高亮明显错了"就是这个。
      const ev = this.evidenceAt(page, bbox);
      this.jump({
        page,
        bbox,
        doc_id: ev ? ev.doc_id : this.activeDocId,
        text: ev ? ev.text : "",
        focus: ev ? ev.focus : null,
      });
    });
    // 点空白处收起划词翻译浮层（浮层自身点击不冒泡到这里）
    document.addEventListener("mousedown", (e) => {
      if (this.sel.visible && !e.target.closest(".sel-pop")) this.sel.visible = false;
      if (this.citePop.visible && !e.target.closest(".cite-pop") && !e.target.closest(".tw.cite")) {
        this.citePop.visible = false;
      }
      // 阅读区可用宽度：页框按像素算宽度时要靠它，窗口变化必须重新量
      this.measureStage();
    });
    // 图谱在容器尺寸变化后必须重算布局，否则窗口一缩节点就叠在左上角
    window.addEventListener("resize", () => {
      if (this.centerView === "graph" && _chart) _chart.resize();
      if (_topoChart) _topoChart.resize();
      this.measureStage();
      this.syncTextScale();
    });
    // Ctrl/⌘ + / - / 0：只影响页图缩放。输入框里打字不拦截，
    // 否则在问题框里按 Ctrl+减号会被当成缩放，输入法符号打不出来。
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || this.centerView !== "reader") return;
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
      if (e.key === "=" || e.key === "+") { e.preventDefault(); this.zoomBy(1); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); this.zoomBy(-1); }
      else if (e.key === "0") { e.preventDefault(); this.setZoom(100); }
    });
  },
  methods: {
    // mdToHtml 是模块级函数，模板里访问不到实例外的东西，必须挂到 methods 上
    mdToHtml,

    async loadInfo() {
      const r = await fetch("/api/system/info").then((x) => x.json());
      this.info = r;
      this.offlineMode = r.mode === "offline";
      this.offlineInfo = r.offline || {};
      // 用 === true 而不是 !== false：接口没返回/请求失败时宁可不渲染，
      // 也不能让线上实例因为一次读不到配置就把本地入口画出来。
      this.offlineExpose = r.expose_offline === true;
      // 离线模式下生成必然走本机，把这一问的路由也钉过去，避免按钮显示"云端"却跑本地
      if (this.offlineMode) this.askRoute = "local";
      // 后台把全局路由钉成 local 时，界面默认就该显示本地——否则徽标写着云端、
      // 实际跑的是本地，用户看不出这一问到底谁答的。
      if (r && r.llm && r.llm.route === "local") this.askRoute = "local";
      // 本地掉线（Ollama 被杀、模型被删）时把"这一问"拉回云端：
      // 否则按钮灰着、路由还指着 local，下一次提问直接报连接错误。
      if (this.askRoute === "local" && this.info.local && !this.info.local.available) {
        this.askRoute = "cloud";
        ElementPlus.ElMessage.warning("本地模型已断开，本次提问已切回云端");
      }
    },
    /* ---------------- 整篇中译英（官方 1.6.4） ---------------- */

    openEn() {
      this.en.visible = true;
      if (!this.activeDoc) return;
      // 已经生成过就直接展示，没有才开始跑——重复点不该再烧一次模型额度
      if (this.en.docId === this.activeDoc.id && this.en.chars) return;
      this.checkEnStatus(true);
    },

    async checkEnStatus(autoStart) {
      const doc = this.activeDoc;
      if (!doc) return;
      this.en.docId = doc.id;
      this.en.docTitle = doc.title || doc.filename;
      try {
        const s = await fetch(`/api/documents/${doc.id}/translate-en`).then((x) => x.json());
        this.applyEn(s);
        if (s.running) this.pollEn(doc.id);
        else if (!s.chars && autoStart) this.startEn();
      } catch (e) {
        this.en.error = "读取英文版状态失败";
      }
    },

    applyEn(s) {
      this.en.running = !!s.running;
      this.en.done = s.done || 0;
      this.en.total = s.total || 0;
      this.en.error = s.error || "";
      this.en.chars = s.chars || 0;
      this.en.ms = s.ms || 0;
      this.en.segments = s.segments || 0;
      this.en.skipped = s.skipped || 0;
      if (s.preview) this.en.text = s.preview;
    },

    async startEn() {
      const doc = this.activeDoc;
      if (!doc || this.en.running) return;
      this.en.visible = true;
      this.en.docId = doc.id;
      this.en.docTitle = doc.title || doc.filename;
      this.en.error = "";
      this.en.text = "";
      this.en.chars = 0;
      try {
        const r = await fetch(`/api/documents/${doc.id}/translate-en`, { method: "POST" }).then((x) => x.json());
        this.applyEn(r);
        this.pollEn(doc.id);
      } catch (e) {
        this.en.error = "启动译英失败，请检查服务";
      }
    },

    pollEn(docId) {
      clearInterval(this.en.timer);
      const tick = async () => {
        try {
          const s = await fetch(`/api/documents/${docId}/translate-en`).then((x) => x.json());
          this.applyEn(s);
          if (!s.running) {
            clearInterval(this.en.timer);
            this.en.timer = null;
            if (s.error) ElementPlus.ElMessage.error(s.error);
            else if (s.chars) ElementPlus.ElMessage.success(`英文版已生成（${s.chars} 字符）`);
          }
        } catch (e) {
          /* 轮询抖动不打断，下一轮再取 */
        }
      };
      tick();
      this.en.timer = setInterval(tick, 1500);
    },

    downloadEn() {
      if (!this.en.docId) return;
      window.open(`/api/documents/${this.en.docId}/translate-en.md`, "_blank", "noopener");
    },

    /* ---------------- 论文发现与推荐（官方 1.4） ---------------- */

    openDiscover(tab) {
      this.disc.visible = true;
      if (tab) this.disc.tab = tab;
      this.onDiscTab(this.disc.tab);
    },

    onDiscTab(name) {
      // 切到哪个 tab 才去拉哪个 tab 的数据：推荐要调模型（3 秒），不该在打开抽屉时就烧掉
      if (name === "refs" && this.activeDoc && !this.disc.refItems.length) this.loadRefs();
      if (name === "rec" && !this.disc.recItems.length) this.loadRec();
    },

    async doSearch() {
      const q = (this.disc.q || "").trim();
      if (!q || this.disc.searching) return;
      this.disc.searching = true;
      try {
        const r = await fetch(`/api/discover/search?q=${encodeURIComponent(q)}&max=8`).then((x) => x.json());
        this.disc.results = r.items || [];
        this.disc.searchMs = r.ms || 0;
        // 后端现在会区分"没搜到"和"arXiv 挂了"：后者必须说出来，
        // 否则用户盯着空列表会去改关键词，而真正该做的是等一会儿再试。
        if (r.ok === false && r.error) {
          ElementPlus.ElMessage.warning(r.error);
        } else if (r.note && !this.disc.results.length) {
          ElementPlus.ElMessage.info(r.note);
        }
      } catch (e) {
        ElementPlus.ElMessage.error("arXiv 检索失败，检查网络后重试");
      } finally {
        this.disc.searching = false;
        this.disc.searched = true;
      }
    },

    async importPaper(it) {
      if (this.disc.importing) return;
      this.disc.importing = it.arxiv_id;
      try {
        const r = await fetch("/api/discover/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ arxiv_id: it.arxiv_id, title: it.title || "" }),
        }).then((x) => x.json());
        if (r.ok) {
          ElementPlus.ElMessage.success(`已入库：${r.title || it.title}（${r.chunks || 0} 段）`);
          it.in_library = true;
          it.doc_id = r.doc_id || "";
          await this.loadDocs();
        } else if (r.stage === "exists") {
          ElementPlus.ElMessage.info(r.message || "库里已有这篇");
          it.in_library = true;
          it.doc_id = r.doc_id || "";
        } else if (r.stage === "duplicate") {
          // 库里有同族版本：不硬塞，让用户去左栏按版本处理
          ElementPlus.ElMessage.warning(r.message || "库中已有相近版本");
        } else {
          ElementPlus.ElMessage.error(r.error || r.message || "下载入库失败");
        }
      } catch (e) {
        ElementPlus.ElMessage.error("下载失败，检查网络后重试");
      } finally {
        this.disc.importing = "";
      }
    },

    openFromDisc(it) {
      this.disc.visible = false;
      const d = this.docs.find((x) => x.id === it.doc_id);
      if (d) this.openDoc(d);
      else this.loadDocs();
    },

    openArxiv(url) {
      if (url) window.open(url, "_blank", "noopener");
    },

    async loadRefs() {
      if (!this.activeDoc || this.disc.refLoading) return;
      this.disc.refLoading = true;
      try {
        const r = await fetch(`/api/discover/references?doc_id=${this.activeDoc.id}&limit=8`).then((x) => x.json());
        this.disc.refItems = r.items || [];
        this.disc.refTotal = r.total_refs || 0;
        this.disc.refMatched = r.matched || 0;
        // 默认勾上能下的前 5 条：多数人点开就是想"补齐"，不该让他逐个勾
        this.disc.refSelected = (r.items || [])
          .filter((i) => i.arxiv_id && !i.in_library)
          .slice(0, 5)
          .map((i) => i.arxiv_id);
      } catch (e) {
        ElementPlus.ElMessage.error("参考文献分析失败，稍后重试");
      } finally {
        this.disc.refLoading = false;
      }
    },

    toggleRef(it) {
      if (!it.arxiv_id || it.in_library) return;
      const i = this.disc.refSelected.indexOf(it.arxiv_id);
      if (i >= 0) this.disc.refSelected.splice(i, 1);
      else this.disc.refSelected.push(it.arxiv_id);
    },

    async importRefs() {
      if (!this.disc.refSelected.length || this.disc.job) return;
      const titles = {};
      for (const it of this.disc.refItems) if (it.arxiv_id) titles[it.arxiv_id] = it.title || "";
      try {
        const r = await fetch("/api/discover/references/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ arxiv_ids: this.disc.refSelected, titles }),
        }).then((x) => x.json());
        if (!r.ok) {
          ElementPlus.ElMessage.error(r.detail || "任务创建失败");
          return;
        }
        this.disc.job = { total: r.total, done: 0, ok: 0, failed: 0, current: "", items: [], state: "running" };
        this.pollJob(r.job_id);
      } catch (e) {
        ElementPlus.ElMessage.error("批量下载启动失败");
      }
    },

    pollJob(jobId) {
      clearInterval(this.disc.jobTimer);
      this.disc.jobTimer = setInterval(async () => {
        try {
          const s = await fetch(`/api/discover/jobs/${jobId}`).then((x) => x.json());
          this.disc.job = { ...this.disc.job, ...s };
          if (s.state === "done") {
            clearInterval(this.disc.jobTimer);
            this.disc.jobTimer = null;
            ElementPlus.ElMessage.success(`补全完成：成功 ${s.ok} 篇，失败 ${s.failed} 篇`);
            await this.loadDocs();
            await this.loadRefs();
          }
        } catch (e) {
          /* 轮询失败不打断：下一轮再试，用户看到的是进度停住而不是报错弹窗 */
        }
      }, 1500);
    },

    async loadRec() {
      if (this.disc.recLoading) return;
      this.disc.recLoading = true;
      this.disc.recNote = "";
      try {
        const q = `limit=6&days=${this.disc.recDays}` + (this.activeDoc ? `&doc_id=${this.activeDoc.id}` : "");
        const r = await fetch(`/api/discover/recommend?${q}`).then((x) => x.json());
        this.disc.recItems = r.items || [];
        this.disc.recTopics = r.topics || [];
        this.disc.recNote = r.note || r.error || "";
      } catch (e) {
        this.disc.recNote = "推荐加载失败，检查网络后重试";
      } finally {
        this.disc.recLoading = false;
      }
    },

    async sendFeedback(it, action) {
      try {
        await fetch("/api/discover/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ arxiv_id: it.arxiv_id, action, reason: it.reason || "" }),
        });
        if (action === "like") {
          it.liked = true;
          ElementPlus.ElMessage.success("已记录，下次推荐会更多这类方向");
        } else {
          this.disc.recItems = this.disc.recItems.filter((x) => x.arxiv_id !== it.arxiv_id);
          ElementPlus.ElMessage.info("已隐藏，这类方向会减少");
        }
      } catch (e) {
        ElementPlus.ElMessage.error("反馈提交失败");
      }
    },

    async loadDocs() {
      const r = await fetch("/api/documents").then((x) => x.json());
      this.docs = (r.documents || []).map((d) => ({ ...d, _sel: this.selection.includes(d.id) }));
      if (!this.activeDoc && this.docs.length) {
        await this.openDoc(this.docs[0]);
        // 手机版首屏停在「问答」：手机上打开就该能直接提问，而不是先看到一页 PDF。
        // 桌面端三栏并排，这个值不参与布局。
        this.mobilePane = "qa";
        // 深链：#bi 直接进对照视图，评审可以收藏链接直接演示
        if (location.hash === "#bi") {
          this.centerView = "bilingual";
          this.loadBi(true);
        }
      }
    },
    syncSelection() {
      this.selection = this.docs.filter((d) => d._sel).map((d) => d.id);
    },

    toggleSelectAll() {
      if (!this.docs.length) return;
      const all = this.selection.length === this.docs.length;
      this.docs.forEach((d) => { d._sel = !all; });
      this.selection = all ? [] : this.docs.map((d) => d.id);
    },

    clearSelection() {
      this.docs.forEach((d) => { d._sel = false; });
      this.selection = [];
    },

    /// 批量删除选中项。走 /batch-delete 一次收口，失败项单独报。
    async removeSelected() {
      const ids = [...this.selection];
      if (!ids.length || this.batchDeleting) return;
      const picked = this.docs.filter((d) => ids.includes(d.id));
      const names = picked.slice(0, 3).map((d) => d.title || d.filename).join("、");
      const more = picked.length > 3 ? ` 等 ${picked.length} 篇` : "";
      // 全选删除等于清空语料，这种操作必须在确认框里说清后果，
      // 否则回车一按，问答就再没有可检索的东西了
      const warn = ids.length === this.docs.length && this.docs.length > 1
        ? `这是文献库里的全部 ${this.docs.length} 篇，删完就没有可检索的语料了。\n\n`
        : "";
      try {
        await ElementPlus.ElMessageBox.confirm(
          `${warn}将把 ${ids.length} 篇移入回收站：\n${names}${more}\n\n源文件保留，可在左栏「回收」里恢复。`,
          `删除 ${ids.length} 篇文献`,
          { type: "warning", confirmButtonText: "移入回收站", cancelButtonText: "取消" }
        );
      } catch (e) {
        return;
      }

      this.batchDeleting = true;
      try {
        const r = await fetch("/api/documents/batch-delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids, reason: "ui_batch_delete" }),
        }).then((x) => x.json());
        if (!r.ok) {
          ElementPlus.ElMessage.error(r.detail || "批量删除失败");
          return;
        }
        ElementPlus.ElMessage.success(r.message || `已移入回收站 ${r.deleted} 篇`);
        if ((r.failed || []).length) {
          ElementPlus.ElMessage.warning(`${r.failed.length} 篇没能删除，列表里仍然保留`);
        }
        // 正在读的那篇被删了：清干净，否则阅读器停在一个已不存在的文档上
        if (this.activeDoc && ids.includes(this.activeDoc.id)) {
          this.activeDoc = null;
          this.highlights = [];
          this.tl = { ...this.tl, words: [], sents: [], loaded: false };
        }
        this.selection = [];
        await this.loadDocs();
        this.loadInfo();
      } catch (e) {
        ElementPlus.ElMessage.error("批量删除失败：" + e);
      } finally {
        this.batchDeleting = false;
      }
    },

    async doUpload(opt) {
      const fd = new FormData();
      fd.append("file", opt.file);
      this.upQueue += 1;
      try {
        const resp = await fetch("/api/documents/upload", { method: "POST", body: fd });
        // 后端没起、或网关把连接掐掉时 fetch 会直接抛，也可能返回 502 的纯文本。
        // 原来这里既没 try/catch 也不看 resp.ok，结果是"点了上传什么都不发生"——
        // 看起来像功能坏了，其实是请求根本没到达后端。
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const r = await resp.json();
        if (r.stage === "duplicate") {
          this.dup = { visible: true, message: r.message, items: r.duplicates || [], pendingId: r.pending_id };
          return;
        }
        if (!r.ok) {
          ElementPlus.ElMessage.error(r.error || "上传失败");
          return;
        }
        ElementPlus.ElMessage.success(`入库成功：切成 ${r.chunks} 段原文，用时 ${r.elapsed_ms}ms`);
        this.loadDocs();
        this.loadInfo();
      } catch (e) {
        ElementPlus.ElMessage.error(
          `上传失败：${(e && e.message) || e}。请确认后端在 8013 端口运行（可双击 ops/start_8013.bat）`);
      } finally {
        this.upQueue = Math.max(0, this.upQueue - 1);
      }
    },

    async resolveDup(action) {
      const target = this.dup.items.length ? this.dup.items[0].doc_id : null;
      const r = await fetch("/api/documents/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pending_id: this.dup.pendingId, action, target_doc_id: target }),
      }).then((x) => x.json());
      this.dup.visible = false;
      if (r.ok) {
        ElementPlus.ElMessage.success(r.message || "已处理");
        this.loadDocs();
      } else {
        ElementPlus.ElMessage.error(r.error || "处理失败");
      }
    },

    async rescan() {
      this.rescanning = true;
      const askedAt = Math.floor(Date.now() / 1000);
      const r = await fetch("/api/library/rescan", { method: "POST" }).then((x) => x.json());
      ElementPlus.ElMessage.info(r.message || "已开始导入");
      // 后台任务，轮询几次让用户看到增长；跑完后把"入库 N 篇"和
      // "判重跳过 M 篇"一起报出来——只显示磁盘上的 39 篇而库里是 36 篇，
      // 用户会以为导入丢了论文。
      let n = 0;
      const timer = setInterval(async () => {
        await this.loadDocs();
        await this.loadInfo();
        try {
          const st = await fetch("/api/library/rescan/last").then((x) => x.json());
          if (st && st.total && (st.at || 0) >= askedAt - 1) {
            clearInterval(timer);
            this.rescanning = false;
            let msg = `导入完成：入库 ${st.ok} 篇`;
            if (st.dup) msg += `，判重跳过 ${st.dup} 篇`;
            if (st.failed) msg += `，失败 ${st.failed} 篇`;
            ElementPlus.ElMessage[st.failed ? "warning" : "success"](msg);
            const dups = st.duplicates || [];
            if (dups.length) {
              // 判重是正确行为，但必须说清"跟谁重复"，否则等于没解释
              console.info("判重跳过：", dups.map((d) => `${d.file} ≈ ${d.same_as}`).join("；"));
            }
            return;
          }
        } catch (e) { /* 还没跑完就继续轮询 */ }
        if (++n > 40) { clearInterval(timer); this.rescanning = false; }
      }, 3000);
    },

    /// 手机版切栏。切到问答时顺手滚到最新一条：否则从阅读器点引用回来，
    /// 看到的是上一次提问的开头，还以为没跳过来。
    goMobile(pane) {
      this.mobilePane = pane;
      if (pane === "qa") {
        this.$nextTick(() => {
          const el = this.$refs.chatList;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    },

    async openDoc(d) {
      this.activeDoc = d;
      this.mobilePane = "reader";   // 手机上点文献名 = 想看这篇，直接切过去
      this.scope = "doc";
      this.page = 1;
      this.pageCount = d.page_count || 1;
      this.highlights = [];
      this.refIndex = {};           // 先清空上一篇的条目，避免角标临时指错文献
      this.failedPages = {};
      this.bi.segs = [];
      this.bi.loaded = 0;
      this.bi.docId = d.id;
      this.sel.visible = false;
      this.citePop.visible = false;
      const [pages, secs] = await Promise.all([
        fetch(`/api/documents/${d.id}/pages`).then((x) => x.json()),
        fetch(`/api/documents/${d.id}/sections`).then((x) => x.json()),
      ]);
      this.pageSizes = {};
      (pages.pages || []).forEach((p) => (this.pageSizes[p.page] = p));
      this.sections = secs.sections || [];
      // 引用索引按整篇加载一次即可：正文角标 → References 条目的映射与页码无关
      await this.loadRefIndex(d.id);
      this.loadTextLayer();
      // 换文献要从第 1 页看起：连续滚动下滚动位置不会自己归零，不重置会停在上一次的页码
      this.$nextTick(() => {
        this._scrollLock = 0;
        this.measureStage();      // 阅读区宽度可能因章节树开关而变，重算页宽
        this.scrollToPage(1, false);
      });
      // 对照视图下切文献要立刻换成新文献的段落，否则会继续显示上一篇
      if (this.centerView === "bilingual") this.loadBi(true);
    },

    pageUrlFor(p) {
      if (!this.activeDoc) return "";
      // 放大后按档位提高渲染 dpi：130dpi 的页图在 180% 以上会明显发虚，
      // 但每档都换 URL 会让所有已加载页重下，所以只用三档，不逐像素跟随。
      // 原来这里拼的是 Date.now()：每次重算都换 URL，翻回上一页也要重新下载 70KB。
      // 改成只有点「重新加载」才变（imgNonce），其余交给服务端的 max-age 缓存。
      const dpi = this.zoom >= 175 ? 220 : this.zoom >= 140 ? 165 : 130;
      return `/api/documents/${this.activeDoc.id}/page/${p}.png?dpi=${dpi}&v=${this.imgNonce}`;
    },

    /// 占位框按该页真实宽高比撑开，宽度 = 阅读区可用宽度（铺满）。
    /// 只有"铺满宽度"一种档位：一页一屏那个档位按屏高反算宽度，正文会小到看不清。
    /// 再乘上 zoom 才是最终宽度。
    ///
    /// 宽度必须用像素算，不能交一个百分数给 CSS：
    /// 100% 会被容器的 maxWidth 钳住，而放大到 110% 时又不钳了，
    /// 结果"从 100% 调到 110%"会突然跳一大截（实测 768px → 990px）。
    /// 这里先用容器可用宽算出基准像素，再乘档位，缩放就是严格线性的。
    frameStyle(p) {
      const meta = this.pageSizes[p];
      const w = (meta && meta.width) || 612;
      const h = (meta && meta.height) || 792;
      const ratio = w / h;
      const avail = Math.max(240, (this.stageW || 900) - 24);   // 减掉 .page-stage 左右 padding
      // 不再设 900 上限：宽屏/宽面板下读者要的就是"铺满"，上限会让页图两侧留白。
      // 真嫌大字，用工具条的缩放（50%–300%）压回去。
      const base = avail;
      const z = this.zoom / 100;
      return {
        aspectRatio: `${w} / ${h}`,
        width: `${Math.round(base * z)}px`,
        maxWidth: "none",       // 基准已经算过容器宽度，这里再钳就会让放大失效
      };
    },

    /// 缩放档位：手点一次走一格，避免 1% 一格点几十次
    zoomSteps() {
      return [50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300];
    },

    /// 量一次阅读区可用宽度。frameStyle 需要它把页宽算成像素，
    /// 否则只能靠 CSS 百分比，而百分比在"基准宽度"和"放大宽度"两种算法下不一致。
    measureStage() {
      const st = this.$refs.pageStage;
      const w = st ? st.clientWidth : 0;
      if (w && w !== this.stageW) {
        this.stageW = w;
        this.$nextTick(() => this.syncTextScale());
      }
    },

    zoomBy(dir) {
      const steps = this.zoomSteps();
      const i = steps.findIndex((s) => s >= this.zoom);
      const cur = i < 0 ? steps.length - 1 : (steps[i] === this.zoom ? i : Math.max(0, i - 1));
      const next = steps[Math.min(steps.length - 1, Math.max(0, cur + dir))];
      this.setZoom(next);
    },

    /// 换缩放时保持"正在看的那一页的同一位置"不动：
    /// 直接重排会让视口跳到别的段落，放大一下还要重新找刚才读到哪儿。
    setZoom(v) {
      const z = Math.max(50, Math.min(300, Math.round(v)));
      if (z === this.zoom) return;
      const stage = this.$refs.pageStage;
      let anchor = null;
      if (stage) {
        const top = stage.scrollTop + 4;
        const frames = stage.querySelectorAll(".page-frame");
        let el = null;
        for (const f of frames) {
          if (f.offsetTop <= top) el = f;
          else break;
        }
        if (el) anchor = { page: Number(el.dataset.page), ratio: (top - el.offsetTop) / Math.max(el.offsetHeight, 1) };
      }
      this.zoom = z;
      this.$nextTick(() => {
        const st = this.$refs.pageStage;
        if (st && anchor) {
          const f = st.querySelector(`.page-frame[data-page="${anchor.page}"]`);
          if (f) {
            this._scrollLock = Date.now() + 400;
            st.scrollTop = f.offsetTop + anchor.ratio * f.offsetHeight - 4;
          }
        }
        this.syncTextScale();
      });
    },

    onStageWheel(e) {
      // Ctrl/⌘ + 滚轮：与浏览器缩放同一套习惯，但在阅读器内只缩放页图，
      // 不把整个界面（含左右两栏）一起放大，否则工具栏会被挤出屏幕。
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      this._wheelAcc = (this._wheelAcc || 0) + e.deltaY;
      if (Math.abs(this._wheelAcc) < 40) return;
      const dir = this._wheelAcc > 0 ? -1 : 1;
      this._wheelAcc = 0;
      this.zoomBy(dir);
    },

    goPage(p, opts) {
      if (p < 1 || p > this.pageCount) return;
      this.page = p;
      this.imgFailed = !!this.failedPages[p];
      this.highlights = [];
      this.sel.visible = false;
      this.citePop.visible = false;
      this.loadTextLayer();
      // noScroll：调用方自己决定滚到哪（章节跳转要把高亮居中，不能先滚到页顶）
      if (!(opts && opts.noScroll)) this.$nextTick(() => this.scrollToPage(p));
    },

    /* ---------------- 章节树跳转：跳过去 + 高亮 1 秒 ---------------- */

    /**
     * 点章节树：翻到该节所在页，并把**标题文字**高亮 1 秒。
     *
     * 为什么只闪标题而不是整页：541 页的书里翻到某一页，读者仍然不知道
     * "这一节的标题在哪一行"；把标题那几个词点亮，视线一下就落上去。
     * 定位用页面文本层的词坐标做匹配（章节表里没有 bbox，也不必为此改数据库）。
     * 匹配不到（扫描件没有文字层、或标题被拆行）时退化成整页描边闪一下。
     */
    async goSection(s) {
      if (!s) return;
      // 先别用"页顶对齐"这种方式滚：章节标题常在页面的中下部，页顶对齐后
      // 它正好掉到视口下面（放大页码时更明显），读者只看到高亮框一闪就没有了。
      // 这里先只换页，等拿到高亮词，再把高亮滚到视口中间。
      this.goPage(s.page, { noScroll: true });
      const page = this.page;
      const docId = this.activeDoc && this.activeDoc.id;
      const started = Date.now();
      // 等这一页的文本层到位：goPage 里是异步请求，最多等 1.5s 就放弃高亮
      while (Date.now() - started < 1500) {
        if (this.tl.page === page && this.tl.loaded) break;
        await new Promise((r) => setTimeout(r, 60));
      }
      if (!this.activeDoc || this.activeDoc.id !== docId || this.page !== page) return;
      this.flashSection(s, page);
    },

    flashSection(s, page) {
      clearTimeout(this._secFlashTimer);
      const ms = Number(localStorage.getItem("sciagent.secFlashMs")) || 1000;
      const words = this.page === page && this.tl.page === page ? this.tl.words : [];
      const hit = words.length ? this.matchSectionWords(words, s.title) : [];
      if (hit.length) {
        hit.forEach((w) => { w.secflash = true; });
        this.centerOnFlash(ms);
        this._secFlashTimer = setTimeout(() => {
          hit.forEach((w) => { w.secflash = false; });
        }, ms);
        return;
      }
      // 没有文字层：整页描边闪一下，至少让读者知道"跳到这一页了"
      this.scrollToPage(page);
      this.secFlashPage = 0;
      this.$nextTick(() => {
        this.secFlashPage = page;
        this._secFlashTimer = setTimeout(() => { this.secFlashPage = 0; }, ms);
      });
    },

    /**
     * 把刚点亮的高亮滚到视口中间。
     *
     * 为什么不是"滚到那一页顶部"：章节标题在页面里的纵坐标是随机的，页顶对齐后
     * 高亮经常落在视口下缘之外——放大到 150% 时几乎必然被挡住，用户以为没跳过去。
     * 这里按高亮框的实际屏幕位置反算滚动量，让它的中心落在视口 45% 高度处
     * （略偏上，符合阅读习惯，也留出上方上下文）。
     * 高亮组比视口还高时（整节标题被拆成多行）以上边缘为准，避免滚过内容。
     */
    async centerOnFlash(holdMs = 1000) {
      const stage = this.$refs.pageStage;
      if (!stage) return;
      await this.$nextTick();
      const boxes = Array.from(stage.querySelectorAll(".tw.secflash"));
      if (!boxes.length) return;
      const sr = stage.getBoundingClientRect();
      const rects = boxes.map((b) => b.getBoundingClientRect());
      const top = Math.min(...rects.map((r) => r.top));
      const bottom = Math.max(...rects.map((r) => r.bottom));
      const h = bottom - top;
      const viewH = stage.clientHeight;
      // 程序化滚动会沿途触发 scroll，把"当前页"算成中间经过的页，先上锁
      this._scrollLock = Date.now() + 700;
      let target;
      if (h > viewH * 0.7) {
        target = stage.scrollTop + (top - sr.top) - viewH * 0.12;
      } else {
        const center = (top + bottom) / 2;
        target = stage.scrollTop + (center - sr.top) - viewH * 0.45;
      }
      // 别把当前页的页顶滚出视口上方：那样"当前页"会被算成上一页，页码乱跳。
      // 标题正好在页面顶端时，这条会把滚动量拉回"页顶对齐"，属预期行为。
      const frame = stage.querySelector(".page-frame.is-cur");
      if (frame) target = Math.max(target, frame.offsetTop - 8);
      stage.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
      // 滚动期间锁住"当前页"计算，免得高亮刚亮起来就被切页清掉
      this._scrollLock = Date.now() + holdMs + 200;
    },

    /// 在页面词层里找出"标题那一行"，返回该行的词数组。
    matchSectionWords(words, title) {
      const norm = (t) => String(t || "").replace(/\s+/g, "").toLowerCase();
      const target = norm(title);
      if (!target) return [];
      const PH = this.tl.H || 792;
      const lines = [];
      for (const w of words) {
        // 同一个视觉行：y 相差小于 3pt。竖排/水印词（h 很大）不参与匹配。
        if (w.h > 40) continue;
        let line = lines.find((l) => Math.abs(l.y - w.y) < 3);
        if (!line) { line = { y: w.y, words: [] }; lines.push(line); }
        line.words.push(w);
      }
      for (const l of lines) {
        l.words.sort((a, b) => a.x - b.x);
        l.text = norm(l.words.map((w) => w.t).join(""));
        // 字号取该行词高中的中位数，用来在多个候选里挑"最像标题"的那个
        const hs = l.words.map((w) => w.h).sort((a, b) => a - b);
        l.size = hs[Math.floor(hs.length / 2)] || 0;
        // 页眉/页脚：书里每页顶部都印着"当前小节名 + 页码"，和小节标题**完全同名**。
        // 不排除的话，点 1.2 会高亮到页眉那一条，正文里的真标题反而没亮（实测踩到）。
        // 阈值放到 13%：这本书的页眉在 y≈108pt，比 9%（71pt）还低一点。
        l.band = l.y < PH * 0.13 || l.y > PH * 0.94;
      }
      // 先整行匹配；书名/期刊名里标题常带页码或连字符，再退一步用前 8 个字符
      const keys = [target];
      if (target.length > 8) keys.push(target.slice(0, 8));
      for (const k of keys) {
        if (k.length < 2) continue;
        const cands = [];
        for (const l of lines) {
          const at = l.text.indexOf(k);
          if (at < 0) continue;
          cands.push({ line: l, at, exact: l.text === target });
        }
        if (!cands.length) continue;
        // 排序：先排除页眉页脚 → 字号大的优先（标题总比正文大）→ 整行精确命中优先
        cands.sort((a, b) => (a.line.band - b.line.band)
          || (b.line.size - a.line.size)
          || ((b.exact ? 1 : 0) - (a.exact ? 1 : 0)));
        const best = cands[0];
        const l = best.line;
        // 只点亮标题那几个词：中文期刊常把"正文 + 小节标题"排在同一行，
        // 整行涂色会连半句正文一起染黄，看起来像误选。
        let acc = 0;
        const sel = [];
        for (const w of l.words) {
          const t = norm(w.t);
          const start = acc;
          acc += t.length;
          if (acc > best.at && start < best.at + k.length) sel.push(w);
        }
        return sel.length ? sel : l.words;
      }
      return [];
    },

    scrollToPage(p, smooth = true) {
      const stage = this.$refs.pageStage;
      const frame = stage && stage.querySelector(`.page-frame[data-page="${p}"]`);
      if (!stage || !frame) return;
      // 程序化滚动会一路触发 scroll 事件，沿途每一页都会被算成"当前页"，
      // 于是文本层被反复加载、页码乱跳。锁 700ms，让平滑滚动走完再交还给用户。
      this._scrollLock = Date.now() + 700;
      const top = Math.max(0, frame.offsetTop - 8);
      // 不用 CSS scroll-behavior：它会让"瞬移"也变平滑，跳引用时会横穿整篇文档
      if (smooth) stage.scrollTo({ top, behavior: "smooth" });
      else stage.scrollTop = top;
    },

    /// 连续滚动下不再有"翻页"，只做一件事：算出当前看的是第几页。
    /// 以视口中线所在的页为当前页——比"顶部对齐"更符合阅读直觉（正在读的那页）。
    onPageScroll(e) {
      if (this.centerView !== "reader" || !this.activeDoc) return;
      if (Date.now() < (this._scrollLock || 0)) return;
      const stage = e.target;
      if (!stage) return;
      if (this._pageRaf) return;
      this._pageRaf = requestAnimationFrame(() => {
        this._pageRaf = 0;
        const mid = stage.scrollTop + stage.clientHeight * 0.4;
        const frames = stage.querySelectorAll(".page-frame");
        let cur = this.page;
        for (const f of frames) {
          if (f.offsetTop <= mid) cur = Number(f.dataset.page);
          else break;
        }
        if (cur !== this.page) {
          this.page = cur;
          this.imgFailed = !!this.failedPages[cur];
          this.highlights = [];
          this.sel.visible = false;
          this.citePop.visible = false;
          // 文本层只跟当前页，切页才请求；滚动中连续请求会被"页码对不上就丢弃"挡掉
          this.loadTextLayer();
        }
      });
    },

    /* ---------------- 页内文本层：让页图上的文字可选、角标可点 ---------------- */

    async loadRefIndex(docId) {
      // 不在这里清空：清空会让"索引已到但新的还没回来"的几百毫秒里，
      // 正文所有角标都退化成"不可跳"的橙色，用户正好在这时点就是一次误报。
      // 换文献时由 openDoc 同步清空，保证不会串到上一篇的条目上。
      try {
        const r = await fetch(`/api/documents/${docId}/references`).then((x) => x.json());
        if (!this.activeDoc || this.activeDoc.id !== docId) return;  // 期间切了文献
        const map = {};
        (r.entries || []).forEach((e) => (map[e.index] = e));
        this.refIndex = map;
      } catch (e) {
        console.warn("引用索引加载失败", e);
      }
    },

    /// 角标能不能跳，取决于"此刻"的引用索引。
    /// 原来在 makeWord 时算一次存进 w.refhit，索引晚到（并发加载/切换文献）
    /// 就会永久留成不可点的橙色角标，必须刷新页面才好——改成渲染时现查。
    isRefhit(w) {
      return !!w.cite && !!this.refIndex[w.refIndex];
    },

    async loadTextLayer() {
      const doc = this.activeDoc;
      if (!doc) {
        this.tl = { words: [], sents: [], loaded: false, scale: 1, W: 0, H: 0, page: 0 };
        return;
      }
      const page = this.page;
      this.tl = { words: [], sents: [], loaded: false, scale: this.tl.scale || 1, W: 0, H: 0, page, zhLoading: false };
      try {
        const r = await fetch(`/api/documents/${doc.id}/page/${page}/text`).then((x) => x.json());
        // 快速翻页时旧请求可能后到：页码或文献对不上就丢弃，否则会出现"第 5 页的
        // 文字叠在第 6 页图上"，比没有文本层更糟。
        if (this.page !== page || !this.activeDoc || this.activeDoc.id !== doc.id) return;
        this.tl.W = r.width || 612;
        this.tl.H = r.height || 792;
        this.tl.words = (r.words || []).map((w) => this.makeWord(w));
        // 悬停翻译要的是"整句"，不是单个词。句子在这里一次性切好，
        // 悬停时只做一次数组取值——否则每次 mouseenter 都要重扫 200 个词。
        this.tl.sents = this.buildSentences(this.tl.words);
        this.tl.loaded = true;
        this.$nextTick(() => this.syncTextScale());
      } catch (e) {
        console.warn("文本层加载失败", e);
        this.tl.loaded = true;
      }
    },

    /// 把一个 PDF 词转成绝对定位的 span 数据。
    /// 文本层内部一律按 PDF 原始 pt 布局（1pt = 1px），整体再靠 transform: scale
    /// 缩放到图片实际宽度——这样窗口缩放只需改一个 scale 值，不用重算几百个词的样式。
    makeWord(w) {
      const [t, x, y, ww, hh] = w;
      const h = Math.max(hh, 1);
      // 正文角标：LaTeX 常见 [11] [3, 7] [11], 三种形态，统一按首个数提取
      const m = /^\[(\d{1,3})[,\]]/.exec(t);
      const idx = m ? Number(m[1]) : 0;
      return {
        t,
        // 数值坐标留给"句级精确高亮"用：几何计算不能反复去 parse "12.3px" 字符串
        x,
        y,
        w: Math.max(ww, 1),
        h,
        cite: !!idx,
        refIndex: idx,
        refhit: !!idx && !!this.refIndex[idx],
        style: {
          left: x + "px",
          top: y + "px",
          width: Math.max(ww, 1) + "px",
          height: h + "px",
          fontSize: h + "px",
          lineHeight: h + "px",
          // 竖排文字（arXiv 侧边水印、页边竖排书名）的词框高度是正常行高的十几倍，
          // 会盖在正文词上面：鼠标停在正文上，命中的却是水印，悬停译出
          // "arXiv:2310.06825v1 [cs.CL] 2023年10月10日"（实测如此）。
          // 这类文字没有阅读价值，让指针穿透即可，不影响正文的选中与悬停。
          ...(h > 40 ? { pointerEvents: "none" } : {}),
        },
      };
    },

    syncTextScale() {
      // 连续滚动后一页对应一张图，ref 不能再写死成 pageImg；直接按页码取当前页那张
      const img = this.$refs["img" + this.page];
      const el = Array.isArray(img) ? img[0] : img;
      if (!el || !el.clientWidth || !this.tl.W) return;
      this.tl.scale = el.clientWidth / this.tl.W;
    },

    onWordClick(w, ev) {
      if (!w.cite) return;
      ev && ev.stopPropagation();
      const e = this.refIndex[w.refIndex];
      if (!e) {
        // 编号在 References 区找不到（引用编号错乱/OCR 缺失）时如实说明，
        // 不做"跳到 References 首页"这种假装成功的兜底。
        ElementPlus.ElMessage.warning(`References 区未找到第 [${w.refIndex}] 条`);
        return;
      }
      this.openRefEntry(e);
    },

    async openRefEntry(e) {
      this.citePop = {
        visible: true,
        index: e.index,
        page: e.page,
        bbox: e.bbox,
        text: e.text || "",
        style: {},
        x: 0,
        y: 0,
        cites: [],
        occ: 0,
      };
      // 反查"正文哪里引用了它"，与这次正跳并行：正跳是本地几何计算，很快；
      // 反查要扫全文，慢了就慢在列表上，不该拖住跳转。
      this.loadCites(e.index);
      await this.jump({ page: e.page, bbox: e.bbox, doc_id: this.activeDocId, text: e.text });
    },

    /** 第 n 条参考文献被正文哪些地方引用（官方 1.3.2 的「从 References 跳回去」）。 */
    async loadCites(index) {
      if (!this.activeDoc) return;
      const docId = this.activeDoc.id;
      try {
        const r = await fetch(`/api/documents/${docId}/references/${index}/cites`).then((x) => x.json());
        // 请求返回时用户可能已经点了别的条目：只认当前条目的结果，否则列表会串号
        if (!this.citePop.visible || this.citePop.index !== index) return;
        this.citePop.cites = r.hits || [];
        this.citePop.occ = r.occurrences || 0;
      } catch (e) {
        // 反查失败不影响正向跳转，保持静默：浮层里少一块，不等于功能坏了
      }
    },

    async jumpToCite(hit) {
      this.citePop.visible = false;
      const bbox = Array.isArray(hit.bbox) && hit.bbox.length === 4 ? hit.bbox : null;
      if (!bbox) {
        ElementPlus.ElMessage.warning("这条引用没有可用的坐标，已跳到对应页");
        this.goPage(hit.page);
        return;
      }
      await this.jump({
        page: hit.page,
        bbox,
        doc_id: this.activeDocId,
        text: hit.sentence || hit.context || "",
        // 带 focus 才会按句高亮；不带就只能按 bbox 内所有词逐行画
        focus: hit.sentence ? [hit.sentence] : null,
      });
    },

    /* ---------------- 悬停翻译（官方 1.3.3.1） ---------------- */

    saveHoverPref(v) {
      try {
        localStorage.setItem("sciagent.hoverTrans", v ? "1" : "0");
      } catch (e) {
        /* 隐私模式下 localStorage 会抛异常，功能不该因此失效 */
      }
      if (!v) this.hideHover(true);
    },

    /**
     * 按顺序把词拼成句子，并给每个词回填句子号。
     *
     * 为什么在前端切而不是后端：句子只服务于悬停，和检索、锚点无关；
     * 放在这里就不用改文本层接口，也不会让"页码/坐标"这套契约多一个字段。
     */
    buildSentences(words) {
      const sents = [];
      // 这些缩写后面跟句点时不是句子结束，切错了译文会缺主语
      const ABBR = /(?:e\.g|i\.e|et al|fig|eq|tab|sec|ref|vs|no|cf|approx|resp|dr|prof)\.$/i;
      // 节标题是天然的句子边界：作者列表用逗号分隔、整段没有句号，
      // 不切的话"作者行 + Abstract + 第一句"会粘成一大段，悬停作者名就译出几百字
      const HEADS = /^(Abstract|Introduction|Related\s+Work|Background|Methods?|Experiments?|Results?|Discussion|Conclusions?|References|Appendix|Acknowledg\w*)$/i;
      let cur = [];
      let buf = "";
      const flush = () => {
        if (!cur.length) return;
        sents.push({ text: buf.trim(), idx: cur.slice() });
        cur = [];
        buf = "";
      };
      words.forEach((w, i) => {
        if (cur.length && buf.length > 30 && HEADS.test(w.t)) flush();
        cur.push(i);
        buf += (buf ? " " : "") + w.t;
        const ends = /[.?!]$/.test(w.t) && !ABBR.test(w.t);
        // 240 字符封顶：超长"句子"多半是作者列表、表格行或断句失败的整段，
        // 一次送去翻译既慢又贵，译出来也不准
        if (ends || buf.length > 240) flush();
      });
      flush();
      sents.forEach((s, si) => s.idx.forEach((i) => { words[i].si = si; }));
      return sents;
    },

    onWordEnter(w, i, ev) {
      if (!this.hover.on || this.sel.visible || this.sel.dragging) return;
      // 角标是"点击跳转"的目标，鼠标扫过时弹译文会挡住点击
      if (w.cite) return;
      const sents = this.tl.sents || [];
      const sent = w.si != null ? sents[w.si] : null;
      if (!sent || !sent.text || sent.text.length < 8) return;

      // 高亮立刻给，不跟着 380ms 的翻译延迟：先告诉用户"要译的是这句"，
      // 再出译文，比等译文出来才标位置更符合"悬停即定位"的直觉
      if (this.hover.si !== w.si) {
        this.hover.si = w.si;
        this.hover.rects = this.sentenceRects(w.si);
      }

      clearTimeout(this.hover.timer);
      // 380ms 是实测手感的上限：再短会在移动鼠标时连环触发，
      // 再长用户会以为没反应
      this.hover.timer = setTimeout(
        () => this.translateHover(sent.text, ev.clientX, ev.clientY),
        380
      );
    },

    onWordLeave() {
      clearTimeout(this.hover.timer);
      // 不立刻隐藏：移到相邻词上会先 leave 再 enter，立刻隐藏会闪一下
      this.hover.timer = setTimeout(() => this.hideHover(), 260);
    },

    /// 某一句在页面上的矩形（按行拆成多条），坐标是 pt，与文本层同一套。
    /// 直接合并所有词会得到一个跨行的大方块，把行间空白也涂上；按 y 聚行才像
    /// 一条"荧光笔划线"。
    sentenceRects(si) {
      const words = (this.tl.words || []).filter((w) => w.si === si);
      if (!words.length) return [];
      const rows = [];
      words.forEach((w) => {
        const row = rows.find((r) => Math.abs(r.y - w.y) < Math.max(3, w.h * 0.5));
        if (row) {
          row.words.push(w);
          row.y = Math.min(row.y, w.y);
        } else {
          rows.push({ y: w.y, words: [w] });
        }
      });
      return rows.map((r) => {
        const x0 = Math.min(...r.words.map((w) => w.x));
        const x1 = Math.max(...r.words.map((w) => w.x + w.w));
        const y0 = Math.min(...r.words.map((w) => w.y));
        const y1 = Math.max(...r.words.map((w) => w.y + w.h));
        return { left: x0 - 1 + "px", top: y0 - 1 + "px",
                 width: x1 - x0 + 2 + "px", height: y1 - y0 + 2 + "px" };
      });
    },

    async translateHover(text, cx, cy) {
      const key = text.slice(0, 160);
      if (this.hover.cache[key]) {
        this.showHover(text, this.hover.cache[key], cx, cy);
        return;
      }
      this.showHover(text, "", cx, cy, true);
      try {
        const r = await fetch("/api/translate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        }).then((x) => x.json());
        const out = r.text || r.zh || "";
        if (!r.ok || !out) {
          this.hideHover(true);
          return;
        }
        const keys = Object.keys(this.hover.cache);
        if (keys.length > 240) this.hover.cache = {}; // 简单上限，长会话不涨内存
        this.hover.cache[key] = out;
        // 只有在还悬停着同一句时才更新，否则会把上一句的译文贴到新句子上
        if (this.hover.visible && this.hover.src === text) {
          this.hover.zh = out;
          this.hover.loading = false;
        }
      } catch (e) {
        this.hideHover(true);
      }
    },

    showHover(src, zh, cx, cy, loading) {
      const W = 340;
      let left = cx + 16;
      let top = cy + 20;
      // 贴边翻转：右侧放不下就放光标左边，下方放不下就放上方
      if (left + W > window.innerWidth - 12) left = Math.max(10, cx - W - 16);
      if (top + 170 > window.innerHeight - 12) top = Math.max(10, cy - 160);
      this.hover.visible = true;
      this.hover.src = src;
      this.hover.zh = zh;
      this.hover.loading = !!loading;
      this.hover.style = { left: left + "px", top: top + "px" };
    },

    hideHover(force) {
      clearTimeout(this.hover.timer);
      if (force || !this.hover.loading) {
        this.hover.visible = false;
        this.hover.loading = false;
        // 高亮和气泡同进同退：气泡收了还留着黄框，用户会以为选中了某个区域
        this.hover.si = null;
        this.hover.rects = [];
      }
    },

    /* ---------------- 中文译本导出（官方 1.3.3.2） ---------------- */

    async openZh() {
      if (!this.activeDoc) return;
      this.zh.visible = true;
      await this.loadZh();
    },

    async loadZh() {
      const doc = this.activeDoc;
      if (!doc || this.zh.loading) return;
      this.zh.loading = true;
      try {
        const r = await fetch(`/api/documents/${doc.id}/translate-zh?preview=6000`).then((x) => x.json());
        if (!r.ok) throw new Error(r.detail || r.error || "导出失败");
        Object.assign(this.zh, {
          docId: doc.id,
          title: r.title || doc.title || doc.filename,
          markdown: r.preview || "",
          chars: r.chars || 0,
          total: r.total || 0,
          translated: r.translated || 0,
          missing: r.missing || 0,
          verbatim: r.verbatim || 0,
          ratio: r.ratio || 0,
          truncated: !!r.truncated,
        });
      } catch (e) {
        ElementPlus.ElMessage.error(e.message || "导出失败，稍后重试");
      } finally {
        this.zh.loading = false;
      }
    },

    downloadZh() {
      if (!this.zh.docId) return;
      // 走浏览器下载而不是前端拼 Blob：服务端每次重新拼，
      // 拿到的永远是最新覆盖率的版本（库里的译文随时在增加）
      window.open(`/api/documents/${this.zh.docId}/translate-zh.md`, "_blank", "noopener");
    },

    /* ---------------- 划词翻译 ---------------- */

    onTextSelect() {
      const s = window.getSelection();
      const txt = (s ? String(s) : "").trim();
      if (!txt || txt.length < 2) return;
      // 英文选区 → 译中；中文选区 → 译英（官方 1.6.4 的划词形态）。
      // 纯数字/符号仍然没意义，直接忽略。
      const hasEn = /[A-Za-z]{2,}/.test(txt);
      const hasZh = /[\u4e00-\u9fff]{2,}/.test(txt);
      if (!hasEn && !hasZh) return;
      let rect = null;
      try {
        rect = s.getRangeAt(0).getBoundingClientRect();
      } catch (err) {
        return;
      }
      if (!rect || !rect.width) return;
      const left = Math.min(Math.max(8, rect.left), window.innerWidth - 320);
      const top = rect.top > 150 ? rect.top - 46 : rect.bottom + 8;
      this.sel = {
        visible: true,
        loading: false,
        title: "划词翻译",
        src: txt,
        zh: "",
        truncated: false,
        style: { left: left + "px", top: top + "px" },
      };
    },

    async translateSelection() {
      if (!this.sel.src || this.sel.loading) return;
      this.sel.loading = true;
      try {
        const r = await fetch("/api/translate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: this.sel.src }),
        }).then((x) => x.json());
        if (r.ok) {
          this.sel.zh = r.text || r.zh;
          this.sel.title = r.to === "en" ? "译成英文" : "划词翻译";
          this.sel.truncated = !!r.truncated;
          // 译文比按钮态高得多，靠近视口底部时会被截在屏幕外，译完先收进来
          this.keepSelInView();
        } else {
          ElementPlus.ElMessage.error(r.error || "翻译失败");
          this.sel.visible = false;
        }
      } catch (e) {
        ElementPlus.ElMessage.error("翻译请求失败，请检查服务");
        this.sel.visible = false;
      } finally {
        this.sel.loading = false;
      }
    },

    // 浮框拖动：以「按下点相对浮框左上角的偏移」为锚，移动时只换算新左上角，
    // 所以抓手按在标题栏任意位置都不会跳一下。边界用 offsetWidth/Height 现取，
    // 译文折行改变高度后也仍能整框留在视口内。
    startSelDrag(e) {
      if (e.button) return;              // 只认左键，右键/中键不参与
      const el = e.currentTarget.closest(".sel-pop");
      if (!el) return;
      e.preventDefault();                // 拖动时不许顺带选中页面文字
      const r = el.getBoundingClientRect();
      const offX = e.clientX - r.left;
      const offY = e.clientY - r.top;
      this.sel.dragging = true;
      document.body.classList.add("is-dragging");
      const move = (ev) => {
        const w = el.offsetWidth;
        const h = el.offsetHeight;
        const x = Math.min(Math.max(4, ev.clientX - offX), Math.max(4, window.innerWidth - w - 4));
        const y = Math.min(Math.max(4, ev.clientY - offY), Math.max(4, window.innerHeight - h - 4));
        this.sel.x = x;
        this.sel.y = y;
        this.sel.style = { left: x + "px", top: y + "px" };
      };
      const up = () => {
        this.sel.dragging = false;
        document.body.classList.remove("is-dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
      };
      // 监听挂在 window 上：鼠标甩出浮框（甚至甩出窗口）也不会丢事件
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    },

    keepSelInView() {
      this.$nextTick(() => {
        const el = document.querySelector(".sel-pop");
        if (!el) return;
        const r = el.getBoundingClientRect();
        let left = r.left;
        let top = r.top;
        if (r.bottom > window.innerHeight - 8) top = Math.max(8, window.innerHeight - r.height - 8);
        if (r.right > window.innerWidth - 8) left = Math.max(8, window.innerWidth - r.width - 8);
        if (left !== r.left || top !== r.top) {
          this.sel.x = left;
          this.sel.y = top;
          this.sel.style = { left: left + "px", top: top + "px" };
        }
      });
    },

    // 扫描件整页翻译：页面没有词级文本层，划词走不通，改由服务端按段落顺序取该页
    // chunk 文本送翻译。译文复用划词浮层展示（可拖动、可滚动），不另造一个弹窗。
    async translatePage() {
      const doc = this.activeDoc;
      if (!doc || this.tl.zhLoading) return;
      const page = this.page;
      this.tl.zhLoading = true;
      try {
        const r = await fetch(`/api/documents/${doc.id}/page/${page}/translate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }).then((x) => x.json());
        if (!r.ok) {
          ElementPlus.ElMessage.error(r.error || "整页翻译失败");
          return;
        }
        this.sel = {
          visible: true,
          loading: false,
          dragging: false,
          title: `整页翻译 · p${page}`,
          src: r.en || "",
          zh: r.zh,
          truncated: false,
          style: { left: Math.max(8, (window.innerWidth - 320) / 2) + "px", top: "104px" },
        };
        // 少数段落模型会漏译；如实报数，别让用户以为整页都译完了
        if (r.missing) ElementPlus.ElMessage.warning(`该页有 ${r.missing} 段未译出，可稍后重试`);
        this.keepSelInView();
      } catch (e) {
        ElementPlus.ElMessage.error("整页翻译请求失败，请检查服务");
      } finally {
        this.tl.zhLoading = false;
      }
    },

    onImgError(p) {
      this.failedPages = { ...this.failedPages, [p]: true };
      if (p !== this.page) return;   // 别的页失败不影响当前阅读，等滚过去再说
      this.imgFailed = true;
      // 顺手把文本层清掉：页图都没出来，叠加一层透明文字只会更混乱
      this.tl = { ...this.tl, words: [], sents: [], loaded: false };
    },

    retryImg() {
      this.imgFailed = false;
      this.failedPages = {};
      this.imgNonce += 1;
    },

    onImgLoad(p) {
      if (p !== this.page) return;
      this.imgFailed = false;
      // 已存在的高亮在换页/缩放后需要按新尺寸重算
      this.highlights = this.highlights.map((h) => ({
        ...h,
        style: h.pt ? this.rectStyle(h.pt) : this.boxStyle(h.bbox),
      }));
      // 文本层按 pt 布局，缩放比例只能等图片真正渲染出来才知道
      this.syncTextScale();
    },

    /** pt 矩形 → 相对页面的百分比（高亮层与页图同尺寸，用百分比就不怕缩放） */
    rectStyle(pt) {
      const meta = this.pageSizes[this.page];
      if (!meta || !pt) return { display: "none" };
      return {
        left: (pt.x / meta.width) * 100 + "%",
        top: (pt.y / meta.height) * 100 + "%",
        width: (pt.w / meta.width) * 100 + "%",
        height: (pt.h / meta.height) * 100 + "%",
      };
    },

    /**
     * 计算一次证据点击该画哪些框。
     *
     * 目标：句级精确。旧实现直接拿 chunk 的 bbox 画一个矩形——chunk 通常就是一整段
     * （实测 bbox 高 94pt ≈ 7 行），于是整块正文被涂黄，既挡字也看不出重点。
     * 现在用页面的词级文本层：把关键句的每个词找出来，按行聚成小框，
     * 句首从行中间开始、句尾到该行最后一个词为止。
     *
     * 退化顺序：关键句 → 整段文字 → chunk bbox（扫描件无文本层时才走最后这步）。
     */
    buildHighlights(ev) {
      const arr = Array.isArray(ev.bbox) ? ev.bbox : String(ev.bbox || "").split(",").map(Number);
      const bbox = arr.length === 4 && arr.every((v) => Number.isFinite(v)) ? arr : null;
      const text = ev.text || "";
      const words = this.tl.page === this.page && this.tl.words.length ? this.tl.words : [];

      let rects = [];
      if (bbox && words.length) {
        const spans = ev.focus && ev.focus.length ? ev.focus : (text ? [text] : []);
        // 没有文字（正文里的 p7 ▸ 锚点只带坐标）时，退一步按"bbox 内的所有词"
        // 逐行画：虽然还是整段，但至少是贴着行的多条，不会糊成一块。
        rects = spans.length
          ? this.textRects(words, bbox, spans)
          : this.areaRects(words, bbox);
        // 文字对不上（版本不同、连字符断词太多）时再退一步：仍按 bbox 内的词逐行画，
        // 只要页面有词级坐标，就绝不拿一个大矩形糊上去。
        if (!rects.length) rects = this.areaRects(words, bbox);
      }
      if (!rects.length) {
        // 无文本层（扫描件）或文字对不上：退回整块 bbox，但标 approx，
        // 用虚线边框如实表示"这是区块级范围，不是句子级"。
        return bbox
          ? [{ bbox, pt: null, approx: true, text, style: this.boxStyle(bbox) }]
          : [];
      }
      return rects.map((pt) => ({ pt, bbox: null, approx: false, text, style: this.rectStyle(pt) }));
    },

    /**
     * bbox 内所有词 → 逐行矩形。用于没有文字内容的定位（正文锚点 p7 ▸、
     * 或证据文本与页面版本对不上），保证"至少是逐行"而不是一整块。
     */
    areaRects(words, bbox) {
      const [x0, y0, x1, y1] = bbox;
      const pad = 2;
      const idx = [];
      for (let i = 0; i < words.length; i++) {
        const w = words[i];
        const cx = w.x + w.w / 2;
        const cy = w.y + w.h / 2;
        if (cx < x0 - pad || cx > x1 + pad || cy < y0 - pad || cy > y1 + pad) continue;
        idx.push(i);
      }
      if (idx.length < 3) return [];
      return lineRects(words, idx);
    },

    /**
     * 在一页的词级文本层里定位若干句子，返回每行一个的矩形（pt）。
     * 只在 chunk bbox 范围内找词：同一句话在摘要和正文里可能都出现，
     * 不限定范围会标到另一处去。
     */
    textRects(words, bbox, spans) {
      const [x0, y0, x1, y1] = bbox;
      const pad = 2;
      const inside = [];
      for (const w of words) {
        const cx = w.x + w.w / 2;
        const cy = w.y + w.h / 2;
        if (cx < x0 - pad || cx > x1 + pad || cy < y0 - pad || cy > y1 + pad) continue;
        const tok = normTok(w.t);
        if (!tok) continue;
        inside.push({ ...w, tok });
      }
      if (inside.length < 3) return [];

      const out = [];
      const seen = new Set();
      for (const span of spans) {
        const toks = String(span || "").toLowerCase().split(/\s+/).map(normTok).filter(Boolean);
        if (!toks.length) continue;

        // 贪心顺序对齐，允许跳过页面上没对上的词（连字符断词、"e.g." 被拆开等）。
        // 不回退匹配位置，避免一个高频词把整句拖到别处。
        const hit = [];
        let pi = 0;
        for (const t of toks) {
          const lim = Math.min(inside.length, pi + 40);
          for (let k = pi; k < lim; k++) {
            if (inside[k].tok === t) { hit.push(k); pi = k + 1; break; }
          }
        }
        // 命中太少说明这段文字并不在这块区域（或版本不一致），宁可不画也不乱画
        if (hit.length < Math.min(4, Math.max(2, Math.floor(toks.length * 0.3)))) continue;

        for (const r of lineRects(inside, hit)) {
          const key = [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)].join(",");
          if (seen.has(key)) continue;
          seen.add(key);
          out.push(r);
        }
      }
      return out;
    },

    boxStyle(bbox) {
      const meta = this.pageSizes[this.page];
      if (!meta || !bbox) return { display: "none" };
      // bbox 有两个来源：检索证据给的是数组，数据库/老接口可能是 "x0,y0,x1,y1" 字符串。
      // 对字符串直接解构会拿到一个个字符（"1","0","8"...），算出 1% 宽的畸形框，
      // 所以这里统一先归一化成数字数组，非有限值直接不显示。
      const arr = Array.isArray(bbox) ? bbox : String(bbox).split(",").map(Number);
      const [x0, y0, x1, y1] = arr;
      if (![x0, y0, x1, y1].every((v) => Number.isFinite(v))) return { display: "none" };
      return {
        left: (x0 / meta.width) * 100 + "%",
        top: (y0 / meta.height) * 100 + "%",
        width: Math.max(((x1 - x0) / meta.width) * 100, 0.6) + "%",
        height: Math.max(((y1 - y0) / meta.height) * 100, 0.4) + "%",
      };
    },

    /** 用「页码 + 坐标」回查这次回答里的证据，拿到它属于哪篇文献、原文是哪几句。 */
    evidenceAt(page, bbox) {
      const near = (a, b) => Math.abs(a - b) <= 2;
      for (const m of this.messages) {
        for (const e of m.evidence || []) {
          if (e.page !== page || !Array.isArray(e.bbox) || e.bbox.length !== 4) continue;
          if (near(e.bbox[0], bbox[0]) && near(e.bbox[1], bbox[1])
              && near(e.bbox[2], bbox[2]) && near(e.bbox[3], bbox[3])) return e;
        }
      }
      return null;
    },

    async jump(ev) {
      if (ev.doc_id && (!this.activeDoc || this.activeDoc.id !== ev.doc_id)) {
        const d = this.docs.find((x) => x.id === ev.doc_id);
        if (d) await this.openDoc(d);
      }
      this.page = ev.page;
      // 跳页不走 goPage，这里要自己把文本层换成目标页，否则会残留上一页的文字。
      // 必须 await：句级高亮要用这一页的词级坐标，文本层没到位就只能退回大矩形。
      if (this.tl.page !== this.page || !this.tl.loaded) await this.loadTextLayer();
      await nextTick();
      // 连续滚动下目标页可能隔了几十屏：先无动画落到那一页，再按高亮位置微调。
      // 直接对高亮做 smooth 会看到滚动条横穿整篇文档，观感很差。
      this.scrollToPage(this.page, false);
      await nextTick();
      this.highlights = this.buildHighlights(ev);
      // 必须滚到"高亮框"而不是滚整个阅读区：参考文献条目常在页面底部，
      // 只让阅读区进入视口的话目标仍在视野外，用户会以为跳转失败了（实测如此）。
      // 这里用 rect 差值直接算滚动量，不依赖 offsetParent 与 scrollIntoView 的实现差异。
      await nextTick();
      const stage = document.querySelector(".page-stage");
      const boxes = stage ? Array.from(stage.querySelectorAll(".hl")) : [];
      if (stage && boxes.length) {
        const sr = stage.getBoundingClientRect();
        const rects = boxes.map((b) => b.getBoundingClientRect());
        const top = Math.min(...rects.map((r) => r.top));
        const bottom = Math.max(...rects.map((r) => r.bottom));
        // 一句话被拆成多行时，按整组的中心对齐；组高超过视口时留 24px 顶部余量
        const delta = top - sr.top - Math.max(24, (stage.clientHeight - (bottom - top)) / 2);
        stage.scrollTo({ top: Math.max(0, stage.scrollTop + delta), behavior: "smooth" });
      }
    },

    /**
     * 点击推荐问题：先把"范围"和"当前文献"摆到这条问题的前提上，再提问。
     * 就着手上的范围直接问，等于把一个必错的组合交给用户。
     *   · 带 doc 的（前三条）→ 切到那篇原文再问，库里没有就退回跨库；
     *   · 不带 doc 的（第 4 条）→ 强制跨库，否则一定在无关文献里检索。
     */
    async ask(text) {
      const q = (text || this.question || "").trim();
      if (!q || this.loading) return;
      this.question = "";
      this.messages.push({ id: ++this.msgSeq, role: "user", html: mdToHtml(q) });
      this.scrollChat();
      if (this.streamMode) return this.askStream(q);
      this.loading = true;

      // 检索范围：勾选了文献就用勾选的；否则按"当前文献"限定时只用正在读的那篇。
      // 没有这个约束，库内 37 篇时问"这篇论文的第六作者"会被别的文献抢答。
      let docIds = this.selection.length ? this.selection : [];
      // 不再有"对比模式"开关：跨不跨文献由范围决定，问题里带「对比/区别」时
      // 后端会自己切对比模板（COMPARE_HINT）。选「当前文献」就只有一篇，
      // 自然也不会被强行做成对比表——这正是原来那个按钮的重复之处。
      if (!docIds.length && this.scope === "doc" && this.activeDoc) {
        docIds = [this.activeDoc.id];
      }
      const t0 = Date.now();
      // 在发请求这一刻把路由钉下来：等 30 秒本地出完答案时，用户可能已经把开关切回云端，
      // 那一刻的 this.askRoute 不能代表"这条回答是谁答的"
      const askedRoute = this.askRoute;
      try {
        const r = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...this.routeHeader(askedRoute) },
          // 本地 3B 没有思维链，深思开关对它无意义：这里先不讲下去，
          // 后端也会把 deep 收敛成 false，两边一致才不会把缓存键劈成两份。
          body: JSON.stringify({ question: q, doc_ids: docIds, mode: "qa",
                                 thinking: this.thinkDeep && askedRoute !== "local" }),
        }).then((x) => x.json());

        this.messages.push({
          id: ++this.msgSeq,
          role: "assistant",
          html: mdToHtml(r.answer || "（无返回）"),
          evidence: r.evidence || [],
          verify: r.verify || {},
          trace: r.trace || [],
          meta: this.modeLabel(((Date.now() - t0) / 1000).toFixed(1), { route: askedRoute }),
        });
      } catch (e) {
        this.messages.push({ id: ++this.msgSeq, role: "assistant", html: "请求失败：" + e });
      } finally {
        this.loading = false;
        this.scrollChat();
      }
    },

    /// 删除单条消息（提问或回答）。
    /// 生成中禁删：流式回答还在往这条 msg 上写，删掉之后再写就是往已卸载的节点上写，
    /// 轻则报错重则整条流断掉。
    delMsg(i) {
      if (this.loading) {
        ElementPlus.ElMessage.warning("正在生成回答，等这一轮结束再删");
        return;
      }
      const m = this.messages[i];
      if (!m) return;
      this.messages.splice(i, 1);
      ElementPlus.ElMessage({
        message: m.role === "user" ? "已删除这条提问" : "已删除这条回答",
        type: "info", duration: 1200,
      });
    },

    /// 清空整个对话。聊天记录只存在内存里（刷新即空），所以这里不做二次存储，
    /// 只清数组；确认框是为了防手滑——一次删掉几十条回答没法找回。
    async clearChat() {
      if (!this.messages.length) return;
      if (this.loading) {
        ElementPlus.ElMessage.warning("正在生成回答，等这一轮结束再清空");
        return;
      }
      const n = this.messages.length;
      try {
        await ElementPlus.ElMessageBox.confirm(
          `将删除当前对话的全部 ${n} 条记录（提问与回答）。\n聊天记录不落盘，删掉就找不回来了。`,
          "清空对话",
          { type: "warning", confirmButtonText: "全部删除", cancelButtonText: "取消" }
        );
      } catch (e) {
        return;
      }
      this.messages.splice(0, this.messages.length);
      this.scrollChat();
      ElementPlus.ElMessage({ message: `已清空 ${n} 条记录`, type: "success", duration: 1500 });
    },

    /// 一键切换云 / 纯本地。
    /// 切换是全局的（影响所有人、下一次提问立即生效），所以失败时必须把开关拨回去，
    /// 否则界面显示"离线"而实际还在云端，比不提供开关更糟。
    async toggleOffline(want) {
      if (this.offlineSwitching) return;
      this.offlineSwitching = true;
      try {
        const r = await fetch("/api/system/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: want ? "offline" : "cloud" }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
        this.offlineMode = data.mode === "offline";
        this.offlineInfo = data.offline || {};
        this.info.mode = data.mode;
        if (this.offlineMode) this.askRoute = "local";
        ElementPlus.ElMessage({
          message: this.offlineMode
            ? "已切到纯本地：检索与生成本机完成，断网仍可用"
            : "已切回云端：检索与生成恢复正常",
          type: "success", duration: 2200,
        });
      } catch (e) {
        this.offlineMode = !want;      // 拨回原状态
        ElementPlus.ElMessage.error("切换失败：" + e.message);
      } finally {
        this.offlineSwitching = false;
      }
    },

    toggleThink() {
      if (this.askRoute === "local") {
        ElementPlus.ElMessage.warning(
          "本地 3B 没有思维链，深思对它不生效；切回云端再开深思");
        return;
      }
      this.thinkDeep = !this.thinkDeep;
      ElementPlus.ElMessage({
        message: this.thinkDeep
          ? "深思：模型先推理再作答，慢约 2–3 秒，概念辨析题更准"
          : "快速：直接作答，适合查数值、列举、结构化抽取",
        type: "info", duration: 2000,
      });
    },

    // 标签显示在回答上方。把耗时写出来是有意的：让"深思换到了什么"变成看得见的东西
    // 带上"本地"二字同样是有意的：切到本地后的回答要能自证是本地跑的，否则截图里
    // 云端和本地长得一模一样，加分项等于没做。
    modeLabel(seconds, meta) {
      const local = meta && meta.route === "local" ? " · 本地" : "";
      // 本地没有深思：即使开关还停在"深思"，标签也只写"快速"，否则截图会自相矛盾
      const deep = local
        ? false
        : (meta && typeof meta.thinking === "boolean" ? meta.thinking : this.thinkDeep);
      return `${deep ? "深思" : "快速"}${local} · ${seconds}s`;
    },

    // 切换"这一问"的后端。刻意不写后端全局配置：全局路由是全站共享的，
    // 页面上点一下就把所有人（包括公网访客）的模型换掉，既不对也没法回滚。
    async setAskRoute(route) {
      if (route === this.askRoute) return;
      if (route === "local") {
        // 再探一次：用户可能刚关掉 Ollama 或改了模型名，10 秒缓存里的旧结论会让
        // "按钮还在，一问就报错"重新出现。
        const fresh = await fetch("/api/system/info").then((x) => x.json()).catch(() => null);
        if (fresh && fresh.local) this.info.local = fresh.local;
        if (!this.localReady) {
          this.askRoute = "cloud";
          ElementPlus.ElMessage.warning(
            (this.info.local && this.info.local.reason) || "本地模型不可用，已保持云端");
          return;
        }
        // 顶栏合并成一个控件后，「本地 3B」不只是换个生成模型：检索用的向量表、
        // 重排也要一起切到本机，否则断网时检索那一层还在发网络请求，"能用"就落空。
        if (!this.offlineMode) await this.toggleOffline(true);
        ElementPlus.ElMessage.info(
          `已切到本地 ${this.info.local.model}：检索与生成都在本机，断网可用；`
          + `3B 出字约 6 字/秒，长答案要等 30 秒左右`
          + (this.thinkDeep ? "；本地没有思维链，深思已临时关闭" : ""));
      } else if (this.offlineMode) {
        await this.toggleOffline(false);
      }
      this.askRoute = route;
    },

    // 明确告诉后端"这一问走哪"。只在两边不一致时才发头：一致时保持空请求，
    // 线上抓包看到的默认流量与改动前完全一样，排查时不会多一个变量。
    routeHeader(route) {
      const global = (this.info.llm && this.info.llm.route) || "";
      return route && route !== global ? { "X-LLM-Route": route } : {};
    },

    docIdsFor() {
      // 对比不再由开关控制：跨不跨文献看范围；问题里的「对比/区别」由后端切模板
      let ids = this.selection.length ? this.selection : [];
      if (!ids.length && this.scope === "doc" && this.activeDoc) {
        ids = [this.activeDoc.id];
      }
      return ids;
    },

    /* SSE 流式问答：thinking 事件渲染为思考时间线，token 事件逐字追加。
     * 不套 JSON 包裹，所以拿到的是模型真实 delta；done 事件再补证据与校验。 */
    async askStream(q) {
      // 必须是 reactive：push 进 this.messages 之后，模板渲染读的是数组里的代理，
      // 而我们手里这个变量是原始对象——直接改原始对象的属性不会触发重新渲染，
      // 所有中间态（逐字正文、推理增量、阶段状态）都会被吞掉，直到 done 时才
      // 因为 loading 变化一次性画出来。这正是"流式看不出来"的真正原因。
      const msg = reactive({
        id: ++this.msgSeq,
        role: "assistant", html: "", evidence: [], verify: {}, trace: [], thinking: [],
        // 推理原文（深思模式下模型真的在想什么），逐段追加；完成前默认展开
        reasonText: "", reasonChars: 0, reasonOpen: true,
        _raw: "", _timer: null,
      });
      this.messages.push(msg);
      this.loading = true;
      const t0 = Date.now();
      // 同 ask()：发请求那一刻钉住路由，标签才不会在等待期间被后来的切换改错
      const askedRoute = this.askRoute;
      let raw = "";
      try {
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...this.routeHeader(askedRoute) },
          body: JSON.stringify({ question: q, doc_ids: this.docIdsFor(), mode: "qa",
                                 thinking: this.thinkDeep && askedRoute !== "local",
                                 // 回归测试会设 window.__SCIAGENT_NO_CACHE=1：
                                 // 缓存命中的 token 事件是"回放"，测不出真实的流式链路
                                 no_cache: !!window.__SCIAGENT_NO_CACHE }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) {
            const line = buf.slice(0, cut).trim();
            buf = buf.slice(cut + 2);
            if (!line.startsWith("data: ")) continue;
            const payload = line.slice(6);
            if (payload === "[DONE]") continue;
            let ev;
            try { ev = JSON.parse(payload); } catch (e) { continue; }
            if (ev.event === "token") {
              raw += ev.text || "";
              msg._raw = raw;
              this.scheduleStream(msg);
            } else if (ev.event === "reason") {
              // 推理原文：直接追加，不走 Markdown（推理里没有结构化内容，
              // 逐段跑 md 解析只会拖慢刷新）
              msg.reasonText += ev.text || "";
              msg.reasonChars = ev.chars || msg.reasonText.length;
              this.scrollChat();
            } else if (ev.event === "thinking") {
              this.upsertThinking(msg, ev);
              this.scrollChat();
            } else if (ev.event === "done") {
              if (msg._timer) { clearTimeout(msg._timer); msg._timer = null; }
              msg.html = mdToHtml(ev.answer || raw);
              msg.evidence = ev.evidence || [];
              msg.verify = ev.verify || {};
              msg.meta = this.modeLabel(((Date.now() - t0) / 1000).toFixed(1),
                                        { ...(ev.meta || {}), route: askedRoute });
              // 答完就把推理收起来：它是过程，不是结论；要看还能再展开
              msg.reasonOpen = false;
            } else if (ev.event === "error") {
              if (msg._timer) { clearTimeout(msg._timer); msg._timer = null; }
              this.upsertThinking(msg, { stage: "error", text: ev.text, state: "done" });
              if (!msg.html) msg.html = mdToHtml("生成中断，已安全降级：" + ev.text);
            }
          }
        }
        if (!msg.html) msg.html = mdToHtml("（未收到有效内容）");
      } catch (e) {
        if (msg._timer) { clearTimeout(msg._timer); msg._timer = null; }
        this.upsertThinking(msg, { stage: "error", text: String(e), state: "done" });
        msg.html = mdToHtml("请求失败：" + e);
      } finally {
        if (msg._timer) { clearTimeout(msg._timer); msg._timer = null; }
        this.loading = false;
        this.scrollChat();
      }
    },

    /// 阶段事件按 stage 归并：同一个阶段先"进行中"后"完成"，只占时间线上的一行，
    /// 否则检索那一行会变成两行重复的话。
    upsertThinking(msg, ev) {
      const LABEL = { plan: "规划", retrieve: "检索", read: "通读", depth: "深思",
                      depth_done: "推理", verify: "校验", generate: "生成", error: "错误" };
      const next = { stage: ev.stage, label: LABEL[ev.stage] || ev.stage,
                     text: ev.text || "", state: ev.state || "done" };
      const item = (msg.thinking || []).find((x) => x && x.stage === ev.stage);
      if (item) Object.assign(item, next);
      else msg.thinking.push(next);
    },

    /// 流式期间的渲染节流。
    ///
    /// 原来是"每个 token 重建整篇 Markdown"：38 个 token 在 0.9 秒里到齐，
    /// 浏览器把这些重排合并成一次绘制，肉眼看到的就变成"一下子全出来"。
    /// 现在改成 40ms 合并一次、且只做转义 + 换行（不跑 md 解析），
    /// 字才会真的一个个往外冒；收尾时再用 mdToHtml 定型（引用徽章、公式）。
    scheduleStream(msg) {
      if (msg._timer) return;
      msg._timer = setTimeout(() => {
        msg._timer = null;
        msg.html = '<p class="streaming">' + escHtml(msg._raw).replace(/\n/g, "<br>") + "</p>";
        this.scrollChat();
      }, 40);
    },

    async openTrash() {
      const r = await fetch("/api/documents/trash").then((x) => x.json());
      this.trash.items = r.items || [];
      this.trash.visible = true;
    },

    async restoreTrash(t) {
      const r = await fetch(`/api/documents/${t.doc_id}/restore?actor=ui`, { method: "POST" })
        .then((x) => x.json());
      if (r.ok) ElementPlus.ElMessage.success(r.message || "已恢复");
      else ElementPlus.ElMessage.error(r.error || "恢复失败");
      await this.openTrash();
      await this.loadDocs();
      await this.loadInfo();
    },

    // 彻底删除单条：二次确认 → DELETE。恢复靠的是磁盘上的 PDF，
    // 这一步把 PDF 也删了，所以确认文案必须写清"无法撤销"，不能只写"确定吗"。
    async purgeTrash(t) {
      const name = t.title || t.filename;
      try {
        await ElementPlus.ElMessageBox.confirm(
          `彻底删除《${name}》？磁盘上的 PDF 会一并删掉，之后连恢复的入口都没有了。`,
          "确认彻底删除",
          {
            type: "warning",
            confirmButtonText: "彻底删除",
            cancelButtonText: "取消",
            confirmButtonClass: "el-button--danger",
          }
        );
      } catch (e) {
        return;   // 用户取消
      }
      const r = await fetch(`/api/documents/trash/${t.doc_id}?actor=ui`, { method: "DELETE" })
        .then((x) => x.json())
        .catch((e) => ({ ok: false, error: String(e) }));
      if (r.ok) {
        ElementPlus.ElMessage.success(r.note ? `${r.message}（${r.note}）` : (r.message || "已彻底删除"));
      } else {
        ElementPlus.ElMessage.error(r.error || "彻底删除失败");
      }
      await this.openTrash();
      await this.loadDocs();
      await this.loadInfo();
      if (this.activeDoc && this.activeDoc.id === t.doc_id) this.activeDoc = null;
    },

    async purgeAllTrash() {
      const n = this.trash.items.length;
      if (!n) return;
      try {
        await ElementPlus.ElMessageBox.confirm(
          `清空回收站？这 ${n} 条记录和磁盘上的 PDF 都会被删掉，无法撤销。`,
          "确认清空回收站",
          {
            type: "warning",
            confirmButtonText: `删除全部 ${n} 条`,
            cancelButtonText: "取消",
            confirmButtonClass: "el-button--danger",
          }
        );
      } catch (e) {
        return;
      }
      this.trash.purging = true;
      try {
        const r = await fetch("/api/documents/trash/purge-all?actor=ui", { method: "POST" })
          .then((x) => x.json());
        if (r.ok) ElementPlus.ElMessage.success(r.message || "已清空回收站");
        else ElementPlus.ElMessage.warning(r.message || "部分记录未能删除");
        // 失败项逐条列出：静默吞掉会让人以为都删干净了
        (r.failed || []).slice(0, 3).forEach((f) => {
          ElementPlus.ElMessage.error(`${f.filename || f.doc_id}：${f.error}`);
        });
      } catch (e) {
        ElementPlus.ElMessage.error("清空失败：" + e);
      } finally {
        this.trash.purging = false;
      }
      await this.openTrash();
      await this.loadDocs();
      await this.loadInfo();
    },

    scrollChat() {
      nextTick(() => {
        const el = this.$refs.chatList;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    toggleTree() { this.showTree = !this.showTree; },

    /* ---------------- 引用图谱 ---------------- */
    onViewChange(v) {
      if (v === "graph") this.loadGraph(false);
      if (v === "bilingual") this.loadBi(true);
      // 从对照/图谱切回阅读器时，文本层可能还停在旧页
      if (v === "reader" && this.activeDoc && this.tl.page !== this.page) this.loadTextLayer();
    },

    /* ---------------- 写作 Copilot（官方 1.6.1） ---------------- */

    /// 正文里的 [n] 变成可点徽章：点一下滚到 References 对应条目。
    /// 这是"无幻觉"最直观的呈现——读者不用相信模型，自己点过去看条目就行。
    citeHtml(text) {
      const html = mdToHtml(text || "");
      return html.replace(/\[(\d{1,3})\]/g,
        (m, n) => `<span class="wcite" data-i="${n}">[${n}]</span>`);
    },

    jumpToRef(idx) {
      const list = document.querySelectorAll(".write-ref");
      const target = [...list].find((el) => {
        const t = el.querySelector(".write-ref-idx");
        return t && t.textContent.trim() === `[${idx}]`;
      });
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1400);
    },

    async genOutline() {
      const idea = (this.wr.idea || "").trim();
      if (!idea) {
        ElementPlus.ElMessage.warning("先写一句你的研究想法");
        return;
      }
      if (this.wr.loading) return;
      this.wr.loading = true;
      this.wr.error = "";
      try {
        const scope = this.selection.length ? this.selection : null;
        const r = await fetch("/api/write/outline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ idea, doc_ids: scope || [], top_k: 6 }),
        }).then((x) => x.json());
        if (!r.ok) {
          this.wr.error = r.error || "生成失败";
          ElementPlus.ElMessage.error(this.wr.error);
          this.wr.result = null;
        } else {
          this.wr.result = r;
        }
      } catch (e) {
        this.wr.error = "请求失败，请检查服务是否在运行";
        ElementPlus.ElMessage.error(this.wr.error);
      } finally {
        this.wr.loading = false;
      }
    },

    // 点 References 里的一条 → 打开那篇文献读原文（条目本身就是真实库内文献，所以一定打得开）
    async openFromWrite(r) {
      const d = this.docs.find((x) => x.id === r.doc_id);
      if (!d) return;
      this.centerView = "reader";
      await this.openDoc(d);
    },

    /* ---------------- 架构拓扑图（官方 1.6.2） ----------------
     * 后端只回四个格式的代码字符串，这里负责"选格式 → 复制/下载 → Mermaid 预览"。
     * 预览不做兜底渲染：mermaid 挂了就空白，代码仍然是好的，复制走即可。
     */
    async genTopology() {
      // 框架页写过 idea 就直接沿用，省得在两个 tab 之间重敲一遍
      const idea = (this.wv.idea || "").trim() || (this.wr.idea || "").trim();
      if (idea.length < 2) {
        ElementPlus.ElMessage.warning("先写一句你的研究思路");
        return;
      }
      if (this.wv.tLoading) return;
      this.wv.tLoading = true;
      this.wv.tError = "";
      const body = { idea };
      // 已经生成过框架时，把章节带过去：架构图应该和刚写的大纲对得上，而不是另起一套
      if (this.wr.result && this.wr.result.sections) {
        body.outline = [this.wr.result.title || ""].concat(
          this.wr.result.sections.map((s) => `${s.name}：${(s.text || "").slice(0, 180)}`)
        ).join("\n");
      }
      try {
        const r = await fetch("/api/write/topology", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then((x) => x.json());
        if (!r.ok) {
          this.wv.tError = r.error || r.detail || "生成失败";
          this.wv.topology = null;
          ElementPlus.ElMessage.error(this.wv.tError);
        } else {
          this.wv.topology = r;
          this.wv.tFmt = "mermaid";
          await this.$nextTick();
          this.renderTopoChart();
        }
      } catch (e) {
        this.wv.tError = "请求失败，请检查服务是否在运行";
        ElementPlus.ElMessage.error(this.wv.tError);
      } finally {
        this.wv.tLoading = false;
      }
    },

    async pickTopoFmt(k) {
      // 预览画的是"这张图的结构"，与当前选中的代码格式无关——四种格式本来就是同一张图
      this.wv.tFmt = k;
    },

    /// 用 ECharts 自己画分层架构图：按 group 分行、行内均分。
    /// 不用 Mermaid 渲染预览的原因：它的 dagre 布局把流水线拉成一根竖柱，
    /// 在写作视图这 700px 宽的栏里缩到看不清；而 TikZ / DOT 的排版又需要外部工具。
    renderTopoChart(retry = 0) {
      const t = this.wv.topology;
      const el = this.$refs.topoChart;
      if (!t || !el) return;
      if (!window.echarts) {
        ensureEcharts().then((ok) => { if (ok) this.renderTopoChart(retry); });
        return;
      }

      // 先给容器一个高度，再确认它有真实宽度。面板刚切过来/刚生成时
      // clientWidth 可能还是 0，此时 echarts.init 出来的 canvas 只有几十像素，
      // 整张图会被压成左下角一小团（实测截图）。等布局稳定再画，最多重试 6 次。
      el.style.height = `${Math.max(260, t.groups.length * 122 + 30)}px`;
      const availW = el.clientWidth;
      if (availW < 40) {
        if (retry < 6) setTimeout(() => this.renderTopoChart(retry + 1), 80);
        return;
      }
      // 面板被 v-if 销毁重建后，旧实例绑在已移除的 DOM 上，必须重建
      if (_topoChart && _topoChart.getDom() !== el) {
        _topoChart.dispose();
        _topoChart = null;
      }

      const byGroup = new Map(t.groups.map((g) => [g, []]));
      t.nodes.forEach((n) => {
        if (!byGroup.has(n.group)) byGroup.set(n.group, []);
        byGroup.get(n.group).push(n);
      });

      const ROW_H = 122;
      const COL_W = 252;   // 同层两个框之间要留出 ~84px，否则边标签会压在框上
      const pos = {};
      [...byGroup.values()].forEach((row, r) => {
        row.forEach((n, i) => {
          pos[n.id] = [i * COL_W - ((row.length - 1) * COL_W) / 2, r * ROW_H];
        });
      });

      el.style.height = `${Math.max(260, t.groups.length * ROW_H + 30)}px`;
      if (!_topoChart) {
        _topoChart = window.echarts.init(el, null, { renderer: "canvas", width: availW });
      } else {
        _topoChart.resize({ width: availW });
      }

      // 节点类型 → 填充/描边，与 TikZ、Matplotlib 两套模板同一组配色
      const PALETTE = {
        input: ["#eef3fb", "#4a6fa5"],
        module: ["#f6f7f9", "#5b6472"],
        data: ["#eef7f0", "#3f7d52"],
        output: ["#fbf3ee", "#a5703f"],
        loss: ["#f7eef2", "#a04a63"],
        tool: ["#f3f0fa", "#6b5aa5"],
      };
      const nameOf = {};
      t.nodes.forEach((n) => { nameOf[n.id] = n.label; });

      _topoChart.setOption({
        animationDuration: 320,
        tooltip: { show: false },
        series: [{
          type: "graph",
          layout: "none",
          roam: false,
          symbol: "rect",   // roundRect 的圆角在 graph 系列里固定为半高，会变成胶囊形
          edgeSymbol: ["none", "arrow"],
          edgeSymbolSize: 9,
          label: {
            show: true, fontSize: 12, color: "#1f2430",
            width: 162, overflow: "truncate", align: "center",
          },
          // 预览里不画边标签：写作视图只有 700px 宽，同层两框之间才 84px，
          // 标签必然压到框上。动作语义在导出的四种代码里都保留着。
          edgeLabel: { show: false },
          itemStyle: { borderRadius: 6 },
          lineStyle: { color: "#98a1b0", width: 1.1, curveness: 0 },
          data: t.nodes.map((n) => {
            const [fill, stroke] = PALETTE[n.kind] || PALETTE.module;
            return {
              name: n.label,
              x: pos[n.id][0],
              y: pos[n.id][1],
              symbolSize: [168, 46],
              itemStyle: { color: fill, borderColor: stroke, borderWidth: 1 },
            };
          }),
          links: t.edges.map((e) => ({
            source: nameOf[e.from],
            target: nameOf[e.to],
            edgeName: e.label || "",
          })),
        }],
      }, true);
    },

    /* ---------------- 数据图表 + 图注（官方 1.6.3） ---------------- */
    syncChartFmt() {
      // 切到雷达/表格时，原来的 Mermaid 不可用，自动落到该类型的第一种格式
      const list = CHART_FMTS[this.wv.cKind] || [];
      if (!list.some((f) => f.k === this.wv.cFmt)) this.wv.cFmt = list[0] ? list[0].k : "latex";
    },
    onChartKindChange() {
      this.syncChartFmt();
      this.markChartStale();
    },
    markChartStale() {
      // 换类型/格式不会自动重算（一次生成要调模型写图注，不该偷偷花钱），
      // 但旧结果留在页面上、标题却跟着下拉变，用户会以为三个选项出图一样。
      if (this.wv.chart) this.wv.chartStale = true;
    },

    onCsvFile(e) {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        this.wv.csv = String(reader.result || "").slice(0, 200000);
        ElementPlus.ElMessage.success(`已读入 ${f.name}（${this.wv.csv.split("\n").length} 行）`);
      };
      reader.readAsText(f, "utf-8");
      e.target.value = "";   // 清空才能再次选中同一个文件
    },

    async genChart() {
      const csv = (this.wv.csv || "").trim();
      if (!csv) {
        ElementPlus.ElMessage.warning("先粘贴或选择一份 CSV");
        return;
      }
      if (this.wv.cLoading) return;
      this.wv.cLoading = true;
      this.wv.cError = "";
      try {
        const r = await fetch("/api/tools/csv2chart", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            csv,
            format: this.wv.cFmt,
            kind: this.wv.cKind,
            title: (this.wv.cTitle || "").trim() || "实验结果",
            caption: true,
          }),
        }).then((x) => x.json());
        if (!r.ok) {
          this.wv.cError = r.error || r.detail || "生成失败";
          this.wv.chart = null;
          ElementPlus.ElMessage.error(this.wv.cError);
        } else {
          this.wv.chart = r;
          this.wv.chartSvg = "";
          this.wv.chartStale = false;
          await this.$nextTick();
          this.renderChartChart();
        }
      } catch (e) {
        this.wv.cError = "请求失败，请检查服务是否在运行";
        ElementPlus.ElMessage.error(this.wv.cError);
      } finally {
        this.wv.cLoading = false;
      }
    },

    /* ---------------- 数据图表预览 ----------------
     * 为什么不用 mermaid 的 xychart 渲染预览：10.9 的 xychart 忽略我们给出的
     * `y-axis 0 --> 200`，纵轴自动从数据最小值起（实测 28.4/34.5/41.0 画出来
     * 是 28 起的柱子，第一根直接贴底），而导出的 Mermaid / Matplotlib 代码是
     * 从 0 起的——预览和代码不一致，还会把"差 13 分"看成"差 3 倍"。
     * 预览改用 ECharts 自己画：纵轴强制 0 起，柱顶标数值，与导出代码同一份数据。
     */
    renderChartChart(retry = 0) {
      const c = this.wv.chart;
      const el = this.$refs.chartChart;
      if (!c || !el) return;
      if (!window.echarts) {
        ensureEcharts().then((ok) => { if (ok) this.renderChartChart(retry); });
        return;
      }
      if (c.kind === "table") return;   // 表格由模板渲染，不进 ECharts
      el.style.height = c.kind === "radar" ? "360px" : "320px";
      // 与架构图同因：面板刚切过来 clientWidth 还是 0，零尺寸画布会把图压成一小团
      const availW = el.clientWidth;
      if (availW < 40) {
        if (retry < 6) setTimeout(() => this.renderChartChart(retry + 1), 80);
        return;
      }
      if (_chartChart && _chartChart.getDom() !== el) {
        _chartChart.dispose();
        _chartChart = null;
      }
      if (!_chartChart) _chartChart = window.echarts.init(el, null, { renderer: "canvas", width: availW });
      else _chartChart.resize({ width: availW });

      const rows = c.raw_values || {};
      const labels = c.labels || [];
      const num = (v) => {
        const n = parseFloat(String(v).replace(/[^0-9.\-]/g, ""));
        return Number.isFinite(n) ? n : 0;
      };

      // 雷达：角度轴 = CSV 第一列（方法），每条多边形 = 一个指标——与导出的
      // TikZ polaraxis 完全同一套语义，预览和代码不能各画各的。
      if (c.kind === "radar") {
        const cols = Object.keys(rows);
        if (!cols.length || !labels.length) return;
        const all = cols.flatMap((k) => (rows[k] || []).map(num));
        const top = Math.max(...all, 1) * 1.12;
        const maxOf = cols.map((k) => Math.max(...(rows[k] || []).map(num), 1));
        const spread = Math.max(...maxOf) / Math.max(Math.min(...maxOf), 1);
        _chartChart.setOption({
          animationDuration: 320,
          title: {
            text: c.title || "", left: "center", top: 4,
            subtext: spread > 8 ? "各指标量纲差异较大，形状仅供示意" : "",
            textStyle: { fontSize: 13, fontWeight: 600, color: "#3a4150" },
            subtextStyle: { fontSize: 11, color: "#b26a2f" },
          },
          tooltip: {},
          legend: { bottom: 0, textStyle: { fontSize: 11, color: "#6b7280" } },
          radar: {
            center: ["50%", "54%"], radius: "60%",
            indicator: labels.map((l) => ({ name: l, max: Math.ceil(top) })),
            axisName: { fontSize: 11, color: "#50596a" },
            splitLine: { lineStyle: { color: "#e6ebf3" } },
            splitArea: { areaStyle: { color: ["#fbfcfe", "#f4f7fb"] } },
          },
          series: [{
            type: "radar",
            symbolSize: 4,
            data: cols.map((k) => ({
              name: k,
              value: labels.map((_, i) => num((rows[k] || [])[i])),
              areaStyle: { opacity: 0.06 },
              lineStyle: { width: 1.4 },
            })),
          }],
        }, true);
        return;
      }

      const kind = c.kind === "line" ? "line" : "bar";
      const series = Object.keys(rows).map((name) => ({
        name,
        type: kind,
        smooth: kind === "line",
        symbolSize: 7,
        barMaxWidth: 46,
        data: (rows[name] || []).map(num),
        // 点位标数值：评委不用对着轴估，这也是"逐格可核对"的一部分
        label: { show: labels.length <= 6, position: "top", fontSize: 10, color: "#50596a" },
      }));
      _chartChart.setOption({
        animationDuration: 320,
        grid: { left: 52, right: 20, top: 46, bottom: 36 },
        title: { text: c.title || "", left: "center", top: 4,
                 textStyle: { fontSize: 13, fontWeight: 600, color: "#3a4150" } },
        tooltip: { trigger: "axis" },
        legend: { top: 26, textStyle: { fontSize: 11, color: "#6b7280" } },
        xAxis: { type: "category", data: labels,
                 axisLabel: { fontSize: 11, color: "#6b7280" },
                 axisLine: { lineStyle: { color: "#d8dee8" } } },
        // min: 0 是这一段的重点：柱状图纵轴不从 0 起会放大差异，学术图表里是硬伤
        yAxis: { type: "value", min: 0, axisLabel: { fontSize: 11, color: "#6b7280" },
                 splitLine: { lineStyle: { color: "#eef1f5" } } },
        series,
      }, true);
    },

    /* ---------------- 两个面板共用的工具 ---------------- */
    async renderMermaid(code, target) {
      const put = (svg) => {
        if (target === "topo") this.wv.svg = svg;
        else this.wv.chartSvg = svg;
      };
      if (!code) return put("");
      if (!(await ensureMermaid()) || !window.mermaid) return put("");
      try {
        // render 的 id 必须唯一：复用同一个 id 时 mermaid 会拿到上一次的残留节点
        const id = "mmd" + Math.random().toString(36).slice(2, 9);
        const out = await window.mermaid.render(id, code);
        put(out && out.svg ? out.svg : "");
      } catch (err) {
        // 语法错时 mermaid 会往 body 里塞一个错误图，这里顺手清掉，别让它出现在页面上
        console.warn("Mermaid 渲染失败：", err);
        document.querySelectorAll('[id^="dmmd"], .mermaid-error').forEach((n) => n.remove());
        put("");
      }
    },

    async copyText(text, label) {
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        ElementPlus.ElMessage.success(`${label || "内容"}已复制`);
      } catch (e) {
        // 非 https 或旧内核下 clipboard API 不可用，退回选中复制
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;opacity:0;left:-9999px";
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy");
          ElementPlus.ElMessage.success(`${label || "内容"}已复制`);
        } catch (_) {
          ElementPlus.ElMessage.error("复制失败，请手动选中代码");
        }
        document.body.removeChild(ta);
      }
    },

    downloadText(text, name, fmt) {
      if (!text) return;
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${name}.${EXT[fmt] || "txt"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1500);
    },

    fmtHint(fmt) {
      return {
        dot: "Graphviz：保存为 .gv，执行 dot -Tpdf architecture.gv -o architecture.pdf",
        tikz: "TikZ：粘进 .tex 导言区加 tikz 宏包；中文标签用 ctex / XeLaTeX 编译",
        matplotlib: "Matplotlib：保存为 .py，执行 python architecture.py 生成 PDF / PNG",
        latex: "LaTeX 三线表：需要 booktabs 宏包，编号与数值均可再改",
        markdown: "Markdown 表格：可直接贴进笔记、README 或综述初稿",
        mermaid: "Mermaid：浏览器渲染失败时，代码仍可粘到 mermaid.live 查看",
      }[fmt] || "复制代码到对应工具中使用";
    },

    /// Markdown 表格 → HTML 表格，用来在右侧给"表格"类型一个真实预览。
    mdTableHtml(code) {
      const lines = String(code || "").split("\n").filter((l) => l.trim().startsWith("|"));
      if (lines.length < 2) return "";
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const head = cells(lines[0]);
      const body = lines.slice(2).map(cells);
      const th = head.map((h) => `<th>${esc(h)}</th>`).join("");
      const tr = body
        .map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`)
        .join("");
      return `<table class="viz-table"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
    },

    /* ---------------- 中英对照阅读 ----------------
     * 联动策略：不做"滚动比例同步"（比例同步在两栏长度不同时会持续漂移），
     * 而是以"视口顶部所在段落"为锚：找到源栏顶部那一段，把目标栏滚到同一段，
     * 并保留段内相对位置。段落高度不同也不影响对应关系，因为对应关系是按段落算的。
     */
    _biLock() {
      // 程序性滚动会再次触发 scroll 事件，用时间戳护栏防回环
      this._biLockUntil = performance.now() + 120;
    },

    /// 鼠标指向某段：左右两侧同时高亮同一段（不滚动，滚动交给用户自己）。
    biHover(i) {
      this.bi.hover = i;
    },

    biZoomSteps() {
      return [70, 80, 90, 100, 110, 125, 140, 160, 180, 200];
    },

    biZoomBy(dir) {
      const steps = this.biZoomSteps();
      const i = steps.indexOf(this.bi.zoom);
      const cur = i < 0 ? steps.indexOf(100) : i;
      const next = steps[Math.min(steps.length - 1, Math.max(0, cur + dir))];
      this.biSetZoom(next);
    },

    biSetZoom(v) {
      const z = Math.max(70, Math.min(200, Math.round(v)));
      if (z === this.bi.zoom) return;
      // 记下两栏各自的"顶部段落 + 段内比例"，缩放后原样还原：
      // 字号一变段落高度全变，不锚定的话视口会莫名跳到别的段落。
      const anchorOf = (pane) => {
        if (!pane) return null;
        const segs = pane.querySelectorAll(".bi-seg");
        if (!segs.length) return null;
        const dr = pane.getBoundingClientRect();
        for (const el of segs) {
          const r = el.getBoundingClientRect();
          if (r.bottom > dr.top + 4) {
            return { i: Number(el.dataset.i), ratio: Math.max(0, (dr.top + 4 - r.top) / Math.max(r.height, 1)) };
          }
        }
        return null;
      };
      const aEn = anchorOf(this.$refs.biEn);
      const aZh = anchorOf(this.$refs.biZh);
      this.bi.zoom = z;
      this.$nextTick(() => {
        const restore = (pane, a) => {
          if (!pane || !a) return;
          const el = pane.querySelector(`.bi-seg[data-i="${a.i}"]`);
          if (!el) return;
          const dr = pane.getBoundingClientRect();
          const r = el.getBoundingClientRect();
          this._biLockUntil = performance.now() + 200;
          pane.scrollTop += (r.top - dr.top) + a.ratio * r.height - 4;
        };
        restore(this.$refs.biEn, aEn);
        restore(this.$refs.biZh, aZh);
      });
    },

    onBiWheel(e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      this._biWheelAcc = (this._biWheelAcc || 0) + e.deltaY;
      if (Math.abs(this._biWheelAcc) < 40) return;
      const dir = this._biWheelAcc > 0 ? -1 : 1;
      this._biWheelAcc = 0;
      this.biZoomBy(dir);
    },

    onBiScroll(side) {
      const src = side === "en" ? this.$refs.biEn : this.$refs.biZh;
      const dst = side === "en" ? this.$refs.biZh : this.$refs.biEn;
      if (!src) return;

      // 触底预取：先判断，避免快速滑动时漏掉
      if (src.scrollTop + src.clientHeight > src.scrollHeight - 500) this.loadBi(false);

      if (!this.bi.sync || !dst || performance.now() < (this._biLockUntil || 0)) return;
      // 不要用 requestAnimationFrame 做节流：无头浏览器 + 虚拟时间下 rAF 可能
      // 长时间不回调，滚动事件全部被吞掉，表现就是"两侧完全不联动"（实测 5 次里挂 2 次）。
      // 改成 16ms 时间闸 + setTimeout 尾随：跟手、可预测，也不打满主线程。
      const now = performance.now();
      const gap = now - (this._biLastAt || 0);
      if (gap >= 16) {
        this._biLastAt = now;
        this._syncBiScroll(side);
        return;
      }
      this._biPending = side;
      if (this._biTimer) return;
      this._biTimer = setTimeout(() => {
        this._biTimer = 0;
        this._biLastAt = performance.now();
        this._syncBiScroll(this._biPending || side);
      }, 16);
    },

    _syncBiScroll(side) {
      const src = side === "en" ? this.$refs.biEn : this.$refs.biZh;
      const dst = side === "en" ? this.$refs.biZh : this.$refs.biEn;
      if (!src || !dst || performance.now() < (this._biLockUntil || 0)) return;
      const segs = src.querySelectorAll(".bi-seg");
      if (!segs.length) return;

      // 一律用 rect 差值算，不用 offsetTop。
      // offsetTop 是相对 offsetParent 的，而 offsetParent 取决于哪一级祖先有定位；
      // 一换布局（或某次渲染前后节点被替换）基准就变，算出来的落点会离谱到
      // "英文滚到 60%，中文跳回 25px"。rect 差值只和两栏视口有关，跟层级无关。
      const sr = src.getBoundingClientRect();
      const topEdge = sr.top + 4;
      let s = segs[segs.length - 1];
      for (const el of segs) {
        if (el.getBoundingClientRect().bottom > topEdge) { s = el; break; }
      }
      const sRect = s.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (topEdge - sRect.top) / Math.max(sRect.height, 1)));

      const t = dst.querySelector(`.bi-seg[data-i="${s.dataset.i}"]`);
      if (t) {
        const dr = dst.getBoundingClientRect();
        const tr = t.getBoundingClientRect();
        // 目标段落里"同一比例位置"当前离 dst 顶部有多远，就把 dst 滚多少
        const delta = (tr.top - dr.top) + ratio * tr.height;
        if (Math.abs(delta) > 2) {
          this._biLock();
          dst.scrollTop = Math.max(0, dst.scrollTop + delta);
        }
      }
      this.bi.cur = Number(s.dataset.i);
    },

    async loadBi(reset) {
      const doc = this.activeDoc;
      if (!doc) return;
      const docId = doc.id;
      if (reset) {
        this.bi.segs = [];
        this.bi.loaded = 0;
        this.bi.cur = 0;
        this.bi.docId = docId;
      }
      if (this.bi.loadingMore) return;
      if (this.bi.loaded >= this.pageCount) return;

      const from = this.bi.loaded + 1;
      this.bi.loadingMore = true;
      try {
        // 第一段请求先不翻译（快），英文立刻可见；译文随后补齐
        const r = await fetch(
          `/api/documents/${docId}/bilingual?page=${from}&pages=1&translate=0`
        ).then((x) => x.json());
        if (this.bi.docId !== docId) return; // 期间切换了文献
        if (r.detail) { ElementPlus.ElMessage.error(r.detail); return; }

        const base = this.bi.segs.length;
        const add = (r.segments || []).map((s, k) => ({ ...s, i: base + k }));
        this.bi.segs = this.bi.segs.concat(add);
        this.bi.loaded = r.page_to || from;
        this.bi.cached = r.cached || this.bi.cached;

        // 译文补齐（异步，不挡渲染）
        if (add.some((s) => !s.zh)) this.fillZh(docId, from, this.bi.loaded);
      } catch (e) {
        ElementPlus.ElMessage.error("对照加载失败：" + e);
      } finally {
        this.bi.loadingMore = false;
      }
    },

    async fillZh(docId, pageFrom, pageTo) {
      try {
        await fetch(`/api/documents/${docId}/bilingual/translate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ page_from: pageFrom, page_to: pageTo, all: false }),
        });
        if (this.bi.docId !== docId) return;
        const r = await fetch(
          `/api/documents/${docId}/bilingual?page=${pageFrom}&pages=${Math.max(1, pageTo - pageFrom + 1)}&translate=0`
        ).then((x) => x.json());
        const zh = {};
        (r.segments || []).forEach((s) => (zh[s.chunk_id] = s.zh));
        this.bi.segs = this.bi.segs.map((s) => (zh[s.chunk_id] ? { ...s, zh: zh[s.chunk_id] } : s));
        this.bi.cached = r.cached || this.bi.cached;
        // 注意：这里**不能**顺手预取下一页。预取一旦写在"翻译完成"回调里，
        // 就会形成 loadBi→fillZh→loadBi 的无限链，用户只是打开一页，
        // 后台却把整篇（乃至 541 页的书）全译完，模型账单直接失控。
        // 下一页只在用户真的滚动到底部时才加载（见 onBiScroll）。
      } catch (e) {
        console.warn("译文补齐失败", e);
      }
    },

    async translateAll() {
      const doc = this.activeDoc;
      if (!doc) return;
      const r = await fetch(`/api/documents/${doc.id}/bilingual/translate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }),
      }).then((x) => x.json());

      if (r.job && r.job.error) {
        ElementPlus.ElMessage.warning(r.job.error);
        return;
      }
      this.bi.job = r.job || { running: true, done: 0, total: this.pageCount };
      ElementPlus.ElMessage.info(r.message || "已开始整篇翻译");

      const timer = setInterval(async () => {
        const st = await fetch(`/api/documents/${doc.id}/bilingual/status`).then((x) => x.json());
        this.bi.job = st.job || this.bi.job;
        this.bi.cached = st.cached || this.bi.cached;
        if (!this.bi.job.running) {
          clearInterval(timer);
          // 译完把已加载的段落整体刷新一次，保持滚动位置不跳
          if (this.bi.loaded) this.refreshLoaded();
          if (this.bi.job.error) ElementPlus.ElMessage.error(this.bi.job.error);
          else ElementPlus.ElMessage.success("整篇翻译完成");
        }
      }, 1500);
    },

    async refreshLoaded() {
      const docId = this.bi.docId;
      const zh = {};
      for (let p = 1; p <= this.bi.loaded; p += 4) {
        const r = await fetch(
          `/api/documents/${docId}/bilingual?page=${p}&pages=${Math.min(4, this.bi.loaded - p + 1)}&translate=0`
        ).then((x) => x.json());
        (r.segments || []).forEach((s) => (zh[s.chunk_id] = s.zh));
      }
      this.bi.segs = this.bi.segs.map((s) => (zh[s.chunk_id] ? { ...s, zh: zh[s.chunk_id] } : s));
    },

    // 点段落 → 回到原文同一页同一 bbox：对照阅读与锚点溯源共用一套坐标
    async biJump(seg) {
      const [x0, y0, x1, y1] = (seg.bbox || "0,0,0,0").split(",").map(Number);
      this.centerView = "reader";
      this.mobilePane = "reader";
      await this.jump({ page: seg.page, bbox: [x0, y0, x1, y1], doc_id: this.activeDocId, text: seg.en });
    },

    // 图谱的文献筛选（只把勾选的文献送进建图，不是把别的节点藏起来：
    // 后端在子图上重算 PageRank 与角色，否则被选中的边缘节点会继承库外的影响力）
    openGraphPick() {
      if (!this.docs.length) {
        ElementPlus.ElMessage.warning("文献库为空，先导入文献");
        return;
      }
      this.graphPick.ids = this.graphPick.applied.length
        ? [...this.graphPick.applied]
        : this.docs.map((d) => d.id);
      this.graphPick.visible = true;
    },

    graphPickAll() {
      this.graphPick.ids = this.docs.map((d) => d.id);
    },

    graphPickNone() {
      this.graphPick.ids = [];
    },

    async applyGraphPick() {
      if (!this.graphPick.ids.length) {
        ElementPlus.ElMessage.warning("至少选一篇文献；如果想回到全库，点「全选」再构建");
        return;
      }
      // 全选等价于全库：回落到不带 doc_ids 的路径，免得同一张图在缓存里存两份
      this.graphPick.applied = this.graphPick.ids.length === this.docs.length
        ? []
        : [...this.graphPick.ids];
      this.graphPick.visible = false;
      await this.loadGraph(true);
      const n = (this.graph.nodes || []).length;
      ElementPlus.ElMessage.success(
        this.graphPick.applied.length ? `已按 ${n} 篇文献重新建图` : "已恢复全库图谱"
      );
    },

    async loadGraph(force) {
      this.graphLoading = true;
      try {
        // 只在"确实筛过"的时候带 doc_ids：全选等于全库，走不带参的旧路径，
        // 这样默认打开图谱仍然是 39 篇 213 边，和之前完全一致。
        const qs = new URLSearchParams();
        if (force) qs.set("refresh", "true");
        if (this.graphPick.applied.length) qs.set("doc_ids", this.graphPick.applied.join(","));
        const r = await fetch(`/api/graph${qs.toString() ? "?" + qs.toString() : ""}`).then((x) => x.json());
        this.graph = r;
        this.selectedNode = null;   // 换了文献集合，旧选中节点可能已不在图上
        await nextTick();
        this.renderGraph();
        if (force) ElementPlus.ElMessage.success("已重算图谱");
      } catch (e) {
        ElementPlus.ElMessage.error("图谱加载失败：" + e);
      } finally {
        this.graphLoading = false;
      }
    },

    renderGraph() {
      const el = this.$refs.graphEl;
      if (!el) return;
      if (!window.echarts) {
        ensureEcharts().then((ok) => { if (ok) this.renderGraph(); });
        return;
      }
      if (!_chart) _chart = window.echarts.init(el, null, { renderer: "canvas" });
      _chart.resize();

      const roleColor = {
        cornerstone: "#d94f4f",
        bridge: "#8a63d2",
        normal: "#4a7fd4",
        peripheral: "#9aa4b2",
      };
      const focus = this.graphFocus();
      _chart.setOption(
        {
          tooltip: {
            formatter: (p) => {
              if (p.dataType !== "node") return "";
              const v = p.data.value;
              return `<b>${v.label}</b><br/>被引 ${v.in_degree} · 引文 ${v.out_degree}<br/>PageRank ${v.pagerank}`;
            },
          },
          series: [{ type: "graph", ...this.graphSeries() }],
          animationDuration: 700,
        },
        true
      );

      _chart.off("click");
      _chart.on("click", (p) => {
        if (p.dataType === "node") {
          // 再点同一个节点 = 取消聚焦，回到全局视图
          this.selectedNode = this.selectedNode && this.selectedNode.id === p.data.value.id
            ? null : p.data.value;
        } else {
          this.selectedNode = null;
        }
        this.applyGraphFocus();
      });
    },

    // 选中某篇后，它的直接邻居（谁引它 / 它引谁）要把标题显示出来：
    // 默认只有基石/桥接常显标签，39 篇全显会糊成一片；但聚焦之后"这些分支叫什么"
    // 恰恰是点这一下想知道的答案，藏起来就没意义了。
    graphFocus() {
      const sel = this.selectedNode && this.selectedNode.id;
      const nb = new Set();
      if (sel) {
        for (const e of this.graph.edges || []) {
          if (e.source === sel) nb.add(e.target);
          else if (e.target === sel) nb.add(e.source);
        }
      }
      return { sel, nb };
    },

    graphSeries() {
      const roleColor = {
        cornerstone: "#d94f4f", bridge: "#8a63d2", normal: "#4a7fd4", peripheral: "#9aa4b2",
      };
      const { sel, nb } = this.graphFocus();
      const data = (this.graph.nodes || []).map((n) => {
        const inFocus = !sel || n.id === sel || nb.has(n.id);
        return {
          id: n.id,
          name: n.label,
          symbolSize: n.size,
          value: n,
          itemStyle: { color: roleColor[n.role] || "#4a7fd4", opacity: inFocus ? 1 : 0.16 },
          label: {
            // 每个节点都显示标题——这是明确要求：图上不许有"没有名字的圈"。
            // 挤的问题是靠上面的文献选择器解决（少选几篇），不是靠藏标签。
            show: true,
            fontSize: n.id === sel ? 13 : 10.5,
            fontWeight: n.id === sel ? "bold" : "normal",
          },
          category: n.cluster,
        };
      });
      const links = (this.graph.edges || []).map((e) => {
        const hot = sel && (e.source === sel || e.target === sel);
        return {
          source: e.source,
          target: e.target,
          lineStyle: { width: hot ? 2.2 : 1, opacity: sel ? (hot ? 0.95 : 0.1) : 0.75 },
        };
      });
      return {
        layout: "force",
        roam: true,
        draggable: true,
        // 39 篇 213 条边，原来的 repulsion 260 / edgeLength 60–160 会把节点挤成一团，
        // 中间几个大点直接叠在一起。拉开斥力与边长，并把重力调低，让外圈的边缘节点
        // 真正散到外围去，结构才看得出来。
        force: { repulsion: [700, 1400], edgeLength: [130, 300], gravity: 0.035 },
        scaleLimit: { min: 0.35, max: 3 },
        emphasis: { focus: "adjacency", lineStyle: { width: 2.5 } },
        // 不启用 hideOverlap：那个选项会在标签重叠时把一部分直接隐藏，
        // 与本项目"每个圆圈都要有名字"的要求冲突。挤的时候应该少选几篇文献，
        // 而不是让图悄悄少显示几个名字。
        labelLayout: { hideOverlap: false },
        label: { position: "right", fontSize: 10, formatter: (p) => (p.name || "").slice(0, 26) },
        lineStyle: { color: "#c2c9d4", width: 1, curveness: 0.12, opacity: 0.75 },
        edgeSymbol: ["none", "arrow"],
        edgeSymbolSize: [0, 6],
        data,
        links,
      };
    },

    // 从侧卡点邻居：把焦点移过去，图上随之重聚焦
    focusNode(n) {
      if (!n) return;
      this.selectedNode = n;
      this.applyGraphFocus();
    },

    // 只更新样式，不重建图表：notMerge 会让力导向重新布局，整片节点跳走。
    applyGraphFocus() {
      if (!_chart) return;
      _chart.setOption({ series: [this.graphSeries()] });
    },

    roleName(role) {
      return { cornerstone: "基石论文", bridge: "桥接论文", peripheral: "边缘论文", normal: "普通" }[role] || role;
    },

    async genSurvey() {
      if (!this.graph.nodes || !this.graph.nodes.length) {
        ElementPlus.ElMessage.warning("文献库为空，先导入文献");
        return;
      }
      this.survey.loading = true;
      try {
        const r = await fetch("/api/graph/survey", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_papers: 8 }),
        }).then((x) => x.json());
        if (r.detail) {
          ElementPlus.ElMessage.error(r.detail);
          return;
        }
        this.survey.html = mdToHtml(r.survey || "");
        this.survey.papers = (r.papers || []).map((p) => ({
          ...p,
          title: p.title || "（无标题）",
        }));
        this.survey.visible = true;
      } catch (e) {
        ElementPlus.ElMessage.error("综述生成失败：" + e);
      } finally {
        this.survey.loading = false;
      }
    },

    // 未来工作探索（赛题 1.5.4）。内容由后端从各篇 Future Work / Limitations 原句抽取，
    // 这里只取一次、原样列出：不写第二次加工，免得"Agent 提出"变成"前端编的"。
    async openFutureWork() {
      if (!this.docs.length) {
        ElementPlus.ElMessage.warning("文献库为空，先导入文献");
        return;
      }
      this.fw.loading = true;
      try {
        const r = await fetch("/api/research/future-work?limit=40").then((x) => x.json());
        if (r.detail) {
          ElementPlus.ElMessage.error(r.detail);
          return;
        }
        this.fw.items = r.items || [];
        this.fw.method = r.method || "";
        this.fw.stats = r.stats || null;
        this.fw.visible = true;
      } catch (e) {
        ElementPlus.ElMessage.error("获取未来工作方向失败：" + e);
      } finally {
        this.fw.loading = false;
      }
    },

    async openFromGraph(node) {
      const d = this.docs.find((x) => x.id === node.id);
      this.centerView = "reader";
      if (d) {
        await this.openDoc(d);
      } else {
        this.activeDoc = {
          id: node.id,
          filename: node.filename || node.label,
          title: node.label,
          page_count: node.pages || 1,
        };
        this.pageCount = node.pages || 1;
        this.page = 1;
      }
    },

    async removeDoc(d) {
      // P0：原实现点一下就永久删库。现在必须二次确认，且后端只移入回收站。
      try {
        await ElementPlus.ElMessageBox.confirm(
          `确定删除「${d.title || d.filename}」吗？\n会移入回收站，源文件保留，可随时恢复。`,
          "删除文献", { type: "warning", confirmButtonText: "移入回收站", cancelButtonText: "取消" }
        );
      } catch (e) {
        return;
      }
      const r = await fetch(`/api/documents/${d.id}?actor=ui&reason=ui_delete`, { method: "DELETE" })
        .then((x) => x.json());
      if (!r.ok) { ElementPlus.ElMessage.error("删除失败"); return; }
      ElementPlus.ElMessage.success("已移入回收站（左栏「回收站」可恢复）");
      if (this.activeDoc && this.activeDoc.id === d.id) { this.activeDoc = null; this.highlights = []; }
      this.loadDocs();
      this.loadInfo();
    },
  },
});

app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
// 暴露实例供 QA 回归台驱动真实方法（生产无副作用，也便于评审现场调试）
window.__sciagent = app.mount("#app");
