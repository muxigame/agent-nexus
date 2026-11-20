// The local console (docs/ui-spec.md §4): shell, routing, polling. Runs in the Tauri window and any local browser.
import { renderShell, createRouter, fetchJSON, el, mount, setTheme, restoreTheme, icon } from "/ui/ui.js";
import { mountInbox } from "/ui/inbox.js";
import { hubHost } from "/ui/invite.js";
import { overviewPage } from "/local/overview.js";
import { pluginsPage } from "/local/plugins.js";
import { cloudPage } from "/local/cloud.js";
import { settingsPage } from "/local/settings.js";
import { devicesPage, lockButton } from "/local/devices.js";
import { terminalPage } from "/ui/terminal.js";
import { CLIENT_TERMINAL_API } from "/ui/terminal-config.js";

restoreTheme();

// "/cloud?invite=…" (the old join page) and "#/enroll?invite=…" (agentnexus:// deep links) land on #/cloud.
if (location.pathname === "/cloud") {
  history.replaceState(null, "", `/#/cloud${location.search}`);
} else if (location.hash.startsWith("#/enroll")) {
  history.replaceState(null, "", `/#/cloud${location.hash.slice("#/enroll".length)}`);
}

const NAV = [
  { path: "/", label: "概览", icon: "layout-dashboard", primary: true },
  { path: "/plugins", label: "插件", icon: "puzzle", primary: true },
  { path: "/devices", label: "设备", icon: "monitor", primary: true },
  { path: "/cloud", label: "连接云端", icon: "cloud", primary: true, short: "云端" },
  { path: "/inbox", label: "收件箱", icon: "inbox", primary: true },
  { section: "其他" },
  { path: "/settings", label: "设置", icon: "settings" },
];

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  setTheme(dark ? "light" : "dark");
}

const shell = renderShell(document.body, { sub: "本机", nav: NAV, status: { state: "connecting", label: "正在读取…" } });
shell.setActions(lockButton(), el("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "切换主题", title: "切换主题", onclick: toggleTheme },
  icon("sun", { size: 18 })));

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

const router = createRouter({
  "/": (route) => overviewPage(context("概览"), route),
  "/plugins/:name?": (route) => pluginsPage(context("插件"), route),
  "/cloud": (route) => cloudPage(context("连接云端"), route),
  "/devices": (route) => devicesPage(context("设备"), route),
  "/terminal/:device": (route) => terminalPage(context("终端", { wide: true }), route, CLIENT_TERMINAL_API),
  "/inbox/:thread?": inboxPage,
  "/settings": (route) => settingsPage(context("设置"), route),
}, { fallback: "/" });

refreshStatus().finally(() => router.start());
setInterval(() => { if (visible()) refreshStatus(); }, 10000);
window.addEventListener("mcpmvisibilitychange", () => { if (visible()) refreshStatus(); });
document.addEventListener("visibilitychange", () => { if (visible()) refreshStatus(); });
