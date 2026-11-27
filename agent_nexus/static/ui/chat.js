// Agent chat (docs/agent-chat.md §3, §4, §10, §11, §16): session sidebar grouped by CLI, a light head line
// (会话列表 · title · ⋯), unified event stream, composer with a chip row (运行设备 / Agent / 目录 / 权限) and the
// context-usage ring.
//
//   import { mountChat } from "/ui/chat.js";
//   const chat = mountChat(container, api, {onLogin(kind, device, command, info) {…}, onEditPrompts(device) {…},
//                                           localDevice: true /* false: no synthetic 本机 entry (the cloud console) */,
//                                           storageKey: "anx-chat:local" /* §16.2: the state kept per console */,
//                                           persistent: true /* keep running while detached (ui/chat-page.js) */,
//                                           want: {device, kind, session: {kind, id}, fresh} /* URL params, win over storage */});
//   chat.apply(want);  // later URL params (#/chat?device=&kind=&session=)
//   chat.destroy();    // stops polling (without `persistent` also by itself once the container leaves the document)
//
// `api` adapts the console's transport so the same component runs in the cloud console and the local one:
//   call(op, params, device) -> Promise<result>   POST JSON → JSON; `device` = runner device id from devices()
//                                                 (cloud: /api/devices/{device}/agents/{op};
//                                                  local: /api/agents/local/{op} for 本机, /api/agents/remote/{device}/{op})
//   devices()               -> Promise<[{id, name, online, self, hint?}]>   (hint: why a device can't be picked)
//   devicesNote()           -> string (optional): a line under the 运行设备 menu, e.g. why remote devices are missing
//   conversation(address)   -> Promise<{messages, asks}>   (me ↔ the run's mailbox address, §10)
//   answerAsk(id, answer)   -> Promise<result>             answer = {choice} | {choices} | {text}

import { el, icon, mount, toast, modal, drawer, confirmDialog, relTime, copy } from "./ui.js";
import {
  xicon, KINDS, KIND_ORDER, kindLabel as baseLabel, kindShort as baseShort, kindMark as baseMark, callOp, normalizeAgents, agentStatus, notReadyReason,
  watchInstall, progressBar, jobActive, loginCommand, shortPath, toSeconds, mountTmpManager,
} from "./agent-admin.js";
import { mountPromptEditor } from "./prompts.js";

const PERMISSIONS = [
  ["ask", "需要审批", "改文件、运行命令前先问你"],
  ["edits", "自动改文件", "直接修改工作目录里的文件，其他操作仍需审批"],
  ["readonly", "只读", "只读取和分析，不做任何修改"],
  ["full", "完全信任", "不再询问，可以执行任何命令"],
];
const PERMISSION_LABEL = Object.fromEntries(PERMISSIONS.map(([key, label]) => [key, label]));
// A CLI that cannot ask before each action (Codex exec) runs "ask" as a sandbox: say exactly that.
const NO_APPROVAL_ASK = ["沙箱内自动执行", "不支持逐项审批：只能在工作目录里改文件、运行命令，越界的操作会被沙箱拒绝"];
const EXAMPLES = [
  ["梳理项目结构", "梳理这个项目的结构，告诉我先读哪几个文件"],
  ["修复失败的测试", "运行测试，找出失败的原因并修复"],
  ["写提交说明", "看看工作区里未提交的改动，帮我写一条提交说明"],
  ["检查依赖", "检查依赖里有没有需要升级的安全问题，列出来但先别改"],
];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// §16.2: a hidden page keeps its long-poll for this long, then pauses until it is shown again.
export const PAUSE_HIDDEN_MS = 10 * 60 * 1000;

/** §16.2: the chat state of one console, one JSON object under `anx-chat:<console>`. */
export function chatStore(key) {
  const read = () => {
    try { const raw = localStorage.getItem(key); const value = raw ? JSON.parse(raw) : {}; return value && typeof value === "object" ? value : {}; } catch { return {}; }
  };
  return {
    key, read,
    write(patch) {
      const next = { ...read(), ...patch };
      try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* private mode / quota */ }
      return next;
    },
  };
}

/**
 * One device's remembered choices ({kind, workdir, permission, preset, context, session, rid}) in a stored chat
 * state: its `byDevice` entry. The top-level fields are the last device's, so they count only in a state without
 * `byDevice`, and only for that device -- else the previous device's workdir leaks onto a newly chosen one.
 */
export function devicePrefs(stored, id) {
  const st = stored && typeof stored === "object" ? stored : {};
  const byDevice = st.byDevice && typeof st.byDevice === "object" ? st.byDevice : {};
  if (Object.keys(byDevice).length) return byDevice[id ?? "local"] || {};
  return st.device === id ? st : {};   // a state written before byDevice existed
}

// ---------------------------------------------------------------- folders on other devices (§1 mode gateway)

const folderBase = (path) => String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).pop() || String(path || "");

/**
 * The devices a run on `runner` can work on through the gateway, from the chat's device list: every device but the
 * runner itself with an id on the gateway (本机 of the local console carries it as `gid`). `id` is that gateway id
 * (the context's device), `call` the id the console's api takes for the device's own ops (null = 本机), `online`
 * whether the gateway reaches it (`reachable`; a device without agents/1 still serves use_device), `agents` whether
 * its own AgentHub answers (agent_dirs: its recent folders, workspaces, browsing), `files` whether files/1 lists it.
 */
export function contextTargets(devices, runner) {
  const out = [];
  for (const d of Array.isArray(devices) ? devices : []) {
    if (!d || (d.self && runner === null) || (d.id != null && d.id === runner)) continue;
    const id = d.id || d.gid;
    if (!id || (d.gid && d.gid === runner)) continue;
    const agents = d.online !== false;
    out.push({ id, call: d.id ?? null, self: !!d.self, name: d.self ? `本机${d.name && d.name !== "本机" ? `（${d.name}）` : ""}` : d.name || id,
      online: d.reachable !== undefined ? !!d.reachable : agents, agents, files: !!d.files,
      workdirs: Array.isArray(d.workdirs) ? d.workdirs.filter((w) => w && w.path) : [], hint: d.hint || "" });
  }
  return out;
}

/**
 * The folders the 目录 menu offers for one other device, newest first and each once: the folders this console used
 * there (`used`: [{device, workdir}], newest first), its own recent folders and workspaces (agent_dirs, when its
 * AgentHub answers), its registered workdirs (the device list). `limit` cuts the list.
 */
export function targetFolders(target, { used = [], dirs = null, limit = 4 } = {}) {
  const seen = new Set(), out = [];
  const push = (path, name, from) => {
    if (typeof path !== "string" || !path || seen.has(path)) return;
    seen.add(path);
    out.push({ path, name: name || folderBase(path), from });
  };
  for (const u of used) if (u && u.device === target.id) push(u.workdir, "", "used");
  for (const r of dirs?.recent || []) push(typeof r === "string" ? r : r?.path, "", "recent");
  for (const w of dirs?.workspaces || []) push(typeof w === "string" ? w : w?.path, typeof w === "string" ? "" : w?.name, "workspace");
  for (const w of target.workdirs || []) push(w.path, w.name, "workspace");
  return out.slice(0, limit);
}

/** The 目录 chip: {value, title}; in gateway mode "<folder> · <device>" with the whole story in the title. */
export function folderChip({ workdir, context, runnerName, targetName }) {
  if (context?.mode === "gateway" && context.workdir) {
    const device = targetName || context.device;
    return { value: folderBase(context.workdir), device, gateway: true,
      title: `目录：${device} 上的 ${context.workdir}（经网关；CLI 在 ${runnerName} 上运行）` };
  }
  if (!workdir) return { value: "选择目录", device: "", gateway: false, title: "目录：未选择" };
  return { value: folderBase(shortPath(workdir)) || workdir, device: "", gateway: false, title: `目录：${workdir}` };
}

/**
 * Remember a gateway folder (newest first, each device+folder once, at most `max`), with the device's display name
 * so it still reads as that name while the device is not in the list (gateway out of reach, device removed).
 */
export function rememberTarget(list, device, workdir, { max = 24, name = "" } = {}) {
  const rest = (Array.isArray(list) ? list : []).filter((u) => u && !(u.device === device && u.workdir === workdir));
  return [{ device, workdir, ...(name ? { name } : {}) }, ...rest].slice(0, max);
}

// ---------------------------------------------------------------- API models (§16.4): kind = "api:<provider>/<model>"

export const isApiKind = (kind) => String(kind || "").startsWith("api:");
const apiName = (kind) => String(kind).slice(4).split("/").filter(Boolean).pop() || "API";
function kindLabel(kind, info) { return isApiKind(kind) && !info?.label ? apiName(kind) : baseLabel(kind, info); }
function kindShort(kind, info) { return isApiKind(kind) ? info?.label || apiName(kind) : baseShort(kind, info); }
function kindMark(kind, opts = {}) {
  if (!isApiKind(kind)) return baseMark(kind, opts);
  return el("span", { class: `aa-mark ${opts.size || "sm"}`, dataset: { kind: "api" }, "aria-hidden": "true" }, "API");
}

// ---------------------------------------------------------------- context usage (§16.3)

/** 872100 -> "872.1K", 1000000 -> "1M" (the ring's title and its no-limit count). */
export function tokenCount(n) {
  const v = Math.max(0, Number(n) || 0);
  const trim = (x) => x.toFixed(1).replace(/\.0$/, "");
  return v >= 1e6 ? `${trim(v / 1e6)}M` : v >= 1e3 ? `${trim(v / 1e3)}K` : String(Math.round(v));
}

/** {used, limit, model} -> {pct|null, tone: ""|warn|bad, title}; null when nothing is known yet. */
export function usageView(context) {
  if (!context || !(Number(context.used) >= 0) || context.used === null) return null;
  const used = Number(context.used) || 0;
  const limit = Number(context.limit) > 0 ? Number(context.limit) : null;
  const model = context.model ? `· 模型 ${context.model}` : "";
  if (!limit) return { pct: null, tone: "", title: `已用 ${tokenCount(used)}（上限未知）${model}` };
  const ratio = used / limit;
  const pct = Math.round(ratio * 100);
  return { pct, ratio, tone: ratio > 0.95 ? "bad" : ratio > 0.8 ? "warn" : "", title: `已用 ${tokenCount(used)} / ${tokenCount(limit)}（${pct}%）${model}` };
}

function usageRing(view) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 18 18"); svg.setAttribute("width", "18"); svg.setAttribute("height", "18"); svg.setAttribute("aria-hidden", "true");
  const r = 7, c = 2 * Math.PI * r;
  const track = document.createElementNS(ns, "circle");
  const bar = document.createElementNS(ns, "circle");
  for (const node of [track, bar]) { node.setAttribute("cx", "9"); node.setAttribute("cy", "9"); node.setAttribute("r", String(r)); }
  track.setAttribute("class", "chat-usage-track");
  bar.setAttribute("class", "chat-usage-bar");
  bar.setAttribute("stroke-dasharray", `${(Math.min(1, view.ratio) * c).toFixed(2)} ${c.toFixed(2)}`);
  bar.setAttribute("transform", "rotate(-90 9 9)");
  svg.append(track, bar);
  return svg;
}

// ---------------------------------------------------------------- popovers and menus (appended to <body>)

let openPop = null;
function closePop() { openPop?.close(); }
// Popovers opened from the keyboard focus their first item; opened by pointer they focus the panel
// (arrow keys still work) so no focus ring flashes on a mouse click.
let keyboardUse = false;
if (typeof document !== "undefined") {   // (the module also loads without a DOM: tests/test_chat_page_ui.cjs)
  document.addEventListener("keydown", () => { keyboardUse = true; }, true);
  document.addEventListener("pointerdown", () => { keyboardUse = false; }, true);
}

function placePop(panel, anchor, align) {
  const rect = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const vw = window.innerWidth, vh = window.innerHeight;
  panel.style.maxWidth = `${vw - 16}px`;
  const w = panel.offsetWidth, h = panel.offsetHeight;
  let left = align === "end" ? rect.right - w : rect.left;
  left = Math.max(8, Math.min(left, vw - w - 8));
  let top = rect.bottom + 6;
  if (top + h > vh - 8 && rect.top - h - 6 > 8) top = rect.top - h - 6;
  top = Math.max(8, Math.min(top, vh - h - 8));
  Object.assign(panel.style, { position: "fixed", left: `${Math.round(left)}px`, top: `${Math.round(top)}px` });
}

function popover(anchor, content, { align = "start", role = "dialog", label = "", cls = "", onClose = null } = {}) {
  closePop();
  const panel = el("div", { class: `menu chat-pop ${cls}`, role, "aria-label": label || null, tabindex: "-1" }, content);
  document.body.append(panel);
  placePop(panel, anchor, align);
  const isEl = anchor instanceof Element;
  const outside = (event) => { if (!panel.contains(event.target) && !(isEl && anchor.contains(event.target))) close(); };
  const onKey = (event) => {
    if (event.key === "Escape") { event.stopPropagation(); close(); if (isEl) anchor.focus(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const items = Array.from(panel.querySelectorAll("button:not(:disabled), input:not(:disabled), [tabindex='0']"));
      if (!items.length) return;
      const index = items.indexOf(document.activeElement);
      const next = event.key === "ArrowDown" ? (index + 1) % items.length : (index <= 0 ? items.length - 1 : index - 1);
      items[next].focus();
      event.preventDefault();
    }
  };
  const onResize = () => close();
  document.addEventListener("pointerdown", outside, true);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("resize", onResize);
  if (isEl) anchor.setAttribute("aria-expanded", "true");
  function close() {
    if (!panel.isConnected) return;
    panel.remove();
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", onResize);
    if (isEl) anchor.setAttribute("aria-expanded", "false");
    if (openPop?.panel === panel) openPop = null;
    onClose?.();
  }
  openPop = { close, panel, reposition: () => placePop(panel, anchor, align) };
  const viaKeyboard = keyboardUse;
  requestAnimationFrame(() => {
    const first = viaKeyboard && (panel.querySelector("[aria-checked='true'], [aria-selected='true']") || panel.querySelector("button:not(:disabled), input"));
    (first || panel).focus({ preventScroll: true });
  });
  return openPop;
}

/** items: [{label, desc, icon, checked, disabled, danger, hint, onClick} | "sep" | {heading}] */
function menu(anchor, items, opts = {}) {
  let pop;
  const nodes = items.map((item) => {
    if (item === "sep") return el("div", { class: "menu-sep", role: "separator" });
    if (item.heading) return el("div", { class: "chat-menu-heading" }, item.heading);
    const radio = item.checked !== undefined;
    return el("button", {
      class: `menu-item chat-menu-item ${item.danger ? "danger" : ""} ${item.desc ? "two-line" : ""}`, type: "button",
      role: radio ? "menuitemradio" : "menuitem", "aria-checked": radio ? String(!!item.checked) : null, disabled: !!item.disabled,
      onclick: () => { pop.close(); item.onClick?.(); },
    },
    item.icon ? xicon(item.icon, { size: 15 }) : radio ? el("span", { class: "chat-menu-tick" }, item.checked ? xicon("check", { size: 14 }) : null) : null,
    el("span", { class: "chat-menu-text" }, el("span", { class: "truncate" }, item.label), item.desc ? el("span", { class: "chat-menu-desc" }, item.desc) : null),
    item.hint ? el("span", { class: "chat-menu-hint" }, item.hint) : null);
  });
  pop = popover(anchor, nodes, { role: "menu", ...opts });
  return pop;
}

// ---------------------------------------------------------------- markdown-lite (text only; never innerHTML)

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s<>()（）]+[^\s<>().,;:!?'"）。，、])/g;
function inline(text) {
  const out = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[1]) out.push(el("code", { class: "chat-ic" }, match[1].slice(1, -1)));
    else if (match[2]) out.push(el("strong", {}, match[2].slice(2, -2)));
    else if (match[3]) out.push(el("a", { href: match[5], target: "_blank", rel: "noopener noreferrer" }, match[4]));
    else if (match[6]) out.push(el("a", { href: match[6], target: "_blank", rel: "noopener noreferrer" }, match[6]));
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function codeBlock(code, lang = "") {
  return el("div", { class: "chat-code" },
    el("div", { class: "chat-code-head" }, el("span", {}, lang || "代码"),
      el("button", { class: "chat-code-copy", type: "button", "aria-label": "复制代码", onclick: () => copy(code) }, icon("copy", { size: 13 }), "复制")),
    el("pre", {}, el("code", {}, code)));
}

const LIST_ITEM = /^\s*([-*+]|\d+[.)])\s+/;
export function renderMarkdown(text) {
  const root = el("div", { class: "chat-md" });
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let para = [];
  const flush = () => { if (para.length) { root.append(el("p", {}, inline(para.join("\n")))); para = []; } };
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/);
    if (fence) {
      flush();
      const buf = [];
      for (i += 1; i < lines.length && !lines[i].trim().startsWith(fence[1]); i += 1) buf.push(lines[i]);
      i += 1;
      root.append(codeBlock(buf.join("\n"), fence[2]));
      continue;
    }
    if (!line.trim()) { flush(); i += 1; continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { flush(); root.append(el("div", { class: `chat-h h${heading[1].length}` }, inline(heading[2]))); i += 1; continue; }
    if (LIST_ITEM.test(line)) {
      flush();
      const list = el(/^\s*\d/.test(line) ? "ol" : "ul");
      while (i < lines.length && LIST_ITEM.test(lines[i])) {
        const item = el("li", {}, inline(lines[i].replace(LIST_ITEM, "")));
        for (i += 1; i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_ITEM.test(lines[i]); i += 1) item.append("\n", ...inline(lines[i].trim()));
        list.append(item);
      }
      root.append(list);
      continue;
    }
    if (/^>\s?/.test(line)) {
      flush();
      const buf = [];
      for (; i < lines.length && /^>\s?/.test(lines[i]); i += 1) buf.push(lines[i].replace(/^>\s?/, ""));
      root.append(el("blockquote", {}, inline(buf.join("\n"))));
      continue;
    }
    para.push(line);
    i += 1;
  }
  flush();
  return root;
}

