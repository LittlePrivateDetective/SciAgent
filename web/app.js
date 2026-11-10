/* SciAgent 前端：Vue3 + Element Plus，单文件无构建链。
 * 设计取舍：不上 React/Vite，因为 8 天工期里构建链是纯风险项；
 * 但保留了"组件化 + 状态驱动"的结构，后续换框架只替换视图层。
 */

const { createApp, reactive, nextTick } = Vue;

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
      loading: false,
      rescanning: false,
      dup: { visible: false, message: "", items: [], pendingId: "" },
      samples: [
        "Attention Is All You Need 的第 6 作者是谁，在哪个机构？",
        "Transformer 训练 Base 模型用了多少块 GPU，什么型号？",
        "Transformer (big) 在 EN-DE 上的 BLEU 是多少？比之前最好结果提升多少？",
        "对比 GPT-1、GPT-2 和 GPT-3 的参数量变化",
      ],
    };
  },
  computed: {
    activeDocId() { return this.activeDoc ? this.activeDoc.id : ""; },
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
  },
  methods: {
    async loadInfo() {
      const r = await fetch("/api/system/info").then((x) => x.json());
      this.info = r;
    },
    async loadDocs() {
      const r = await fetch("/api/documents").then((x) => x.json());
      this.docs = (r.documents || []).map((d) => ({ ...d, _sel: this.selection.includes(d.id) }));
      if (!this.activeDoc && this.docs.length) this.openDoc(this.docs[0]);
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
      this.page = 1;
      this.pageCount = d.page_count || 1;
      this.highlights = [];
      const [pages, secs] = await Promise.all([
        fetch(`/api/documents/${d.id}/pages`).then((x) => x.json()),
        fetch(`/api/documents/${d.id}/sections`).then((x) => x.json()),
      ]);
      this.pageSizes = {};
      (pages.pages || []).forEach((p) => (this.pageSizes[p.page] = p));
      this.sections = secs.sections || [];
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

      const docIds = this.selection.length ? this.selection : [];
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
