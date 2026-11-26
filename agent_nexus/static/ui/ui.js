// Agent Nexus UI helpers (no build step, no dependencies besides ./icons.js).
//   import { el, toast, modal, drawer, confirmDialog, copy, fetchJSON, createRouter, renderShell } from "/ui/ui.js";
// Contract and examples: docs/ui-spec.md, "Design system".

import { icon, iconHTML, hydrateIcons } from "./icons.js";

export { icon, iconHTML, hydrateIcons };

// ---------------------------------------------------------------- DOM

/**
 * el("button", {class: "btn btn-primary", onclick: fn, dataset: {id: 1}}, "保存")
 * Attribute values: false/null/undefined are skipped, true sets an empty attribute.
 * Children: strings, numbers, nodes, arrays, null. Strings are text, never HTML;
 * pass {html: "..."} only for trusted markup (icons).
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class" || key === "className") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/** Replace a node's children. */
export function mount(target, ...children) {
  target.replaceChildren();
  append(target, children);
  return target;
}

// ---------------------------------------------------------------- brand

/** The Agent Nexus mark: a hub with three linked nodes on the accent tile. */
export function logoSVG(size = 28) {
  return `<svg class="brand-mark" width="${size}" height="${size}" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
<rect width="32" height="32" rx="9" fill="var(--accent)"/>
<g stroke="var(--text-on-accent)" stroke-width="2.2" stroke-linecap="round" fill="none">
<path d="M16 16 L16 8.5 M16 16 L9.5 20.5 M16 16 L22.5 20.5" opacity=".75"/></g>
<g fill="var(--text-on-accent)"><circle cx="16" cy="16" r="3.6"/><circle cx="16" cy="7.6" r="2.4"/><circle cx="8.7" cy="21.2" r="2.4"/><circle cx="23.3" cy="21.2" r="2.4"/></g>
</svg>`;
}

export function brand({ sub = "", size = 28, href = "#/" } = {}) {
  return el("a", { class: "brand", href, html: logoSVG(size) },
    el("span", { class: "brand-text" }, "Agent Nexus", sub ? el("span", { class: "brand-sub" }, sub) : null));
}

// ---------------------------------------------------------------- status

/** Every state any page shows, mapped to label and tone (docs/ui-spec.md "Status vocabulary"). */
export const STATUS = {
  online: ["在线", "ok"], offline: ["离线", "neutral"], idle: ["空闲", "info"], revoked: ["已吊销", "bad"],
  needs_login: ["需要登录", "warn"], error: ["错误", "bad"], ok: ["正常", "ok"], connecting: ["连接中", "info"],
  running: ["运行中", "ok"], starting: ["启动中", "info"], stopped: ["已停止", "neutral"], disabled: ["已禁用", "neutral"],
  crashed: ["已崩溃", "bad"], unhealthy: ["不健康", "warn"], conflict: ["端口冲突", "bad"], external: ["外部进程", "info"],
  needs_config: ["需要配置", "warn"], installing: ["安装中", "info"], update_failed: ["更新失败", "bad"],
  pending_device_upgrade: ["设备需升级", "warn"], host_revoked: ["宿主已吊销", "bad"], device_offline: ["设备离线", "neutral"],
  unused: ["未使用", "accent"], used: ["已使用", "neutral"], expired: ["已过期", "neutral"],
  joined: ["已加入", "ok"], not_joined: ["未加入", "neutral"], testing: ["测试中", "info"],
  not_installed: ["未安装", "neutral"], invalid: ["配置错误", "bad"], unsupported: ["不支持此平台", "neutral"],
};

export function pill(state, label) {
  const [text, tone] = STATUS[state] || [state, "neutral"];
  return el("span", { class: `pill ${tone}`, dataset: { state } }, label || text);
}

// ---------------------------------------------------------------- feedback

let toastStack;
/** toast("已保存") · toast("失败", {type: "bad", title: "安装失败", action: {label: "重试", onClick}}) */
export function toast(message, { type = "ok", title = "", timeout = 3600, action = null } = {}) {
  if (!toastStack || !toastStack.isConnected) {
    toastStack = el("div", { class: "toast-stack", role: "status", "aria-live": "polite" });
    document.body.append(toastStack);
  }
  const glyph = { ok: "circle-check", bad: "circle-alert", warn: "triangle-alert", info: "info" }[type] || "info";
  const node = el("div", { class: `toast ${type}` },
    icon(glyph, { size: 16 }),
    el("div", { class: "toast-body" }, title ? el("div", { class: "toast-title" }, title) : null, el("div", {}, message)),
    action ? el("button", { class: "toast-action", type: "button", onclick: () => { action.onClick?.(); dismiss(); } }, action.label) : null,
    el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "关闭", onclick: () => dismiss(), html: iconHTML("x", { size: 14 }) }));
  let timer = timeout ? setTimeout(dismiss, timeout) : null;
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", () => { if (timeout) timer = setTimeout(dismiss, 1800); });
  function dismiss() {
    clearTimeout(timer);
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 200);
  }
  toastStack.append(node);
  return { dismiss };
}

