// 本机 · 插件 (docs/ui-spec.md §4.2; design §6.5): list, enable switch, detail drawer (概览/配置/日志), install dialog.
import { el, mount, icon, iconHTML, fetchJSON, toast, modal, drawer, confirmDialog, pill, relTime, emptyState, skeletonRows, errorState, copy, supervisorText, bytes } from "/ui/ui.js";
import { schemaForm } from "/ui/schema-form.js";

const ATTENTION = new Set(["needs_config", "crashed", "conflict", "invalid", "unhealthy", "update_failed"]);
const SOURCE_TEXT = { builtin: "内置", git: "git", path: "本地文件夹", link: "链接文件夹", archive: "压缩包", hub: "插件库", external: "外部", none: "" };
const RECENT_KEY = "anx-recent-plugin-paths";
const enc = encodeURIComponent;

function recentPaths() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter((p) => typeof p === "string").slice(0, 6); } catch { return []; }
}
function rememberPath(path) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([path, ...recentPaths().filter((p) => p !== path)].slice(0, 6))); } catch { /* ignore */ }
}

function avatar(plugin, size = "lg") {
  const accent = plugin.status === "running" ? "accent" : "";
  if (plugin.icon_url) return el("span", { class: `avatar ${size} ${accent}` }, el("img", { src: plugin.icon_url, alt: "" }));
  if (plugin.name === "device") return el("span", { class: `avatar ${size} accent` }, icon("monitor", { size: 18 }));
  return el("span", { class: `avatar ${size} ${accent}` }, String(plugin.title || plugin.name)[0].toUpperCase());
}

function head(text, actions) {
  return el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "插件"), el("p", {}, text)),
    el("div", { class: "actions" }, actions));
}

