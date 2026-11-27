// The local console (docs/ui-spec.md §4): shell, routing, polling. Runs in the Tauri window and any local browser.
import { renderShell, createRouter, fetchJSON, el, mount, restoreTheme, icon, cacheGets, prefetch, appearanceButton } from "/ui/ui.js";
import { mountInbox } from "/ui/inbox.js";
import { hubHost } from "/ui/invite.js";
import { overviewPage } from "/local/overview.js";
import { pluginsPage } from "/local/plugins.js";
import { cloudPage } from "/local/cloud.js";
import { settingsPage } from "/local/settings.js";
import { devicesPage, lockButton } from "/local/devices.js";
import { desktopPage, loadNoVNC } from "/local/desktop.js";
import { localConnectorsPage } from "/local/connectors.js";
import { terminalPage, loadVendor } from "/ui/terminal.js";
import { CLIENT_TERMINAL_API } from "/ui/terminal-config.js";
import { agentPage, localAgentApi, loginHandler } from "/ui/agent-wire.js";
import { chatPage } from "/ui/chat-page.js";

restoreTheme();
// What the gateway answers (devices, inbox, 插件库): pages show the last answer at once.
const GATEWAY_GETS = ["/api/devices", "/api/inbox/threads", "/api/inbox/summary", "/api/plugin-repo"];
cacheGets(GATEWAY_GETS);

// "/cloud?invite=…" (the old join page) and "#/enroll?invite=…" (agentnexus:// deep links) land on #/cloud.
if (location.pathname === "/cloud") {
  history.replaceState(null, "", `/#/cloud${location.search}`);
} else if (location.hash.startsWith("#/enroll")) {
  history.replaceState(null, "", `/#/cloud${location.hash.slice("#/enroll".length)}`);
}

const NAV = [
  { path: "/", label: "概览", icon: "layout-dashboard", primary: true },
  { path: "/chat", label: "对话", icon: "message-square", primary: true },
  { path: "/plugins", label: "插件", icon: "puzzle", primary: true },
  { path: "/devices", label: "设备", icon: "monitor", primary: true },
  { path: "/agent", label: "Agent", icon: "bot" },
  { path: "/connectors", label: "连接器", icon: "plug" },
  { path: "/cloud", label: "连接云端", icon: "cloud", short: "云端" },
  { path: "/inbox", label: "收件箱", icon: "inbox", primary: true },
  { section: "其他" },
  { path: "/settings", label: "设置", icon: "settings" },
];


const shell = renderShell(document.body, { sub: "本机", nav: NAV, status: { state: "connecting", label: "正在读取…" } });
shell.setActions(lockButton(), appearanceButton());

/** The Tauri shell hides the window without making the document hidden (WebView2); it tells us instead. */
export function visible() {
  return !document.hidden && window.__mcpmNativeVisible !== false;
}

let cleanup = [];
function context(title, { wide = false } = {}) {
  for (const fn of cleanup) fn();
  cleanup = [];
  shell.setTitle(title, [{ label: "本机", href: "#/" }]);
  shell.page.classList.toggle("wide", wide);
  mount(shell.page);
  window.scrollTo(0, 0);
  return {
    page: shell.page, shell,
    every(ms, fn) {
      const timer = setInterval(() => { if (visible()) fn(); }, ms);
      cleanup.push(() => clearInterval(timer));
    },
    onLeave(fn) { cleanup.push(fn); },
    refreshStatus: () => refreshStatus(),
  };
}

const CLOUD_LABEL = {
  connected: (c) => ["online", `已连接 ${hubHost(c.hub || c.url) || "网关"}`],
  connecting: (c) => ["connecting", `正在连接 ${hubHost(c.hub || c.url) || "网关"}`],
  error: () => ["error", "连接错误"],
  revoked: () => ["revoked", "已被吊销"],
  not_joined: () => ["offline", "仅本机 · 未加入网关"],
};