/**
 * modal({title, description, body, actions: [{label, variant: "primary"|"danger"|"ghost", onClick, icon}], size})
 * onClick may return false (or a Promise resolving to false) to keep the dialog open;
 * while the Promise is pending the button shows a spinner.
 * Returns {root, close, closed: Promise<void>}.
 */
export function modal(options) { return overlay("modal", options); }

/** Same options as modal(); slides in from the right (a bottom sheet on phones). */
export function drawer(options) { return overlay("drawer", options); }

function overlay(kind, { title = "", description = "", body = null, actions = [], size = "", footLeft = null, onClose = null } = {}) {
  let resolveClosed;
  const closed = new Promise((resolve) => { resolveClosed = resolve; });
  const previous = document.activeElement;
  const titleId = `ov-${Math.random().toString(36).slice(2, 9)}`;
  const buttons = actions.map((action) => {
    const variant = action.variant ? `btn-${action.variant}` : "";
    const button = el("button", { class: `btn ${variant}`, type: action.submit ? "submit" : "button" },
      action.icon ? icon(action.icon, { size: 16 }) : null, action.label);
    button.addEventListener("click", async (event) => {
      if (!action.onClick) { close(); return; }
      event.preventDefault();
      button.disabled = true;
      const original = Array.from(button.childNodes);
      button.replaceChildren(el("span", { class: "spinner" }), action.label);
      try {
        const keep = await action.onClick({ close, root: panel, event });
        if (keep !== false) close();
      } finally {
        button.disabled = false;
        button.replaceChildren(...original);
      }
    });
    return button;
  });
  const panel = el("div", { class: `${kind} ${size}`, role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    el("div", { class: `${kind}-head` },
      el("div", { class: "titles" }, el("h2", { id: titleId }, title), description ? el("p", {}, description) : null),
      el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "关闭", onclick: () => close(), html: iconHTML("x", { size: 16 }) })),
    el("div", { class: `${kind}-body` }, body),
    buttons.length || footLeft ? el("div", { class: `${kind}-foot` }, footLeft ? el("div", { class: "left" }, footLeft) : null, buttons) : null);
  const root = el("div", { class: `overlay ${kind === "drawer" ? "drawer-overlay" : ""}` }, panel);
  root.addEventListener("mousedown", (event) => { if (event.target === root) close(); });
  const onKey = (event) => {
    if (event.key === "Escape") close();
    if (event.key === "Tab") trapFocus(panel, event);
  };
  document.addEventListener("keydown", onKey);
  document.body.append(root);
  document.body.style.overflow = "hidden";
  hydrateIcons(panel);
  requestAnimationFrame(() => (panel.querySelector("[autofocus], input:not([type=hidden]), select, textarea") || buttons.at(-1) || panel).focus?.());
  function close() {
    if (!root.isConnected) return;
    document.removeEventListener("keydown", onKey);
    root.remove();
    if (!document.querySelector(".overlay")) document.body.style.overflow = "";
    previous?.focus?.();
    onClose?.();
    resolveClosed();
  }
  return { root: panel, close, closed };
}

function trapFocus(panel, event) {
  const focusable = $$("a[href], button:not(:disabled), input:not(:disabled):not([type=hidden]), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])", panel);
  if (!focusable.length) return;
  const first = focusable[0], last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
  else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
}

/** await confirmDialog({title: "吊销设备？", message: "…", confirmLabel: "吊销", danger: true}) → boolean */
export function confirmDialog({ title, message = "", confirmLabel = "确定", cancelLabel = "取消", danger = false } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const dialog = modal({
      title, size: "sm", body: message ? el("p", { class: "muted" }, message) : null,
      onClose: () => resolve(answer),
      actions: [
        { label: cancelLabel, onClick: () => { answer = false; } },
        { label: confirmLabel, variant: danger ? "danger solid" : "primary", onClick: () => { answer = true; } },
      ],
    });
    return dialog;
  });
}

