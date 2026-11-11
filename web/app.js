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
      tl: { words: [], loaded: false, scale: 1, W: 0, H: 0, page: 0 },
      refIndex: {},   // 引用编号 → {index,page,bbox,text}
      // 写作 Copilot：模型只写论述 + [n]，References 条目全部由服务端从库内元数据拼出
      wr: { idea: "", loading: false, result: null, error: "" },
      sel: { visible: false, loading: false, src: "", zh: "", style: {}, truncated: false, x: 0, y: 0 },
      citePop: { visible: false, index: 0, page: 1, bbox: null, text: "", style: {}, x: 0, y: 0 },
      messages: [],
      question: "",
      mode: "qa",
      scope: "all",
      streamMode: false,
      trash: { visible: false, items: [] },
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
  },
  mounted() {
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
      this.jump({ page: Number(el.dataset.page), bbox: [x0, y0, x1, y1], doc_id: this.activeDocId });
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
        this.tl = { words: [], loaded: false, scale: 1, W: 0, H: 0, page: 0 };
        return;
      }
      const page = this.page;
      this.tl = { words: [], loaded: false, scale: this.tl.scale || 1, W: 0, H: 0, page };
      try {
        const r = await fetch(`/api/documents/${doc.id}/page/${page}/text`).then((x) => x.json());
        // 快速翻页时旧请求可能后到：页码或文献对不上就丢弃，否则会出现"第 5 页的
        // 文字叠在第 6 页图上"，比没有文本层更糟。
        if (this.page !== page || !this.activeDoc || this.activeDoc.id !== doc.id) return;
        this.tl.W = r.width || 612;
        this.tl.H = r.height || 792;
        this.tl.words = (r.words || []).map((w) => this.makeWord(w));
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
      };
      await this.jump({ page: e.page, bbox: e.bbox, doc_id: this.activeDocId, text: e.text });
    },

    /* ---------------- 划词翻译 ---------------- */

    onTextSelect() {
      const s = window.getSelection();
      const txt = (s ? String(s) : "").trim();
      if (!txt || txt.length < 2) return;
      // 中文选区不再翻译；纯数字/符号也没意义
      if (!/[A-Za-z]{2,}/.test(txt)) return;
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
          this.sel.zh = r.zh;
          this.sel.truncated = !!r.truncated;
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

    onImgLoad(e) {
      // 已存在的高亮在换页后需要按新尺寸重算
      this.highlights = this.highlights.map((h) => ({ ...h, style: this.boxStyle(h.bbox) }));
      // 文本层按 pt 布局，缩放比例只能等图片真正渲染出来才知道
      this.syncTextScale();
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

    async jump(ev) {
      if (ev.doc_id && (!this.activeDoc || this.activeDoc.id !== ev.doc_id)) {
        const d = this.docs.find((x) => x.id === ev.doc_id);
        if (d) await this.openDoc(d);
      }
      this.page = ev.page;
      // 跳页不走 goPage，这里要自己把文本层换成目标页，否则会残留上一页的文字
      if (this.tl.page !== this.page) this.loadTextLayer();
      await nextTick();
      this.highlights = [{ bbox: ev.bbox, text: ev.text || "", style: this.boxStyle(ev.bbox) }];
      // 必须滚到"高亮框"而不是滚整个阅读区：参考文献条目常在页面底部，
      // 只让阅读区进入视口的话目标仍在视野外，用户会以为跳转失败了（实测如此）。
      // 这里用 rect 差值直接算滚动量，不依赖 offsetParent 与 scrollIntoView 的实现差异。
      await nextTick();
      const stage = document.querySelector(".page-stage");
      const hl = stage && stage.querySelector(".hl");
      if (stage && hl) {
        const sr = stage.getBoundingClientRect();
        const hr = hl.getBoundingClientRect();
        const delta = hr.top - sr.top - (stage.clientHeight - hr.height) / 2;
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
