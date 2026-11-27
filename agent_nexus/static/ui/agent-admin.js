// Agent admin pieces (docs/agent-chat.md §2, §8, §11) plus the helpers chat.js shares with them.
//   import { mountTmpManager, mountCliManager } from "/ui/agent-admin.js";
//
// `api` is the same adapter chat.js takes: {call(op, params, device) -> Promise<result>}. The third
// argument is the runner device id (null / omitted = the adapter's default, e.g. 本机). Handlers
// answer {"error": "…", "code": "…"} instead of failing; callOp() turns that into a thrown Error.

import { el, icon, mount, toast, bytes, relTime, emptyState, skeletonRows, errorState, confirmDialog, copy } from "./ui.js";
import { mountApiAdmin } from "./api-admin.js";

/** Extra tabs of the 「Agent」 page (agent-wire.js agentPage): {id, label, mount(container, api, {device, local})}. */
export const AGENT_PAGE_TABS = [{ id: "api", label: "API", mount: (container, api, options) => mountApiAdmin(container, api, options) }];

// ---------------------------------------------------------------- icons not in the shared set (Lucide, ISC)

const EXTRA_ICONS = {
  pin: '<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
  archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
  "grip-vertical": '<circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/>',
  "arrow-up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  "arrow-down": '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  "square-pen": '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>',
  "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  "corner-left-up": '<path d="M14 9 9 4 4 9"/><path d="M20 20h-7a4 4 0 0 1-4-4V4"/>',
  lightbulb: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5 .7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
  "file-diff": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M9 10h6"/><path d="M12 13V7"/><path d="M9 17h6"/>',
  "shield-alert": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
};

/** icon() from ui.js, plus the few extra glyphs the agent pages need. */
export function xicon(name, { size = 16, cls = "" } = {}) {
  if (!EXTRA_ICONS[name]) return icon(name, { size, cls });
  const tpl = document.createElement("template");
  tpl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false" aria-hidden="true">${EXTRA_ICONS[name]}</svg>`;
  return tpl.content.firstElementChild;
}

// ---------------------------------------------------------------- CLI kinds and status

export const KINDS = {
  claude: { label: "Claude Code", short: "Claude", mark: "C", login: "claude auth login" },
  codex: { label: "Codex", short: "Codex", mark: "Cx", login: "codex login" },
  dsh: { label: "DeepSeek Harness", short: "DeepSeek", mark: "DS", login: "dsh" },
  gemini: { label: "Gemini CLI", short: "Gemini", mark: "G", login: "gemini" },
};
export const KIND_ORDER = ["claude", "codex", "dsh", "gemini"];

export function kindLabel(kind, info) { return info?.label || KINDS[kind]?.label || kind || "Agent"; }
export function kindShort(kind, info) { return KINDS[kind]?.short || info?.label || kind || "Agent"; }
export function kindMark(kind, { size = "sm" } = {}) {
  const text = KINDS[kind]?.mark || String(kind || "?").slice(0, 2).toUpperCase();
  return el("span", { class: `aa-mark ${size}`, dataset: { kind: kind || "" }, "aria-hidden": "true" }, text);
}
export function loginCommand(kind, info) {
  const command = info?.login_command;
  if (Array.isArray(command) && command.length) return command.join(" ");
  return (typeof command === "string" && command) || KINDS[kind]?.login || kind;
}

/** Call one op; a handler's {"error"} answer becomes a thrown Error (message = the Chinese text). */
export async function callOp(api, op, params = {}, device = null) {
  const result = await api.call(op, params, device);
  if (result && typeof result === "object" && !Array.isArray(result) && result.error) {
    const error = new Error(result.error);
    error.code = result.code;
    error.data = result;
    throw error;
  }
  return result ?? {};
}

/** agent_info answers {agents: [...]}, a bare list, or {kind: info}; always return a list in KIND_ORDER. */
export function normalizeAgents(result) {
  let list = [];
  if (Array.isArray(result)) list = result;
  else if (Array.isArray(result?.agents)) list = result.agents;
  else if (result && typeof result === "object") {
    list = Object.entries(result).filter(([, value]) => value && typeof value === "object" && !Array.isArray(value))
      .map(([kind, value]) => ({ kind, ...value }));
  }
  list = list.filter((item) => item && item.kind);
  const rank = (kind) => { const index = KIND_ORDER.indexOf(kind); return index < 0 ? 99 : index; };
  return list.sort((a, b) => rank(a.kind) - rank(b.kind));
}

export function percent(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round(n < 1 ? n * 100 : n)));
}

export function shortVersion(version) {
  const match = String(version || "").match(/\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?/);
  return match ? match[0] : String(version || "");
}