/** Copy text; toasts "已复制". Falls back to a hidden textarea when the Clipboard API is unavailable (http, Tauri). */
export async function copy(text, { label = "已复制到剪贴板", quiet = false } = {}) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    const area = el("textarea", { style: { position: "fixed", opacity: "0", top: "0" }, readonly: true });
    area.value = text;
    document.body.append(area);
    area.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    area.remove();
  }
  if (!quiet) toast(ok ? label : "复制失败，请手动选择复制", { type: ok ? "ok" : "bad", timeout: 2000 });
  return ok;
}

// ---------------------------------------------------------------- building blocks

/** Read-only monospace value with a copy button; masked values get a reveal toggle. */
export function tokenField(value, { masked = false, multiline = false, large = false, copyLabel = "复制" } = {}) {
  const code = el("code", {}, masked ? "•".repeat(Math.min(32, value.length)) : value);
  let shown = !masked;
  const reveal = masked ? el("button", {
    class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "显示", html: iconHTML("eye", { size: 14 }),
    onclick: (event) => {
      shown = !shown;
      code.textContent = shown ? value : "•".repeat(Math.min(32, value.length));
      event.currentTarget.innerHTML = iconHTML(shown ? "eye-off" : "eye", { size: 14 });
    },
  }) : null;
  return el("div", { class: `token ${multiline ? "multiline" : ""} ${large ? "lg" : ""}` }, code, reveal,
    el("button", { class: "btn btn-sm", type: "button", onclick: () => copy(value) }, icon("copy", { size: 14 }), copyLabel));
}

/** emptyState({icon: "plug", title, text, action: {label, icon, onClick, href}, secondary}) */
export function emptyState({ icon: glyph = "inbox", title, text = "", action = null, secondary = null, compact = false }) {
  const button = (spec, primary) => spec && el(spec.href ? "a" : "button", {
    class: `btn ${primary ? "btn-primary" : ""}`, type: spec.href ? null : "button", href: spec.href || null, onclick: spec.onClick || null,
  }, spec.icon ? icon(spec.icon, { size: 16 }) : null, spec.label);
  return el("div", { class: `empty ${compact ? "compact" : ""}` },
    el("div", { class: "empty-icon", html: iconHTML(glyph, { size: 22 }) }),
    el("h3", {}, title), text ? el("p", {}, text) : null,
    action || secondary ? el("div", { class: "actions" }, button(action, true), button(secondary, false)) : null);
}

/** skeletonRows(3) → placeholder list rows while data loads. */
export function skeletonRows(count = 3) {
  return el("div", { class: "list", "aria-busy": "true", "aria-label": "加载中" },
    Array.from({ length: count }, (_, index) => el("div", { class: "list-item" },
      el("span", { class: "skeleton circle" }),
      el("div", { class: "stack grow" }, el("span", { class: `skeleton ${index % 2 ? "w-40" : "w-60"}` }), el("span", { class: "skeleton w-80", style: { height: "10px" } })))));
}

/** errorState(err, retry) for a failed section load. */
export function errorState(error, retry) {
  return emptyState({ icon: "circle-alert", title: "加载失败", text: error?.message || String(error), action: retry ? { label: "重试", icon: "refresh-cw", onClick: retry } : null, compact: true });
}

// ---------------------------------------------------------------- formatting

export function relTime(input) {
  if (input === null || input === undefined || input === "") return "—";
  const then = typeof input === "number" ? (input < 1e12 ? input * 1000 : input) : Date.parse(input);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  const future = seconds < 0, s = Math.abs(seconds);
  const unit = s < 45 ? "刚刚" : s < 3600 ? `${Math.round(s / 60)} 分钟` : s < 86400 ? `${Math.round(s / 3600)} 小时` : s < 86400 * 30 ? `${Math.round(s / 86400)} 天` : null;
  if (unit === "刚刚") return future ? "即将" : "刚刚";
  if (unit) return future ? `${unit}后` : `${unit}前`;
  return new Date(then).toLocaleDateString("zh-CN");
}

export function bytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(n), index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