export async function pluginsPage(ctx, route) {
  const { page } = ctx;
  const state = { plugins: null, tab: route.query.get("tab") === "attention" ? "attention" : "all", busy: new Set(), error: null, drawer: null,
    repo: null };
  const listHolder = el("div", { class: "stack lg" });
  const rescan = el("button", { class: "btn", type: "button", onclick: async () => {
    try { await fetchJSON("/api/rescan", { method: "POST" }); await load(); toast(`已重新扫描，共 ${state.plugins.length} 个插件`); } catch { /* toasted */ }
  } }, icon("refresh-cw", { size: 16 }), el("span", { class: "hide-sm" }, "重新扫描"));
  const install = el("button", { class: "btn btn-primary", type: "button", onclick: () => openInstall() }, icon("plus", { size: 16 }), "安装插件");
  mount(page, head("在这台电脑上运行的 MCP 插件。启用后自动启动，并通过网关提供给你的所有 Agent。", [rescan, install]), listHolder);
  mount(listHolder, skeletonRows(4));

  async function load() {
    try {
      const data = await fetchJSON("/api/plugins", { quiet: true });
      state.plugins = data.plugins;
      state.error = null;
    } catch (error) {
      state.error = error;
    }
    render();
    if (state.drawer) state.drawer.update(state.plugins?.find((p) => p.name === state.drawer.name));
  }

  /** The gateway's 插件库 (design §6.12), when this device has joined one. */
  async function loadRepo() {
    try {
      state.repo = await fetchJSON("/api/plugin-repo", { quiet: true });
    } catch (error) {
      state.repo = { available: false, packages: [], error: error.message, code: error.data?.code || "error" };
    }
    if (state.plugins) render();
  }
  const repoJoined = () => Boolean(state.repo) && state.repo.code !== "not_joined";
  /** The newest package of ``name`` in the 插件库, or null. */
  const repoFor = (name) => (state.repo?.packages || []).find((p) => p.name === name && p.latest) || null;

  function render() {
    if (state.error && !state.plugins) { mount(listHolder, errorState(state.error, load)); return; }
    const plugins = state.plugins || [];
    const installed = plugins.filter((p) => p.installed);
    const notInstalled = plugins.filter((p) => !p.installed);
    const disabled = installed.filter((p) => p.status === "disabled");
    const attention = installed.filter((p) => ATTENTION.has(p.status));
    const running = installed.filter((p) => p.status === "running");
    const shown = state.tab === "running" ? running : state.tab === "attention" ? attention : plugins;
    const tab = (id, label, count) => el("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(state.tab === id),
      onclick: () => { state.tab = id; render(); } }, label, " ", el("span", { class: "count" }, String(count)));
    mount(listHolder,
      notInstalled.length ? el("div", { class: "banner accent" }, icon("package", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, `${notInstalled.length} 个插件还没有安装`),
        el("div", { class: "muted" }, "安装包只自带 device 插件。", notInstalled.map((p) => p.title || p.name).join("、"),
          notInstalled.some((p) => repoFor(p.name)) ? " 可以直接从网关的插件库安装：点击对应行的“安装”。"
            : " 需要从它们的文件夹、Git 仓库或压缩包安装：点击对应行的“安装”。")),
        el("div", { class: "banner-actions" }, el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => openInstall() }, "安装插件"))) : null,
      disabled.length ? el("div", { class: "banner" }, icon("power", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, `${disabled.length} 个插件尚未启用`),
        el("div", { class: "muted" }, "打开右侧开关即可启动。需要账号的插件会提示你先完成配置。")),
        el("div", { class: "banner-actions" }, el("button", { class: "btn btn-sm", type: "button", onclick: () => enableAll(disabled) }, "全部启用"))) : null,
      el("div", { class: "tabs", role: "tablist" }, tab("all", "全部", plugins.length), tab("running", "运行中", running.length), tab("attention", "需要处理", attention.length)),
      shown.length ? el("div", { class: "list" }, shown.map(row)) :
        el("section", { class: "card" }, el("div", { class: "card-body" }, plugins.length
          ? emptyState({ icon: state.tab === "attention" ? "circle-check" : "puzzle", title: state.tab === "attention" ? "没有需要处理的插件" : "没有运行中的插件", compact: true })
          : emptyState({ icon: "puzzle", title: "还没有插件", text: "插件是一个带 mcp-service.json 的文件夹。可以从文件夹、Git 仓库或压缩包安装。",
            action: { label: "安装插件", icon: "plus", onClick: () => openInstall() } }))));
  }

  function row(plugin) {
    const busy = state.busy.has(plugin.name);
    const tags = [plugin.version ? el("span", { class: "tag mono" }, plugin.version) : null,
      SOURCE_TEXT[plugin.source?.type] ? el("span", { class: "tag hide-sm" }, SOURCE_TEXT[plugin.source.type]) : null];
    const message = plugin.message && plugin.status !== "running" ? supervisorText(plugin.message) : "";
    const open = (tabName) => openDetail(plugin.name, tabName);
    const pkg = repoFor(plugin.name);
    if (!plugin.installed) {
      return el("div", { class: "list-item wrap-sm plugin-row not-installed", dataset: { name: plugin.name } },
        avatar(plugin),
        el("div", { class: "stack grow", style: { gap: "2px" } },
          el("div", { class: "row wrap" }, el("span", { class: "title" }, plugin.title || plugin.name), plugin.title !== plugin.name ? el("span", { class: "tag mono hide-sm" }, plugin.name) : null),
          el("span", { class: "sub truncate" }, pkg ? `网关插件库里有 ${pkg.version}，可以直接安装` : "配置里登记了这个插件，但这台电脑上还没有它的代码")),
        el("div", { class: "meta" }, pill("not_installed"),
          el("button", { class: "btn btn-ghost btn-sm hide-sm", type: "button", title: "从配置里移除这一项", onclick: () => forget(plugin) }, "移除"),
          el("button", { class: `btn btn-sm ${pkg ? "btn-primary" : ""}`, type: "button", title: pkg ? "从插件库安装" : "安装",
            onclick: () => openInstall({ name: plugin.name, mode: pkg ? "repo" : null }) }, icon(pkg ? "package" : "download", { size: 14 }), pkg ? "从插件库安装" : "安装")));
    }
    if (pkg?.update_available) tags.push(el("span", { class: "tag accent", title: `插件库里有 ${pkg.version}` }, `有新版本 ${pkg.version}`));
    const toggle = el("input", { type: "checkbox", role: "switch", checked: plugin.enabled, disabled: busy || plugin.kind === "observe" && false,
      "aria-label": `启用 ${plugin.name}`, onchange: (event) => setEnabled(plugin, event.currentTarget.checked, event.currentTarget) });
    return el("div", { class: "list-item wrap-sm plugin-row clickable", dataset: { name: plugin.name },
      tabindex: "0", "aria-label": `${plugin.title || plugin.name} 详情`,
      onclick: (event) => { if (!event.target.closest("button, a, label, input")) open("overview"); },
      onkeydown: (event) => { if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open("overview"); } } },
      avatar(plugin),
      el("div", { class: "stack grow", style: { gap: "2px" } },
        el("div", { class: "row wrap" }, el("span", { class: "title" }, plugin.title || plugin.name),
          plugin.title && plugin.title !== plugin.name ? el("span", { class: "faint xs mono hide-sm" }, plugin.name) : null, tags),
        el("span", { class: "sub truncate" }, plugin.description || ""),
        message ? el("span", { class: `msg truncate ${["crashed", "conflict", "invalid"].includes(plugin.status) ? "bad" : ""}` }, message) : null),
      el("div", { class: "meta" },
        busy ? el("span", { class: "spinner" }) : pill(plugin.status),
        pkg?.update_available ? el("button", { class: "btn btn-sm", type: "button", onclick: () => openInstall({ name: plugin.name, mode: "repo" }) }, icon("download", { size: 14 }), "更新") : null,
        plugin.status === "needs_config" ? el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => open("config") }, "配置") : null,
        plugin.status === "conflict" ? el("button", { class: "btn btn-sm", type: "button", onclick: () => open("config") }, "改端口") : null,
        el("button", { class: "btn btn-ghost btn-sm btn-icon hide-sm", type: "button", "aria-label": "日志", title: "日志", onclick: () => open("logs") }, icon("file-text", { size: 16 })),
        plugin.status !== "needs_config" ? el("button", { class: "btn btn-ghost btn-sm btn-icon hide-sm", type: "button", "aria-label": "配置", title: "配置", onclick: () => open("config") }, icon("sliders-horizontal", { size: 16 })) : null,
        el("label", { class: "switch", title: plugin.enabled ? "停用" : "启用" }, toggle, el("span"))));
  }

  async function setEnabled(plugin, on, box) {
    state.busy.add(plugin.name);
    render();
    try {
      const result = await fetchJSON(`/api/plugins/${enc(plugin.name)}/${on ? "enable" : "disable"}`, { method: "POST" });
      toast(supervisorText(result.message) || (on ? "已启用" : "已停用"), { type: result.plugin?.status === "needs_config" ? "warn" : "ok" });
      if (on && result.plugin?.status === "needs_config") openDetail(plugin.name, "config");
    } catch { if (box) box.checked = !on; }
    state.busy.delete(plugin.name);
    await load();
    ctx.refreshStatus();
  }

  async function enableAll(list) {
    try {
      const result = await fetchJSON("/api/plugins/enable", { method: "POST", body: { names: list.map((p) => p.name) } });
      const config = result.plugins.filter((p) => p.status === "needs_config").length;
      toast(`已启用 ${result.plugins.length} 个插件${config ? `，其中 ${config} 个需要先配置` : ""}`, { type: result.errors.length ? "warn" : "ok" });
    } catch { /* toasted */ }
    await load();
  }

  async function forget(plugin) {
    if (!await confirmDialog({ title: `从配置里移除 ${plugin.name}？`, message: "只删除 agent-nexus.json 里的这一项登记。以后仍然可以安装它。", confirmLabel: "移除" })) return;
    try { await fetchJSON(`/api/plugins/${enc(plugin.name)}`, { method: "DELETE" }); toast(`已移除 ${plugin.name}`); } catch { /* toasted */ }
    await load();
  }

  // ------------------------------------------------------------ detail drawer

  function openDetail(name, initialTab = "overview") {
    const plugin = state.plugins?.find((p) => p.name === name);
    if (!plugin) return;
    if (!plugin.installed) { openInstall({ name }); return; }
    state.drawer?.close();
    history.replaceState(null, "", `#/plugins/${enc(name)}`);
    let current = plugin, active = initialTab, stream = null;
    const tabs = el("div", { class: "tabs", role: "tablist" });
    const body = el("div");
    const footStart = el("div", { class: "row wrap" });
    const panel = drawer({ title: plugin.title || plugin.name, description: plugin.description, size: "lg", body: el("div", {}, tabs, body), footLeft: footStart,
      actions: [], onClose: () => { stream?.close(); state.drawer = null; history.replaceState(null, "", "#/plugins"); } });
    const lifecycle = async (action) => {
      try {
        const result = await fetchJSON(`/api/plugins/${enc(name)}/${action}`, { method: "POST" });
        toast(supervisorText(result.message) || "完成");
      } catch { /* toasted */ }
      await load();
    };
    function renderFoot() {
      const running = current.pid !== null && current.pid !== undefined;
      mount(footStart,
        running ? el("button", { class: "btn", type: "button", onclick: () => lifecycle("restart") }, icon("rotate-cw", { size: 16 }), "重启") : null,
        running ? el("button", { class: "btn", type: "button", onclick: () => lifecycle("stop") }, icon("square", { size: 16 }), "停止")
          : current.enabled && current.kind !== "observe" && current.status !== "needs_config" ? el("button", { class: "btn btn-primary", type: "button", onclick: () => lifecycle("start") }, icon("play", { size: 16 }), "启动") : null,
        current.removable ? el("button", { class: "btn btn-danger", type: "button", onclick: () => uninstall(current, panel) }, icon("trash", { size: 16 }), "卸载") : null);
    }
    function renderTabs() {
      const tab = (id, label) => el("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(active === id),
        onclick: () => { active = id; renderTabs(); renderBody(); } }, label);
      mount(tabs, tab("overview", "概览"), tab("config", "配置"), tab("logs", "日志"));
    }
    function renderBody() {
      stream?.close();
      stream = null;
      if (active === "overview") mount(body, overviewTab(current));
      else if (active === "config") configTab(current, body, async () => { await load(); });
      else stream = logsTab(current, body);
    }
    renderTabs();
    renderBody();
    renderFoot();
    state.drawer = {
      name, close: panel.close,
      update(next) {
        if (!next || !panel.root.isConnected) return;
        current = next;
        renderFoot();
        if (active === "overview") mount(body, overviewTab(current));
      },
    };
  }

  async function uninstall(plugin, panel) {
    const purge = el("input", { type: "checkbox" });
    let confirmed = false;
    const dialog = modal({ title: `卸载 ${plugin.title || plugin.name}？`, size: "sm",
      body: el("div", { class: "stack" },
        el("p", { class: "muted" }, "会停止插件并删除它的代码。插件数据（plugin-data）、凭据（secrets）和设置默认保留，重新安装后自动恢复。"),
        el("label", { class: "check" }, purge, " 同时删除数据和凭据（不可恢复）")),
      actions: [{ label: "取消" }, { label: "卸载", variant: "danger solid", onClick: () => { confirmed = true; } }] });
    await dialog.closed;
    if (!confirmed) return;
    try {
      const result = await fetchJSON(`/api/plugins/${enc(plugin.name)}${purge.checked ? "?purge=1" : ""}`, { method: "DELETE" });
      toast(result.data_kept ? `已卸载 ${plugin.name}，数据已保留` : `已卸载 ${plugin.name}，数据已删除`);
      panel.close();
    } catch { /* toasted */ }
    await load();
  }

  function overviewTab(p) {
    const health = p.health && p.health.code !== undefined ? `${p.health.code ?? "—"} · ${p.health.ms ?? "—"} ms` : "—";
    const kv = (label, value, mono = false) => [el("dt", {}, label), el("dd", { class: mono ? "mono" : "" }, value ?? "—")];
    const src = p.source || {};
    return el("div", { class: "stack lg" },
      p.message && p.status !== "running" ? el("div", { class: `banner ${ATTENTION.has(p.status) ? "warn" : ""}` }, icon("info", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, pill(p.status)), el("div", { class: "muted" }, supervisorText(p.message)))) : null,
      el("dl", { class: "kv" },
        kv("状态", pill(p.status)),
        kv("版本", p.version || "—", true),
        kv("端口", p.port ? String(p.port) : "—", true),
        kv("PID", p.pid ? String(p.pid) : "—", true),
        kv("运行时长", p.uptime_s ? relTime(Date.now() / 1000 - p.uptime_s).replace("前", "") : "—"),
        kv("健康检查", p.health_path ? `${p.health_path} · ${health}` : "—", true),
        kv("来源", [SOURCE_TEXT[src.type] || src.type || "—", src.url || src.path ? el("div", { class: "faint xs mono" }, src.url || src.path) : null,
          src.ref ? el("div", { class: "faint xs mono" }, `${src.ref}${src.commit ? ` @ ${src.commit.slice(0, 12)}` : ""}`) : null]),
        kv("安装时间", p.installed_at ? relTime(p.installed_at) : "—"),
        kv("目录", p.dir, true),
        kv("命令", (p.command || []).join(" "), true),
        kv("环境变量", (p.env_keys || []).length ? p.env_keys.join(", ") : "—", true)));
  }

  function configTab(p, holder, reload) {
    mount(holder, skeletonRows(3));
    fetchJSON(`/api/plugins/${enc(p.name)}/config`, { quiet: true }).then((config) => {
      const hasSchema = Object.keys(config.schema?.properties || {}).length || config.secrets.length;
      const form = hasSchema ? schemaForm({ schema: config.schema, ui: config.ui, values: config.values, secrets: config.secrets }) : null;
      const raw = !hasSchema ? el("textarea", { class: "textarea mono", rows: 8, spellcheck: "false" }) : null;
      if (raw) raw.value = JSON.stringify(config.settings || {}, null, 2);
      const port = el("input", { class: "input mono", type: "number", min: 1, max: 65535, value: config.runtime.port ?? "", placeholder: "清单里的端口" });
      const autostart = el("input", { type: "checkbox", role: "switch", checked: config.runtime.autostart });
      const autorestart = el("input", { type: "checkbox", role: "switch", checked: config.runtime.autorestart });
      const error = el("span", { class: "error-text", role: "alert", hidden: true });
      const save = el("button", { class: "btn btn-primary", type: "submit" }, "保存");
      const formNode = el("form", { class: "stack xl", onsubmit: async (event) => {
        event.preventDefault();
        error.hidden = true;
        const body = { runtime: { port: port.value ? Number(port.value) : null, autostart: autostart.checked, autorestart: autorestart.checked } };
        if (form) {
          const collected = form.collect();
          form.setErrors(collected.errors);
          if (collected.errors.length) return;
          body.values = collected.values;
          body.secrets = collected.secrets;
        } else {
          try { body.settings = JSON.parse(raw.value || "{}"); } catch { error.textContent = "不是有效的 JSON"; error.hidden = false; return; }
        }
        save.disabled = true;
        mount(save, el("span", { class: "spinner" }), "保存中…");
        try {
          const result = await fetchJSON(`/api/plugins/${enc(p.name)}/config`, { method: "PUT", body, quiet: true });
          toast(result.message || "已保存", { type: result.plugin?.status === "needs_config" ? "warn" : "ok" });
          await reload();
          configTab(result.plugin || p, holder, reload);
        } catch (err) {
          if (form && err.data?.problems) form.setErrors(err.data.problems);
          error.textContent = err.message;
          error.hidden = false;
        } finally {
          save.disabled = false;
          mount(save, "保存");
        }
      } },
        config.missing.length ? el("div", { class: "banner warn" }, icon("triangle-alert", { size: 18 }), el("div", { class: "banner-body" },
          el("div", { class: "banner-title" }, "还缺少必填项"), el("div", { class: "muted" }, `填写 ${(config.missing_titles || config.missing).join("、")} 后插件会自动启动。`))) : null,
        config.hinted ? el("p", { class: "faint xs" }, "这个插件的清单是旧格式（v1），表单由 Agent Nexus 内置的说明生成。") : null,
        form ? form.root : el("div", { class: "field" }, el("label", {}, "settings（JSON）"), raw,
          el("span", { class: "hint" }, "这个插件没有声明配置表单；这里编辑的是 agent-nexus.json 里它的 settings。")),
        el("div", { class: "form" }, el("div", { class: "strong small muted" }, "运行"),
          el("div", { class: "field" }, el("label", {}, "端口"), port, el("span", { class: "hint" }, "端口被占用（端口冲突）时换一个空闲的端口。")),
          el("label", { class: "switch" }, autostart, el("span"), "随 Agent Nexus 自动启动"),
          el("label", { class: "switch" }, autorestart, el("span"), "崩溃后自动重启")),
        error,
        el("div", { class: "row" }, save));
      mount(holder, formNode);
    }).catch((err) => mount(holder, errorState(err, () => configTab(p, holder, reload))));
  }

  function logsTab(p, holder) {
    const sources = p.log_sources?.length ? p.log_sources : [{ id: "stdout", label: "stdout / stderr" }];
    const select = el("select", { class: "select", "aria-label": "日志来源", style: { maxWidth: "260px" } },
      sources.map((s) => el("option", { value: s.id }, s.label)));
    const pre = el("pre", { class: "log", "data-empty": "还没有日志输出。插件启动后，它写到 stdout / stderr 的内容会显示在这里。", style: { maxHeight: "60vh", minHeight: "240px" } });
    const pathText = el("span", { class: "faint xs mono truncate" });
    let es = null, lines = [];
    const MAX = 3000;
    const draw = () => { pre.textContent = lines.join("\n"); pre.scrollTop = pre.scrollHeight; };
    function connect() {
      es?.close();
      lines = [];
      draw();
      es = new EventSource(`/api/plugins/${enc(p.name)}/logs/stream?source=${enc(select.value)}&lines=400`);
      es.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.reset) { lines = []; pathText.textContent = data.path || ""; }
        lines.push(...String(data.text || "").split("\n").filter((line, i, arr) => line || i < arr.length - 1));
        if (lines.length > MAX) lines = lines.slice(-MAX);
        draw();
      };
    }
    select.addEventListener("change", connect);
    mount(holder, el("div", { class: "stack" },
      el("div", { class: "row wrap between" }, select,
        el("div", { class: "row" },
          el("button", { class: "btn btn-sm", type: "button", onclick: () => copy(lines.join("\n")) }, icon("copy", { size: 14 }), "复制"),
          el("button", { class: "btn btn-sm", type: "button", onclick: () => copy(pathText.textContent, { label: "已复制日志路径" }) }, icon("folder-open", { size: 14 }), "复制路径"))),
      pre, pathText));
    connect();
    return { close: () => es?.close() };
  }

  // ------------------------------------------------------------ install dialog

  async function openInstall({ name = "", mode: wanted = null } = {}) {
    const hasRepo = repoJoined();
    let mode = wanted === "repo" && hasRepo ? "repo" : hasRepo && (!name || repoFor(name)) ? "repo" : "path";
    let inspected = null, upload = null, inspectTimer = null, job = null, generation = 0;
    const seg = (value, glyph, label) => el("button", { type: "button", "aria-pressed": String(mode === value), dataset: { value },
      onclick: () => { mode = value; for (const b of segmented.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.value === mode)); showPane(); scheduleInspect(0); } },
      icon(glyph, { size: 14 }), label);
    const segmented = el("div", { class: "segmented block source-tabs", role: "group", "aria-label": "来源" },
      hasRepo ? seg("repo", "package", "插件库") : null,
      seg("path", "folder-open", "本地文件夹"), seg("git", "git-branch", "Git 地址"), seg("archive", "file-archive", "压缩包"));
    // 插件库: the gateway's packages, newest version of each plugin first (design §6.12).
    const repoList = el("div", { class: "choices repo-choices", role: "radiogroup", "aria-label": "插件库" });
    const repoNote = el("div");
    function renderRepoPane() {
      const repo = state.repo || { packages: [] };
      const latest = (repo.packages || []).filter((p) => p.latest);
      if (!repo.available) {
        mount(repoList);
        mount(repoNote, el("div", { class: "banner warn" }, icon("cloud-off", { size: 18 }), el("div", { class: "banner-body" },
          el("div", { class: "banner-title" }, "暂时看不到插件库"), el("div", { class: "muted" }, repo.error || "没有连上网关"))));
        return;
      }
      mount(repoNote, latest.length ? el("p", { class: "faint xs" }, "插件包保存在网关上；这台电脑用自己的凭据下载，校验 sha256 后安装。") : null);
      if (!latest.length) {
        mount(repoList, emptyState({ icon: "package", title: "插件库还是空的", compact: true,
          text: "在网关控制台的“插件”页上传插件包，或在一台电脑上运行 anx plugin publish <插件文件夹>。" }));
        return;
      }
      const preferred = latest.find((p) => p.name === name) || latest.find((p) => !p.installed_version || p.update_available) || latest[0];
      mount(repoList, latest.map((pkg) => {
        const input = el("input", { type: "radio", name: "repo-package", value: pkg.name, checked: pkg === preferred, onchange: () => scheduleInspect(0) });
        const status = pkg.update_available ? el("span", { class: "tag accent" }, `有新版本（已装 ${pkg.installed_version}）`)
          : pkg.installed_version ? el("span", { class: "tag" }, pkg.installed_version === pkg.version ? "已安装" : `已装 ${pkg.installed_version}`)
            : el("span", { class: "tag" }, "未安装");
        return el("label", { class: "choice", dataset: { package: pkg.name } }, input, icon("package", { size: 16 }),
          el("div", { class: "stack grow", style: { gap: "2px", minWidth: 0 } },
            el("div", { class: "row wrap", style: { gap: "6px" } }, el("span", { class: "choice-title" }, pkg.title || pkg.name),
              el("span", { class: "tag mono" }, pkg.version), status),
            el("span", { class: "choice-sub truncate" }, pkg.description || pkg.name),
            el("span", { class: "choice-sub" }, `${bytes(pkg.size)} · 上传于 ${relTime(pkg.uploaded_at)}`)));
      }));
    }
    const selectedPackage = () => {
      const value = repoList.querySelector("input:checked")?.value;
      return (state.repo?.packages || []).find((p) => p.name === value && p.latest) || null;
    };
    const pathInput = el("input", { class: "input mono", id: "p-path", spellcheck: "false", autocomplete: "off",
      placeholder: "例如 D:/work/ones-mcp（文件夹里要有 mcp-service.json）", oninput: () => scheduleInspect() });
    const link = el("input", { type: "checkbox", name: "link" });
    const recents = el("div", { class: "recent-paths" });
    const gitUrl = el("input", { class: "input mono", id: "p-git", spellcheck: "false", placeholder: "https://github.com/owner/anx-plugin-x.git", oninput: () => scheduleInspect() });
    const gitRef = el("input", { class: "input mono", id: "p-ref", spellcheck: "false", placeholder: "默认：最新的 vX.Y.Z 标签", oninput: () => scheduleInspect() });
    const fileInput = el("input", { type: "file", accept: ".zip,.tar.gz,.tgz", id: "p-zip", onchange: () => pickFile(fileInput.files[0]) });
    const fileLabel = el("span", {}, "选择或拖入 .zip / .tar.gz");
    const drop = el("label", { class: "drop-zone", for: "p-zip" }, icon("file-archive", { size: 22 }), fileLabel, el("span", { class: "faint xs" }, "最大 200 MB"), fileInput);
    drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (event) => { event.preventDefault(); drop.classList.remove("over"); pickFile(event.dataTransfer.files[0]); });
    const enable = el("input", { type: "checkbox", role: "switch", checked: true });
    const port = el("input", { class: "input mono", type: "number", min: 1, max: 65535, placeholder: "使用清单里的端口" });
    const preview = el("div");
    const logBox = el("pre", { class: "log install-log", hidden: true });
    const panes = {
      repo: el("div", { class: "form", hidden: true }, repoNote, repoList),
      path: el("div", { class: "form" },
        el("div", { class: "field" }, el("label", { for: "p-path" }, "插件文件夹"), pathInput,
          el("span", { class: "hint" }, "把文件夹复制到 Agent Nexus 的插件目录并安装依赖。可以在资源管理器里按住 Shift 右键文件夹 →“复制文件地址”，再粘贴到这里。")),
        recents,
        el("label", { class: "check" }, link, " 链接而不是复制（直接使用原文件夹，适合开发中的插件）")),
      git: el("div", { class: "form", hidden: true },
        el("div", { class: "field" }, el("label", { for: "p-git" }, "仓库地址"), gitUrl,
          el("span", { class: "hint" }, "私有仓库用这台电脑上 git 已保存的凭据，Agent Nexus 不保存任何 token。")),
        el("div", { class: "field" }, el("label", { for: "p-ref" }, "分支 / 标签 ", el("span", { class: "optional" }, "可选")), gitRef)),
      archive: el("div", { class: "form", hidden: true }, drop),
    };
    function showPane() { for (const [key, node] of Object.entries(panes)) node.hidden = key !== mode; }
    async function renderRecents() {
      let folders = [];
      try { folders = (await fetchJSON("/api/plugins/suggestions", { quiet: true })).folders || []; } catch { /* optional */ }
      const installedNames = new Set((state.plugins || []).filter((p) => p.installed).map((p) => p.name));
      const suggested = folders.filter((f) => !installedNames.has(f.name));
      const recent = recentPaths().filter((path) => !suggested.some((f) => f.path === path));
      if (name) {
        const match = suggested.find((f) => f.name === name);
        if (match && !pathInput.value) { pathInput.value = match.path; scheduleInspect(0); }
      }
      const chip = (path, label) => el("button", { class: "btn btn-sm", type: "button", title: path, onclick: () => { pathInput.value = path; scheduleInspect(0); } },
        icon("folder-open", { size: 14 }), label);
      mount(recents, suggested.length || recent.length ? [
        el("span", { class: "faint xs", style: { width: "100%" } }, suggested.length ? "在旧版管理器里找到的插件文件夹：" : "最近使用："),
        suggested.map((f) => chip(f.path, f.name)), recent.map((path) => chip(path, path.split(/[\\/]/).filter(Boolean).pop() || path))] : null);
    }
    async function pickFile(file) {
      if (!file) return;
      fileLabel.textContent = `${file.name} · 上传中…`;
      upload = null;
      try {
        const response = await fetch(`/api/plugins/uploads?filename=${enc(file.name)}`, { method: "POST", body: file, headers: { "X-ANX": "1", "Content-Type": "application/octet-stream" } });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        upload = data.upload_id;
        fileLabel.textContent = file.name;
        scheduleInspect(0);
      } catch (err) {
        fileLabel.textContent = "选择或拖入 .zip / .tar.gz";
        showPreview({ error: `上传失败：${err.message}` });
      }
    }
    function sourceBody() {
      if (mode === "repo") { const pkg = selectedPackage(); return pkg ? { source: "repo", name: pkg.name, version: pkg.version, pkg } : null; }
      if (mode === "path") return pathInput.value.trim() ? { source: "path", path: pathInput.value.trim().replace(/^"|"$/g, ""), link: link.checked } : null;
      if (mode === "git") return gitUrl.value.trim() ? { source: "git", url: gitUrl.value.trim(), ref: gitRef.value.trim() } : null;
      return upload ? { source: "archive", upload_id: upload } : null;
    }
    function scheduleInspect(delay = 600) {
      clearTimeout(inspectTimer);
      inspected = null;
      const body = sourceBody();
      if (!body) { mount(preview); return; }
      if (body.source === "repo") {
        const pkg = body.pkg;
        mount(preview, el("div", { class: "banner ok" }, icon("circle-check", { size: 18 }), el("div", { class: "banner-body" },
          el("div", { class: "banner-title" }, `将从插件库安装 ${pkg.title || pkg.name} ${pkg.version}`),
          el("div", { class: "muted" }, `${bytes(pkg.size)} · ${pkg.files} 个文件 · sha256 ${String(pkg.sha256).slice(0, 12)}…`),
          pkg.installed_version ? el("div", { class: "small" }, `已安装 ${pkg.installed_version}，将${pkg.installed_version === pkg.version ? "重新安装" : "更新到"} ${pkg.version}（失败会自动回滚）`) : null)));
        return;
      }
      // Cloning a repository just to preview it is slow; git sources are checked when installing.
      if (body.source === "git") { showPreview({ hint: "安装时会克隆仓库并检查 mcp-service.json。" }); return; }
      inspectTimer = setTimeout(async () => {
        const mine = ++generation;
        mount(preview, el("div", { class: "row faint small" }, el("span", { class: "spinner" }), "正在读取插件信息…"));
        try {
          const info = await fetchJSON("/api/plugins/inspect", { method: "POST", body, quiet: true });
          if (mine !== generation) return;
          inspected = info;
          showPreview({ info });
        } catch (err) {
          if (mine === generation) showPreview({ error: err.message });
        }
      }, delay);
    }
    function showPreview({ info = null, error = null, hint = null }) {
      if (error) { mount(preview, el("div", { class: "banner bad" }, icon("circle-alert", { size: 18 }), el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, "无法安装"), el("div", { class: "muted" }, error)))); return; }
      if (hint) { mount(preview, el("p", { class: "faint small" }, hint)); return; }
      const facts = [info.transport, info.port ? `端口 ${info.port}` : null, ...info.requirements].filter(Boolean).join(" · ");
      mount(preview, el("div", { class: `banner ${info.warnings.length || info.builtin ? "warn" : "ok"}` }, icon(info.builtin ? "triangle-alert" : "circle-check", { size: 18 }),
        el("div", { class: "banner-body" },
          el("div", { class: "banner-title" }, `找到插件 ${info.title}${info.title !== info.name ? `（${info.name}）` : ""} ${info.version}`),
          el("div", { class: "muted" }, facts),
          info.exists ? el("div", { class: "small" }, `已安装 ${info.installed_version}，将${info.installed_version === info.version ? "重新安装" : "更新到"} ${info.version}（失败会自动回滚）`) : null,
          info.needs_config ? el("div", { class: "small" }, `安装后需要填写：${info.config_fields.join("、")}`) : null,
          info.install_steps.length ? el("div", { class: "faint xs mono" }, info.install_steps.join("  ·  ")) : null,
          info.warnings.map((w) => el("div", { class: "small" }, w)))));
    }
    const dialog = modal({
      title: "安装插件", description: hasRepo ? "从网关的插件库，或本地文件夹、Git 仓库、压缩包安装 MCP 插件。"
        : "从本地文件夹、Git 仓库或压缩包安装 MCP 插件（插件根目录需要 mcp-service.json）。",
      body: el("div", { class: "form" }, segmented, panes.repo, panes.path, panes.git, panes.archive, preview,
        el("details", { class: "advanced" }, el("summary", {}, "高级"), el("div", { class: "form", style: { marginTop: "12px" } },
          el("div", { class: "field" }, el("label", {}, "端口 ", el("span", { class: "optional" }, "可选")), port,
            el("span", { class: "hint" }, "默认端口被别的程序（例如旧版管理器）占用时，在这里换一个。")))),
        el("label", { class: "switch" }, enable, el("span"), "安装后立即启用"), logBox),
      actions: [{ label: "关闭" }, { label: "安装", variant: "primary", icon: "download", onClick: submit }],
      onClose: () => { clearTimeout(inspectTimer); },
    });
    state.install = dialog;
    if (hasRepo) renderRepoPane();
    showPane();
    renderRecents();
    if (mode === "repo") scheduleInspect(0);

    async function submit({ root }) {
      if (job) return false;
      const body = sourceBody();
      if (!body) { showPreview({ error: mode === "repo" ? "先在插件库里选一个插件" : mode === "path" ? "请填写插件文件夹" : mode === "git" ? "请填写仓库地址" : "请先选择压缩包" }); return false; }
      if (inspected?.builtin) { showPreview({ error: `${inspected.name} 是内置插件，不能另外安装` }); return false; }
      body.enable = enable.checked;
      if (port.value) body.port = Number(port.value);
      let started;
      const request = body.source === "repo"
        ? fetchJSON("/api/plugin-repo/install", { method: "POST", body: { name: body.name, version: body.version, enable: body.enable }, quiet: true })
        : fetchJSON("/api/plugins/install", { method: "POST", body, quiet: true });
      try { started = await request; }
      catch (err) { showPreview({ error: err.message }); return false; }
      if (body.source === "path") rememberPath(body.path);
      job = started.job_id;
      logBox.hidden = false;
      logBox.textContent = "";
      for (const node of root.querySelectorAll(".modal-body input, .modal-body button, .modal-body select")) node.disabled = true;
      let since = 0, result;
      while (true) {
        let data;
        try { data = await fetchJSON(`/api/plugins/jobs/${enc(job)}?since=${since}`, { quiet: true }); }
        catch { await new Promise((r) => setTimeout(r, 1000)); continue; }
        if (data.log.length) { logBox.textContent += data.log.join("\n") + "\n"; logBox.scrollTop = logBox.scrollHeight; }
        since = data.log_total;
        if (data.state !== "running") { result = data; break; }
        await new Promise((r) => setTimeout(r, 700));
      }
      job = null;
      for (const node of root.querySelectorAll(".modal-body input, .modal-body button, .modal-body select")) node.disabled = false;
      await load();
      if (hasRepo) { await loadRepo(); renderRepoPane(); }
      ctx.refreshStatus();
      if (result.state === "failed") {
        showPreview({ error: result.error.split("\n")[0] });
        toast("安装失败，已回滚", { type: "bad", title: "安装失败" });
        return false;
      }
      const r = result.result;
      const text = { running: `${r.name} ${r.version} 已安装并在运行`, needs_config: `${r.name} ${r.version} 已安装，还需要配置`,
        conflict: `${r.name} 已安装，但端口被占用`, disabled: `${r.name} ${r.version} 已安装（未启用）` }[r.state] || `${r.name} 已安装`;
      toast(text, { type: r.state === "running" || r.state === "disabled" ? "ok" : "warn" });
      if (r.state === "needs_config" || r.state === "conflict") setTimeout(() => openDetail(r.name, "config"), 50);
      return true;
    }
    return dialog;
  }

  await Promise.all([load(), loadRepo()]);
  if (route.params.name) openDetail(route.params.name, route.query.get("tab") || "overview");
  if (route.query.get("open") === "install") openInstall({ mode: route.query.get("source") === "repo" ? "repo" : null });
  ctx.every(5000, () => { if (!document.querySelector(".modal")) load(); });
  ctx.every(30000, () => { if (!document.querySelector(".modal")) loadRepo(); });
  ctx.onLeave(() => { state.drawer?.close(); state.install?.close(); });
}