const JOB_ACTIVE = new Set(["queued", "running", "starting"]);
export function jobActive(job) { return !!job && JOB_ACTIVE.has(job.state); }

/** {state: ready|login|missing|installing|unknown, label, tone} for one agent_info entry. */
export function agentStatus(info) {
  if (!info) return { state: "unknown", label: "未检测", tone: "neutral" };
  if (jobActive(info.install)) return { state: "installing", label: `安装中 ${percent(info.install.progress)}%`, tone: "info", progress: percent(info.install.progress) };
  if (!info.installed) return { state: "missing", label: "未安装", tone: "neutral" };
  if (info.login === "missing") return { state: "login", label: "未登录", tone: "warn" };
  const version = shortVersion(info.version);
  return { state: "ready", label: version ? `就绪 v${version}` : "就绪", tone: "ok", unsure: info.login === "unknown" };
}

/** Why the composer cannot send with this CLI ("" = it can). */
export function notReadyReason(info) {
  const status = agentStatus(info);
  const name = kindLabel(info?.kind, info);
  if (status.state === "missing") return `${name} 还没有安装，先在 Agent 菜单里安装`;
  if (status.state === "installing") return `${name} 正在安装（${status.progress}%），装好后就能发送`;
  if (status.state === "login") return `${name} 还没有登录，先在终端里登录`;
  if (status.state === "unknown") return "正在检测这台设备上的 CLI…";
  return "";
}

/**
 * Follow an install job: polls agent_install_job {job_id, since} about once a second until it ends.
 * onUpdate({state, progress, lines, error}) gets the accumulated log lines. Returns {stop()}.
 */
export function watchInstall(api, device, jobId, onUpdate) {
  let stopped = false, since = 0, timer = null;
  const lines = [];
  const tick = async () => {
    if (stopped) return;
    let job;
    try {
      job = await callOp(api, "agent_install_job", { job_id: jobId, since }, device);
    } catch (error) {
      onUpdate({ state: "failed", progress: 0, lines, error: error.message });
      return;
    }
    if (stopped) return;
    const fresh = Array.isArray(job.lines) ? job.lines : Array.isArray(job.log) ? job.log : null;
    if (fresh) lines.push(...fresh.map(String));
    else if (typeof job.log_tail === "string") { lines.length = 0; lines.push(...job.log_tail.split("\n")); }
    if (Number.isFinite(job.next)) since = job.next; else if (fresh) since += fresh.length;
    onUpdate({ state: job.state || "running", progress: percent(job.progress), lines, error: job.error || "", stage: job.stage || "" });
    if (JOB_ACTIVE.has(job.state || "running")) timer = setTimeout(tick, 1000);
  };
  tick();
  return { stop() { stopped = true; clearTimeout(timer); } };
}