/** The supervisor's English status lines (shared with the CLI and logs) in the console's words. */
const SUPERVISOR_TEXT = [
  [/^exited with code (-?\d+)$/, (m) => `进程意外退出（代码 ${m[1]}），日志里有原因`],
  [/^process exited$/, () => "进程已退出"],
  [/^(\S+) started \(pid (\d+)\)$/, (m) => `${m[1]} 已启动`],
  [/^(\S+) already running \(pid (\d+)\)$/, (m) => `${m[1]} 已在运行`],
  [/^(\S+) stopped(?: \((\d+) processes\))?$/, (m) => `${m[1]} 已停止`],
  [/^port (\d+) still in use after stop$/, (m) => `停止后端口 ${m[1]} 仍被占用`],
  [/^refresh failed: (.*)$/, (m) => `刷新失败：${m[1]}`],
  [/^auto-restart failed: (.*)$/, (m) => `自动重启失败：${m[1]}`],
  [/^health check: (.*)$/, (m) => `健康检查未通过：${m[1]}`],
  [/^port (\d+) is held by (.*)$/, (m) => `端口 ${m[1]} 被 ${m[2]} 占用`],
  [/^not listening on (\d+) after (\d+)s$/, (m) => `启动 ${m[2]} 秒后仍未监听端口 ${m[1]}`],
  [/^port (\d+) served by (.*), not started by mcpm$/, (m) => `端口 ${m[1]} 由 ${m[2]} 提供（不是 Agent Nexus 启动的）`],
  [/^platforms: (.*)$/, (m) => `只支持这些平台：${m[1]}`],
];
export function supervisorText(message) {
  const text = String(message || "");
  for (const [pattern, render] of SUPERVISOR_TEXT) {
    const match = text.match(pattern);
    if (match) return render(match);
  }
  return text;
}

export function shortHash(value, size = 12) { return value ? `${value.slice(0, size)}…` : "—"; }

// ---------------------------------------------------------------- API

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

const ERROR_TEXT = {
  0: "网络不可用，请检查连接", 401: "登录已过期，请重新登录", 403: "没有权限执行此操作", 404: "找不到请求的内容",
  409: "当前状态不允许此操作", 413: "文件太大", 429: "操作太频繁，请稍后再试", 502: "上游服务不可用", 504: "等待超时",
};

/**
 * fetchJSON("/api/devices") · fetchJSON("/api/invites", {method: "POST", body: {ttl_s: 86400}})
 * - JSON in and out; non-GET requests carry X-ANX: 1 (both servers require it).
 * - Errors throw ApiError(message, status, data) and toast the message unless {quiet: true}.
 * - 401 dispatches the "anx:unauthorized" window event (pages redirect to their login).
 */
// Answers that come from the gateway (overseas, ~0.3 s or more per round trip) are kept: a page
// renders at once from the last answer while a fresh one is fetched in the background. Any
// change (a non-GET request) forgets them all, so a page reloading after an action sees the truth.
const swr = { prefixes: [], maxAge: 0, fresh: 0, store: new Map(), inflight: new Map(), key: "anx-swr" };

/** Keep GET answers of these path prefixes: shown for up to ``maxAge`` ms, refetched when older than ``fresh`` ms. */
export function cacheGets(prefixes, { maxAge = 120000, fresh = 3000 } = {}) {
  Object.assign(swr, { prefixes, maxAge, fresh });
  try {
    const saved = JSON.parse(sessionStorage.getItem(swr.key) || "{}");
    for (const [path, entry] of Object.entries(saved)) swr.store.set(path, entry);
  } catch { /* no storage: memory only */ }
}

function swrSave() {
  try { sessionStorage.setItem(swr.key, JSON.stringify(Object.fromEntries(swr.store))); } catch { /* full or blocked */ }
}

/** Fetch ``path`` into the cache now (the shell warms the gateway pages before they are opened). */
export function prefetch(path) {
  return swrFetch(path).catch(() => null);
}

/** The cached answer for ``path`` right now (no request), or null: a page draws with it at once
 *  and checks the fresh answer when it arrives. */
export function cachedJSON(path) {
  const entry = swr.store.get(path);
  return entry && Date.now() - entry.at < swr.maxAge ? clone(entry.data) : null;
}

function swrFetch(path) {
  if (!swr.inflight.has(path)) {
    const request = rawFetchJSON(path, { quiet: true }).then((data) => {
      swr.store.set(path, { at: Date.now(), data });
      swrSave();
      return data;
    }).finally(() => swr.inflight.delete(path));
    swr.inflight.set(path, request);
  }
  return swr.inflight.get(path);
}

const clone = (data) => (typeof structuredClone === "function" ? structuredClone(data) : JSON.parse(JSON.stringify(data)));

