/* SciAgent 前端：Vue3 + Element Plus，单文件无构建链。
 * 设计取舍：不上 React/Vite，因为 8 天工期里构建链是纯风险项；
 * 但保留了"组件化 + 状态驱动"的结构，后续换框架只替换视图层。
 */

const { createApp, reactive, nextTick } = Vue;

// ECharts 实例放在组件外：它持有大量 DOM 引用，放进响应式对象会被深度代理拖慢
let _chart = null;

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
        html += '<table class="md-table"><thead><tr>' +
          cells.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
        inTable = true;
      } else {
        html += "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
      }
      continue;
    }
    if (inTable) { html += "</tbody></table>"; inTable = false; }
    if (!line) { html += "<br>"; continue; }
    html += line.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>") + "<br>";
  }
  if (inTable) html += "</tbody></table>";

  // 锚点做成可点击徽章：[p3@120,480,468,512]
  return html.replace(
    /\[p(\d+)@(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]/g,
    (m, p, x0, y0, x1, y1) =>
      `<span class="anchor" data-page="${p}" data-bbox="${x0},${y0},${x1},${y1}">p${p} ▸</span>`
  );
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
      messages: [],
      question: "",
      mode: "qa",
      scope: "all",
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
      return { reader: "阅读器", bilingual: "中英对照", graph: "引用图谱" }[this.centerView] || "阅读器";
    },
    pageUrl() {
      if (!this.activeDoc) return "";
      return `/api/documents/${this.activeDoc.id}/page/${this.page}.png?dpi=130&t=${Date.now()}`;
    },
  },
  mounted() {
    this.loadInfo();
    this.loadDocs();
    document.addEventListener("click", (e) => {
      const el = e.target.closest(".anchor");
      if (!el) return;
      const [x0, y0, x1, y1] = el.dataset.bbox.split(",").map(Number);
      this.jump({ page: Number(el.dataset.page), bbox: [x0, y0, x1, y1], doc_id: this.activeDocId });
    });
    // 图谱在容器尺寸变化后必须重算布局，否则窗口一缩节点就叠在左上角
    window.addEventListener("resize", () => {
      if (this.centerView === "graph" && _chart) _chart.resize();
    });
  },
  methods: {
    async loadInfo() {
      const r = await fetch("/api/system/info").then((x) => x.json());
      this.info = r;
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
      ElementPlus.ElMessage.success(`入库成功：${r.chunks} 个检索块，用时 ${r.elapsed_ms}ms`);
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
      const [pages, secs] = await Promise.all([
        fetch(`/api/documents/${d.id}/pages`).then((x) => x.json()),
        fetch(`/api/documents/${d.id}/sections`).then((x) => x.json()),
      ]);
      this.pageSizes = {};
      (pages.pages || []).forEach((p) => (this.pageSizes[p.page] = p));
      this.sections = secs.sections || [];
      // 对照视图下切文献要立刻换成新文献的段落，否则会继续显示上一篇
      if (this.centerView === "bilingual") this.loadBi(true);
    },

    goPage(p) {
      if (p < 1 || p > this.pageCount) return;
      this.page = p;
      this.highlights = [];
    },

    onImgLoad(e) {
      // 已存在的高亮在换页后需要按新尺寸重算
      this.highlights = this.highlights.map((h) => ({ ...h, style: this.boxStyle(h.bbox) }));
    },

    boxStyle(bbox) {
      const meta = this.pageSizes[this.page];
      if (!meta || !bbox) return { display: "none" };
      const [x0, y0, x1, y1] = bbox;
      return {
        left: (x0 / meta.width) * 100 + "%",
        top: (y0 / meta.height) * 100 + "%",
        width: Math.max(((x1 - x0) / meta.width) * 100, 0.6) + "%",
        height: Math.max(((y1 - y0) / meta.height) * 100, 0.4) + "%",
      };
    },

    async jump(ev) {
      if (ev.doc_id && (!this.activeDoc || this.activeDoc.id !== ev.doc_id)) {
        const d = this.docs.find((x) => x.id === ev.doc_id);
        if (d) await this.openDoc(d);
      }
      this.page = ev.page;
      await nextTick();
      this.highlights = [{ bbox: ev.bbox, text: ev.text || "", style: this.boxStyle(ev.bbox) }];
      const stage = document.querySelector(".page-stage");
      if (stage) stage.scrollIntoView({ behavior: "smooth", block: "center" });
    },

    async ask(text) {
      const q = (text || this.question || "").trim();
      if (!q || this.loading) return;
      this.question = "";
      this.messages.push({ role: "user", html: mdToHtml(q) });
      this.loading = true;
      this.scrollChat();

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
      await fetch(`/api/documents/${d.id}`, { method: "DELETE" });
      if (this.activeDoc && this.activeDoc.id === d.id) { this.activeDoc = null; this.highlights = []; }
      this.loadDocs();
      this.loadInfo();
    },
  },
});

app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
app.mount("#app");
