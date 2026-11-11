/* SciAgent 控制台：Vue3 + Element Plus，与主界面同一套 CDN，无构建链。
   设计取舍：
   - 参数表由服务端下发（键名 / 类型 / 范围 / 生效方式都在后端定义），前端只渲染，
     不在页面里再抄一份默认值，避免"界面默认 8、实际 4"这种鬼故事。
   - 未保存的改动要能一眼看见：脏行左侧有竖条、顶栏显示未保存数量。
   - 密钥类恒不回显：输入框只显示"已配置 ••••"，留空提交表示不修改。
   - 口令放在服务端（ADMIN_PASSWORD），前端只负责输入；所有请求走同一个 api()，
     401 一律退回登录页，避免"会话过期后页面还显示着旧数据"。
*/

const { createApp } = Vue;

const app = createApp({
  data() {
    return {
      // ---- 鉴权 ----
      authReady: false,      // 有没有问过服务端"要不要登录"
      authEnabled: true,     // 服务端是否配置了口令
      authed: false,
      pwd: "",
      loggingIn: false,
      authError: "",

      view: "overview",
      loading: false,
      saving: false,
      params: [],
      groups: [],
      meta: { total: 0, overridden: 0 },
      form: {},
      original: {},
      errors: {},
      q: "",
      showAdvanced: false,
      groupFilter: "",
      overview: {
        counts: {}, corpus: {}, storage: {}, runtime: {}, audit: [],
        llm: {}, ollama: {}, vector: {}, embedding: {}, rerank: {}, keepalive: {},
      },
      auditItems: [],
      test: { visible: false, running: false, prompt: "用一句话说明什么是检索增强生成。", maxTokens: 512, result: null, raw: "" },
      importVisible: false,
      importText: "",
      importing: false,
    };
  },

  computed: {
    navs() {
      return [
        { key: "overview", label: "运行概览" },
        { key: "params", label: "参数中心", count: this.params.length },
        { key: "audit", label: "审计日志" },
      ];
    },
    viewTitle() {
      return { overview: "运行概览", params: "参数中心", audit: "审计日志" }[this.view] || "";
    },
    counts() { return this.overview.counts || {}; },
    corpus() { return this.overview.corpus || {}; },
    storage() { return this.overview.storage || {}; },
    runtime() { return this.overview.runtime || {}; },
    llm() { return this.overview.llm || {}; },
    ollama() { return this.overview.ollama || {}; },
    embedding() { return this.overview.embedding || {}; },
    rerank() { return this.overview.rerank || {}; },

    keepalive() { return this.overview.keepalive || {}; },

    keepaliveText() {
      const k = this.keepalive;
      if (!k.enabled) return "已关闭";
      const parts = [`每 ${k.interval_s || "-"}s`];
      if (k.beats) parts.push(`已跳 ${k.beats} 次`);
      if (k.last_ms !== null && k.last_ms !== undefined) parts.push(`最近 ${k.last_ms}ms`);
      if (k.last_warm && k.last_warm !== "skip" && k.last_warm !== "none") parts.push(`预热 ${k.last_warm}`);
      return parts.join(" · ");
    },

    uptimeText() {
      const s = this.runtime.uptime_sec || 0;
      if (s < 60) return `${s} 秒`;
      if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
      return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
    },

    dirtyKeys() {
      return this.params.filter((p) => this.isDirty(p)).map((p) => p.key);
    },

    visibleGroups() {
      const kw = (this.q || "").trim().toLowerCase();
      return this.groups
        .filter((g) => !this.groupFilter || g.key === this.groupFilter)
        .map((g) => {
          const items = this.params.filter((p) => {
            if (p.group !== g.key) return false;
            if (p.advanced && !this.showAdvanced) return false;
            if (!kw) return true;
            return (p.label + p.key + p.desc).toLowerCase().includes(kw);
          });
          return { ...g, items };
        })
        .filter((g) => g.items.length);
    },
  },

  mounted() {
    this.checkSession();
  },

  methods: {
    /* ---------------- 鉴权 ---------------- */

    async checkSession() {
      try {
        const r = await fetch("/api/admin/session").then((x) => x.json());
        this.authEnabled = !!r.enabled;
        this.authed = !!r.authenticated;
      } catch (e) {
        this.authError = "无法连接服务，请确认后端已启动";
      } finally {
        this.authReady = true;
      }
      if (this.authed) this.reload();
    },

    async doLogin() {
      if (!this.pwd.trim() || this.loggingIn) return;
      this.loggingIn = true;
      this.authError = "";
      try {
        const resp = await fetch("/api/admin/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: this.pwd }),
        });
        if (resp.status === 429) {
          this.authError = "尝试次数过多，请 10 分钟后再试";
          return;
        }
        if (!resp.ok) {
          this.authError = "口令不正确";
          this.pwd = "";
          return;
        }
        this.authed = true;
        this.pwd = "";
        await this.reload();
      } catch (e) {
        this.authError = "登录请求失败，请检查服务";
      } finally {
        this.loggingIn = false;
      }
    },

    async logout() {
      try {
        await fetch("/api/admin/logout", { method: "POST" });
      } catch (e) {
        /* 退出失败也要把本地状态清掉，否则界面会停在"看起来还登着" */
      }
      this.authed = false;
      this.params = [];
      this.overview = {
        counts: {}, corpus: {}, storage: {}, runtime: {}, audit: [],
        llm: {}, ollama: {}, vector: {}, embedding: {}, rerank: {}, keepalive: {},
      };
      this.pwd = "";
    },

    /** 统一请求入口：401 直接退回登录页，避免过期后继续展示旧数据 */
    async api(path, opts) {
      const r = await fetch(path, opts);
      if (r.status === 401) {
        this.authed = false;
        this.authError = "登录已过期，请重新输入口令";
        throw new Error("unauthorized");
      }
      return r.json();
    },

    /* ---------------- 数据 ---------------- */

    async reload() {
      this.loading = true;
      try {
        await Promise.all([this.loadParams(), this.loadOverview()]);
        if (this.view === "audit") await this.loadAudit();
      } catch (e) {
        if (String(e.message) !== "unauthorized") {
          ElementPlus.ElMessage.error("加载失败，请检查服务是否在运行");
        }
      } finally {
        this.loading = false;
      }
    },

    async loadParams() {
      const r = await this.api("/api/admin/params");
      this.params = r.params || [];
      this.groups = r.groups || [];
      this.meta = r.meta || {};
      const form = {};
      const orig = {};
      for (const p of this.params) {
        // 密钥不回显：表单值为空 = 未修改
        form[p.key] = p.kind === "secret" ? "" : p.value;
        orig[p.key] = p.kind === "secret" ? "" : p.value;
      }
      this.form = form;
      this.original = orig;
      this.errors = {};
    },

    async loadOverview() {
      this.overview = await this.api("/api/admin/overview");
    },

    async loadAudit() {
      const r = await this.api("/api/admin/audit?limit=60");
      this.auditItems = r.items || [];
    },

    isDirty(p) {
      if (p.kind === "secret") return !!(this.form[p.key] || "").trim();
      return JSON.stringify(this.form[p.key]) !== JSON.stringify(this.original[p.key]);
    },

    sourceLabel(s) {
      return { db: "后台覆盖", env: ".env", default: "默认值" }[s] || s;
    },

    async save() {
      if (!this.dirtyKeys.length) return;
      const values = {};
      for (const p of this.params) {
        if (!this.isDirty(p)) continue;
        values[p.key] = this.form[p.key];
      }
      this.saving = true;
      this.errors = {};
      try {
        const r = await this.api("/api/admin/params", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values, actor: "admin" }),
        });
        if (!r.ok) {
          for (const it of r.rejected || []) this.errors[it.key] = it.error;
          ElementPlus.ElMessage.error(r.message || "有参数未通过校验，本次未保存");
          return;
        }
        const n = (r.applied || []).length;
        const tips = [];
        if ((r.need_reindex || []).length) tips.push("需重建索引");
        if ((r.need_restart || []).length) tips.push("需重启进程");
        ElementPlus.ElMessage.success(`已保存 ${n} 项${tips.length ? "（" + tips.join("，") + "）" : "，立即生效"}`);
        await this.loadParams();
        await this.loadOverview();
      } catch (e) {
        if (String(e.message) !== "unauthorized") ElementPlus.ElMessage.error("保存失败，请检查服务");
      } finally {
        this.saving = false;
      }
    },

    discard() {
      const form = {};
      for (const p of this.params) form[p.key] = p.kind === "secret" ? "" : this.original[p.key];
      this.form = form;
      this.errors = {};
      ElementPlus.ElMessage.info("已放弃未保存的修改");
    },

    async resetOne(p) {
      await this.resetKeys([p.key], `已把「${p.label}」恢复为启动时的值`);
    },

    async resetGroup(groupKey) {
      const keys = this.params.filter((p) => p.group === groupKey).map((p) => p.key);
      await this.resetKeys(keys, "本组已恢复为启动时的值");
    },

    async resetAll() {
      try {
        await ElementPlus.ElMessageBox.confirm(
          "将清空所有后台覆盖，参数回到启动时 .env 的值（密钥不受影响）。此操作会记入审计。",
          "全部恢复默认", { type: "warning", confirmButtonText: "确认恢复", cancelButtonText: "取消" },
        );
      } catch (e) {
        return;
      }
      await this.resetKeys(null, "全部参数已恢复为启动时的值");
    },

    // 说明写在按钮上：这里的"默认"= 启动时的 .env 值，不是代码里的出厂值
    async resetKeys(keys, msg) {
      try {
        const r = await this.api("/api/admin/params/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keys: keys || [] }),
        });
        if (!r.ok) {
          ElementPlus.ElMessage.error("恢复默认失败");
          return;
        }
        ElementPlus.ElMessage.success(msg);
        await this.loadParams();
        await this.loadOverview();
      } catch (e) {
        if (String(e.message) !== "unauthorized") ElementPlus.ElMessage.error("请求失败");
      }
    },

    exportCfg() {
      fetch("/api/admin/export")
        .then((x) => x.json())
        .then((r) => {
          const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `sciagent-params-${new Date().toISOString().slice(0, 10)}.json`;
          a.click();
          URL.revokeObjectURL(a.href);
        })
        .catch(() => ElementPlus.ElMessage.error("导出失败"));
    },

    async doImport() {
      let obj;
      try {
        obj = JSON.parse(this.importText);
      } catch (e) {
        ElementPlus.ElMessage.error("不是合法的 JSON");
        return;
      }
      const values = obj.values || obj;
      this.importing = true;
      try {
        const r = await this.api("/api/admin/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values }),
        });
        if (!r.ok) {
          const first = (r.rejected || [])[0];
          ElementPlus.ElMessage.error(
            (r.message || "导入被拒绝") + (first ? `：${first.key} ${first.error}` : ""));
          return;
        }
        this.importVisible = false;
        this.importText = "";
        ElementPlus.ElMessage.success(`已导入 ${(r.applied || []).length} 项`);
        await this.loadParams();
        await this.loadOverview();
      } catch (e) {
        if (String(e.message) !== "unauthorized") ElementPlus.ElMessage.error("导入失败");
      } finally {
        this.importing = false;
      }
    },

    openTest() {
      this.test.visible = true;
      this.test.result = null;
      this.test.raw = "";
    },

    async runTest() {
      this.test.running = true;
      this.test.result = null;
      try {
        const r = await this.api("/api/admin/test-model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: this.test.prompt, max_tokens: this.test.maxTokens }),
        });
        this.test.result = r;
        if (r.settings) this.test.raw = JSON.stringify(r.settings, null, 2);
      } catch (e) {
        if (String(e.message) === "unauthorized") return;
        this.test.result = { ok: false, error: "请求失败，服务可能未启动" };
      } finally {
        this.test.running = false;
      }
    },
  },
});

app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
// 挂到 window：与主界面的 window.__sciagent 一致，便于自动化脚本读取真实状态
window.__admin = app.mount("#app");