/** A thin progress bar; set(percent) updates it. */
export function progressBar(value = 0, { label = "安装进度" } = {}) {
  const fill = el("span", { class: "aa-progress-fill" });
  const bar = el("span", { class: "aa-progress", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
  bar.set = (n) => { const p = percent(n); fill.style.width = `${p}%`; bar.setAttribute("aria-valuenow", String(p)); };
  bar.set(value);
  return bar;
}

function shortPath(path) {
  return String(path || "").replace(/^\/(?:home|Users)\/[^/]+/, "~").replace(/^[A-Za-z]:\\Users\\[^\\]+/, "~");
}
export { shortPath };

// ---------------------------------------------------------------- temp dirs (§8)

/**
 * mountTmpManager(container, api, {device}) — every run's $tmpdir with its size; clean one, or everything
 * older than 7 days. Active runs cannot be cleaned (their row says why).
 */
export function mountTmpManager(container, api, { device = null } = {}) {
  let alive = true;
  const root = el("div", { class: "aa-tmp" });
  mount(container, root);

  async function load() {
    mount(root, skeletonRows(3));
    let items, total;
    try {
      const result = await callOp(api, "agent_tmp_list", {}, device);
      items = Array.isArray(result) ? result : result.items || result.dirs || [];
      total = Number.isFinite(result.total) ? result.total : items.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
    } catch (error) {
      if (alive) mount(root, errorState(error, load));
      return;
    }
    if (!alive) return;
    render(items, total);
  }

  async function clean(params, button, what) {
    button.disabled = true;
    try {
      const result = await callOp(api, "agent_tmp_clean", params, device);
      const freed = Number(result.freed ?? result.bytes ?? 0);
      const count = Number(result.removed ?? result.count ?? 0);
      toast(count ? `已清理 ${count} 个临时目录，释放 ${bytes(freed)}` : `${what}：没有可清理的目录`, { type: count ? "ok" : "info" });
    } catch (error) {
      toast(error.message, { type: "bad", title: "清理失败" });
    }
    if (alive) load();
  }

  function render(items, total) {
    const old = items.filter((item) => !item.active && item.updated_at && Date.now() / 1000 - toSeconds(item.updated_at) > 7 * 86400);
    const cleanOld = el("button", { class: "btn btn-sm", type: "button", disabled: !old.length,
      title: old.length ? "" : "没有 7 天前的临时目录",
      onclick: (event) => clean({ older_than_days: 7 }, event.currentTarget, "清理 7 天前") }, xicon("trash", { size: 14 }), "清理 7 天前");
    const head = el("div", { class: "aa-tmp-head" },
      el("div", { class: "aa-tmp-total" },
        el("span", { class: "aa-tmp-total-value" }, bytes(total)),
        el("span", { class: "faint small" }, `${items.length} 个临时目录`, old.length ? ` · ${old.length} 个超过 7 天` : "")),
      el("div", { class: "row" },
        el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "刷新", title: "刷新", onclick: load }, icon("refresh-cw", { size: 14 })),
        cleanOld));
    if (!items.length) {
      mount(root, head, emptyState({ icon: "folder-open", title: "没有临时目录", text: "每次对话会在 .anx/agents/tmp 下得到一个 $tmpdir；对话关闭 7 天后自动清理。", compact: true }));
      return;
    }
    const sorted = items.slice().sort((a, b) => toSeconds(b.updated_at) - toSeconds(a.updated_at));
    const list = el("div", { class: "list aa-tmp-list", role: "list" }, sorted.map((item) => {
      const button = el("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: !!item.active,
        title: item.active ? "对话还在进行，结束后才能清理" : `清理 ${item.path || item.rid}`,
        onclick: async (event) => {
          const target = event.currentTarget;
          const ok = await confirmDialog({ title: "清理这个临时目录？", message: `${shortPath(item.path) || item.rid} 里的 ${item.files ?? 0} 个文件（${bytes(item.size)}）会被删除，不能恢复。`, confirmLabel: "清理", danger: true });
          if (ok) clean({ rid: item.rid }, target, "清理");
        } }, "清理");
      return el("div", { class: "aa-tmp-row", role: "listitem" },
        item.kind ? kindMark(item.kind) : el("span", { class: "aa-mark sm" }, xicon("folder", { size: 13 })),
        el("div", { class: "aa-tmp-main" },
          el("div", { class: "aa-tmp-title truncate" }, item.session || item.title || item.rid,
            item.active ? el("span", { class: "pill ok aa-pill-sm" }, "进行中") : null),
          el("button", { class: "aa-tmp-path mono truncate", type: "button", title: `复制路径 ${item.path || ""}`, onclick: () => item.path && copy(item.path) }, shortPath(item.path) || item.rid)),
        el("div", { class: "aa-tmp-meta" },
          el("span", { class: "aa-tmp-size" }, bytes(item.size)),
          el("span", { class: "faint xs" }, `${item.files ?? 0} 个文件 · ${relTime(item.updated_at)}`)),
        button);
    }));
    mount(root, head, list, el("p", { class: "faint xs aa-tmp-note" }, "对话关闭 7 天后自动删除；总量超过 2 GB 时从最旧的开始删。"));
  }

  load();
  return { refresh: load, destroy() { alive = false; } };
}

function toSeconds(value) {
  if (value === null || value === undefined || value === "") return 0;
  if (typeof value === "number") return value > 1e12 ? value / 1000 : value;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed / 1000;
}
export { toSeconds };

// ---------------------------------------------------------------- CLI manager (§2)

/**
 * mountCliManager(container, api, {device, onLogin(kind, device, command)}) — one row per CLI: status, version,
 * path, login; 安装 / 更新 with a live progress log; 重新检测.
 */
