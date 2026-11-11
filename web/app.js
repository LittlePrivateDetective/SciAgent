/* SciAgent 前端：Vue3 + Element Plus，单文件无构建链。
 * 设计取舍：不上 React/Vite，因为 8 天工期里构建链是纯风险项；
 * 但保留了"组件化 + 状态驱动"的结构，后续换框架只替换视图层。
 */

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

/* ---------- 极简 Markdown 渲染（够用即止：表格 / 粗体 / 行内代码 / 换行） ---------- */
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

const app = createApp({
  data() {
    return {
      info: {},
      docs: [],
      selection: [],
      activeDoc: null,
      page: 1,
      pageCount: 1,
      pageSizes: {},
      sections: [],
      showTree: false,
      highlights: [],
      // 页内文本层：words 里每项带预处理好的 style，避免每帧现算 200+ 个词的定位。
      // scale 由图片实际渲染宽度换算，文本层内部一律按 PDF 原始 pt 布局。
      tl: { words: [], sents: [], loaded: false, scale: 1, W: 0, H: 0, page: 0, zhLoading: false },
      refIndex: {},   // 引用编号 → {index,page,bbox,text}
      // 写作 Copilot：模型只写论述 + [n]，References 条目全部由服务端从库内元数据拼出
      wr: { idea: "", loading: false, result: null, error: "", tab: "outline" },
      // 架构拓扑图（1.6.2）+ 数据图表与图注（1.6.3）：两块共用一套代码/预览布局
      wv: {
        idea: "",
        tLoading: false, tError: "", topology: null, tFmt: "mermaid", svg: "",
        csv: "",
        cKind: "bar", cFmt: "mermaid", cTitle: "实验结果",
        cLoading: false, cError: "", chart: null, chartSvg: "",
        sampleCsv: "Model,BLEU,Params\nTransformer,28.4,65\nBERT,34.5,110\nGPT-3,41.0,175",
      },
      sel: { visible: false, loading: false, src: "", zh: "", title: "划词翻译", style: {}, truncated: false, dragging: false, x: 0, y: 0 },
      citePop: { visible: false, index: 0, page: 1, bbox: null, text: "", style: {}, x: 0, y: 0, cites: [], occ: 0 },
      // 悬停翻译（官方 1.3.3.1）：默认关，打开后鼠标停 0.4 秒译整句。
      // cache 是为了"同一句反复悬停不重复请求"——一页读下来常见句子会被来回扫。
      hover: { on: false, visible: false, loading: false, src: "", zh: "", style: {}, timer: null, cache: {} },
      // 中文译本（官方 1.3.3.2）：读 translations 缓存拼 Markdown，可预览可下载
      zh: {
        visible: false, loading: false, markdown: "", chars: 0, total: 0,
        translated: 0, missing: 0, verbatim: 0, ratio: 0, title: "", docId: "", truncated: false,
      },
      messages: [],
      question: "",
      mode: "qa",
      scope: "all",
      streamMode: false,
      trash: { visible: false, items: [] },
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
        cached: { total: 0, done: 0, ratio: 0 },
        job: { running: false, done: 0, total: 0, error: "" },
        docId: "",
      },
      graph: { nodes: [], edges: [], stats: null, insights: null, clusters: [] },
      graphLoading: false,
      selectedNode: null,
      dup: { visible: false, message: "", items: [], pendingId: "" },
      survey: { visible: false, html: "", papers: [], loading: false },
      samples: [
        "Attention Is All You Need 的第 6 作者是谁，在哪个机构？",
        "Transformer 训练 Base 模型用了多少块 GPU，什么型号？",
        "Transformer (big) 在 EN-DE 上的 BLEU 是多少？比之前最好结果提升多少？",
        "对比 BERT 与 Transformer 编码器在结构上的承袭关系",
      ],
    };
  },
  computed: {
    activeDocId() { return this.activeDoc ? this.activeDoc.id : ""; },
    viewTitle() {
      return { reader: "阅读器", bilingual: "中英对照", graph: "引用图谱", write: "写作助手" }[this.centerView] || "阅读器";
    },
    pageUrl() {
      if (!this.activeDoc) return "";
      return `/api/documents/${this.activeDoc.id}/page/${this.page}.png?dpi=130&t=${Date.now()}`;
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
  },
  mounted() {
    // Mermaid 用 startOnLoad:false —— 让它在切换 tab / 点生成时按需渲染，
    // 否则页面一进来就去扫全站 DOM，白白多一次布局。
    if (window.mermaid) {
      window.mermaid.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "loose",
        flowchart: { htmlLabels: true, useMaxWidth: true, curve: "basis" },
      });
    }
    // 悬停翻译是"高频花钱"的功能，偏好必须跨会话记住：
    // 否则每次刷新都回到关闭，用户每次都要重新打开
    try {
      this.hover.on = localStorage.getItem("sciagent.hoverTrans") === "1";
    } catch (e) {
      this.hover.on = false;
    }
    this.loadInfo();
    this.loadDocs();
    document.addEventListener("click", (e) => {
      const wc = e.target.closest(".wcite");
      if (wc) {
        this.jumpToRef(wc.dataset.i);
        return;
      }
      const el = e.target.closest(".anchor");
      if (!el) return;
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
    });
    // 图谱在容器尺寸变化后必须重算布局，否则窗口一缩节点就叠在左上角
    window.addEventListener("resize", () => {
      if (this.centerView === "graph" && _chart) _chart.resize();
      if (_topoChart) _topoChart.resize();
      this.syncTextScale();
    });
  },
  methods: {
    // mdToHtml 是模块级函数，模板里访问不到实例外的东西，必须挂到 methods 上
    mdToHtml,

    async loadInfo() {
      const r = await fetch("/api/system/info").then((x) => x.json());
      this.info = r;
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

    async doUpload(opt) {
      const fd = new FormData();
      fd.append("file", opt.file);
      const r = await fetch("/api/documents/upload", { method: "POST", body: fd }).then((x) => x.json());
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
      const r = await fetch("/api/library/rescan", { method: "POST" }).then((x) => x.json());
      ElementPlus.ElMessage.info(r.message || "已开始导入");
      // 后台任务，轮询几次让用户看到增长
      let n = 0;
      const timer = setInterval(async () => {
        await this.loadDocs();
        await this.loadInfo();
        if (++n > 40) { clearInterval(timer); this.rescanning = false; }
      }, 3000);
    },

    async openDoc(d) {
      this.activeDoc = d;
      this.scope = "doc";
      this.page = 1;
      this.pageCount = d.page_count || 1;
      this.highlights = [];
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
      // 对照视图下切文献要立刻换成新文献的段落，否则会继续显示上一篇
      if (this.centerView === "bilingual") this.loadBi(true);
    },

    goPage(p) {
      if (p < 1 || p > this.pageCount) return;
      this.page = p;
      this.highlights = [];
      this.sel.visible = false;
      this.citePop.visible = false;
      this.loadTextLayer();
    },

    /* ---------------- 页内文本层：让页图上的文字可选、角标可点 ---------------- */

    async loadRefIndex(docId) {
      this.refIndex = {};
      try {
        const r = await fetch(`/api/documents/${docId}/references`).then((x) => x.json());
        const map = {};
        (r.entries || []).forEach((e) => (map[e.index] = e));
        this.refIndex = map;
      } catch (e) {
        console.warn("引用索引加载失败", e);
      }
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
      const img = this.$refs.pageImg;
      if (!img || !img.clientWidth || !this.tl.W) return;
      this.tl.scale = img.clientWidth / this.tl.W;
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

    onImgLoad(e) {
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

    async ask(text) {
      const q = (text || this.question || "").trim();
      if (!q || this.loading) return;
      this.question = "";
      this.messages.push({ role: "user", html: mdToHtml(q) });
      this.scrollChat();
      if (this.streamMode) return this.askStream(q);
      this.loading = true;

      // 检索范围：勾选了文献就用勾选的；否则按"当前文献"限定时只用正在读的那篇。
      // 没有这个约束，库内 37 篇时问"这篇论文的第六作者"会被别的文献抢答。
      let docIds = this.selection.length ? this.selection : [];
      // 对比模式必须跨文献，否则"当前文献"会把对比限制成一篇自己跟自己比
      if (!docIds.length && this.mode !== "compare" && this.scope === "doc" && this.activeDoc) {
        docIds = [this.activeDoc.id];
      }
      try {
        const r = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, doc_ids: docIds, mode: this.mode }),
        }).then((x) => x.json());

        this.messages.push({
          role: "assistant",
          html: mdToHtml(r.answer || "（无返回）"),
          evidence: r.evidence || [],
          verify: r.verify || {},
          trace: r.trace || [],
        });
      } catch (e) {
        this.messages.push({ role: "assistant", html: "请求失败：" + e });
      } finally {
        this.loading = false;
        this.scrollChat();
      }
    },

    docIdsFor() {
      // 对比模式必须跨文献，否则"当前文献"会把对比限制成一篇自己跟自己比
      let ids = this.selection.length ? this.selection : [];
      if (!ids.length && this.mode !== "compare" && this.scope === "doc" && this.activeDoc) {
        ids = [this.activeDoc.id];
      }
      return ids;
    },

    /* SSE 流式问答：thinking 事件渲染为思考时间线，token 事件逐字追加。
     * 不套 JSON 包裹，所以拿到的是模型真实 delta；done 事件再补证据与校验。 */
    async askStream(q) {
      const msg = { role: "assistant", html: "", evidence: [], verify: {}, trace: [], thinking: [] };
      this.messages.push(msg);
      this.loading = true;
      let raw = "";
      try {
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, doc_ids: this.docIdsFor(), mode: this.mode }),
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
              msg.html = mdToHtml(raw);
            } else if (ev.event === "thinking") {
              msg.thinking.push(`${ev.stage} · ${ev.text}`);
            } else if (ev.event === "done") {
              msg.html = mdToHtml(ev.answer || raw);
              msg.evidence = ev.evidence || [];
              msg.verify = ev.verify || {};
            } else if (ev.event === "error") {
              msg.thinking.push("错误边界 · " + ev.text);
              if (!msg.html) msg.html = mdToHtml("生成中断，已安全降级：" + ev.text);
            }
            this.scrollChat();
          }
        }
        if (!msg.html) msg.html = mdToHtml("（未收到有效内容）");
      } catch (e) {
        msg.thinking.push("错误边界 · " + e);
        msg.html = mdToHtml("请求失败：" + e);
      } finally {
        this.loading = false;
        this.scrollChat();
      }
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
    renderTopoChart() {
      const t = this.wv.topology;
      const el = this.$refs.topoChart;
      if (!t || !el || !window.echarts) return;

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
      if (!_topoChart) _topoChart = window.echarts.init(el, null, { renderer: "canvas" });
      _topoChart.resize();

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
          if (r.format === "mermaid") await this.renderMermaid(r.code, "chart");
        }
      } catch (e) {
        this.wv.cError = "请求失败，请检查服务是否在运行";
        ElementPlus.ElMessage.error(this.wv.cError);
      } finally {
        this.wv.cLoading = false;
      }
    },

    /* ---------------- 两个面板共用的工具 ---------------- */
    async renderMermaid(code, target) {
      const put = (svg) => {
        if (target === "topo") this.wv.svg = svg;
        else this.wv.chartSvg = svg;
      };
      if (!window.mermaid || !code) return put("");
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
      this._biLockUntil = performance.now() + 140;
    },

    onBiScroll(side) {
      const src = side === "en" ? this.$refs.biEn : this.$refs.biZh;
      const dst = side === "en" ? this.$refs.biZh : this.$refs.biEn;
      if (!src) return;

      // 触底预取：先判断，避免快速滑动时漏掉
      if (src.scrollTop + src.clientHeight > src.scrollHeight - 500) this.loadBi(false);

      if (!this.bi.sync || !dst || performance.now() < (this._biLockUntil || 0)) return;
      const segs = src.querySelectorAll(".bi-seg");
      if (!segs.length) return;

      // 二分找"顶部第一条与视口相交的段落"
      const top = src.scrollTop + 4;
      let lo = 0, hi = segs.length - 1, idx = 0;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (segs[mid].offsetTop <= top) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      const s = segs[idx];
      const ratio = Math.min(1, Math.max(0, (src.scrollTop - s.offsetTop) / Math.max(s.offsetHeight, 1)));
      const t = dst.querySelector(`.bi-seg[data-i="${s.dataset.i}"]`);
      if (t) {
        this._biLock();
        dst.scrollTop = t.offsetTop + ratio * t.offsetHeight;
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
      await this.jump({ page: seg.page, bbox: [x0, y0, x1, y1], doc_id: this.activeDocId, text: seg.en });
    },

    async loadGraph(force) {
      this.graphLoading = true;
      try {
        const r = await fetch(`/api/graph${force ? "?refresh=true" : ""}`).then((x) => x.json());
        this.graph = r;
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
      if (!el || !window.echarts) return;
      if (!_chart) _chart = window.echarts.init(el, null, { renderer: "canvas" });
      _chart.resize();

      const roleColor = {
        cornerstone: "#d94f4f",
        bridge: "#8a63d2",
        normal: "#4a7fd4",
        peripheral: "#9aa4b2",
      };
      const nodes = (this.graph.nodes || []).map((n) => ({
        id: n.id,
        name: n.label,
        symbolSize: n.size,
        value: n,
        itemStyle: { color: roleColor[n.role] || "#4a7fd4" },
        // 只给核心节点常显标签，40 篇的图上全显会糊成一团
        label: { show: n.role === "cornerstone" || n.role === "bridge" || n.in_degree >= 2 },
        category: n.cluster,
      }));

      _chart.setOption(
        {
          tooltip: {
            formatter: (p) => {
              if (p.dataType !== "node") return "";
              const v = p.data.value;
              return `<b>${v.label}</b><br/>被引 ${v.in_degree} · 引文 ${v.out_degree}<br/>PageRank ${v.pagerank}`;
            },
          },
          series: [
            {
              type: "graph",
              layout: "force",
              roam: true,
              draggable: true,
              force: { repulsion: 260, edgeLength: [60, 160], gravity: 0.08 },
              emphasis: { focus: "adjacency", lineStyle: { width: 2.5 } },
              label: { position: "right", fontSize: 11, formatter: (p) => (p.name || "").slice(0, 26) },
              lineStyle: { color: "#c2c9d4", width: 1, curveness: 0.12, opacity: 0.75 },
              edgeSymbol: ["none", "arrow"],
              edgeSymbolSize: [0, 6],
              data: nodes,
              links: (this.graph.edges || []).map((e) => ({ source: e.source, target: e.target })),
            },
          ],
          animationDuration: 700,
        },
        true
      );

      _chart.off("click");
      _chart.on("click", (p) => {
        if (p.dataType === "node") this.selectedNode = p.data.value;
      });
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