let cloudState = null;
async function refreshStatus() {
  try {
    const cloud = await fetchJSON("/api/cloud", { quiet: true });
    cloudState = cloud;
    const [state, label] = (CLOUD_LABEL[cloud.state] || CLOUD_LABEL.not_joined)(cloud);
    shell.setStatus(state, label);
  } catch {
    shell.setStatus("offline", "本机服务不可达");
  }
  if (cloudState?.state === "connected") {
    try {
      const summary = await fetchJSON("/api/inbox/summary", { quiet: true });
      shell.setBadge("/inbox", (summary.unread || 0) + (summary.open_asks || 0));
    } catch { shell.setBadge("/inbox", 0); }
  } else shell.setBadge("/inbox", 0);
  try {
    const { plugins } = await fetchJSON("/api/plugins", { quiet: true });
    shell.setBadge("/plugins", plugins.filter((p) => ["needs_config", "crashed", "conflict", "invalid", "unhealthy"].includes(p.status)).length);
  } catch { /* the plugins page shows its own error */ }
}

function inboxPage({ params }) {
  const ctx = context("收件箱", { wide: true });
  const head = el("div", { class: "page-head" },
    el("div", { class: "titles" }, el("h1", {}, "收件箱"),
      el("p", {}, "Agent 发给你（", el("span", { class: "mono" }, "me"), "）的邮件和提问。回复会唤醒对方的会话。")),
    el("div", { class: "actions" }));
  const holder = el("div");
  mount(ctx.page, head, holder);
  if (cloudState && cloudState.state !== "connected") {
    import("/ui/ui.js").then(({ emptyState }) => mount(holder, el("section", { class: "card" }, el("div", { class: "card-body" },
      emptyState({ icon: "cloud-off", title: "收件箱需要连接网关", text: "邮件存放在网关上。加入网关后，这里会显示 Agent 发给你的邮件和提问。",
        action: { label: "连接云端", icon: "cloud", href: "#/cloud" } })))));
    return;
  }
  const inbox = mountInbox(holder, {
    base: "/api/inbox", threadId: params.thread || null,
    onUnread: (unread, asks) => { if (unread !== null) shell.setBadge("/inbox", unread + asks); },
    onOpen: (id) => history.replaceState(null, "", id ? `#/inbox/${encodeURIComponent(id)}` : "#/inbox"),
  });
  mount(head.querySelector(".actions"), inbox.viewerSelect, inbox.composeButton);
  inbox.refresh();
  ctx.every(10000, () => inbox.refresh());
}

// One transport for the 对话 page: its chat instance outlives the page (docs/agent-chat.md §16.2).
const agentApi = localAgentApi();
const router = createRouter({
  "/": (route) => overviewPage(context("概览", { wide: true }), route),
  "/chat": (route) => chatPage(context("对话", { wide: true }), route, { api: agentApi, local: true, onLogin: loginHandler({ local: true }) }),
  "/agent": (route) => agentPage(context("Agent", { wide: true }), route, { api: agentApi, local: true }),
  "/plugins/:name?": (route) => pluginsPage(context("插件"), route),
  "/cloud": (route) => cloudPage(context("连接云端"), route),
  "/devices": (route) => devicesPage(context("设备"), route),
  "/terminal/:device": (route) => terminalPage(context("终端", { wide: true }), route, CLIENT_TERMINAL_API),
  "/desktop/:device": (route) => desktopPage(context("远程桌面", { wide: true }), route),
  "/connectors": (route) => localConnectorsPage(context("连接器"), route),
  "/inbox/:thread?": inboxPage,
  "/settings": (route) => settingsPage(context("设置"), route),
}, { fallback: "/" });

function warm() {
  if (cloudState?.state !== "connected" || !visible()) return;
  for (const path of ["/api/devices", "/api/inbox/threads", "/api/plugin-repo"]) prefetch(path);
}

/** xterm.js and noVNC while the window is idle: the 终端 / 远程桌面 pages then draw without them. */
function preload() {
  const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 1500));
  idle(() => { loadVendor().catch(() => {}); loadNoVNC().catch(() => {}); }, { timeout: 5000 });
}

refreshStatus().finally(() => { router.start(); warm(); preload(); });
setInterval(warm, 30000);
setInterval(() => { if (visible()) refreshStatus(); }, 10000);
window.addEventListener("mcpmvisibilitychange", () => { if (visible()) refreshStatus(); });
document.addEventListener("visibilitychange", () => { if (visible()) refreshStatus(); });