function diffView(diff) {
  return el("pre", { class: "chat-diff" }, String(diff || "").split("\n").map((line) => {
    const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    return el("span", { class: cls }, line || " ");
  }));
}

function diffStats(diff) {
  let add = 0, del = 0;
  for (const line of String(diff || "").split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) add += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) del += 1;
  }
  return { add, del };
}

function compact(n) {
  const v = Number(n) || 0;
  return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v);
}

function formatInput(input) {
  if (input === null || input === undefined || input === "") return "";
  if (typeof input === "string") return input;
  if (typeof input.command === "string") return input.command;
  if (Array.isArray(input.command)) return input.command.join(" ");
  try { return JSON.stringify(input, null, 2); } catch { return String(input); }
}

// ---------------------------------------------------------------- the component

export function mountChat(container, api, opts = {}) {
  const store = chatStore(opts.storageKey || "anx-chat:local");
  const S = {
    devices: [], device: null, agents: [], dirs: null, presets: [], runs: [],
    kind: null, workdir: "", permission: "ask", preset: "default", context: { mode: "direct", device: "", workdir: "" },
    sessions: [], sessionsState: "loading", sessionsError: "", filterKind: "all", query: "",
    session: null, rid: null, cursor: 0, runState: "idle", sessionId: null, address: null,
    deviceError: "", loading: true, gen: 0, alive: true, usage: null,
  };
  const installs = new Map();   // kind -> {watch, job}
  let convTimer = null;
  let fellBack = false;         // the "device offline, using another" toast is said once

  // --- skeleton
  const sideList = el("div", { class: "chat-sessions", role: "list", "aria-label": "会话" });
  const kindTabs = el("div", { class: "chat-kinds", role: "tablist", "aria-label": "按 CLI 筛选会话" });
  const search = el("input", { class: "input chat-search", type: "search", placeholder: "搜索会话或目录", "aria-label": "搜索会话",
    oninput: () => { S.query = search.value.trim().toLowerCase(); renderSessions(); } });
  const side = el("aside", { class: "chat-side", "aria-label": "会话列表" },
    el("div", { class: "chat-side-head" },
      el("h2", {}, "会话"),
      el("button", { class: "btn btn-sm chat-new", type: "button", onclick: () => newChat() }, xicon("square-pen", { size: 14 }), "新对话"),
      el("button", { class: "btn btn-ghost btn-icon btn-sm chat-side-collapse", type: "button", "aria-label": "收起会话列表", title: "收起会话列表", onclick: () => setCollapsed(true) }, icon("panel-left", { size: 16 })),
      el("button", { class: "btn btn-ghost btn-icon btn-sm chat-side-close", type: "button", "aria-label": "收起会话列表", onclick: () => toggleSide(false) }, icon("x", { size: 16 }))),
    kindTabs,
    el("div", { class: "input-icon chat-search-wrap" }, icon("search", { size: 15 }), search),
    sideList);
  const scrim = el("div", { class: "chat-scrim", onclick: () => toggleSide(false) });

  // A light head line (session toggle · title · ⋯) and the composer's chip row (运行设备 / Agent / 目录 / 权限).
  const sideBtn = el("button", { class: "btn btn-ghost btn-icon btn-sm chat-side-toggle", type: "button", "aria-label": "会话列表", title: "会话列表", onclick: () => sideButton() }, icon("panel-left", { size: 17 }));
  const headTitle = el("span", { class: "chat-head-title truncate" });
  const moreBtn = el("button", { class: "btn btn-ghost btn-icon btn-sm chat-more", type: "button", "aria-label": "更多设置", title: "提示词预设、临时目录", "aria-haspopup": "menu",
    onclick: (event) => moreMenu(event.currentTarget) }, icon("ellipsis", { size: 18 }));
  const head = el("div", { class: "chat-head" }, sideBtn, headTitle, moreBtn);
  const chips = el("div", { class: "chat-chips", role: "toolbar", "aria-label": "对话设置" });
  chips.addEventListener("scroll", () => chipsEdge(), { passive: true });
  const ctxLine = el("div", { class: "chat-ctxline", hidden: true });
  const stream = el("div", { class: "chat-stream", role: "log", "aria-label": "对话内容", "aria-live": "off" });
  const working = el("div", { class: "chat-working", hidden: true }, el("span", { class: "chat-dots", "aria-hidden": "true" }, el("i"), el("i"), el("i")), el("span", {}));
  const welcome = el("div", { class: "chat-welcome-wrap" });
  const scroller = el("div", { class: "chat-scroll" }, welcome, stream, working);
  const jump = el("button", { class: "btn btn-sm chat-jump", type: "button", hidden: true, onclick: () => scrollToEnd(true) }, xicon("arrow-down", { size: 14 }), "最新消息");
  const live = el("div", { class: "sr-only", "aria-live": "polite" });

  const input = el("textarea", { class: "chat-input", rows: 1, "aria-label": "消息", placeholder: "" });
  const sendBtn = el("button", { class: "btn btn-primary btn-icon chat-send", type: "button", "aria-label": "发送", title: "发送（Enter）", onclick: () => send() }, xicon("arrow-up", { size: 18 }));
  const stopBtn = el("button", { class: "btn chat-stop", type: "button", hidden: true, "aria-label": "停止", title: "停止这一轮", onclick: () => stop() }, icon("square", { size: 13 }), el("span", { class: "chat-stop-text" }, "停止"));
  const composerNote = el("div", { class: "chat-note", hidden: true });
  // a button: phones show no title tooltip, so a tap shows the figures
  const usageNode = el("button", { class: "chat-usage", type: "button", hidden: true, onclick: () => {
    const view = usageView(S.usage);
    if (view) toast(`上下文${view.title}`, { type: "info", timeout: 5000 });
  } });
  const composer = el("div", { class: "chat-composer" },
    composerNote,
    input,
    el("div", { class: "chat-composer-foot" }, chips, el("div", { class: "chat-composer-actions" }, usageNode, stopBtn, sendBtn)));
  const main = el("section", { class: "chat-main", "aria-label": "对话" }, head, ctxLine, el("div", { class: "chat-body" }, scroller, jump), el("div", { class: "chat-composer-wrap" }, composer), live);
  const root = el("div", { class: "chat" }, side, scrim, main);
  if (opts.height) root.style.setProperty("--chat-h", typeof opts.height === "number" ? `${opts.height}px` : opts.height);
  // Wide layouts open the session list by default. Only a collapse made with this version (`sideClosed`) keeps
  // it closed: the older `sideCollapsed` preference is dropped once.
  { const stored = store.read(); if ("sideCollapsed" in stored) store.write({ sideCollapsed: undefined }); if (stored.sideClosed === true) root.classList.add("side-collapsed"); }
  mount(container, root);

  const call = (op, params = {}) => callOp(api, op, params, S.device);
  const deviceApi = () => { const device = S.device; return { ...api, call: (op, params) => api.call(op, params, device) }; };
  const agentInfo = (kind = S.kind) => S.agents.find((item) => item.kind === kind) || null;
  // The permission modes the CLI has (agent_info.permissions) and the device allows (policy.full).
  const permModes = (info = agentInfo()) => (Array.isArray(info?.permissions) && info.permissions.length ? info.permissions : PERMISSIONS.map(([key]) => key));
  const fullAllowed = () => S.policy?.full !== false;
  const permUsable = (key, info = agentInfo()) => permModes(info).includes(key) && (key !== "full" || fullAllowed());
  const noApprovals = (info = agentInfo()) => info?.supports?.approvals === false;
  const permLabel = (key = S.permission, info = agentInfo()) => (key === "ask" && noApprovals(info) ? NO_APPROVAL_ASK[0] : PERMISSION_LABEL[key] || key);
  const canResume = (kind) => agentInfo(kind)?.supports?.resume !== false;
  // After the Agent changes: a mode it does not have falls back to its first one, and the user is told.
  function fitPermission({ quiet = false } = {}) {
    const info = agentInfo();
    if (!info || permUsable(S.permission, info)) return;
    const next = permModes(info).find((key) => key !== "full" && permUsable(key, info)) || "ask";
    if (next === S.permission) return;
    const before = permLabel(S.permission, info);
    S.permission = next;
    if (!quiet) toast(`${kindShort(S.kind, info)} 没有“${before}”模式，已改为“${permLabel(next, info)}”`, { type: "info", timeout: 3200 });
  }
  const deviceName = (id = S.device) => {
    const d = S.devices.find((item) => item.id === id);
    if (d) return d.self ? "本机" : d.name || d.id;
    if (id === undefined) return "未选择";
    return opts.localDevice === false ? id : "本机";
  };

  // --- the work context (§1): direct = the run device's folder; gateway = a folder on another device of the network
  const directContext = () => ({ mode: "direct", device: "", workdir: "" });
  let dirFieldSeq = 0;              // ids for the 目录 menu's per-device path fields (aria-controls)
  const targetDirs = new Map();   // gateway device id -> {state, value, at, promise}: its agent_dirs, for the 目录 menu
  const targets = () => contextTargets(S.devices, S.device);
  const targetOf = (id) => targets().find((t) => t.id === id) || null;
  // the device's name: from the device list, else the name stored with the context or the remembered folders
  // (never the raw gateway id while the list lacks it), else the id
  const knownName = (id) => (S.context.device === id && S.context.name) || usedTargets().find((u) => u?.device === id && u.name)?.name
    || (typeof seenNames()[id] === "string" ? seenNames()[id] : "");
  const seenNames = () => { const value = store.read().deviceNames; return value && typeof value === "object" ? value : {}; };
  /** The names of the devices this console has listed (gateway id -> name, the newest 64), for knownName. */
  function rememberNames(devices) {
    const before = seenNames(), fresh = {};
    for (const d of devices) { const id = d?.id || d?.gid; if (id && d.name && !(d.self && d.name === "本机")) fresh[id] = d.self ? `本机（${d.name}）` : d.name; }
    const merged = Object.fromEntries(Object.entries({ ...before, ...fresh }).slice(-64));
    if (JSON.stringify(merged) !== JSON.stringify(before)) store.write({ deviceNames: merged });
  }
  const targetName = (id) => targetOf(id)?.name || S.devices.find((d) => d.id === id || (d.gid && d.gid === id))?.name || knownName(id) || id || "未选择设备";
  const gatewayCtx = () => S.context.mode === "gateway" && !!S.context.device && !!S.context.workdir;
  const usedTargets = () => { const value = store.read().targets; return Array.isArray(value) ? value : []; };
  /** Why the gateway context cannot run now ("" = it can): the gateway out of reach, the device offline. */
  function contextProblem() {
    if (!gatewayCtx()) return "";
    if (S.devicesNote && !targets().length) return "连不上网关，暂时用不了其他设备上的目录";
    const t = targetOf(S.context.device);
    if (!t) return `${targetName(S.context.device)} 已不在设备列表里`;
    if (!t.online) return `${t.name} ${t.hint || "离线"}`;
    return "";
  }
  /** A session's or a run's context ({mode, device, workdir[, name]}) becomes the chat's: gateway, or back to direct. */
  function adoptContext(ctx) {
    S.context = ctx && ctx.mode === "gateway" && ctx.device && ctx.workdir ? { mode: "gateway", device: ctx.device, workdir: ctx.workdir } : directContext();
    if (gatewayCtx()) {
      const name = targetOf(S.context.device)?.name || (typeof ctx.name === "string" && ctx.name) || knownName(S.context.device);
      if (name) S.context.name = name;
    }
    // a context on the run device itself is its own folder: direct
    const self = S.devices.find((d) => d.self);
    if (gatewayCtx() && (S.context.device === S.device || (S.device === null && self?.gid === S.context.device))) {
      S.workdir = S.context.workdir;
      S.context = directContext();
    }
  }

  // --- input behaviour
  input.addEventListener("input", autoGrow);
  let draftTimer = null;
  input.addEventListener("input", () => { clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 300); });
  function saveDraft() { clearTimeout(draftTimer); store.write({ draft: input.value }); }
  { const draft = store.read().draft; if (typeof draft === "string" && draft) { input.value = draft; requestAnimationFrame(autoGrow); } }
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      send();
    }
  });
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
    renderComposer();
  }

  // --- scrolling: stick to the bottom unless the user scrolled up
  // Content keeps growing below while we follow it, so only a move *up* from where we last put the
  // view counts as the user leaving the bottom; reaching the bottom again re-attaches.
  let stick = true, autoTop = 0;
  scroller.addEventListener("scroll", () => {
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    if (atBottom) { stick = true; jump.hidden = true; }
    else if (scroller.scrollTop < autoTop - 8) stick = false;
  }, { passive: true });
  function scrollToEnd(force = false) {
    if (force || stick) {
      scroller.scrollTop = scroller.scrollHeight;
      autoTop = scroller.scrollTop;
      stick = true;
      jump.hidden = true;
    } else jump.hidden = false;
  }
  let scrollQueued = false;
  function queueScroll() {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => { scrollQueued = false; scrollToEnd(); });
  }

  // --- side drawer (narrow layouts): a modal panel -- Escape closes it, focus goes back to its toggle and
  // the covered chat is inert (no Tab into it, no clicks through the scrim).
  const narrow = () => getComputedStyle(scrim).display !== "none";
  function toggleSide(open) {
    const was = root.classList.contains("side-open");
    root.classList.toggle("side-open", open);
    const modalSide = open && narrow();
    main.inert = modalSide;
    if (modalSide) side.setAttribute("aria-modal", "true"); else side.removeAttribute("aria-modal");
    if (open) (sideList.querySelector(".chat-sess.active .chat-sess-main") || search).focus();
    else if (was && side.contains(document.activeElement)) sideBtn.focus();
  }
  // wide layouts: the list is open by default and can be collapsed (remembered, §16.2); the head's toggle brings it back
  function setCollapsed(on) {
    root.classList.toggle("side-collapsed", on);
    store.write({ sideClosed: on });
    if (on) sideBtn.focus(); else (sideList.querySelector(".chat-sess.active .chat-sess-main") || search).focus();
  }
  function sideButton() {
    // the same breakpoint as the `@container chat (max-width: 719px)` drawer layout
    if (root.clientWidth < 720) toggleSide(!root.classList.contains("side-open")); else setCollapsed(!root.classList.contains("side-collapsed"));
  }
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !root.classList.contains("side-open") || event.defaultPrevented) return;
    if (openPop || event.target.closest?.(".chat-rename")) return;  // a menu or the rename field takes it first
    event.preventDefault();
    toggleSide(false);
    sideBtn.focus();
  });
  // Growing past the narrow layout with the drawer open: the chat is usable again.
  window.addEventListener("resize", () => { if (root.isConnected && main.inert && !narrow()) main.inert = false; });

  // ============================================================ data loading

  const usableDevice = (id) => S.devices.some((d) => d.id === id && d.online !== false);
  /** The default runner: 本机 when it can run, else the first usable device. */
  const defaultDevice = () => (S.devices.find((d) => d.self && d.online !== false) || S.devices.find((d) => d.online !== false) || {}).id;
  /** `fromLink`: the device came from a URL param (Overview card, inbox), else from the stored state. */
  function fallBackToast(wanted, used, { fromLink = false } = {}) {
    if (fellBack) return;
    fellBack = true;
    const d = S.devices.find((item) => item.id === wanted);
    const source = fromLink ? "链接里的运行设备" : "上次的运行设备";
    const name = d ? `“${d.self ? "本机" : d.name || d.id}”` : "";
    const why = d ? `不可用（原因：${d.hint || (d.online === false ? "离线" : "不支持 Agent 对话")}）` : "已不在设备列表里";
    toast(`${source}${name}${why}，已改用${deviceName(used)}`, { type: "info", timeout: 4200 });
  }

  /** Store a fresh device list (sorted, with 本机) and its note. */
  function applyDevices(devices) {
    devices = devices.slice().sort((a, b) => (b.self ? 1 : 0) - (a.self ? 1 : 0) || (b.online ? 1 : 0) - (a.online ? 1 : 0) || String(a.name).localeCompare(String(b.name), "zh-CN"));
    if (opts.localDevice !== false && !devices.some((d) => d.self)) devices.unshift({ id: null, name: "本机", online: true, self: true });
    S.devices = devices;
    // a fresh device list: keep the other devices' folder lists (read at most once a minute), drop the ones gone offline
    rememberNames(devices);
    for (const id of [...targetDirs.keys()]) if (!targets().some((t) => t.id === id && t.online && t.agents)) targetDirs.delete(id);
    if (gatewayCtx() && targetOf(S.context.device)) S.context.name = targetOf(S.context.device).name;
    S.devicesNote = (typeof api.devicesNote === "function" && api.devicesNote()) || "";
    devicesAt = Date.now();
    pollDevices();
    return devices;
  }

  // The gateway link is often still coming up when the page opens (app start, just joined): the list then
  // holds only 本机. Read it again every few seconds while there is a note, and when the menu opens.
  let devicesAt = 0, devicesTimer = null, devicesBusy = false;
  function pollDevices() {
    clearTimeout(devicesTimer);
    devicesTimer = S.devicesNote ? setTimeout(() => { if (alive()) refreshDevices(); }, 5000) : null;
  }
  async function refreshDevices() {
    if (devicesBusy || !alive()) return;
    devicesBusy = true;
    let devices = null;
    try { devices = (await api.devices()) || []; } catch { devices = null; } finally { devicesBusy = false; }
    if (!alive()) return;
    if (!devices) { pollDevices(); return; }
    const before = S.devices.map((d) => `${d.id}:${d.online}:${d.hint || ""}`).join() + S.devicesNote;
    applyDevices(devices);
    if (before !== S.devices.map((d) => `${d.id}:${d.online}:${d.hint || ""}`).join() + S.devicesNote) {
      if (S.device === undefined && S.devices.some((d) => d.online !== false)) { init(); return; }
      renderBar(); renderComposer(); renderWelcome();
    }
  }

  async function init(want = {}) {
    let devices = [];
    try { devices = (await api.devices()) || []; } catch { devices = []; }
    if (!alive()) return;
    devices = applyDevices(devices);
    if (!devices.some((d) => d.online !== false)) {
      S.device = undefined;
      S.loading = false;
      S.deviceError = devices.length ? "现在没有能运行 Agent 对话的设备：设备需要在线，且客户端版本支持 Agent 对话"
        : "还没有设备：在电脑上安装客户端并加入网关后，就能在这里和它上面的 CLI 对话";
      S.sessionsState = "error";
      S.sessionsError = "没有可用的设备";
      renderAll();
      return;
    }
    // URL params win over the stored state (§16.2); a device that is offline or no longer supported falls
    // back to the default once, with a toast.
    const wanted = want.device !== undefined ? want.device : store.read().device;
    let pick = wanted !== undefined && usableDevice(wanted) ? wanted : defaultDevice();
    if (pick === undefined) pick = devices[0].id;
    if (wanted !== undefined && wanted !== pick) fallBackToast(wanted, pick, { fromLink: want.device !== undefined });
    const asked = !!(want.session || want.fresh);
    await selectDevice(pick, { initial: true, restore: !asked });
    if (alive()) await applyWant({ ...want, device: undefined });
  }

  /** Act on URL params ({device, kind, session: {kind, id}, fresh}) once the chat has a device. */
  async function applyWant(want = {}) {
    if (!want || !alive()) return;
    const asked = !!(want.session || want.fresh);
    if (want.device !== undefined && want.device !== S.device) {
      if (usableDevice(want.device)) await selectDevice(want.device, { restore: !asked });
      else if (S.devices.length) { fellBack = false; fallBackToast(want.device, S.device, { fromLink: true }); }
      if (!alive()) return;
    }
    if (want.fresh && (S.session || S.rid)) newChat();
    if (want.session?.id) {
      const { kind, id } = want.session;
      const same = S.session && S.session.id === id && (!kind || S.session.kind === kind);
      const found = S.sessions.find((x) => x.id === id && (!kind || x.kind === kind));
      if (!same) {
        const run = !found && S.runs.find((r) => r.session_id === id && r.state !== "closed");
        if (found) await openSession(found);
        else if (run) attachRun(run);
        else if (kind) await openSession({ kind, id });
      }
    } else if (want.kind && want.kind !== S.kind && agentInfo(want.kind)) {
      S.kind = want.kind; fitPermission(); persist(); renderBar(); renderWelcome(); renderComposer();
    }
    if (asked && want.fresh) input.focus({ preventScroll: true });
  }

  /** Per-device choices ({kind, workdir, permission, preset, context, session, rid}) from the console's store. */
  function prefsFor(id) { return devicePrefs(store.read(), id); }

  async function selectDevice(id, { initial = false, restore = true } = {}) {
    S.gen += 1;
    const gen = S.gen;
    // read this device's choices before the store's top-level fields are re-attributed to it: those belong to
    // the device chosen before (prefsFor falls back to them only when they were that device's own)
    const prefs = prefsFor(id);
    S.device = id;
    store.write({ device: id });
    Object.assign(S, { agents: [], policy: null, dirs: null, presets: [], runs: [], sessions: [], sessionsState: "loading", deviceError: "", loading: true,
      session: null, rid: null, cursor: 0, runState: "idle", sessionId: null, address: null });
    for (const entry of installs.values()) entry.watch?.stop();
    installs.clear();
    S.kind = prefs.kind || null;
    S.workdir = prefs.workdir || "";
    S.permission = PERMISSION_LABEL[prefs.permission] && prefs.permission !== "full" ? prefs.permission : "ask";
    S.preset = prefs.preset || "default";
    adoptContext(prefs.context);
    clearStream();
    renderAll();
    const results = await Promise.allSettled([
      call("agent_info", {}), call("agent_sessions", {}), call("agent_dirs", {}), call("agent_run_list", {}), call("agent_prompts", {}),
    ]);
    if (gen !== S.gen || !alive()) return;
    const [info, sessions, dirs, runs, prompts] = results;
    if (info.status === "rejected") {
      S.deviceError = info.reason?.message || "无法连接这台设备";
      S.loading = false;
      S.sessionsState = "error";
      S.sessionsError = S.deviceError;
      renderAll();
      return;
    }
    S.agents = normalizeAgents(info.value);
    S.policy = info.value?.policy || null;
    if (sessions.status === "fulfilled") { S.sessions = listOf(sessions.value, "sessions"); S.sessionsState = "ready"; }
    else { S.sessionsState = "error"; S.sessionsError = sessions.reason?.message || "读取会话失败"; }
    S.dirs = dirs.status === "fulfilled" ? dirs.value : null;
    S.runs = runs.status === "fulfilled" ? listOf(runs.value, "runs") : [];
    S.presets = prompts.status === "fulfilled" ? listOf(prompts.value, "presets") : [];
    if (!S.kind || !agentInfo(S.kind)) S.kind = (S.agents.find((a) => agentStatus(a).state === "ready") || S.agents[0] || {}).kind || null;
    if (!S.workdir) S.workdir = defaultDir();
    fitPermission({ quiet: true });
    S.loading = false;
    for (const agent of S.agents) if (jobActive(agent.install) && agent.install.job_id) followInstall(agent.kind, agent.install.job_id);
    renderAll();
    // back on the chat (reload, another device, from another page): reopen the conversation that was open --
    // a run that is still alive is followed again (agent_run_read), else the session's history opens.
    const lastRun = prefs.rid && S.runs.find((r) => r.rid === prefs.rid && r.state !== "closed");
    const lastSession = prefs.session && S.sessions.find((x) => x.kind === prefs.session.kind && x.id === prefs.session.id);
    if (!restore) { /* the URL asked for a session or a new chat */ }
    else if (lastSession) {
      const g = S.gen;
      await openSession(lastSession);   // follows the session's live run itself (runFor)
      if (lastRun && S.gen === g + 1 && !S.rid) attachRun(lastRun, { keep: true });
    } else if (lastRun) attachRun(lastRun);
    else if (prefs.session?.id && prefs.session.kind) {
      // not in the list (past its limit, hidden, written late by the CLI) and no live run: its history opens
      await openSession({ kind: prefs.session.kind, id: prefs.session.id }, { restoring: true });
    }
    if (initial && !S.session && !S.rid && root.isConnected) input.focus({ preventScroll: true });
  }

  function listOf(value, key) { return Array.isArray(value) ? value : Array.isArray(value?.[key]) ? value[key] : []; }

  function recentDirs() {
    const seen = new Set(), out = [];
    const push = (path) => { if (path && !seen.has(path)) { seen.add(path); out.push(path); } };
    for (const path of S.dirs?.recent || []) push(typeof path === "string" ? path : path.path);
    // (a gateway session's cwd is only the run device's base, not a folder anyone picked)
    for (const session of S.sessions.slice().sort((a, b) => toSeconds(b.updated_at) - toSeconds(a.updated_at))) if (!session.cwd_missing && !session.context) push(session.cwd);
    return out;
  }
  function defaultDir() {
    return recentDirs()[0] || S.dirs?.home || (S.dirs?.roots || [])[0]?.path || "";
  }

  function persist() {
    const session = S.session ? { kind: S.session.kind, id: S.session.id } : S.sessionId && S.kind ? { kind: S.kind, id: S.sessionId } : null;
    // "full" is never remembered: it needs its explicit confirmation every time
    const prefs = { kind: S.kind, workdir: S.workdir, permission: S.permission === "full" ? "ask" : S.permission, preset: S.preset, context: S.context, session, rid: S.rid || null };
    const st = store.read();
    const byDevice = { ...(st.byDevice && typeof st.byDevice === "object" ? st.byDevice : {}), [S.device ?? "local"]: prefs };
    store.write({ device: S.device, ...prefs, byDevice });
  }

  async function refreshSessions() {
    const gen = S.gen;
    try {
      const value = await call("agent_sessions", {});
      if (gen !== S.gen) return;
      S.sessions = listOf(value, "sessions");
      S.sessionsState = "ready";
    } catch (error) {
      if (gen !== S.gen) return;
      S.sessionsState = "error";
      S.sessionsError = error.message;
    }
    renderSessions();
  }

  async function refreshAgents({ fresh = true } = {}) {
    const gen = S.gen;
    try {
      const value = await call("agent_info", fresh ? { fresh: true } : {});
      if (gen !== S.gen) return;
      S.agents = normalizeAgents(value);
      S.policy = value?.policy || null;
    } catch { /* keep the last answer */ }
    renderBar(); renderWelcome(); renderComposer();
  }

  // ============================================================ rendering: sidebar

  function renderAll() { renderKinds(); renderSessions(); renderBar(); renderWelcome(); renderComposer(); }

  function renderKinds() {
    const counts = {};
    for (const s of S.sessions) if (!s.hidden) { const k = tabKind(s.kind); counts[k] = (counts[k] || 0) + 1; }
    const kinds = KIND_ORDER.filter((k) => counts[k] || S.agents.some((a) => a.kind === k && a.installed));
    for (const k of Object.keys(counts)) if (!kinds.includes(k) && k !== "api") kinds.push(k);
    if (counts.api) kinds.push("api");
    if (S.filterKind !== "all" && !kinds.includes(S.filterKind)) S.filterKind = "all";
    const tab = (key, label, count) => el("button", {
      class: "chat-kind", type: "button", role: "tab", "aria-selected": String(S.filterKind === key),
      onclick: () => { S.filterKind = key; renderKinds(); renderSessions(); },
    }, label);
    mount(kindTabs, tab("all", "全部", 0), kinds.map((k) => tab(k, k === "api" ? "API" : kindShort(k, agentInfo(k)), counts[k] || 0)));
    requestAnimationFrame(kindsEdge);
  }
  // More CLIs than fit: fade the cut edge so the strip reads as scrollable.
  function kindsEdge() {
    const max = kindTabs.scrollWidth - kindTabs.clientWidth;
    kindTabs.classList.toggle("fade-end", max > 2 && kindTabs.scrollLeft < max - 2);
    kindTabs.classList.toggle("fade-start", max > 2 && kindTabs.scrollLeft > 2);
  }
  kindTabs.addEventListener("scroll", kindsEdge, { passive: true });

  // the sidebar's CLI tabs: every API model's sessions share one "API" tab (§16.4)
  function tabKind(kind) { return isApiKind(kind) ? "api" : kind; }
  const kindFilter = (kind) => S.filterKind === "all" || tabKind(kind) === S.filterKind;

  function sessionTitle(s) { return s.alias || s.title || "（无标题）"; }
  function runFor(s) { return S.runs.find((r) => r.session_id && r.session_id === s.id && r.state !== "closed"); }

  function renderSessions() {
    renderKinds();
    if (S.sessionsState === "loading") {
      mount(sideList, Array.from({ length: 5 }, (_, i) => el("div", { class: "chat-sess skeleton-row" },
        el("span", { class: `skeleton ${i % 2 ? "w-60" : "w-80"}` }), el("span", { class: "skeleton w-40", style: { height: "10px" } }))));
      return;
    }
    if (S.sessionsState === "error") {
      mount(sideList, el("div", { class: "chat-side-empty" }, el("p", {}, S.sessionsError || "读取会话失败"),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => (S.device === undefined ? init() : refreshSessions()) }, icon("refresh-cw", { size: 14 }), "重试")));
      return;
    }
    const q = S.query;
    const visible = S.sessions.filter((s) => !s.hidden && kindFilter(s.kind)
      && (!q || [s.alias, s.title, s.cwd, s.context?.workdir, s.context ? targetName(s.context.device) : ""].some((v) => String(v || "").toLowerCase().includes(q))));
    // a run whose session is not on disk yet (new, or a CLI that writes its file late) still gets a row
    const known = new Set(S.sessions.map((x) => `${x.kind}:${x.id}`));
    const pendingRuns = S.runs.filter((r) => r.state !== "closed" && (!r.session_id || !known.has(`${r.kind}:${r.session_id}`))
      && kindFilter(r.kind));
    const byTime = (a, b) => toSeconds(b.updated_at) - toSeconds(a.updated_at);
    const pinned = visible.filter((s) => s.pinned && !s.archived).sort(byTime);
    const recent = visible.filter((s) => !s.pinned && !s.archived).sort(byTime);
    const archived = visible.filter((s) => s.archived).sort(byTime);
    if (!visible.length && !pendingRuns.length) {
      mount(sideList, el("div", { class: "chat-side-empty" },
        el("p", {}, q ? "没有匹配的会话" : S.filterKind === "all" ? "这台设备上还没有会话" : `还没有 ${S.filterKind === "api" ? "API" : kindShort(S.filterKind)} 会话`),
        q ? null : el("p", { class: "faint xs" }, "发出第一条消息后，会话会出现在这里；在终端里用 CLI 聊过的也会被发现。")));
      return;
    }
    const showKind = S.filterKind === "all";
    const groups = [];
    if (pendingRuns.length) groups.push(pendingRuns.map((r) => runItem(r, showKind)));
    if (pinned.length) groups.push(el("div", { class: "chat-group-label" }, xicon("pin", { size: 12 }), "置顶"), pinned.map((s) => sessionItem(s, showKind)));
    if (recent.length) groups.push(pinned.length ? el("div", { class: "chat-group-label" }, "最近") : null, recent.map((s) => sessionItem(s, showKind)));
    if (archived.length) {
      const details = el("details", { class: "chat-archived", open: !!S.archivedOpen || !!q, ontoggle: (e) => { S.archivedOpen = e.currentTarget.open; } },
        el("summary", { class: "chat-group-label" }, icon("chevron-right", { size: 12 }), `已归档 ${archived.length}`),
        archived.map((s) => sessionItem(s, showKind)));
      groups.push(details);
    }
    mount(sideList, groups);
  }

  // A session row's folder: a gateway session lists under its run device like any other, and says whose folder
  // it works on ("⇄ NAS · photos"); the run device's own cwd is only its base then.
  function whereText(ctx, cwd) {
    if (ctx?.mode === "gateway" && ctx.workdir) return `⇄ ${targetName(ctx.device)} · ${shortPath(ctx.workdir)}`;
    return shortPath(cwd) || "—";
  }
  function whereTitle(ctx, cwd) {
    if (ctx?.mode === "gateway" && ctx.workdir) return `经网关操作 ${targetName(ctx.device)} 上的 ${ctx.workdir}`;
    return cwd || "";
  }

  function runItem(run, showKind) {
    const active = S.rid === run.rid;
    const busy = ["starting", "running"].includes(active ? S.runState : run.state);
    return el("div", { class: `chat-sess ${active ? "active" : ""}`, role: "listitem" },
      el("button", { class: "chat-sess-main", type: "button", "aria-current": active ? "true" : null, onclick: () => attachRun(run) },
        showKind ? kindMark(run.kind) : null,
        el("span", { class: "chat-sess-body" },
          el("span", { class: "chat-sess-title" }, busy ? el("span", { class: "chat-live", title: "进行中" }) : null, el("span", { class: "truncate" }, run.title || "新对话")),
          el("span", { class: "chat-sess-sub" }, el("span", { class: "truncate" }, whereText(run.context, run.workdir)), el("span", { class: "chat-sess-time" }, busy ? "进行中" : "空闲")))));
  }

  function sessionItem(s, showKind) {
    const active = S.session && S.session.id === s.id && S.session.kind === s.kind;
    const running = runFor(s);
    const more = el("button", { class: "btn btn-ghost btn-icon btn-sm chat-sess-more", type: "button", "aria-label": `${sessionTitle(s)} 的操作`, "aria-haspopup": "menu",
      onclick: (event) => { event.stopPropagation(); sessionMenu(s, more); } }, icon("ellipsis", { size: 16 }));
    const title = el("span", { class: "chat-sess-title" },
      running && ["starting", "running"].includes(running.rid === S.rid ? S.runState : running.state) ? el("span", { class: "chat-live", title: "进行中" }) : null,
      s.pinned ? xicon("pin", { size: 12, cls: "chat-pin" }) : null,
      el("span", { class: "truncate" }, sessionTitle(s)));
    const node = el("div", { class: `chat-sess ${active ? "active" : ""} ${s.archived ? "archived" : ""}`, role: "listitem", dataset: { id: s.id, kind: s.kind } },
      el("button", { class: "chat-sess-main", type: "button", "aria-current": active ? "true" : null, title: whereTitle(s.context, s.cwd), onclick: () => openSession(s) },
        showKind ? kindMark(s.kind) : null,
        el("span", { class: "chat-sess-body" }, title,
          el("span", { class: "chat-sess-sub" }, el("span", { class: "truncate" }, whereText(s.context, s.cwd)),
            s.subagents > 0 ? el("span", { class: "chat-sess-subagents", title: `${s.subagents} 个子代理会话` }, `子代理 ${s.subagents}`) : null,
            el("span", { class: "chat-sess-time" }, relTime(s.updated_at))))),
      more);
    node.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      // the keyboard's menu key reports (0, 0): anchor to the row's ⋯ button instead
      sessionMenu(s, event.clientX || event.clientY ? { x: event.clientX, y: event.clientY } : more);
    });
    return node;
  }

  function sessionMenu(s, anchor) {
    menu(anchor, [
      { label: "重命名", icon: "pencil", onClick: () => renameInline(s) },
      { label: s.pinned ? "取消置顶" : "置顶", icon: "pin", onClick: () => setMeta(s, { pinned: !s.pinned }, s.pinned ? "已取消置顶" : "已置顶") },
      { label: s.archived ? "取消归档" : "归档", icon: "archive", onClick: () => setMeta(s, { archived: !s.archived }, s.archived ? "已移回最近" : "已归档") },
      { label: "复制会话 ID", icon: "copy", onClick: () => copy(s.id) },
      "sep",
      { label: "从列表移除", icon: "eye-off", onClick: () => removeFromList(s) },
      { label: "删除会话文件…", icon: "trash", danger: true, onClick: () => deleteFile(s) },
    ], { align: "end", label: "会话操作" });
  }

  async function setMeta(s, patch, done) {
    const before = { ...s };
    Object.assign(s, patch);
    renderSessions();
    try {
      await call("agent_session_meta", { kind: s.kind, id: s.id, ...patch });
      if (done) toast(done, { timeout: 1800 });
    } catch (error) {
      Object.assign(s, before);
      renderSessions();
      toast(error.message, { type: "bad", title: "没有保存" });
    }
  }

  function renameInline(s) {
    const row = sideList.querySelector(`.chat-sess[data-id="${CSS.escape(s.id)}"][data-kind="${CSS.escape(s.kind)}"]`);
    if (!row) return;
    const field = el("input", { class: "input chat-rename", value: sessionTitle(s), "aria-label": "会话名称", maxlength: "120" });
    let finished = false;
    const finish = (commit) => {
      if (finished) return;
      finished = true;
      const value = field.value.trim();
      if (commit && value && value !== sessionTitle(s)) setMeta(s, { alias: value }, "已重命名");
      else renderSessions();
    };
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    field.addEventListener("blur", () => finish(true));
    mount(row, field);
    field.focus();
    field.select();
  }

  async function removeFromList(s) {
    await setMeta(s, { hidden: true });
    if (S.session?.id === s.id) newChat();
    toast("已从列表移除（会话文件还在）", { action: { label: "撤销", onClick: () => setMeta(s, { hidden: false }) } });
  }

  async function deleteFile(s) {
    const ok = await confirmDialog({
      title: "删除会话文件？",
      message: `“${sessionTitle(s)}”的记录文件会从 ${deviceName()} 上删除，${kindLabel(s.kind)} 也无法再续接这个会话。此操作不能撤销。`,
      confirmLabel: "删除文件", danger: true,
    });
    if (!ok) return;
    try {
      await call("agent_session_meta", { kind: s.kind, id: s.id, delete_file: true });
      S.sessions = S.sessions.filter((x) => !(x.id === s.id && x.kind === s.kind));
      if (S.session?.id === s.id) newChat();
      renderSessions();
      toast("会话文件已删除");
    } catch (error) {
      toast(error.message, { type: "bad", title: "删除失败" });
    }
  }

  // ============================================================ rendering: head line + the composer's chips

  /** One chip of the composer's settings row: a small icon and a short value; the full text is its name. */
  function chip({ name, value, iconNode = null, onClick, cls = "" }) {
    return el("button", { class: `chat-chip ${cls}`, type: "button", "aria-haspopup": "true", "aria-expanded": "false", title: name, "aria-label": name, onclick: onClick },
      iconNode, el("span", { class: "chat-chip-value" }, typeof value === "string" ? el("span", { class: "truncate" }, value) : value));
  }

  function renderBar() {
    const info = agentInfo();
    const status = info ? installStatus(info) : agentStatus(null);
    const remote = S.devices.find((d) => d.id === S.device)?.self === false;
    const device = chip({ name: `运行设备：${deviceName()}`, value: deviceName(), cls: `chat-chip-device ${deviceName().length <= 6 ? "fit" : ""}`,
      iconNode: icon(remote ? "monitor" : "laptop", { size: 14, cls: "chat-chip-icon" }), onClick: (event) => deviceMenu(event.currentTarget) });
    const agentName = S.kind ? kindShort(S.kind, info) : S.loading ? "检测中…" : "选择 Agent";
    const agent = chip({ name: `Agent：${S.kind ? `${kindLabel(S.kind, info)} · ${status.label}` : agentName}`, cls: `chat-chip-agent ${agentName.length <= 8 ? "fit" : ""}`,
      iconNode: S.kind ? el("span", { class: `dot ${status.tone === "neutral" ? "" : status.tone}`, "aria-hidden": "true" }) : icon("bot", { size: 14, cls: "chat-chip-icon" }),
      value: [el("span", { class: "truncate" }, agentName), S.kind && status.tone !== "ok" ? el("span", { class: "chat-chip-status" }, status.label) : null],
      onClick: (event) => agentMenu(event.currentTarget) });
    const folder = folderChip({ workdir: S.workdir, context: gatewayCtx() ? S.context : null, runnerName: deviceName(), targetName: targetName(S.context.device) });
    const dir = chip({ name: folder.title, cls: `chat-chip-dir ${folder.gateway ? "via" : S.workdir ? "" : "no-dir"} ${folder.gateway && contextProblem() ? "warn" : ""}`,
      value: folder.gateway ? [el("span", { class: "truncate chat-chip-folder" }, folder.value), el("span", { class: "truncate chat-chip-on" }, `· ${folder.device}`)] : folder.value,
      iconNode: xicon(folder.gateway ? "network" : "folder", { size: 14, cls: "chat-chip-icon" }), onClick: (event) => dirMenu(event.currentTarget) });
    const perm = chip({ name: `权限：${permLabel()}`, value: permShort(), cls: `chat-chip-perm ${S.permission === "full" ? "danger" : ""}`,
      iconNode: icon(S.permission === "full" ? "triangle-alert" : "shield-check", { size: 14, cls: "chat-chip-icon" }), onClick: (event) => permissionMenu(event.currentTarget) });
    mount(chips, device, agent, dir, perm);
    requestAnimationFrame(() => chipsEdge());
    renderTitle();
    renderCtxLine();
  }
  // the chip's short form of the permission ("沙箱内自动"); the menu and the chip's name keep the full one
  function permShort() { return S.permission === "ask" && noApprovals() ? NO_APPROVAL_ASK[0].replace(/执行$/, "") : permLabel(); }

  function renderTitle() {
    const text = S.session ? sessionTitle(S.session) : S.rid ? "对话进行中" : "新对话";
    headTitle.textContent = text;
    headTitle.title = S.session?.cwd ? `${text} · ${S.session.cwd}` : text;
  }

  // Chips cut off at the right edge fade out, so the row reads as scrollable (phones scroll it sideways).
  function chipsEdge() {
    const max = chips.scrollWidth - chips.clientWidth;
    chips.classList.toggle("fade-end", max > 2 && chips.scrollLeft < max - 2);
  }
  window.addEventListener("resize", () => { if (root.isConnected) chipsEdge(); });

  function renderCtxLine() {
    const c = S.context;
    const deviceBanner = S.deviceError ? el("div", { class: "chat-ctx bad" }, icon("circle-alert", { size: 14 }),
      el("span", { class: "grow" }, S.device === undefined ? S.deviceError : `${deviceName()} 暂时用不了 Agent 对话：${S.deviceError}`),
      el("button", { class: "btn btn-sm", type: "button", onclick: () => (S.devices.length ? selectDevice(S.device) : init()) }, "重试")) : null;
    // gateway mode: one line saying where the CLI runs and whose folder it works on (and what is in the way)
    const problem = contextProblem();
    const mcpMissing = agentInfo()?.gateway_mcp === false;
    const dirChip = () => chips.querySelector(".chat-chip-dir");
    const gateway = gatewayCtx() ? el("div", { class: `chat-ctx ${problem ? "warn" : ""}` }, icon(problem ? "triangle-alert" : "network", { size: 14 }),
      el("span", { class: "grow chat-ctx-text", title: `CLI 在 ${deviceName()} 上运行，经网关操作 ${targetName(c.device)} 上的 ${c.workdir}` },
        // phones: the run device part is dropped and the target's name is cut short, so the folder always shows
        el("span", { class: "chat-ctx-run" }, "CLI 在 ", el("strong", {}, deviceName()), " 上运行，"), "经网关操作 ",
        el("strong", { class: "chat-ctx-target" }, targetName(c.device)), " 的 ", el("code", {}, shortPath(c.workdir)),
        problem ? `（${problem}）` : ""),
      mcpMissing ? el("span", { class: "chat-ctx-warn", title: `${kindShort(S.kind, agentInfo())} 没有配 Agent Nexus MCP 连接器，调用不了 use_device：在 ${deviceName()} 上添加网关 MCP（网关控制台 → 设置 → AI 客户端接入）` },
        icon("triangle-alert", { size: 13 }), "未检测到网关连接器") : null,
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { const anchor = dirChip(); if (anchor) dirMenu(anchor); } }, "更改"),
      el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": `改回 ${deviceName()} 上的目录`, title: `改回 ${deviceName()} 上的目录`,
        onclick: () => { S.context = directContext(); persist(); renderBar(); renderComposer(); renderWelcome(); } }, icon("x", { size: 14 }))) : null;
    mount(ctxLine, deviceBanner, gateway);
    ctxLine.hidden = !deviceBanner && !gateway;
  }

  function deviceMenu(anchor) {
    if (Date.now() - devicesAt > 10000) refreshDevices();
    menu(anchor, [
      { heading: "CLI 在哪台设备上运行" },
      ...S.devices.map((d) => ({
        label: d.self ? `本机${d.name && d.name !== "本机" ? `（${d.name}）` : ""}` : d.name || d.id,
        checked: d.id === S.device, disabled: d.online === false,
        hint: d.hint || (d.online === false ? "离线" : d.self ? "不经网关" : ""),
        onClick: () => { if (d.id !== S.device) selectDevice(d.id); },
      })),
      ...(S.devicesNote ? ["sep", { heading: S.devicesNote }] : []),
    ], { label: "运行设备" });
  }

  function permissionMenu(anchor) {
    const info = agentInfo();
    const name = kindShort(S.kind, info);
    menu(anchor, PERMISSIONS.map(([key, label, desc]) => {
      const supported = permModes(info).includes(key);
      const blocked = key === "full" && supported && !fullAllowed();
      const relabel = key === "ask" && noApprovals(info);
      return {
        label: relabel ? NO_APPROVAL_ASK[0] : label,
        desc: relabel ? `${name} ${NO_APPROVAL_ASK[1]}` : desc,
        checked: S.permission === key, danger: key === "full",
        disabled: !supported || blocked,
        hint: !supported ? `${name} 不支持` : blocked ? (S.policy?.full_reason || "这台设备不允许") : "",
        onClick: () => { if (key === "full" && S.permission !== "full") confirmFull(); else { S.permission = key; persist(); renderBar(); renderComposer(); } },
      };
    }), { label: "权限" });
  }

  function confirmFull() {
    const check = el("input", { type: "checkbox" });
    const body = el("div", { class: "stack" },
      el("p", { class: "muted" }, `${kindLabel(S.kind)} 将不再请求批准，可以在 ${deviceName()} 上以执行账户的权限运行任何命令、修改或删除任何文件。`),
      el("p", { class: "muted" }, "只在你信任这次任务、并且目录有备份时使用。这个选择只对当前对话有效，不会被记住。"),
      el("label", { class: "check chat-full-check" }, check, "我了解风险，允许完全信任"));
    const dialog = modal({
      title: "使用完全信任模式？", size: "sm", body,
      actions: [
        { label: "取消" },
        { label: "完全信任", variant: "danger solid", onClick: () => {
          if (!check.checked) { toast("先勾选“我了解风险”", { type: "warn" }); return false; }
          S.permission = "full"; renderBar(); renderComposer();
          return true;
        } },
      ],
    });
    return dialog;
  }

  // --- Agent picker: each CLI with status and its action (安装 / 安装中 / 在终端登录)
  function agentMenu(anchor) {
    const list = el("div", { class: "chat-agents", role: "listbox", "aria-label": "选择 Agent" });
    const fill = () => {
      const agents = S.agents.length ? S.agents : KIND_ORDER.map((kind) => ({ kind, installed: false }));
      const cli = agents.filter((a) => !isApiKind(a.kind));
      const models = agents.filter((a) => isApiKind(a.kind));
      mount(list, el("div", { class: "chat-menu-heading" }, `${deviceName()} 上的 CLI`),
        cli.map((info) => agentRow(info, () => pop.close())),
        models.length ? [el("div", { class: "menu-sep" }), el("div", { class: "chat-menu-heading" }, "API 模型"),
          models.map((info) => agentRow(info, () => pop.close()))] : null,
        el("div", { class: "menu-sep" }),
        el("button", { class: "menu-item", type: "button", onclick: () => { refreshAgents().then(() => openPop?.panel === pop.panel && fill()); } }, icon("refresh-cw", { size: 14 }), "重新检测"));
    };
    const pop = popover(anchor, list, { label: "选择 Agent", cls: "chat-agent-pop" });
    fill();
    pop.reposition();
    agentMenu.refill = () => { if (pop.panel.isConnected) fill(); };
  }

  function agentRow(info, close) {
    const status = installStatus(info);
    const selected = info.kind === S.kind;
    let action = null;
    if (status.state === "missing") {
      action = el("button", { class: "btn btn-sm", type: "button", onclick: (event) => { event.stopPropagation(); startInstall(info.kind); } }, icon("download", { size: 13 }), "安装");
    } else if (status.state === "installing") {
      const barNode = progressBar(status.progress);
      action = el("span", { class: "chat-agent-progress" }, barNode, el("span", { class: "xs faint" }, `${status.progress}%`));
    } else if (status.state === "login" && isApiKind(info.kind)) {
      action = el("button", { class: "btn btn-sm", type: "button", onclick: (event) => { event.stopPropagation(); close(); apiSettings(); } }, icon("key-round", { size: 13 }), "设置密钥");
    } else if (status.state === "login") {
      action = el("button", { class: "btn btn-sm", type: "button", onclick: (event) => { event.stopPropagation(); close(); login(info.kind); } }, icon("terminal", { size: 13 }), "在终端登录");
    }
    return el("div", { class: `chat-agent-row ${selected ? "selected" : ""}` },
      el("button", { class: "chat-agent-choose", type: "button", role: "option", "aria-selected": String(selected),
        onclick: () => { S.kind = info.kind; fitPermission(); persist(); close(); renderBar(); renderWelcome(); renderComposer(); input.focus(); } },
        kindMark(info.kind, { size: "md" }),
        el("span", { class: "chat-agent-text" },
          el("span", { class: "strong" }, kindLabel(info.kind, info)),
          isApiKind(info.kind) && info.provider_label ? el("span", { class: "chat-menu-desc" }, info.provider_label) : null,
          el("span", { class: `chat-agent-state tone-${status.tone}` }, status.label, status.unsure ? el("span", { class: "faint" }, " · 登录状态未知") : null)),
        selected ? xicon("check", { size: 15, cls: "chat-agent-check" }) : null),
      action);
  }

  function installStatus(info) {
    const entry = installs.get(info.kind);
    if (entry && jobActive(entry.job)) return { state: "installing", label: `安装中 ${entry.job.progress}%`, tone: "info", progress: entry.job.progress };
    const status = agentStatus(info);
    if (isApiKind(info.kind)) {
      // an API model has nothing to install or sign in to: only its provider's key
      if (status.state === "login" || status.state === "missing") return { ...status, state: "login", label: "未设置密钥", tone: "warn" };
      if (status.state === "ready") return { ...status, label: "就绪", unsure: false };
    }
    return status;
  }

  /** Agent 设置 → API (§16.5), on this runner device. */
  function apiSettings() {
    if (opts.onApiSettings) { opts.onApiSettings(S.device); return; }
    // always name the device: without one the Agent page recalls its own last device (maybe another computer);
    // an empty value is 本机 there
    apiBack = true;
    location.hash = `#/agent?tab=api&device=${encodeURIComponent(S.device ?? "")}`;
  }

  async function startInstall(kind) {
    try {
      const result = await call("agent_install", { kind });
      if (!result.job_id) throw new Error("设备没有返回安装任务");
      followInstall(kind, result.job_id);
    } catch (error) {
      toast(error.message, { type: "bad", title: `无法安装 ${kindLabel(kind)}` });
    }
  }

  function followInstall(kind, jobId) {
    installs.get(kind)?.watch?.stop();
    const entry = { job: { state: "running", progress: 0, lines: [] }, watch: null };
    installs.set(kind, entry);
    const device = S.device;
    entry.watch = watchInstall(api, device, jobId, (job) => {
      if (!alive() || device !== S.device) return;
      entry.job = job;
      agentMenu.refill?.();
      renderBar(); renderWelcome(); renderComposer();
      if (!jobActive(job)) {
        installs.delete(kind);
        if (job.state === "done" || job.state === "ok") toast(`${kindLabel(kind)} 已安装好`, { title: "安装完成" });
        else toast(job.error || "安装命令没有成功", { type: "bad", title: `${kindLabel(kind)} 安装失败`, timeout: 0,
          action: { label: "查看日志", onClick: () => showLog(kind, job.lines) } });
        refreshAgents().then(() => agentMenu.refill?.());
      }
    });
    renderBar(); renderComposer();
  }

  function showLog(kind, lines) {
    drawer({ title: `${kindLabel(kind)} 安装日志`, size: "lg", body: el("pre", { class: "log" }, (lines || []).join("\n") || "（没有输出）") });
  }

  function login(kind) {
    const command = loginCommand(kind, agentInfo(kind));
    if (opts.onLogin) opts.onLogin(kind, S.device, command, agentInfo(kind));
    else copy(command, { label: `已复制登录命令，在 ${deviceName()} 的终端里运行：${command}` });
  }

  // --- directory picker: the run device's folders (recent + workspaces + browse), then every other online device of
  // the network: a folder there makes the run work on it through the gateway (§1 mode gateway), one on the run
  // device works directly.
  function useDirect(path) {
    S.workdir = path;
    S.context = directContext();
    persist(); renderBar(); renderComposer(); renderWelcome();
  }
  function useTarget(t, path) {
    S.context = { mode: "gateway", device: t.id, workdir: path, name: t.name };
    store.write({ targets: rememberTarget(usedTargets(), t.id, path, { name: t.name }) });
    persist(); renderBar(); renderComposer(); renderWelcome();
  }

  /** A device's own folder list (agent_dirs on its AgentHub), read once a minute at most while the menu opens. */
  function loadTargetDirs(t) {
    if (!t.agents || !t.online) return null;
    const have = targetDirs.get(t.id);
    if (have && (have.state === "loading" || Date.now() - have.at < 60000)) return have.state === "loading" ? have.promise : null;
    const entry = { state: "loading", at: Date.now(), value: have?.value || null };
    entry.promise = callOp(api, "agent_dirs", {}, t.call)
      .then((value) => { entry.state = "ready"; entry.value = value; }, (error) => { entry.state = "error"; entry.error = error.message; })
      .finally(() => { entry.at = Date.now(); });
    targetDirs.set(t.id, entry);
    return entry.promise;
  }
  // Listing another device's folders: its AgentHub (agent_dirs, the same roots as the run device's picker) or,
  // for a device without agents/1, the files/1 listing of the console (P2P or the gateway relay).
  const canBrowse = (t) => t.online && (t.agents || (t.files && typeof api.files === "function"));
  const filesLister = (t) => t.files && typeof api.files === "function";
  // agent_dirs failed there (an older AgentHub, agents turned off): browse with files/1 when the device has it
  const agentsListing = (t) => t.agents && !(targetDirs.get(t.id)?.state === "error" && filesLister(t));
  function targetLister(t) {
    if (agentsListing(t)) return (params) => callOp(api, "agent_dirs", params, t.call);
    return async (params) => {
      let listing = await api.files(t.id, params.path || "");
      if (!params.path && listing?.home && listing.home !== listing.path) listing = await api.files(t.id, listing.home);
      const sep = listing.sep || (String(listing.path).includes("\\") ? "\\" : "/");
      const join = (name) => (String(listing.path).endsWith(sep) ? listing.path + name : `${listing.path}${sep}${name}`);
      return { path: listing.path, parent: listing.parent || null,
        dirs: (listing.entries || []).filter((e) => e.dir && !String(e.name).startsWith(".")).map((e) => ({ name: e.name, path: join(e.name) })) };
    };
  }
  async function checkTargetPath(t, path) {
    if (agentsListing(t)) return (await callOp(api, "agent_dirs", { path }, t.call))?.path || path;
    if (t.files && typeof api.files === "function") {
      const listing = await api.files(t.id, path);
      if (listing?.error) throw new Error(listing.error);
      return listing?.path || path;
    }
    if (!/^(~|\/|[A-Za-z]:[\\/])/.test(path)) throw new Error("填写完整路径（例如 ~/projects/web 或 D:\\work）");
    return path;
  }
  function browseTarget(t) {
    browseDirs({ device: t.call, lister: targetLister(t), initial: S.context.device === t.id ? S.context.workdir : null,
      title: `选择 ${t.name} 上的目录`, where: `${t.name} 上的目录；CLI 在 ${deviceName()} 上运行，经网关操作这里。`,
      onPick: (path) => useTarget(t, path) });
  }

  function dirMenu(anchor) {
    const gateway = gatewayCtx();
    const pathInput = (placeholder, label, check, apply) => {
      const field = el("input", { class: "input chat-sm mono chat-dir-input", placeholder, "aria-label": label });
      field.addEventListener("keydown", async (event) => {
        if (event.key !== "Enter" || event.isComposing) return;
        event.preventDefault();
        const typed = field.value.trim();
        if (!typed) return;
        // check it on the device first: a typo or a folder outside the roots is said here, not after the first message
        field.disabled = true;
        try { const path = await check(typed); pop.close(); apply(path); } catch (error) {
          toast(error.message, { type: "warn", title: "不能用这个目录" });
          field.disabled = false;
          field.focus();
        }
      });
      return field;
    };
    const row = (path, name, on, pick) => el("button", { class: `menu-item chat-dir-item ${on ? "on" : ""}`, type: "button", title: path, dataset: { path }, onclick: () => { pop.close(); pick(path); } },
      xicon("folder", { size: 15 }), el("span", { class: "chat-menu-text" }, el("span", { class: "truncate" }, name || folderBase(shortPath(path)) || path),
        el("span", { class: "chat-menu-desc mono truncate" }, shortPath(path))), on ? xicon("check", { size: 14 }) : null);

    const field = pathInput("输入路径，回车使用", "工作目录路径", async (typed) => (await call("agent_dirs", { path: typed }))?.path || typed, useDirect);
    if (!gateway) field.value = S.workdir || "";
    const all = targets();
    // with other devices below, the run device's part stays short (3 recent, 3 workspaces not already among them; 浏览
    // reaches the rest), so the other devices start within the first screen
    const recent = recentDirs().slice(0, all.length ? 3 : 6);
    const workspaces = (S.dirs?.workspaces || []).map((w) => (typeof w === "string" ? { path: w } : w)).filter((w) => w?.path && !recent.includes(w.path))
      .slice(0, all.length ? 3 : Infinity);
    const online = all.filter((t) => t.online);
    const offline = all.filter((t) => !t.online);
    const remote = S.devices.find((d) => d.id === S.device)?.self === false;
    const here = (path) => !gateway && path === S.workdir;

    const sections = new Map();
    const section = (t) => {
      // head and path field are built once; only the folder list below them is redrawn when the device's folders
      // arrive, so typing in the field or a focused button is never replaced under the user
      const fieldId = `chat-dir-field-${++dirFieldSeq}`;
      const pathField = pathInput(`${t.name} 上的路径，回车使用`, `${t.name} 上的目录路径`, (typed) => checkTargetPath(t, typed), (path) => useTarget(t, path));
      const fieldBox = el("div", { class: "chat-dir-field", id: fieldId, hidden: true }, pathField);
      const toggle = el("button", { class: "btn btn-ghost btn-sm chat-dir-act", type: "button", "aria-expanded": "false", "aria-controls": fieldId, title: `输入 ${t.name} 上的路径`,
        onclick: () => {
          fieldBox.hidden = !fieldBox.hidden;
          toggle.setAttribute("aria-expanded", String(!fieldBox.hidden));
          if (!fieldBox.hidden) pathField.focus();
          pop.reposition();
        } }, "输入路径…");
      const list = el("div", { class: "chat-dir-list" });
      const box = el("div", { class: "chat-dir-dev", role: "group", "aria-label": t.name, dataset: { device: t.id } },
        el("div", { class: "chat-dir-dev-head" },
          icon(t.self ? "laptop" : "monitor", { size: 14 }),
          el("span", { class: "chat-dir-dev-name truncate", title: t.name }, t.name),
          toggle,
          canBrowse(t) ? el("button", { class: "btn btn-ghost btn-sm chat-dir-act", type: "button", title: `浏览 ${t.name} 上的目录`,
            onclick: () => { pop.close(); browseTarget(t); } }, "浏览…") : null),
        fieldBox, list);
      const paint = () => {
        const entry = targetDirs.get(t.id);
        const folders = targetFolders(t, { used: usedTargets(), dirs: entry?.value });
        const on = (path) => gateway && S.context.device === t.id && S.context.workdir === path;
        const focused = list.contains(document.activeElement) ? document.activeElement.dataset.path : undefined;
        const failed = entry?.state === "error" ? el("p", { class: "chat-dirs-empty chat-dir-error", role: "status" }, icon("triangle-alert", { size: 12 }),
          ` 读不到这台设备的目录：${entry.error || "未知错误"}`) : null;
        mount(list,
          folders.map((f) => row(f.path, f.name, on(f.path), () => useTarget(t, f.path))),
          failed,
          !folders.length ? el("p", { class: "chat-dirs-empty" }, entry?.state === "loading" ? "读取中…"
            : canBrowse(t) ? (failed ? "可以浏览或输入路径" : "还没有用过的目录：浏览或输入路径") : "这台设备不能浏览：输入它上面的完整路径") : null);
        // a keyboard user on a folder row keeps their place (the same folder, else the first one) across the redraw
        if (focused !== undefined) (Array.from(list.querySelectorAll(".chat-dir-item")).find((b) => b.dataset.path === focused) || list.querySelector(".chat-dir-item") || toggle).focus();
      };
      paint();
      sections.set(t.id, paint);
      return box;
    };

    const info = agentInfo();
    const mcpWarn = info?.gateway_mcp === false ? el("div", { class: "chat-dir-warn" }, icon("triangle-alert", { size: 13 }),
      el("span", {}, `只影响其他设备上的目录：${kindShort(S.kind, info)} 要通过网关的 Agent Nexus MCP 才能操作别的设备，${deviceName()} 上的它还没配这个 MCP（网关控制台 → 设置 → AI 客户端接入）。本机目录不受影响。`)) : null;
    const content = el("div", { class: "chat-dirs" },
      all.length || S.devicesNote ? el("div", { class: "chat-dir-dev-head runner" }, icon(remote ? "monitor" : "laptop", { size: 14 }),
        el("span", { class: "chat-dir-dev-name truncate", title: deviceName() }, deviceName()), el("span", { class: "chat-dir-tag" }, "运行设备"),
        // the other devices are further down: a jump there, with how many are online
        all.length ? el("button", { class: "btn btn-ghost btn-sm chat-dir-act chat-dir-jump", type: "button", title: "跳到其他设备的目录（经网关）",
          onclick: () => { const to = content.querySelector(".chat-dir-others"); to?.scrollIntoView({ block: "start" }); (content.querySelector(".chat-dir-dev .chat-dir-act") || to)?.focus?.(); } },
          icon("network", { size: 12 }), ` 其他设备 ${online.length}`, icon("chevron-down", { size: 12 })) : null) : null,
      el("div", { class: "chat-dir-field" }, field),
      recent.length ? [el("div", { class: "chat-menu-heading" }, "最近使用"), recent.map((p) => row(p, "", here(p), useDirect))] : null,
      workspaces.length ? [el("div", { class: "chat-menu-heading" }, "已登记的工作区"), workspaces.map((w) => row(w.path, w.name, here(w.path), useDirect))] : null,
      !recent.length && !workspaces.length ? el("p", { class: "chat-dirs-empty" }, "还没有最近使用的目录，点“浏览”挑一个。") : null,
      el("button", { class: "menu-item", type: "button", onclick: () => { pop.close(); browseDirs(); } }, icon("folder-open", { size: 15 }), "浏览其他目录…"),
      all.length ? [
        el("div", { class: "menu-sep" }),
        el("div", { class: "chat-menu-heading chat-dir-others" }, icon("network", { size: 12 }), "其他设备 · 经网关"),
        mcpWarn,
        online.map(section),
        !online.length ? el("p", { class: "chat-dirs-empty" }, "网关上没有其他在线设备。") : null,
        offline.length ? el("p", { class: "chat-dirs-empty chat-dir-offline" },
          `不可用：${offline.map((t) => `${t.name}（${t.hint || "离线"}）`).join("、")}`) : null,
      ] : null,
      S.devicesNote ? [el("div", { class: "menu-sep" }), el("p", { class: "chat-dirs-empty" }, icon("network", { size: 12 }), ` ${S.devicesNote}：现在只能用${deviceName()}上的目录。`)] : null);
    const pop = popover(anchor, content, { label: "工作目录", cls: "chat-dir-pop" });
    for (const t of online) {
      const loading = loadTargetDirs(t);
      if (loading) { sections.get(t.id)?.(); loading.then(() => { if (pop.panel.isConnected) { sections.get(t.id)?.(); pop.reposition(); } }); }
    }
  }

  function browseDirs({ initial = S.workdir, onPick = null, title = "选择工作目录", device = S.device, lister = null, where = "" } = {}) {
    let current = null;
    const dirsCall = lister || ((params) => callOp(api, "agent_dirs", params, device));
    const crumbs = el("div", { class: "chat-browse-path mono" });
    const list = el("div", { class: "chat-browse-list", role: "list" });
    const usePick = onPick || useDirect;
    async function go(path) {
      mount(list, el("div", { class: "chat-browse-loading" }, el("span", { class: "spinner" }), "读取中…"));
      let result;
      try { result = await dirsCall(path ? { path } : {}); } catch (error) {
        mount(list, el("div", { class: "chat-browse-loading" }, icon("circle-alert", { size: 16 }), error.message));
        return;
      }
      current = result.path || path || null;
      const dirs = (result.dirs || []).map((d) => (typeof d === "string" ? { name: d.split(/[\\/]/).pop(), path: d } : d));
      const roots = (result.roots || []).map((d) => (typeof d === "string" ? { name: d, path: d } : d));
      mount(crumbs, current ? shortPath(current) : "选择一个起点");
      const items = [];
      if (current && result.parent) items.push(el("button", { class: "chat-browse-item up", type: "button", onclick: () => go(result.parent) }, xicon("corner-left-up", { size: 15 }), "上一级"));
      if (!current) items.push(...roots.map((r) => el("button", { class: "chat-browse-item", type: "button", onclick: () => go(r.path) }, icon("hard-drive", { size: 15 }), el("span", { class: "truncate" }, r.name || r.path), el("span", { class: "faint xs mono truncate" }, shortPath(r.path)))));
      // at the start the device sends the roots twice (as roots and as dirs): list them once
      if (current) items.push(...dirs.map((d) => el("button", { class: "chat-browse-item", type: "button", ondblclick: () => { usePick(d.path); dialog.close(); }, onclick: () => go(d.path) },
        xicon("folder", { size: 15 }), el("span", { class: "truncate" }, d.name), icon("chevron-right", { size: 14, cls: "faint" }))));
      if (current && !dirs.length) items.push(el("p", { class: "chat-browse-loading" }, "这里没有子目录"));
      mount(list, items);
      useBtn.disabled = !current;
    }
    const useBtn = el("button", { class: "btn btn-primary", type: "button", disabled: true, onclick: () => { if (current) { usePick(current); dialog.close(); } } }, "使用此目录");
    const dialog = drawer({ title, description: where || `${deviceName(device)} 上的目录；只能在执行账户的主目录和已登记的工作区里选。`, body: el("div", { class: "chat-browse" }, crumbs, list),
      footLeft: el("button", { class: "btn btn-ghost", type: "button", onclick: () => go(null) }, icon("hard-drive", { size: 15 }), "起点"),
      actions: [] });
    dialog.root.querySelector(".drawer-foot")?.append(el("button", { class: "btn", type: "button", onclick: () => dialog.close() }, "取消"), useBtn);
    if (!dialog.root.querySelector(".drawer-foot")) dialog.root.append(el("div", { class: "drawer-foot" }, el("button", { class: "btn", type: "button", onclick: () => dialog.close() }, "取消"), useBtn));
    go(initial || null);
  }

  // --- ⋯ menu: presets, temp dirs (the work context is the 目录 chip's: a folder on another device = gateway mode)
  function moreMenu(anchor) {
    const presets = S.presets.length ? S.presets : [{ id: "default", name: "默认" }];
    menu(anchor, [
      { heading: "提示词预设" },
      ...presets.map((p) => ({ label: p.name || p.id, checked: S.preset === p.id, onClick: () => { S.preset = p.id; persist(); toast(`下次对话使用“${p.name || p.id}”预设`, { timeout: 1800 }); } })),
      { label: "编辑提示词片段…", icon: "sliders-horizontal", onClick: () => editPrompts() },
      "sep",
      { label: "临时目录…", icon: "folder-open", onClick: () => tmpDrawer() },
      "sep",
      { label: "新对话", icon: "square-pen", onClick: () => newChat() },
    ], { align: "end", label: "更多设置" });
  }

  function editPrompts() {
    if (opts.onEditPrompts) { opts.onEditPrompts(S.device); return; }
    const body = el("div", { class: "chat-prompts-host" });
    drawer({ title: "提示词片段", description: `${deviceName()} · 运行时按顺序拼接，$变量 会被替换`, size: "chat-drawer-xl", body,
      onClose: async () => { try { S.presets = listOf(await call("agent_prompts", {}), "presets"); } catch { /* keep */ } } });
    mountPromptEditor(body, deviceApi(), { kind: S.kind, mode: S.context.mode, preset: S.preset });
  }

  function tmpDrawer() {
    const body = el("div");
    let manager;
    drawer({ title: "临时目录", description: `${deviceName()} 上每次对话的 $tmpdir`, body, onClose: () => manager?.destroy() });
    manager = mountTmpManager(body, api, { device: S.device });
  }

  // ============================================================ rendering: welcome + composer

  function renderWelcome() {
    const empty = !S.session && !S.rid && !stream.childElementCount;
    welcome.hidden = !empty;
    stream.hidden = empty;
    if (!empty) { mount(welcome); return; }
    if (S.loading) {
      mount(welcome, el("div", { class: "chat-welcome" }, el("div", { class: "skeleton title" }), el("div", { class: "skeleton w-60" })));
      return;
    }
    root.classList.toggle("empty", S.device === undefined);
    if (S.device === undefined) {
      // No device can run the chat (cloud console: none joined, all offline or too old). A compact card with
      // the next step, so it does not push the onboarding below the fold.
      const unusable = S.devices.filter((d) => d.hint && d.hint !== "离线" && d.hint !== "已吊销");
      const offline = S.devices.filter((d) => d.online === false && d.hint === "离线");
      const detail = !S.devices.length
        ? "Agent 对话运行在你的电脑上：安装客户端、用邀请码加入网关后，就能在这里用它上面的 Claude Code、Codex 等 CLI。"
        : [offline.length ? `${offline.length} 台设备离线` : "", unusable.length ? `${unusable.length} 台设备的客户端需要更新（或没有开放远程 Agent）` : ""]
          .filter(Boolean).join("，") + "。设备在线并且客户端支持后，就能在这里和它上面的 CLI 对话。";
      mount(welcome, el("div", { class: "chat-welcome chat-empty" },
        el("div", { class: "chat-welcome-head" }, el("h2", {}, S.devices.length ? "现在没有能对话的设备" : "还没有设备"), el("p", {}, detail)),
        el("div", { class: "chat-empty-actions" },
          S.devices.length ? el("button", { class: "btn btn-sm", type: "button", onclick: () => init() }, icon("refresh-cw", { size: 14 }), "重新检查") : null,
          unusable.length ? el("a", { class: "btn btn-sm", href: opts.downloadHref || "#/downloads" }, icon("download", { size: 14 }), "更新客户端") : null,
          !S.devices.length ? el("a", { class: "btn btn-sm", href: opts.downloadHref || "#/downloads" }, icon("download", { size: 14 }), "下载客户端") : null,
          el("a", { class: "btn btn-sm btn-primary", href: opts.inviteHref || "#/devices?invite=1" }, icon("ticket", { size: 14 }), "邀请新设备"))));
      return;
    }
    const agents = S.agents.length ? S.agents : [];
    const info = agentInfo();
    mount(welcome, el("div", { class: "chat-welcome" },
      el("div", { class: "chat-welcome-head" },
        el("h2", {}, "开始一个新对话"),
        el("p", {}, S.kind
          ? ["在 ", el("strong", {}, deviceName()), " 上用 ", el("strong", {}, kindLabel(S.kind, info)),
            gatewayCtx() ? ["，经网关在 ", el("strong", {}, targetName(S.context.device)), " 的 ", el("code", {}, shortPath(S.context.workdir)), " 里工作。"]
              : S.workdir ? ["，在 ", el("code", {}, shortPath(S.workdir)), " 里工作。"] : "。先选一个工作目录。"]
          : "先选一个 Agent。")),
      agents.length ? el("div", { class: "chat-welcome-agents", role: "radiogroup", "aria-label": "Agent" }, agents.map((a) => {
        const st = installStatus(a);
        const on = a.kind === S.kind;
        return el("button", { class: `chat-welcome-agent ${on ? "on" : ""}`, type: "button", role: "radio", "aria-checked": String(on),
          onclick: () => { S.kind = a.kind; fitPermission(); persist(); renderBar(); renderWelcome(); renderComposer(); } },
          kindMark(a.kind, { size: "md" }),
          el("span", { class: "chat-agent-text" }, el("span", { class: "strong" }, kindShort(a.kind, a)), el("span", { class: `chat-agent-state tone-${st.tone}` }, st.label)),
          st.state === "installing" ? progressBar(st.progress) : null);
      })) : null,
      el("div", { class: "chat-examples" }, EXAMPLES.map(([title, text]) => el("button", { class: "chat-example", type: "button",
        onclick: () => { input.value = text; autoGrow(); input.focus(); } }, el("span", { class: "chat-example-title" }, title), el("span", { class: "chat-example-text" }, text)))),
      el("p", { class: "chat-welcome-foot" }, "左侧会列出这台设备上已有的会话，包括你在终端里和 CLI 聊过的；点一个就能接着聊。")));
  }

  function blockedReason() {
    if (S.deviceError) return { text: S.device === undefined ? "没有可用的设备" : `${deviceName()} 暂时不可用`, action: null };
    if (S.loading) return { text: "正在连接设备…", action: null };
    if (!S.kind) return { text: "这台设备上还没有可用的 Agent", action: null };
    const info = agentInfo();
    const status = installStatus(info || { kind: S.kind });
    const reason = notReadyReason(info ? { ...info, install: installs.get(S.kind)?.job || info.install } : { kind: S.kind });
    if (status.state === "missing") return { text: reason, action: { label: "安装", icon: "download", onClick: () => startInstall(S.kind) } };
    if (status.state === "installing") return { text: reason, action: null };
    if (status.state === "login" && isApiKind(S.kind)) return { text: `${kindLabel(S.kind, info)} 的服务商还没有设置密钥`, action: { label: "设置密钥", icon: "key-round", onClick: () => apiSettings() } };
    if (status.state === "login") return { text: reason, action: { label: "在终端登录", icon: "terminal", onClick: () => login(S.kind) } };
    if (reason) return { text: reason, action: null };
    if (S.session?.cwd_missing && !S.rid) {
      return { text: "这个会话的目录已不存在，只能查看记录", action: { label: "在其他目录新开对话", icon: "folder-open", onClick: () => { newChat(); browseDirs(); } } };
    }
    const problem = contextProblem();
    if (problem) return { text: problem, action: { label: `改用${deviceName()}上的目录`, icon: "folder-open", onClick: () => { S.context = directContext(); persist(); renderBar(); renderComposer(); renderWelcome(); } } };
    if (!S.workdir && !gatewayCtx()) return { text: "先选择工作目录", action: { label: "选择目录", icon: "folder-open", onClick: () => browseDirs() } };
    return null;
  }

  function renderComposer() {
    const blocked = blockedReason();
    const running = ["starting", "running"].includes(S.runState);
    const name = S.kind ? kindShort(S.kind, agentInfo()) : "Agent";
    input.disabled = !!blocked && !S.rid;
    // the key hint only where there is a keyboard (phones have no Shift); the title keeps it everywhere
    const keys = window.matchMedia?.("(pointer: fine)").matches ? "（Enter 发送，Shift+Enter 换行）" : "";
    input.placeholder = running ? `${name} 正在工作，可以先写下一条…` : `给 ${name} 发消息${keys}`;
    input.title = "Enter 发送 · Shift+Enter 换行";
    stopBtn.hidden = !running;
    sendBtn.disabled = (!!blocked && !S.rid) || running || !input.value.trim();
    sendBtn.title = running ? "等这一轮结束后发送" : "发送（Enter）";
    composer.classList.toggle("blocked", !!blocked && !S.rid);
    renderTitle();
    // One small line above the input, only for state that matters now (the permission shows in its chip).
    if (blocked && !S.rid) {
      mount(composerNote, el("span", { class: "chat-note-warn" }, icon("info", { size: 14 }), blocked.text),
        blocked.action ? el("button", { class: "btn btn-sm", type: "button", onclick: blocked.action.onClick }, icon(blocked.action.icon, { size: 13 }), blocked.action.label) : null);
    } else {
      mount(composerNote,
        S.session && !S.rid ? el("span", { class: "chat-note-resume" }, canResume(S.session.kind)
          ? "将续接这个会话" : `${kindShort(S.session.kind, agentInfo(S.session.kind))} 不支持续接，发消息会开一个新对话`) : null,
        gatewayCtx() && agentInfo()?.gateway_mcp === false
          ? el("span", { class: "chat-note-warn", title: `经网关模式需要 ${name} 配置 Agent Nexus MCP 连接器，否则它调用不了 use_device` },
            icon("triangle-alert", { size: 13 }), `${name} 没有配置网关连接器`) : null);
    }
    composerNote.hidden = !composerNote.childElementCount;
  }

  // ============================================================ the event stream

  const blocks = new Map();     // key -> {node, ev}
  let lastBlock = null;         // {type, role, key, ev, node}
  const pendingUser = [];
  const notices = new Set();    // §16.6: one line per notice text per session
  let turnUsage = null;         // the last live usage event's `turn`, for the "完成 · 输入 … 输出 …" line
  let apiBack = false;          // left for Agent 设置 → API (设置密钥)

  function clearStream() {
    blocks.clear();
    lastBlock = null;
    pendingUser.length = 0;
    notices.clear();
    turnUsage = null;
    S.usage = null;
    renderUsage();
    mount(stream);
    setWorking(false);
    stick = true;
    jump.hidden = true;
  }

  function put(key, node, ev, { at = null } = {}) {
    const existing = key ? blocks.get(key) : null;
    if (existing) {
      keepOpen(existing.node, node);
      existing.node.replaceWith(node);
      existing.node = node;
      existing.ev = ev;
      return existing;
    }
    if (Number(ev.t)) node.dataset.t = String(Number(ev.t));
    const entry = { node, ev, key, type: ev.type, role: ev.role };
    if (key) blocks.set(key, entry);
    if (at !== null) {
      const later = Array.from(stream.children).find((child) => Number(child.dataset.t) > at);
      if (later) { stream.insertBefore(node, later); return entry; }
    }
    stream.append(node);
    lastBlock = entry;
    return entry;
  }

  function keepOpen(oldNode, newNode) {
    if (oldNode.tagName === "DETAILS" && newNode.tagName === "DETAILS" && oldNode.open) newNode.open = true;
  }

  let seqKey = 0;
  function ingest(ev, { history = false } = {}) {
    if (!ev || typeof ev !== "object") return;
    const t = Number(ev.t) || null;
    switch (ev.type) {
      case "user": {
        const index = pendingUser.indexOf(String(ev.text || "").trim());
        if (index >= 0) { pendingUser.splice(index, 1); return; }
        put(null, userNode(ev.text), ev);
        break;
      }
      case "message": {
        if (ev.role === "user") { ingest({ ...ev, type: "user" }, { history }); return; }
        // a block with an id: its deltas append, its whole (non-delta) event replaces the text -- also when another
        // block came in between (an API step's whole thinking and text arrive after both streamed, §16.4)
        const own = ev.id && !history ? blocks.get(`m:${ev.id}`) : null;   // (history: whole events only; ids of older sessions repeat)
        const tail = !ev.id && lastBlock && lastBlock.type === "message" && (ev.delta || ev.partial) ? lastBlock : null;
        const entry = own || tail;
        if (entry) {
          const text = own && !ev.delta && !ev.partial ? ev.text : `${entry.ev.text || ""}${ev.text || ""}`;
          entry.ev = { ...entry.ev, text };
          scheduleText(entry);
        } else {
          put(ev.id && !history ? `m:${ev.id}` : `m${seqKey++}`, assistantNode(ev.text), { ...ev });
        }
        break;
      }
      case "thinking": {
        const own = ev.id && !history ? blocks.get(`k:${ev.id}`) : null;   // (history: whole events only; ids of older sessions repeat)
        const tail = !ev.id && lastBlock && lastBlock.type === "thinking" && (ev.delta || ev.partial) ? lastBlock : null;
        const entry = own || tail;
        if (entry) {
          entry.ev = { ...entry.ev, text: own && !ev.delta && !ev.partial ? ev.text : `${entry.ev.text || ""}${ev.text || ""}` };
          const fresh = thinkingNode(entry.ev.text, !history);
          keepOpen(entry.node, fresh);
          entry.node.replaceWith(fresh);
          entry.node = fresh;
        } else put(ev.id && !history ? `k:${ev.id}` : `k${seqKey++}`, thinkingNode(ev.text, !history), ev);
        break;
      }
      case "tool": {
        const key = `tool:${ev.id || seqKey++}`;
        const prev = blocks.get(key)?.ev || {};
        const merged = { ...prev, ...ev };
        put(key, toolNode(merged), merged);
        break;
      }
      case "file_change": put(`fc${seqKey++}`, fileNode(ev), ev); break;
      case "approval": {
        const key = `ap:${ev.id}`;
        const merged = { ...(blocks.get(key)?.ev || {}), ...ev };
        put(key, approvalNode(merged), merged);
        if (merged.state === "pending" && !history) live.textContent = `需要你的批准：${merged.tool || ""} ${merged.summary || ""}`;
        break;
      }
      case "ask": {
        const ask = normalizeAsk(ev);
        const key = `ask:${ask.ask_id}`;
        const merged = { ...(blocks.get(key)?.ev || {}), ...ask };
        put(key, askNode(merged), merged, { at: blocks.has(key) ? null : t });
        break;
      }
      case "mail": put(`mail:${ev.message_id || seqKey++}`, mailNode(ev), ev, { at: blocks.has(`mail:${ev.message_id}`) ? null : t }); break;
      case "usage": {
        // the ring shows a real context report only (a turn's tokens alone are not the context's size, §16.3)
        if (ev.context && typeof ev.context === "object" && ev.context.used != null) { S.usage = { ...ev.context }; renderUsage(); }
        // a history turn's figures never label the next live turn's end line
        if (ev.turn && typeof ev.turn === "object" && !history) turnUsage = ev.turn;
        return;   // nothing in the stream itself: the ring and the turn line show it
      }
      case "status": {
        if (ev.session_id && ev.session_id !== S.sessionId) { S.sessionId = ev.session_id; if (!history) persist(); }
        if (ev.address && ev.address !== S.address) { S.address = ev.address; startConversation(); }
        if (!history) setRunState(ev.state);
        if (ev.note && !history) put(null, el("div", { class: "chat-turn-end chat-note-line" }, icon("info", { size: 13 }), el("span", {}, ev.note)), ev);
        if (["done", "failed", "stopped"].includes(ev.state)) {
          const usage = ev.usage || turnUsage;
          turnUsage = null;
          if (usage || ev.state !== "done") put(null, turnEndNode({ ...ev, usage }), ev);
        }
        break;
      }
      case "error": {
        const text = String(ev.text || "").trim() || "出错了";
        if (ev.level === "notice") {   // the CLI's own config hints (§16.6): grey, one line, said once
          if (notices.has(text)) return;
          notices.add(text);
          put(null, noticeNode(text), ev);
          break;
        }
        const warn = ev.level === "warn";
        put(null, el("div", { class: `chat-error ${warn ? "warn" : ""}`, role: history ? null : warn ? "status" : "alert" },
          icon(warn ? "triangle-alert" : "circle-alert", { size: 16 }), el("div", {}, text)), ev);
        break;
      }
      default: break;
    }
    if (ev.type === "approval" || ev.type === "ask") setWorking(["starting", "running"].includes(S.runState));
    renderWelcome();
    queueScroll();
  }

  // assistant text re-renders at most once per frame while it streams
  const textQueue = new Set();
  function scheduleText(entry) {
    textQueue.add(entry);
    if (textQueue.size > 1) return;
    requestAnimationFrame(() => {
      for (const item of textQueue) {
        const fresh = assistantNode(item.ev.text);
        item.node.replaceWith(fresh);
        item.node = fresh;
      }
      textQueue.clear();
      queueScroll();
    });
  }

  function userNode(text) {
    return el("div", { class: "chat-user" }, el("div", { class: "chat-bubble" }, String(text || "")));
  }
  function assistantNode(text) {
    return el("div", { class: "chat-assistant" }, renderMarkdown(text));
  }
  function thinkingNode(text, live) {
    const first = String(text || "").trim().split("\n")[0] || "";
    return el("details", { class: "chat-think" },
      el("summary", {}, xicon("lightbulb", { size: 14 }), el("span", { class: "chat-think-label" }, "思考过程"), el("span", { class: "chat-think-peek truncate" }, first), icon("chevron-right", { size: 14, cls: "chat-caret" })),
      el("div", { class: "chat-think-body" }, String(text || "")));
  }
  function statusIcon(status) {
    if (status === "running" || status === "pending" || status === "in_progress") return el("span", { class: "spinner chat-tool-spin", "aria-label": "运行中" });
    if (status === "error" || status === "failed") return icon("circle-x", { size: 15, cls: "chat-tool-bad", label: "失败" });
    return icon("circle-check", { size: 15, cls: "chat-tool-ok", label: "完成" });
  }
  function toolNode(ev) {
    const inputText = formatInput(ev.input);
    const output = String(ev.output ?? "");
    const hasBody = !!(inputText || output);
    const summary = el("summary", {}, statusIcon(ev.status),
      el("span", { class: "chat-tool-name" }, ev.name || "工具"),
      el("span", { class: "chat-tool-title truncate" }, ev.title || (typeof ev.input?.command === "string" ? ev.input.command : "")),
      ev.status === "error" ? el("span", { class: "chat-tool-flag" }, "失败") : null,
      hasBody ? icon("chevron-right", { size: 14, cls: "chat-caret" }) : null);
    return el("details", { class: `chat-tool ${hasBody ? "" : "no-body"}`, dataset: { status: ev.status || "ok" } }, summary,
      hasBody ? el("div", { class: "chat-tool-body" },
        inputText && inputText !== ev.title ? el("pre", { class: "chat-tool-input" }, inputText) : null,
        output ? el("pre", { class: "chat-tool-output" }, output) : el("div", { class: "faint xs" }, ev.status === "running" ? "等待输出…" : "没有输出")) : null);
  }
  function fileNode(ev) {
    const { add, del } = diffStats(ev.diff);
    const kind = { add: ["新增", "ok"], delete: ["删除", "bad"], edit: ["修改", "info"] }[ev.kind] || ["修改", "info"];
    return el("details", { class: "chat-tool chat-file" },
      el("summary", {}, xicon("file-diff", { size: 15, cls: "faint" }),
        el("span", { class: `chat-file-kind tone-${kind[1]}` }, kind[0]),
        el("span", { class: "chat-tool-title mono truncate", title: ev.path }, ev.path || ""),
        ev.diff ? el("span", { class: "chat-file-stat" }, el("span", { class: "add" }, `+${add}`), el("span", { class: "del" }, `−${del}`)) : null,
        ev.diff ? el("span", { class: "chat-file-toggle" }, "查看改动", icon("chevron-right", { size: 14, cls: "chat-caret" })) : null),
      ev.diff ? el("div", { class: "chat-tool-body" }, diffView(ev.diff)) : null);
  }
  function approvalNode(ev) {
    const state = ev.state || "pending";
    if (state !== "pending") {
      const allowed = state === "allowed";
      return el("div", { class: `chat-approval done ${allowed ? "allowed" : "denied"}` },
        icon(allowed ? "circle-check" : "circle-x", { size: 15 }),
        el("span", {}, allowed ? (ev.remembered ? "已总是允许" : "已允许") : ev.timeout ? "超时未批准，已拒绝" : "已拒绝"),
        el("span", { class: "chat-tool-name" }, ev.tool || ""),
        el("span", { class: "chat-tool-title truncate" }, ev.summary || ""));
    }
    const canRemember = ev.remember === true || (Array.isArray(ev.options) && ev.options.some((o) => /always/.test(o.kind || o)));
    const answer = async (allow, remember, button) => {
      card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      button.prepend(el("span", { class: "spinner" }));
      try {
        await call("agent_run_answer", { rid: S.rid, approval_id: ev.id, allow, remember: !!remember });
        ingest({ type: "approval", id: ev.id, state: allow ? "allowed" : "denied", remembered: !!remember });
      } catch (error) {
        toast(error.message, { type: "bad", title: "没有送达" });
        card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        button.querySelector(".spinner")?.remove();
      }
    };
    const card = el("div", { class: "chat-approval", role: "group", "aria-label": "需要批准" },
      el("div", { class: "chat-approval-head" }, xicon("shield-alert", { size: 16 }),
        el("span", { class: "strong" }, "需要你的批准"),
        el("span", { class: "chat-tool-name" }, ev.tool || "")),
      ev.summary ? el("pre", { class: "chat-approval-cmd" }, ev.summary) : null,
      ev.reason ? el("p", { class: "small muted" }, ev.reason) : null,
      el("div", { class: "chat-approval-actions" },
        el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: (e) => answer(true, false, e.currentTarget) }, "允许"),
        canRemember ? el("button", { class: "btn btn-sm", type: "button",
          title: ev.remember_scope === "command" ? "本次对话里完全相同的命令不再询问（别的命令仍会询问）"
            : ev.remember_scope === "tool" ? `本次对话里 ${ev.tool || "这个工具"} 不再询问` : "按 Agent 自己的规则记住这类操作",
          onclick: (e) => answer(true, true, e.currentTarget) }, ev.remember_scope === "command" ? "总是允许此命令" : "总是允许") : null,
        el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: (e) => answer(false, false, e.currentTarget) }, "拒绝"),
        el("span", { class: "chat-approval-hint" }, S.rid ? "10 分钟内不回应会自动拒绝" : "")));
    return card;
  }
  function normalizeAsk(ask) {
    return { ...ask, type: "ask", ask_id: ask.ask_id || ask.id, title: ask.title || ask.question || "", state: ask.state || "open" };
  }
  function askNode(ask) {
    const card = el("div", { class: "chat-ask", role: "group", "aria-label": "Agent 的提问" },
      el("div", { class: "chat-ask-head" }, icon("circle-question-mark", { size: 16 }), el("span", { class: "chat-ask-kicker" }, "Agent 在问你"),
        ask.from ? el("span", { class: "chat-ask-from truncate", title: ask.from }, ask.from) : null),
      el("div", { class: "chat-ask-title" }, ask.title),
      ask.context ? el("div", { class: "chat-ask-context" }, renderMarkdown(ask.context)) : null);
    if (ask.state !== "open") {
      const ans = ask.answer;
      const text = typeof ans === "string" ? ans : ans?.text || ans?.choice || (ans?.choices || []).join("、") || "";
      card.classList.add("answered");
      card.append(el("div", { class: "chat-ask-answer" }, icon("check", { size: 14 }), el("span", {}, ask.state === "answered" ? "已回答" : "已关闭"), text ? el("strong", {}, text) : null));
      return card;
    }
    const options = Array.isArray(ask.options) ? ask.options.map((o) => (typeof o === "string" ? o : o.label || o.value)) : [];
    if (ask.recommended && options.includes(ask.recommended)) options.sort((a, b) => (b === ask.recommended) - (a === ask.recommended));
    const multi = ask.kind === "multi";
    const chosen = new Set();
    const optionButtons = options.map((value) => el("button", { class: "chat-opt", type: "button", "aria-pressed": "false",
      onclick: (event) => {
        const btn = event.currentTarget;
        if (!multi) { chosen.clear(); optionButtons.forEach((b) => b.setAttribute("aria-pressed", "false")); }
        if (chosen.has(value)) { chosen.delete(value); btn.setAttribute("aria-pressed", "false"); } else { chosen.add(value); btn.setAttribute("aria-pressed", "true"); }
        submit.disabled = !chosen.size && !other.value.trim();
      } }, value, value === ask.recommended ? el("span", { class: "chat-opt-rec" }, "推荐") : null));
    const other = el("input", { class: "input chat-sm", placeholder: options.length ? "或者直接写下你的回答" : "你的回答", "aria-label": "回答",
      oninput: () => { submit.disabled = !chosen.size && !other.value.trim(); },
      onkeydown: (event) => { if (event.key === "Enter" && !event.isComposing && !submit.disabled) { event.preventDefault(); submit.click(); } } });
    const submit = el("button", { class: "btn btn-sm btn-primary", type: "button", disabled: true, onclick: async () => {
      const text = other.value.trim();
      const answer = text ? { text } : multi ? { choices: Array.from(chosen) } : { choice: Array.from(chosen)[0] };
      submit.disabled = true;
      try {
        const result = await api.answerAsk(ask.ask_id, answer);
        if (result?.error) throw new Error(result.error);
        ingest({ ...ask, state: "answered", answer });
        toast(result?.already ? "已经有人回答过了" : "已回答，Agent 会继续", { type: result?.already ? "warn" : "ok", timeout: 2000 });
      } catch (error) {
        submit.disabled = false;
        toast(error.message || "回答没有送达", { type: "bad" });
      }
    } }, "回答");
    card.append(
      options.length ? el("div", { class: "chat-opts", role: multi ? "group" : "radiogroup" }, optionButtons) : null,
      el("div", { class: "chat-ask-reply" }, other, submit));
    return card;
  }
  function mailNode(ev) {
    const body = String(ev.body || "");
    const long = body.length > 600;
    const bodyNode = el("div", { class: `chat-mail-body ${long ? "clamped" : ""}` }, renderMarkdown(body));
    return el("div", { class: "chat-mail" },
      el("div", { class: "chat-mail-head" }, icon("mail", { size: 15 }),
        el("span", { class: "mono small" }, ev.from || ""), el("span", { class: "faint xs" }, "→ 你"),
        el("span", { class: "spacer" }), el("span", { class: "faint xs" }, ev.t ? relTime(ev.t) : "")),
      ev.subject ? el("div", { class: "chat-mail-subject" }, ev.subject) : null,
      body ? bodyNode : null,
      long ? el("button", { class: "btn btn-ghost btn-sm chat-mail-more", type: "button", onclick: (e) => { bodyNode.classList.remove("clamped"); e.currentTarget.remove(); } }, "展开全文") : null);
  }
  function noticeNode(text) {
    const first = text.split("\n")[0];
    const more = text.length > first.length || first.length > 90;
    if (!more) return el("div", { class: "chat-notice" }, icon("info", { size: 13 }), el("span", { class: "chat-notice-text" }, first));
    return el("details", { class: "chat-notice" },
      el("summary", {}, icon("info", { size: 13 }), el("span", { class: "chat-notice-text truncate" }, first), icon("chevron-right", { size: 12, cls: "chat-caret" })),
      el("pre", { class: "chat-notice-body" }, text));
  }

  function renderUsage() {
    const view = usageView(S.usage);
    usageNode.hidden = !view;
    if (!view) { mount(usageNode); usageNode.removeAttribute("title"); usageNode.removeAttribute("aria-label"); return; }
    usageNode.className = `chat-usage ${view.tone} ${view.pct === null ? "count" : ""}`;
    usageNode.title = view.title;
    usageNode.setAttribute("aria-label", `上下文${view.title}`);
    mount(usageNode, view.pct === null ? el("span", { class: "chat-usage-num" }, tokenCount(S.usage.used)) : usageRing(view));
  }

  function turnEndNode(ev) {
    const usage = ev.usage || {};
    const parts = [];
    if (ev.state === "failed") parts.push("这一轮失败了");
    else if (ev.state === "stopped") parts.push("已停止");
    else parts.push("完成");
    if (usage.input) parts.push(`输入 ${compact(usage.input)}`);
    if (usage.output) parts.push(`输出 ${compact(usage.output)}`);
    if (usage.cost_usd) parts.push(`$${Number(usage.cost_usd).toFixed(usage.cost_usd < 0.01 ? 4 : 2)}`);
    return el("div", { class: `chat-turn-end ${ev.state}` }, el("span", {}, parts.join(" · ")));
  }

  function setRunState(state) {
    if (!state) return;
    const prev = S.runState;
    S.runState = state;
    const running = state === "starting" || state === "running";
    setWorking(running);
    if (prev !== state && ["done", "failed", "stopped", "idle"].includes(state) && ["starting", "running"].includes(prev)) {
      live.textContent = state === "failed" ? "这一轮失败了" : "这一轮已结束";
      refreshSessions();
      refreshRuns();
    } else if (prev !== state) renderSessions();  // the row's live dot follows the run
    renderComposer();
  }
  function setWorking(on) {
    working.hidden = !on;
    const waiting = Array.from(blocks.values()).some((b) => (b.ev.type === "approval" && b.ev.state === "pending") || (b.ev.type === "ask" && b.ev.state === "open"));
    working.classList.toggle("waiting", waiting);
    working.lastChild.textContent = !on ? "" : waiting ? "等你回应后继续" : `${kindShort(S.kind, agentInfo())} ${S.runState === "starting" ? "正在启动…" : "正在工作…"}`;
    if (on) stream.after(working);
  }

  async function refreshRuns() {
    try { S.runs = listOf(await call("agent_run_list", {}), "runs"); } catch { /* keep */ }
    renderSessions();
  }

  // ============================================================ sessions and runs

  function newChat() {
    S.gen += 1;
    stopConversation();
    Object.assign(S, { session: null, rid: null, cursor: 0, runState: "idle", sessionId: null, address: null });
    persist();
    clearStream();
    toggleSide(false);
    renderSessions(); renderWelcome(); renderComposer();
    input.focus();
  }

  async function openSession(s, { restoring = false } = {}) {
    S.gen += 1;
    const gen = S.gen;
    stopConversation();
    Object.assign(S, { session: s, rid: null, cursor: 0, runState: "idle", sessionId: s.id, address: s.address || null, kind: s.kind || S.kind });
    // the session's context comes back with it: a gateway session works on its device's folder again (its cwd is
    // only the run device's base then), a direct one on its folder -- a dead folder never becomes the next chat's
    adoptContext(s.context);
    // the run device's folder is the session's own in either mode (a gateway session's cwd is where its CLI ran):
    // going back to direct mode then resumes it where Claude keeps it, never in the previous chat's folder
    if (s.cwd && !s.cwd_missing) S.workdir = s.cwd;
    fitPermission({ quiet: true });
    persist();
    clearStream();
    toggleSide(false);
    renderSessions(); renderBar(); renderComposer();
    welcome.hidden = true; stream.hidden = false;
    mount(stream, el("div", { class: "chat-history-loading" }, el("span", { class: "spinner" }), "读取会话记录…"));
    let events = [], result = {};
    try {
      result = await call("agent_session_read", { kind: s.kind, id: s.id });
      events = listOf(result, "events");
      if (result.address) S.address = result.address;
    } catch (error) {
      if (gen !== S.gen) return;
      if (restoring && error.code === "not_found") {
        // the remembered session is gone (deleted): forget it and start fresh, without an error
        Object.assign(S, { session: null, sessionId: null, address: null });
        persist(); clearStream(); renderSessions(); renderWelcome(); renderComposer();
        return;
      }
      mount(stream, el("div", { class: "chat-error" }, icon("circle-alert", { size: 16 }), el("div", {}, `读不到这个会话的记录：${error.message}。${canResume(s.kind) ? "仍然可以发消息续接。" : ""}`)));
      renderComposer();
      return;
    }
    if (gen !== S.gen) return;
    mount(stream);
    const run = runFor(s);
    const since = run && Number(run.started_at) ? toSeconds(run.started_at) : null;
    for (const ev of events) if (!since || !ev.t || toSeconds(ev.t) < since) ingest(ev, { history: true });
    if (result.note) stream.append(el("div", { class: "chat-history-mark" }, el("span", {}, result.note)));
    if (events.length) stream.append(el("div", { class: "chat-history-mark" }, el("span", {}, "以上是历史记录")));
    if (s.cwd_missing) stream.append(el("div", { class: "chat-error" }, icon("circle-alert", { size: 16 }),
      el("div", {}, `这个会话的工作目录 ${shortPath(s.cwd)} 在 ${deviceName()} 上已经不存在，没法续接；可以看记录，或者在其他目录新开对话。`)));
    else if (!canResume(s.kind)) stream.append(el("div", { class: "chat-history-mark" }, el("span", {}, `${kindLabel(s.kind)} 不支持续接已有会话：以上记录只能查看，发消息会开一个新对话`)));
    if (run) attachRun(run, { keep: true });
    else { renderComposer(); renderWelcome(); }
    if (S.address) startConversation();
    scrollToEnd(true);
  }

  function attachRun(run, { keep = false } = {}) {
    if (!keep) {
      S.gen += 1;
      stopConversation();
      clearStream();
      Object.assign(S, { session: S.sessions.find((x) => x.id === run.session_id) || null });
    }
    Object.assign(S, { rid: run.rid, cursor: 0, runState: run.state || "running", kind: run.kind || S.kind, address: run.address || S.address });
    if (run.context) adoptContext({ ...run.context, name: run.context.name || run.context_device });
    else if (run.mode === "direct") adoptContext(null);
    else if (run.mode === "gateway") {
      // an older AgentHub names the gateway run's device and folder but not the context: find the device by name,
      // else keep a gateway context already shown, else show the run device's folder (sends go to the run anyway)
      const t = run.context_device && run.context_workdir ? targets().find((x) => x.name === run.context_device || x.id === run.context_device) : null;
      if (t) adoptContext({ mode: "gateway", device: t.id, workdir: run.context_workdir, name: t.name });
      else if (!gatewayCtx()) adoptContext(null);
    }
    if (run.workdir) S.workdir = run.workdir;
    toggleSide(false);
    renderSessions(); renderBar(); renderComposer(); renderWelcome();
    setWorking(["starting", "running"].includes(S.runState));
    persist();
    if (S.address) startConversation();
    poll(run.rid, S.gen);
  }

  async function poll(rid, gen) {
    let failures = 0;
    while (alive() && gen === S.gen && S.rid === rid) {
      const nap = asleep();
      if (nap) { await nap; continue; }
      let result;
      try {
        result = await call("agent_run_read", { rid, cursor: S.cursor, wait: 20 });
        failures = 0;
      } catch (error) {
        if (!alive() || gen !== S.gen) return;
        if (["not_found", "unknown_run", "closed"].includes(error.code)) {
          S.rid = null;
          setRunState("idle");
          put(null, el("div", { class: "chat-turn-end" }, el("span", {}, "对话进程已关闭；再发消息会续接这个会话")), { type: "status" });
          renderComposer();
          return;
        }
        failures += 1;
        if (failures === 3) toast(`和 ${deviceName()} 的连接不稳定，正在重试…`, { type: "warn" });
        await sleep(Math.min(10000, 1500 * failures));
        continue;
      }
      if (!alive() || gen !== S.gen || S.rid !== rid) return;
      for (const ev of result.events || []) ingest(ev);
      if (Number.isFinite(result.cursor)) S.cursor = result.cursor; else S.cursor += (result.events || []).length;
      if (result.dropped) put(null, el("div", { class: "chat-turn-end" }, el("span", {}, `有 ${result.dropped} 条较早的事件没有保留`)), { type: "status" });
      if (result.state) setRunState(result.state);
      if (result.state === "closed") { S.rid = null; renderComposer(); return; }
      if (!(result.events || []).length && !result.state) await sleep(300);
    }
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    if (["starting", "running"].includes(S.runState)) { toast("等这一轮结束再发，或者先点“停止”", { type: "info", timeout: 2200 }); return; }
    const blocked = blockedReason();
    if (blocked && !S.rid) { toast(blocked.text, { type: "warn" }); return; }
    input.value = "";
    autoGrow();
    saveDraft();
    pendingUser.push(text);
    put(null, userNode(text), { type: "user", text });
    renderWelcome();
    scrollToEnd(true);
    setRunState("starting");
    try {
      if (S.rid) {
        await call("agent_run_send", { rid: S.rid, prompt: text });
      } else {
        const params = {
          // gateway mode: the folder is the other device's; the CLI starts in the session's own folder when resuming
          // (Claude keys its sessions by folder), else in the run device's home (the hub's default)
          kind: S.kind, workdir: gatewayCtx() ? S.session?.cwd || "" : S.workdir, prompt: text, permission: S.permission, preset: S.preset,
          resume_id: (S.session ? canResume(S.session.kind) : canResume(S.kind)) ? (S.session?.id || S.sessionId || undefined) : undefined,
          title: S.session ? sessionTitle(S.session) : text.replace(/\s+/g, " ").slice(0, 80),
          context: gatewayCtx() ? { mode: "gateway", device: S.context.device, workdir: S.context.workdir } : { mode: "direct" },
        };
        const result = await call("agent_run_open", params);
        if (result.attached) {  // the session already runs (another tab / console): follow that run instead
          toast(result.note || "这个会话已经在另一个窗口运行，已切换到那个对话", { type: "info", timeout: 3200 });
          refreshRuns();
          attachRun({ ...result, kind: S.kind, workdir: S.workdir, state: result.state });
          return;
        }
        S.rid = result.rid;
        S.cursor = 0;
        if (result.session_id) S.sessionId = result.session_id;
        if (result.address) { S.address = result.address; startConversation(); }
        persist();
        refreshRuns();
        poll(S.rid, S.gen);
      }
    } catch (error) {
      setRunState("idle");
      if (pendingUser.includes(text)) pendingUser.splice(pendingUser.indexOf(text), 1);
      if (error.data?.attached && error.data.rid && !S.rid) {
        attachRun({ rid: error.data.rid, kind: S.kind, workdir: S.workdir, state: "running", session_id: S.session?.id });
      }
      put(null, el("div", { class: "chat-error", role: "alert" }, icon("circle-alert", { size: 16 }),
        el("div", {}, el("div", { class: "strong" }, "没有发出去"), el("div", {}, error.message))), { type: "error" });
      if (!input.value) { input.value = text; autoGrow(); }
    }
  }

  async function stop() {
    if (!S.rid) return;
    stopBtn.disabled = true;
    try { await call("agent_run_stop", { rid: S.rid }); }
    catch (error) { toast(error.message, { type: "bad", title: "没能停止" }); }
    finally { stopBtn.disabled = false; }
  }

  // --- ask_user / mail for this run's address (§10)
  function startConversation() {
    stopConversation();
    if (!S.address || !api.conversation) return;
    const gen = S.gen, address = S.address;
    const tick = async () => {
      if (!alive() || gen !== S.gen || S.address !== address) return;
      const nap = asleep();
      if (nap) { await nap; tick(); return; }
      try {
        const result = (await api.conversation(address)) || {};
        if (gen !== S.gen) return;
        for (const m of result.messages || []) {
          if (m.to && m.to !== "me" && !String(m.to).startsWith("me")) continue;
          if (m.ask_id) continue;
          ingest({ type: "mail", message_id: m.id || m.message_id, from: m.from_display || m.from, subject: m.subject, body: m.body, t: m.t || m.created_at });
        }
        for (const a of result.asks || []) ingest({ ...a, type: "ask", t: a.t || a.created_at });
      } catch { /* the gateway may be unreachable (本机 + 云端断开): the chat still works */ }
      convTimer = setTimeout(tick, ["starting", "running"].includes(S.runState) ? 4000 : 12000);
    };
    tick();
  }
  function stopConversation() { clearTimeout(convTimer); convTimer = null; }

  // ============================================================ lifecycle

  let destroyed = false;
  // `persistent` (the 对话 page, §16.2): the instance outlives its page -- detached from the document it keeps
  // its run and long-poll; otherwise it stops by itself once the container leaves the document.
  function alive() {
    if (destroyed) return false;
    if (!opts.persistent && !root.isConnected) { destroy(); return false; }
    return true;
  }

  // Hidden for more than PAUSE_HIDDEN_MS: the long-poll and the mailbox poll wait until the page is shown.
  // (The local console's desktop window reports its own visibility: window.__mcpmNativeVisible.)
  const isHidden = () => document.hidden || window.__mcpmNativeVisible === false;
  let hiddenSince = isHidden() ? Date.now() : 0;
  const wakers = [];
  function onVisibility() {
    if (isHidden()) { if (!hiddenSince) hiddenSince = Date.now(); return; }
    hiddenSince = 0;
    for (const wake of wakers.splice(0)) wake();
  }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("mcpmvisibilitychange", onVisibility);
  /** A promise that resolves when the page is shown again, or null while polling may go on. */
  function asleep() {
    if (!hiddenSince || !isHidden() || Date.now() - hiddenSince < (opts.pauseAfter ?? PAUSE_HIDDEN_MS)) return null;
    return new Promise((resolve) => wakers.push(resolve));
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    S.gen += 1;
    saveDraft();
    stopConversation();
    for (const entry of installs.values()) entry.watch?.stop();
    installs.clear();
    closePop();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("mcpmvisibilitychange", onVisibility);
    for (const wake of wakers.splice(0)) wake();
  }

  /** The page put the (persistent) chat back into the document. */
  function attached() {
    if (destroyed) return;
    requestAnimationFrame(() => { chipsEdge(); kindsEdge(); if (stick) scrollToEnd(true); else jump.hidden = false; });
    if (!S.loading && S.device !== undefined) {
      refreshSessions(); refreshRuns();
      // back from Agent 设置 (a key set, models added or enabled there): the pickers and the composer re-read it
      // (re-probing the CLIs only when coming back from 设置密钥; else the device's cached probe is enough)
      refreshAgents({ fresh: apiBack }).then(() => agentMenu.refill?.());
      apiBack = false;
    }
  }
  /** The page took the chat out of the document (another page opened): menus close, the run goes on. */
  function detached() {
    closePop();
    saveDraft();
    if (root.classList.contains("side-open")) toggleSide(false);
  }

  renderAll();
  let ready = init(opts.want || {}).catch(() => {});

  return {
    destroy, attached, detached,
    root,
    get ready() { return ready; },
    /** URL params after the first mount (#/chat?device=&kind=&session=…); they win over the current state. */
    apply(want) { ready = ready.then(() => applyWant(want)).catch(() => {}); return ready; },
    refresh: () => selectDevice(S.device),
    setDevice: (id) => selectDevice(id),
    newChat,
    get state() { return { device: S.device, kind: S.kind, workdir: S.workdir, rid: S.rid, session: S.session?.id || null, runState: S.runState, usage: S.usage }; },
    /** What the Overview's 对话 card lists: the runner device and its sessions / runs as last read. */
    snapshot() { return { device: S.device, devices: S.devices.slice(), sessions: S.sessions.slice(), runs: S.runs.slice(), loading: S.loading || S.sessionsState === "loading", error: S.deviceError || "" }; },
  };
}

export { kindMark as chatKindMark, kindShort as chatKindShort };