export function mountCliManager(container, api, { device = null, onLogin = null } = {}) {
  let alive = true;
  const watchers = new Map();   // kind -> {stop}
  const jobs = new Map();       // kind -> {state, progress, lines, error}
  let agents = [];
  const root = el("div", { class: "aa-cli" });
  mount(container, root);

  async function load({ quiet = false } = {}) {
    if (!quiet) mount(root, skeletonRows(3));
    try {
      agents = normalizeAgents(await callOp(api, "agent_info", { fresh: true }, device));
    } catch (error) {
      if (alive) mount(root, errorState(error, load));
      return;
    }
    if (!alive) return;
    for (const info of agents) {
      if (jobActive(info.install) && !watchers.has(info.kind)) follow(info.kind, info.install.job_id);
    }
    render();
  }

  function follow(kind, jobId) {
    watchers.get(kind)?.stop();
    jobs.set(kind, { state: "running", progress: 0, lines: [] });
    watchers.set(kind, watchInstall(api, device, jobId, (job) => {
      if (!alive) return;
      jobs.set(kind, { ...job, lines: job.lines.slice() });
      if (!jobActive(job)) {
        watchers.delete(kind);
        const name = kindLabel(kind);
        if (job.state === "done" || job.state === "ok") toast(`${name} 已安装好`, { title: "安装完成" });
        else toast(job.error || "安装命令没有成功，日志里有原因", { type: "bad", title: `${name} 安装失败` });
        load({ quiet: true });
      } else renderRow(kind);
    }));
  }

  async function install(kind, update) {
    try {
      const result = await callOp(api, "agent_install", { kind, update: !!update }, device);
      if (!result.job_id) throw new Error("设备没有返回安装任务");
      follow(kind, result.job_id);
      renderRow(kind);
    } catch (error) {
      toast(error.message, { type: "bad", title: update ? "无法更新" : "无法安装" });
    }
  }

  const rows = new Map();
  function renderRow(kind) {
    const info = agents.find((item) => item.kind === kind) || { kind };
    const job = jobs.get(kind);
    const busy = jobActive(job);
    const status = busy ? { state: "installing", label: `安装中 ${job.progress}%`, tone: "info" } : agentStatus(info);
    const actions = el("div", { class: "aa-cli-actions" });
    if (busy) actions.append(el("span", { class: "faint xs" }, job.stage || "正在安装…"));
    else if (status.state === "missing") actions.append(el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => install(kind, false) }, icon("download", { size: 14 }), "安装"));
    else {
      if (status.state === "login") actions.append(el("button", { class: "btn btn-sm", type: "button", onclick: () => login(kind, info) }, icon("terminal", { size: 14 }), "在终端登录"));
      actions.append(el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => install(kind, true) }, icon("refresh-cw", { size: 14 }), "更新"));
    }
    const facts = el("dl", { class: "aa-cli-facts" },
      el("dt", {}, "版本"), el("dd", {}, info.version ? shortVersion(info.version) : "—"),
      el("dt", {}, "路径"), el("dd", { class: "mono truncate", title: info.path || "" }, info.path ? shortPath(info.path) : "—"),
      el("dt", {}, "登录"), el("dd", {}, info.installed ? ({ ok: "已登录", missing: "未登录", unknown: "无法确认（首次对话时会提示）" }[info.login] || "—") : "—"));
    const bar = busy ? progressBar(job.progress) : null;
    const showLog = job && (busy || job.state === "failed") && job.lines?.length;
    const log = showLog ? el("pre", { class: "log aa-cli-log" }, job.lines.slice(-80).join("\n")) : null;
    const node = el("div", { class: "aa-cli-row", dataset: { kind } },
      el("div", { class: "aa-cli-head" },
        kindMark(kind, { size: "md" }),
        el("div", { class: "aa-cli-name" }, el("div", { class: "strong" }, kindLabel(kind, info)),
          el("div", { class: "faint xs" }, info.installed ? "" : info.install_hint || installHint(kind))),
        el("span", { class: `pill ${status.tone}` }, status.label),
        actions),
      info.installed ? facts : null, bar, log);
    if (log) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    const old = rows.get(kind);
    rows.set(kind, node);
    if (old?.isConnected) old.replaceWith(node);
    return node;
  }

  function login(kind, info) {
    const command = loginCommand(kind, info);
    if (onLogin) onLogin(kind, device, command, info);
    else copy(command, { label: `已复制登录命令：${command}` });
  }

  function render() {
    rows.clear();
    const known = new Set(agents.map((item) => item.kind));
    for (const kind of KIND_ORDER) if (!known.has(kind)) agents.push({ kind, installed: false });
    mount(root,
      el("div", { class: "list aa-cli-list" }, agents.map((info) => renderRow(info.kind))),
      el("div", { class: "row between aa-cli-foot" },
        el("span", { class: "faint xs" }, "安装以执行账户运行，使用 npm 全局安装；没有 Node.js 时会提示先安装。"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load() }, icon("refresh-cw", { size: 14 }), "重新检测")));
  }

  load();
  return { refresh: () => load({ quiet: true }), destroy() { alive = false; for (const w of watchers.values()) w.stop(); } };
}

function installHint(kind) {
  return { claude: "npm i -g @anthropic-ai/claude-code", codex: "npm i -g @openai/codex", dsh: "npm i -g @deepseek-ai/dsh（需要 Node.js 22+）", gemini: "npm i -g @google/gemini-cli" }[kind] || "";
}
