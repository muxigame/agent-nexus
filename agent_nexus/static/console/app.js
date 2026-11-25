// The gateway console (docs/ui-spec.md §3): shell, routing, polling.
import { renderShell, createRouter, fetchJSON, el, mount, setTheme, restoreTheme, icon } from "/ui/ui.js";
import { mountInbox } from "/ui/inbox.js";
import { overviewPage, devicesPage, settingsPage, pluginsPage } from "/console/pages.js";
import { renderDownloads } from "/console/downloads.js";
import { connectorsPage } from "/console/connectors.js";
import { terminalPage } from "/ui/terminal.js";
import { topologyPage } from "/console/topology.js";

restoreTheme();
const NAV = [
  { path: "/", label: "概览", icon: "layout-dashboard", primary: true },
  { path: "/devices", label: "设备", icon: "monitor", primary: true },
  { path: "/topology", label: "拓扑", icon: "network" },
  { path: "/connectors", label: "连接器", icon: "plug", primary: true },
  { path: "/plugins", label: "插件", icon: "puzzle" },
  { path: "/inbox", label: "收件箱", icon: "inbox", primary: true },
  { section: "其他" },
  { path: "/downloads", label: "下载", icon: "download" },
  { path: "/settings", label: "设置", icon: "settings" },
];

window.addEventListener("anx:unauthorized", () => {
  location.href = `/auth/login?next=${encodeURIComponent("/" + location.hash)}`;
});

async function logout() {
  try { await fetchJSON("/auth/logout", { method: "POST", quiet: true }); } catch { /* signed out anyway */ }
  location.href = "/auth/login";
}

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  setTheme(dark ? "light" : "dark");
}

const shell = renderShell(document.body, {
  sub: "云端控制台", nav: NAV, status: { state: "connecting", label: "正在连接网关…" },
  footer: [{ label: "退出登录", icon: "log-out", onClick: logout }],
});
shell.setActions(el("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "切换主题", title: "切换主题", onclick: toggleTheme }, icon("sun", { size: 18 })));

/** Everything a page needs: where to draw, the shell, and a way to poll while it is shown. */
let cleanup = [];
function context(title, { wide = false } = {}) {
  for (const fn of cleanup) fn();
  cleanup = [];
  shell.setTitle(title, [{ label: "云端控制台", href: "#/" }]);
  shell.page.classList.toggle("wide", wide);
  mount(shell.page);
  window.scrollTo(0, 0);
  return {
    page: shell.page, shell,
    every(ms, fn) {
      const timer = setInterval(() => { if (!document.hidden) fn(); }, ms);
      cleanup.push(() => clearInterval(timer));
    },
    onLeave(fn) { cleanup.push(fn); },
  };
}

let gatewayInfo = null;
async function refreshStatus() {
  try {
    const summary = await fetchJSON("/api/inbox/summary", { quiet: true });
    shell.setBadge("/inbox", (summary.unread || 0) + (summary.open_asks || 0));
  } catch { /* the mailbox may be disabled */ }
  try {
    const health = await fetchJSON("/healthz", { quiet: true });
    gatewayInfo = health;
    shell.setStatus("online", `网关在线 · v${health.version}`);
  } catch {
    shell.setStatus("offline", "网关不可达");
  }
}

function inboxPage({ params }) {
  const ctx = context("收件箱", { wide: true });
  const holder = el("div");
  const head = el("div", { class: "page-head" },
    el("div", { class: "titles" }, el("h1", {}, "收件箱"),
      el("p", {}, "Agent 发给你（", el("span", { class: "mono" }, "me"), "）的邮件和提问。回复会唤醒对方的会话。")),
    el("div", { class: "actions" }));
  mount(ctx.page, head, holder);
  const inbox = mountInbox(holder, {
    base: "/api/inbox", threadId: params.thread || null,
    onUnread: (unread, asks) => { if (unread !== null) shell.setBadge("/inbox", unread + asks); },
    onOpen: (id) => history.replaceState(null, "", id ? `#/inbox/${encodeURIComponent(id)}` : "#/inbox"),
  });
  mount(head.querySelector(".actions"), inbox.viewerSelect, inbox.composeButton);
  inbox.refresh();
  ctx.every(10000, () => inbox.refresh());
}

async function downloadsPage() {
  const ctx = context("下载");
  const holder = el("div", { class: "stack xl" });
  mount(ctx.page, holder);
  await renderDownloads(holder, { inviteHref: "#/devices?invite=1" });
}

const router = createRouter({
  "/": (route) => overviewPage(context("概览"), route),
  "/devices": (route) => devicesPage(context("设备"), route),
  "/topology": (route) => topologyPage(context("拓扑", { wide: true }), route),
  "/connectors": (route) => connectorsPage(context("连接器"), route),
  "/plugins/:device?": (route) => pluginsPage(context("插件"), route),
  "/terminal/:device": (route) => terminalPage(context("终端", { wide: true }), route),
  "/inbox/:thread?": inboxPage,
  "/downloads": downloadsPage,
  "/settings": (route) => settingsPage(context("设置"), route),
}, { fallback: "/" });
router.start();
refreshStatus();
setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
export { gatewayInfo };
