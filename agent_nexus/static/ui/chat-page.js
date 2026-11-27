// The 对话 page and the Overview's 对话 card (docs/agent-chat.md §16.1, §16.2), shared by both consoles.
//
//   import { chatPage, chatCard } from "/ui/chat-page.js";
//   "/chat": (route) => chatPage(context("对话", { wide: true }), route, { api, local, onLogin }),
//   const card = chatCard({ api, local });   // card.node goes on the Overview; card.refresh() is throttled
//
// One chat instance per console lives at module level: leaving the page only takes its DOM out of the document,
// so a running turn and its long-poll go on, and coming back puts the same DOM back (§16.2).
// Routes: #/chat?device=<id|local>&kind=<kind>&session=<id>  (open that session) · #/chat?new=1 (a new chat).

import { el, mount, icon, relTime } from "./ui.js";
import { mountChat, chatStore, chatKindMark, chatKindShort } from "./chat.js";
import { callOp, shortPath, toSeconds } from "./agent-admin.js";

const LOCAL_ID = "local";   // the URL's name for 本机 in the local console (its device id is null)

/** `anx-chat:<console>`: `local`, or `cloud:<origin>` (§16.2). */
export function chatStorageKey(local) { return `anx-chat:${local ? "local" : `cloud:${location.origin}`}`; }

/** URLSearchParams of #/chat → the `want` mountChat / chat.apply take ({} when the URL asks for nothing). */
export function chatWant(query, { local = false } = {}) {
  const want = {};
  const get = (name) => { const value = query?.get?.(name); return value === null || value === undefined ? "" : String(value); };
  const device = get("device");
  if (device) want.device = local && device === LOCAL_ID ? null : device;
  const kind = get("kind");
  const session = get("session");
  if (session) want.session = { kind: kind || null, id: session };
  else if (kind) want.kind = kind;
  if (get("new") === "1") want.fresh = true;
  return want;
}

/** The #/chat link for a device / session (the inverse of chatWant). */
export function chatHref({ device, kind, session, fresh = false } = {}, { local = false } = {}) {
  const params = new URLSearchParams();
  if (device !== undefined) {
    if (device === null) { if (local) params.set("device", LOCAL_ID); } else params.set("device", device);
  }
  if (kind) params.set("kind", kind);
  if (session) params.set("session", session);
  if (fresh) params.set("new", "1");
  const text = params.toString();
  return text ? `#/chat?${text}` : "#/chat";
}

const instances = new Map();   // storage key -> {chat, host}

/** The persistent chat of this console (created on first use). */
function instance({ api, local = false, want = {}, ...options }) {
  const key = chatStorageKey(local);
  let entry = instances.get(key);
  if (entry) return { entry, fresh: false };
  const host = el("div", { class: "chat-page" });
  const chat = mountChat(host, api, { ...options, localDevice: local, storageKey: key, persistent: true, want });
  entry = { chat, host };
  instances.set(key, entry);
  return { entry, fresh: true };
}

/** The live chat of a console, if its page was opened in this tab (the Overview card reads it). */
export function chatInstance(local) { return instances.get(chatStorageKey(local))?.chat || null; }

/** #/chat: the chat fills the content area (height = viewport − top bar, − the tab bar on phones). */
export function chatPage(ctx, { query } = {}, { api, local = false, ...options } = {}) {
  const want = chatWant(query, { local });
  const { entry, fresh } = instance({ api, local, want, ...options });
  ctx.page.classList.add("page-chat");
  mount(ctx.page, entry.host);
  ctx.onLeave(() => {
    ctx.page.classList.remove("page-chat");
    entry.chat.detached();
    entry.host.remove();
  });
  if (!fresh) {
    entry.chat.attached();
    if (Object.keys(want).length) entry.chat.apply(want);
  }
  // Applied: drop the params, so a reload restores the state the chat is in by then rather than this link.
  if (Object.keys(want).length && location.hash.startsWith("#/chat?")) history.replaceState(null, "", "#/chat");
  return entry.chat;
}

// ---------------------------------------------------------------- the Overview's 对话 card

const CARD_EVERY_MS = 30000;

/**
 * {node, refresh()}: the 3 most recent sessions of the console's chat device (links into #/chat) and 新对话.
 * The node is created once; the Overview's 10 s redraw can re-place it, refresh() reads at most every 30 s.
 */
