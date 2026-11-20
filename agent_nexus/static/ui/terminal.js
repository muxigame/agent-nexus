// Remote terminal page (xterm.js), shared by the gateway console and the client's 设备 page.
import { el, mount, icon, fetchJSON, toast, emptyState, errorState, pill } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";
import { TERMINAL_THEME, TERMINAL_FONT, EXTRA_KEYS, terminalSocketURL, GATEWAY_TERMINAL_API } from "/ui/terminal-config.js";

let vendor = null;
function loadVendor() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (vendor) return vendor;
  const script = (src) => new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = src; tag.onload = resolve; tag.onerror = () => reject(new Error(`无法加载 ${src}`));
    document.head.append(tag);
  });
  if (!document.querySelector('link[href="/ui/xterm.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/ui/xterm.css" }));
  }
  vendor = script("/ui/xterm.js").then(() => script("/ui/addon-fit.js"));
  return vendor;
}

function shellKey(deviceId) { return `anx.terminal.shell.${deviceId}`; }
function rememberedShell(deviceId) { try { return localStorage.getItem(shellKey(deviceId)) || ""; } catch { return ""; } }
function rememberShell(deviceId, shell) { try { localStorage.setItem(shellKey(deviceId), shell); } catch { /* private mode */ } }

export async function terminalPage(ctx, { params }, api = GATEWAY_TERMINAL_API) {
  const deviceId = params.device;
  const { page, shell: appShell } = ctx;
  appShell.setTitle("终端", [api.back]);
  page.classList.add("wide");
  mount(page, el("div", { class: "stack" }, el("span", { class: "skeleton w-40" }), el("div", { class: "term-frame skeleton-block" })));

  let device = null;
  let info = null;
  try {
    const list = await fetchJSON(api.devices, { quiet: true });
    if (list.lock?.locked) throw new Error("这台设备已锁定，解锁后才能操作其他设备（右上角的锁）");
    device = (list.devices || []).find((d) => d.device_id === deviceId || d.agent_id === deviceId);
    if (!device) throw new Error("没有这台设备");
    if (!device.online) throw new Error(`${device.display_name} 现在不在线`);
    if (!device.terminal) throw new Error(`${device.display_name} 的客户端（${device.version || "旧版本"}）不支持远程终端，升级到 0.3.0 以上后重试`);
    info = await fetchJSON(api.info(deviceId), { quiet: true });
    await loadVendor();
  } catch (error) {
    mount(page, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "终端"))),
      errorState(error, () => terminalPage(ctx, { params }, api)));
    return;
  }

  const shells = info.shells || [];
  if (!shells.length) {
    mount(page, emptyState({ icon: "terminal", title: "这台设备上没有找到可用的 shell" }));
    return;
  }
  let shellId = rememberedShell(deviceId);
  if (!shells.some((s) => s.id === shellId)) shellId = (shells.find((s) => s.default) || shells[0]).id;

  const mark = osMark(device.os_family);
  const status = el("span", { class: "term-status" }, pill("connecting", "连接中"));
  const routeTag = el("span", { class: "tag term-route", hidden: true });
  const select = el("select", { class: "select", "aria-label": "Shell", onchange: () => { shellId = select.value; rememberShell(deviceId, shellId); connect(); } },
    shells.map((s) => el("option", { value: s.id, selected: s.id === shellId, title: s.command }, s.name)));
  const frame = el("div", { class: "term-frame", tabindex: "-1" });
  const keys = el("div", { class: "term-keys" }, EXTRA_KEYS.map(([label, bytes]) =>
    el("button", { class: "btn btn-sm", type: "button", onclick: () => { send(bytes); term.focus(); } }, label)));
  const clis = info.clis || [];
  const openExternal = api.external ? el("button", { class: "btn btn-sm", type: "button", title: "在这台电脑的终端（Windows Terminal 等）里打开",
    onclick: async () => {
      try {
        const result = await fetchJSON(api.external(deviceId), { method: "POST", body: { shell: shellId, title: device.display_name } });
        toast(result.opened === "windows-terminal" ? "已在 Windows Terminal 中打开" : "已在新终端窗口中打开");
      } catch { /* toasted */ }
    } }, icon("external-link", { size: 14 }), "在本机终端打开") : null;
  const toolbar = el("div", { class: "term-toolbar" },
    el("div", { class: "row grow", style: { gap: "10px", minWidth: 0 } },
      mark ? el("span", { class: "avatar os-mark", html: mark.svg, title: mark.label }) : el("span", { class: "avatar" }, icon("monitor", { size: 16 })),
      el("div", { class: "stack", style: { gap: 0, minWidth: 0 } }, el("span", { class: "title truncate" }, device.display_name),
        el("span", { class: "sub mono truncate" }, device.device_id)),
      status, routeTag),
    el("div", { class: "row", style: { gap: "6px" } }, select, openExternal,
      el("button", { class: "btn btn-sm", type: "button", title: "重新连接", onclick: () => connect() }, icon("refresh-cw", { size: 14 }), "重连"),
      el("button", { class: "btn btn-sm btn-icon", type: "button", title: "全屏", "aria-label": "全屏",
        onclick: () => (document.fullscreenElement ? document.exitFullscreen() : frame.parentElement.requestFullscreen?.()) }, icon("maximize", { size: 14 }))));
  const cliCard = el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, "这台设备上的命令行工具"), el("span", { class: "faint xs" }, `${clis.length} 个 · 点击输入`)),
    el("div", { class: "card-body" }, clis.length ? el("div", { class: "chips" }, clis.map((cli) =>
      el("button", { class: "chip", type: "button", title: cli.path, onclick: () => { send(`${cli.id === "python" ? cli.path.split(/[\\/]/).pop().replace(/\.exe$/i, "") : cli.id} `); term.focus(); } },
        el("span", {}, cli.name), cli.version ? el("span", { class: "mono faint" }, cli.version) : null)))
      : el("p", { class: "muted small" }, "PATH 里没有找到常见的命令行工具。")));
  mount(page, el("div", { class: "term-shell" }, toolbar, frame, keys), cliCard);

  const term = new window.Terminal({
    fontFamily: TERMINAL_FONT, fontSize: 14, lineHeight: 1.15, cursorBlink: true, scrollback: 5000,
    theme: TERMINAL_THEME, allowProposedApi: false, macOptionIsMeta: true,
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(frame);
  fit.fit();

  const encoder = new TextEncoder();
  let socket = null;
  let closedByUs = false;
  function send(data) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(typeof data === "string" ? encoder.encode(data) : data);
  }
  function setStatus(state, label) { mount(status, pill(state, label)); }

  function connect() {
    if (socket) { closedByUs = true; socket.close(); }
    closedByUs = false;
    term.reset();
    routeTag.hidden = true;
    setStatus("connecting", "连接中");
    const ws = new WebSocket(terminalSocketURL(deviceId, { shell: shellId, cols: term.cols, rows: term.rows }, location, api.socketBase));
    ws.binaryType = "arraybuffer";
    socket = ws;
    ws.onmessage = (event) => {
      if (typeof event.data !== "string") { term.write(new Uint8Array(event.data)); return; }
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "route") {
        routeTag.hidden = false;
        routeTag.textContent = message.p2p ? `直连 · ${message.route}` : message.route;
        routeTag.title = message.p2p ? "打洞直连：数据不经过网关" : "经网关转发；直连可用时会自动优先使用";
        routeTag.classList.toggle("is-p2p", !!message.p2p);
      } else if (message.type === "ready") {
        setStatus("online", message.shell?.name || "已连接");
        term.focus();
      } else if (message.type === "exit") {
        setStatus("offline", "已退出");
        term.write(`\r\n\x1b[90m[会话结束${message.code !== null && message.code !== undefined ? `，退出码 ${message.code}` : ""}，点“重连”开新会话]\x1b[0m\r\n`);
      } else if (message.type === "error") {
        setStatus("error", "失败");
        term.write(`\r\n\x1b[31m${message.error}\x1b[0m\r\n`);
      }
    };
    ws.onclose = (event) => {
      if (ws !== socket || closedByUs) return;
      if (event.code === 4401) { window.dispatchEvent(new Event("anx:unauthorized")); return; }
      setStatus("offline", "已断开");
    };
    ws.onerror = () => { if (ws === socket) setStatus("error", "连接失败"); };
  }

  term.onData((data) => send(data));
  term.onBinary((data) => send(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff)));
  term.onResize(({ cols, rows }) => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows }));
  });
  const refit = () => { try { fit.fit(); } catch { /* hidden */ } };
  const observer = new ResizeObserver(refit);
  observer.observe(frame);
  connect();

  ctx.onLeave(() => {
    closedByUs = true;
    observer.disconnect();
    socket?.close();
    term.dispose();
    page.classList.remove("wide");
  });
}