export async function fetchJSON(path, options = {}) {
  const method = options.method || "GET";
  if (method !== "GET") {
    // a change may make any cached answer stale; ``keepCache``: a POST that changes nothing shown
    // (preparing a tunnel) -- clearing made the page after it wait for the device list again
    if (!options.keepCache && swr.store.size) { swr.store.clear(); swrSave(); }
    return rawFetchJSON(path, options);
  }
  if (!options.signal && swr.prefixes.some((prefix) => path === prefix || path.startsWith(prefix + "/") || path.startsWith(prefix + "?"))) {
    const entry = swr.store.get(path);
    const age = entry ? Date.now() - entry.at : Infinity;
    if (entry && age < swr.maxAge) {
      if (age > swr.fresh) swrFetch(path).catch(() => {});
      return clone(entry.data);
    }
    try {
      return clone(await swrFetch(path));
    } catch (error) {
      if (!options.quiet) toast(error.message, { type: "bad" });
      throw error;
    }
  }
  return rawFetchJSON(path, options);
}

async function rawFetchJSON(path, { method = "GET", body, headers = {}, quiet = false, signal } = {}) {
  const init = { method, credentials: "same-origin", cache: "no-store", signal, headers: { Accept: "application/json", ...headers } };
  if (method !== "GET" && method !== "HEAD") init.headers["X-ANX"] = "1";
  if (body !== undefined) {
    if (body instanceof FormData) init.body = body;
    else if (body instanceof Blob) { init.headers["Content-Type"] = body.type || "application/octet-stream"; init.body = body; }
    else { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    if (cause?.name === "AbortError") throw cause;
    const error = new ApiError(ERROR_TEXT[0], 0, null);
    if (!quiet) toast(error.message, { type: "bad" });
    throw error;
  }
  let data = null;
  const text = await response.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 300) }; } }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new CustomEvent("anx:unauthorized", { detail: { path } }));
    const message = (data && (data.message || data.error)) || ERROR_TEXT[response.status] || `请求失败（HTTP ${response.status}）`;
    const error = new ApiError(message, response.status, data);
    if (!quiet) toast(message, { type: "bad", title: ERROR_TEXT[response.status] && message !== ERROR_TEXT[response.status] ? ERROR_TEXT[response.status] : "" });
    throw error;
  }
  return data ?? {};
}

// ---------------------------------------------------------------- router

/**
 * const router = createRouter({"/": overview, "/devices": devices, "/inbox/:thread?": inbox}, {fallback: "/"});
 * router.start(); router.go("/devices");
 * Handlers get ({params, path, query}); the shell's nav highlights the first path segment.
 */