export function chatCard({ api, local = false } = {}) {
  const key = chatStorageKey(local);
  const meta = el("span", { class: "faint xs truncate chat-card-device" });
  const body = el("div", { class: "chat-card-body" }, skeleton());
  const newLink = el("a", { class: "btn btn-sm btn-primary", href: chatHref({ fresh: true }, { local }) }, icon("plus", { size: 14 }), "新对话");
  const node = el("section", { class: "card flush chat-card" },
    el("div", { class: "card-head" }, el("h2", {}, "对话"), meta,
      el("div", { class: "actions" },
        el("a", { class: "btn btn-ghost btn-sm", href: "#/chat" }, "全部", icon("chevron-right", { size: 14 })),
        newLink)),
    body);
  let last = 0, busy = null;

  function skeleton() {
    return el("div", { class: "list inset" }, Array.from({ length: 3 }, () => el("div", { class: "list-item" },
      el("span", { class: "skeleton", style: { width: "22px", height: "22px" } }),
      el("div", { class: "stack grow", style: { gap: "4px" } }, el("span", { class: "skeleton w-60" }), el("span", { class: "skeleton w-40", style: { height: "10px" } })))));
  }

  async function read() {
    const live = chatInstance(local)?.snapshot();
    // the live chat's view, unless its device failed to load: then read it here, so the card shows the error
    // (with 重试, which really reads again) and never "还没有会话"
    if (live && !live.loading && live.device !== undefined && !live.error) return live;
    let devices = [];
    try { devices = (await api.devices()) || []; } catch { devices = []; }
    if (local && !devices.some((d) => d.self)) devices.unshift({ id: null, name: "本机", online: true, self: true });
    const usable = (id) => devices.some((d) => d.id === id && d.online !== false);
    const stored = chatStore(key).read().device;
    const device = stored !== undefined && usable(stored) ? stored
      : (devices.find((d) => d.self && d.online !== false) || devices.find((d) => d.online !== false) || {}).id;
    if (device === undefined) return { device: undefined, devices, sessions: [], runs: [] };
    const [sessions, runs] = await Promise.allSettled([callOp(api, "agent_sessions", {}, device), callOp(api, "agent_run_list", {}, device)]);
    if (sessions.status === "rejected") return { device, devices, error: sessions.reason?.message || "读取会话失败" };
    const list = (value, name) => (Array.isArray(value) ? value : Array.isArray(value?.[name]) ? value[name] : []);
    return { device, devices, sessions: list(sessions.value, "sessions"), runs: runs.status === "fulfilled" ? list(runs.value, "runs") : [] };
  }

  function render(data) {
    const d = data.devices?.find((item) => item.id === data.device);
    meta.textContent = data.device === undefined ? "" : d ? (d.self ? "本机" : d.name || d.id) : data.device === null ? "本机" : data.device;
    newLink.href = chatHref({ fresh: true, device: data.device }, { local });
    if (data.error) {
      mount(body, el("div", { class: "card-body" }, el("p", { class: "muted small" }, data.error),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => refresh({ force: true }) }, icon("refresh-cw", { size: 14 }), "重试")));
      return;
    }
    if (data.device === undefined) {
      mount(body, el("div", { class: "card-body" }, el("p", { class: "muted small" }, "现在没有能运行 Agent 对话的设备：设备需要在线，且客户端版本支持 Agent 对话。")));
      return;
    }
    const recent = (data.sessions || []).filter((s) => !s.hidden && !s.archived)
      .sort((a, b) => toSeconds(b.updated_at) - toSeconds(a.updated_at)).slice(0, 3);
    if (!recent.length) {
      mount(body, el("div", { class: "card-body" }, el("p", { class: "muted small" }, "还没有会话。点“新对话”，和这台设备上的 Claude Code、Codex 等 CLI 聊起来。")));
      return;
    }
    const liveRun = (s) => (data.runs || []).some((r) => r.session_id === s.id && ["starting", "running"].includes(r.state));
    mount(body, el("div", { class: "list inset" }, recent.map((s) => el("a", {
      class: "list-item clickable chat-card-item", href: chatHref({ device: data.device, kind: s.kind, session: s.id }, { local }), title: s.cwd || "",
    },
    chatKindMark(s.kind),
    el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } },
      el("span", { class: "title" }, liveRun(s) ? el("span", { class: "chat-live", title: "进行中" }) : null, el("span", { class: "truncate" }, s.alias || s.title || "（无标题）")),
      el("span", { class: "sub truncate" }, `${chatKindShort(s.kind)} · ${shortPath(s.cwd) || "—"}`)),
    el("div", { class: "meta" }, el("span", { class: "faint xs" }, relTime(s.updated_at)))))));
  }

  function refresh({ force = false } = {}) {
    if (busy) return busy;
    if (!force && Date.now() - last < CARD_EVERY_MS) return Promise.resolve();
    last = Date.now();
    busy = read().then(render, (error) => render({ error: error?.message || "读取会话失败" })).finally(() => { busy = null; });
    return busy;
  }

  refresh({ force: true });
  return { node, refresh };
}
