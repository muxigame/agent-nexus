// Mockup harness: renders the real shell from ui.js around <template id="content">.
// Serve the repository root over HTTP (ES modules do not load from file://):
//   uv run python -m http.server 8765   →   http://127.0.0.1:8765/docs/mockups/cloud-overview.html
// Query flags: ?open=<template id> opens a modal/drawer, ?theme=dark|light forces a theme.
import { renderShell, hydrateIcons, modal, drawer, toast, copy, setTheme, setStyle, el, logoSVG, appearanceButton } from "../../agent_nexus/static/ui/ui.js";

const NAV = {
  cloud: {
    sub: "云端控制台",
    status: { state: "online", label: "网关在线 · v0.2.0" },
    nav: [
      { path: "/", label: "概览", icon: "layout-dashboard", primary: true },
      { path: "/devices", label: "设备", icon: "monitor", primary: true },
      { path: "/connectors", label: "连接器", icon: "plug", primary: true, short: "连接器" },
      { path: "/plugins", label: "插件", icon: "puzzle" },
      { path: "/inbox", label: "收件箱", icon: "inbox", badge: 3, primary: true },
      { section: "其他" },
      { path: "/downloads", label: "下载", icon: "download" },
      { path: "/settings", label: "设置", icon: "settings" },
    ],
    footer: [{ label: "退出登录", icon: "log-out" }],
  },
  local: {
    sub: "本机",
    status: { state: "online", label: "已连接 hub.example.com" },
    nav: [
      { path: "/", label: "概览", icon: "layout-dashboard", primary: true },
      { path: "/plugins", label: "插件", icon: "puzzle", primary: true },
      { path: "/cloud", label: "连接云端", icon: "cloud", primary: true, short: "云端" },
      { path: "/inbox", label: "收件箱", icon: "inbox", badge: 3, primary: true },
      { section: "其他" },
      { path: "/settings", label: "设置", icon: "settings" },
    ],
    footer: [],
  },
};

const params = new URLSearchParams(location.search);
if (params.get("theme")) setTheme(params.get("theme"));
// ?style=auto|pure|mica picks the shipped appearance style (style-pure.css / style-mica.css, see ui.js setStyle).
if (params.get("style")) setStyle(params.get("style"));
// ?skin=<name> layers docs/mockups/skins/<name>.css over tokens.css + ui.css (style exploration);
// the shipped style is switched off so the two do not mix.
if (params.get("skin")) {
  delete document.documentElement.dataset.style;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL(`skins/${params.get("skin")}.css`, import.meta.url).href;
  document.head.append(link);
}

const body = document.body;
const kind = body.dataset.shell;
if (kind) {
  const config = NAV[kind];
  if (body.dataset.status) config.status = JSON.parse(body.dataset.status);
  const shell = renderShell(body, { ...config, active: body.dataset.active || "/" });
  shell.setTitle(body.dataset.title || "", [{ label: config.sub, href: "#/" }]);
  shell.setActions(
    appearanceButton(),
    el("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "帮助", html: '<i data-icon="circle-help"></i>' }));
  shell.page.append(document.getElementById("content").content.cloneNode(true));
  if (body.dataset.wide) shell.page.classList.add("wide");
} else {
  const content = document.getElementById("content");
  if (content) body.prepend(content.content.cloneNode(true));
}
hydrateIcons();
for (const node of document.querySelectorAll("[data-logo]")) node.outerHTML = logoSVG(Number(node.dataset.logo) || 32);


function openTemplate(id) {
  const tpl = document.getElementById(id);
  if (!tpl) return;
  const fragment = tpl.content.cloneNode(true);
  const left = fragment.querySelector("[data-slot=left]");
  if (left) left.remove();
  const actions = JSON.parse(tpl.dataset.actions || "[]").map((action) => ({
    ...action, onClick: action.toast ? () => { toast(action.toast); } : undefined,
  }));
  const open = tpl.dataset.kind === "drawer" ? drawer : modal;
  open({ title: tpl.dataset.title, description: tpl.dataset.desc, size: tpl.dataset.size || "", body: fragment, actions, footLeft: left });
}

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-open]");
  if (opener) { event.preventDefault(); openTemplate(opener.dataset.open); }
  const toaster = event.target.closest("[data-toast]");
  if (toaster) toast(toaster.dataset.toast, { type: toaster.dataset.toastType || "ok" });
  const copier = event.target.closest("[data-copy]");
  if (copier) copy(copier.dataset.copy);
  const seg = event.target.closest(".segmented button");
  if (seg) {
    for (const b of seg.parentElement.children) b.setAttribute("aria-pressed", String(b === seg));
    const group = seg.closest("[data-seg-group]");
    if (group) for (const pane of group.querySelectorAll("[data-pane]")) pane.hidden = pane.dataset.pane !== seg.dataset.value;
  }
});

if (params.get("open")) openTemplate(params.get("open"));
if (params.get("toast")) toast(params.get("toast"), { title: params.get("toastTitle") || "" });