export function createRouter(routes, { fallback = "/", onChange = null } = {}) {
  const table = Object.entries(routes).map(([pattern, handler]) => {
    const keys = [];
    const source = pattern.replace(/\/:(\w+)(\?)?/g, (_, key, optional) => { keys.push(key); return optional ? "(?:/([^/]+))?" : "/([^/]+)"; });
    return { regex: new RegExp(`^${source === "/" ? "/?" : source}/?$`), keys, handler };
  });
  function current() {
    const raw = location.hash.replace(/^#/, "") || "/";
    const [path, search = ""] = raw.split("?");
    return { path, query: new URLSearchParams(search) };
  }
  function resolve() {
    const { path, query } = current();
    for (const { regex, keys, handler } of table) {
      const match = path.match(regex);
      if (match) {
        const params = Object.fromEntries(keys.map((key, index) => [key, match[index + 1] && decodeURIComponent(match[index + 1])]));
        setActiveNav(path);
        onChange?.({ path, params, query });
        handler({ path, params, query });
        return;
      }
    }
    go(fallback, { replace: true });
  }
  function go(path, { replace = false } = {}) {
    if (replace) { history.replaceState(null, "", `#${path}`); resolve(); }
    else location.hash = path;
  }
  return { start() { window.addEventListener("hashchange", resolve); resolve(); }, go, resolve, current };
}

function setActiveNav(path) {
  const section = `/${path.split("/")[1] || ""}`;
  for (const link of $$("[data-nav]")) {
    const on = link.dataset.nav === section;
    link.classList.toggle("active", on);
    if (on) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
}

// ---------------------------------------------------------------- shell

/**
 * renderShell(document.body, {
 *   sub: "云端控制台",
 *   nav: [{path: "/", label: "概览", icon: "layout-dashboard", primary: true}, {section: "管理"}, ...],
 *   status: {state: "online", label: "网关在线"},
 *   footer: [{label: "退出登录", icon: "log-out", onClick}],
 * }) → {main, topbar, page, setTitle(title, crumbs?), setActions(...nodes), setBadge(path, n)}
 *
 * Desktop: sidebar. 769–1023px: icon rail. Phone: bottom tab bar with the
 * `primary` items plus "更多", which opens the full sidebar as a drawer.
 */
export function renderShell(target, { sub = "", nav = [], status = null, footer = [], active = null } = {}) {
  const navLink = (item) => el("a", { class: "nav-item", href: `#${item.path}`, dataset: { nav: item.path }, title: item.label },
    icon(item.icon, { size: 18 }), el("span", { class: "nav-label" }, item.label),
    el("span", { class: "nav-badge", hidden: !item.badge, dataset: { badgeFor: item.path } }, item.badge || ""));
  const sidebar = el("aside", { class: "sidebar", "aria-label": "主导航" },
    el("div", { class: "sidebar-brand" }, brand({ sub })),
    el("nav", { class: "nav" }, nav.map((item) => (item.section ? el("div", { class: "nav-section" }, item.section) : navLink(item)))),
    el("div", { class: "sidebar-foot" },
      status ? el("div", { class: "sidebar-status", title: status.label }, el("span", { class: `dot ${STATUS[status.state]?.[1] || "neutral"}` }), el("span", { class: "label truncate" }, status.label)) : null,
      footer.map((item) => el("a", { class: "nav-item", href: item.href || "#", title: item.label, onclick: item.onClick ? (event) => { event.preventDefault(); item.onClick(); } : null },
        icon(item.icon, { size: 18 }), el("span", { class: "nav-label" }, item.label)))));
  const title = el("div", { class: "crumbs" });
  const actions = el("div", { class: "topbar-actions" });
  const topbar = el("header", { class: "topbar" }, el("div", { class: "only-sm brand" }, brand({ size: 26 })), title, actions);
  const page = el("main", { class: "page", id: "page" });
  const main = el("div", { class: "main" }, topbar, page);
  const primary = nav.filter((item) => item.primary);
  const scrim = el("div", { class: "scrim", hidden: true, onclick: () => toggleMore(false) });
  const tabbar = el("nav", { class: "tabbar", "aria-label": "主导航" },
    primary.map((item) => el("a", { href: `#${item.path}`, dataset: { nav: item.path } }, icon(item.icon, { size: 20 }), item.short || item.label,
      el("span", { class: "nav-badge", hidden: !item.badge, dataset: { badgeFor: item.path } }, item.badge || ""))),
    el("button", { type: "button", onclick: () => toggleMore(true) }, icon("menu", { size: 20 }), "更多"));
  function toggleMore(open) {
    sidebar.classList.toggle("open", open);
    scrim.hidden = !open;
  }
  sidebar.addEventListener("click", (event) => { if (event.target.closest(".nav-item")) toggleMore(false); });
  const app = el("div", { class: "app" }, sidebar, main);
  target.prepend(app, tabbar, scrim);
  window.addEventListener("scroll", () => topbar.classList.toggle("scrolled", window.scrollY > 4), { passive: true });
  if (active) setActiveNav(active);
  return {
    main, topbar, page,
    setTitle(text, crumbs = []) {
      mount(title, crumbs.flatMap((crumb) => [el("a", { href: crumb.href }, crumb.label), icon("chevron-right", { size: 14, cls: "faint" })]), el("span", { class: "current" }, text));
      document.title = `${text} · Agent Nexus`;
    },
    setActions(...nodes) { mount(actions, nodes); },
    setBadge(path, count) {
      for (const badge of $$(`[data-badge-for="${CSS.escape(path)}"]`)) { badge.textContent = count > 99 ? "99+" : String(count || ""); badge.hidden = !count; }
    },
    setStatus(state, label) {
      const node = $(".sidebar-status", sidebar);
      if (!node) return;
      mount(node, el("span", { class: `dot ${STATUS[state]?.[1] || "neutral"}` }), el("span", { class: "label truncate" }, label));
      node.title = label;
    },
  };
}

// ---------------------------------------------------------------- theme

/** setTheme("light" | "dark" | "system"); remembered per browser. */
export function setTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme;
  try { localStorage.setItem("anx-theme", mode); } catch { /* storage may be unavailable */ }
}

export function restoreTheme() {
  try { const mode = localStorage.getItem("anx-theme"); if (mode) setTheme(mode); } catch { /* ignore */ }
}
