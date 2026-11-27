// Remote terminal page (xterm.js), shared by the gateway console and the client's 设备 page.
import { el, mount, icon, fetchJSON, cachedJSON, toast, errorState, pill, confirmDialog } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";
import { TERMINAL_THEME, TERMINAL_FONT, EXTRA_KEYS, terminalSocketURL, GATEWAY_TERMINAL_API, runCommand, codexPlan } from "/ui/terminal-config.js";

let vendor = null;
/** xterm.js and its fit addon (once per window; the shell preloads them while idle). */
export function loadVendor() {
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
  vendor.catch(() => { vendor = null; });
  return vendor;
}

/** Something to type once the next 终端 page of a device is connected (the agent chat's 在终端登录):
 *  {program, args, note}. Kept in memory only -- a link can never make the terminal type a command. */
const pendingInput = new Map();
export function queueTerminalInput(deviceId, { program, args = [], note = "" } = {}) {
  if (deviceId && program) pendingInput.set(deviceId, { program, args, note, at: Date.now() });
}

function shellKey(deviceId) { return `anx.terminal.shell.${deviceId}`; }
function rememberedShell(deviceId) { try { return localStorage.getItem(shellKey(deviceId)) || ""; } catch { return ""; } }
function rememberShell(deviceId, shell) { try { localStorage.setItem(shellKey(deviceId), shell); } catch { /* private mode */ } }

function findDevice(list, deviceId) {
  return (list?.devices || []).find((d) => d.device_id === deviceId || d.agent_id === deviceId) || null;
}

/** Why this device's terminal cannot open, from the device list ("" = it can). */
function refusal(list, device) {
  if (list.lock?.locked) return "这台设备已锁定，解锁后才能操作其他设备（右上角的锁）";
  if (!device) return "没有这台设备";
  if (!device.online) return `${device.display_name} 现在不在线`;
  if (!device.terminal) return `${device.display_name} 的客户端（${device.version || "旧版本"}）不支持远程终端，升级到 0.3.0 以上后重试`;
  return "";
}

/**
 * The page draws and connects at once: the socket opens with the shell used last time (or the
 * device's default) while the device list, the shell list and the CLI scan arrive -- waiting
 * for them first (a gateway round trip each, abroad and back) kept the page blank for seconds.
 * ``#/terminal/<id>?run=codex`` starts Codex as soon as the shell is up.
 */
export async function terminalPage(ctx, { params, query }, api = GATEWAY_TERMINAL_API) {
  const deviceId = params.device;
  const { page, shell: appShell } = ctx;
  appShell.setTitle("终端", [api.back]);
  page.classList.add("wide");
  let launchOnReady = query?.get?.("run") === "codex";

  let device = findDevice(cachedJSON(api.devices), deviceId);
  let info = null;
  let shellId = rememberedShell(deviceId);
  let failed = false;

  const nameNode = el("span", { class: "title truncate" }, device?.display_name || deviceId);
  const markNode = el("span", { class: "avatar" }, icon("monitor", { size: 16 }));
  const drawMark = () => {
    const mark = device ? osMark(device.os_family) : null;
    if (mark) { markNode.className = "avatar os-mark"; markNode.innerHTML = mark.svg; markNode.title = mark.label; }
  };
  drawMark();
  const status = el("span", { class: "term-status" }, pill("connecting", "连接中"));
  const routeTag = el("span", { class: "tag term-route", hidden: true });
  const select = el("select", { class: "select", "aria-label": "Shell", onchange: () => { shellId = select.value; rememberShell(deviceId, shellId); connect(); } },
    el("option", { value: shellId }, shellId || "默认 Shell"));
  const frame = el("div", { class: "term-frame", tabindex: "-1" });
  const keys = el("div", { class: "term-keys" }, EXTRA_KEYS.map(([label, bytes]) =>
    el("button", { class: "btn btn-sm", type: "button", onclick: () => { send(bytes); term?.focus(); } }, label)));
  const codexButton = el("button", { class: "btn btn-sm btn-primary", type: "button", title: "在这台设备上启动 Codex（OpenAI 的编程 Agent）",
    onclick: () => codex() }, icon("zap", { size: 14 }), "Codex");
  const openExternal = api.external ? el("button", { class: "btn btn-sm", type: "button", title: "在这台电脑的终端（Windows Terminal 等）里打开",
    onclick: async () => {
      try {
        const result = await fetchJSON(api.external(deviceId), { method: "POST", body: { shell: shellId, title: device?.display_name || deviceId } });
        toast(result.opened === "windows-terminal" ? "已在 Windows Terminal 中打开" : "已在新终端窗口中打开");
      } catch { /* toasted */ }
    } }, icon("external-link", { size: 14 }), "在本机终端打开") : null;
  const toolbar = el("div", { class: "term-toolbar" },
    el("div", { class: "row grow", style: { gap: "10px", minWidth: 0 } }, markNode,
      el("div", { class: "stack", style: { gap: 0, minWidth: 0 } }, nameNode, el("span", { class: "sub mono truncate" }, deviceId)),
      status, routeTag),
    el("div", { class: "row", style: { gap: "6px" } }, codexButton, select, openExternal,
      el("button", { class: "btn btn-sm", type: "button", title: "重新连接", onclick: () => connect() }, icon("refresh-cw", { size: 14 }), "重连"),
      el("button", { class: "btn btn-sm btn-icon", type: "button", title: "全屏", "aria-label": "全屏",
        onclick: () => (document.fullscreenElement ? document.exitFullscreen() : frame.parentElement.requestFullscreen?.()) }, icon("maximize", { size: 14 })),
      el("button", { class: "btn btn-sm btn-danger", type: "button", title: "结束会话并返回设备列表", "data-action": "exit",
        onclick: () => {
          closedByUs = true;
          socket?.close();
          if (document.fullscreenElement) document.exitFullscreen();
          location.hash = api.back?.href || "#/devices";
        } }, icon("log-out", { size: 14 }), "退出")));
  const cliHolder = el("div", { class: "card-body" }, el("p", { class: "muted small" }, "正在读取这台设备上的命令行工具…"));
  const cliCount = el("span", { class: "faint xs" });
  const cliCard = el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, "这台设备上的命令行工具"),
      el("div", { class: "row", style: { gap: "8px" } }, cliCount,
        el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "重新检测（刚装了工具时）", onclick: () => loadInfo(true) },
          icon("refresh-cw", { size: 14 }), "刷新"))),
    cliHolder);
  mount(page, el("div", { class: "term-shell" }, toolbar, frame, keys), cliCard);

  let term = null;
  let fit = null;
  let socket = null;
  let observer = null;
  let closedByUs = false;
  let ready = false;
  let left = false;
  ctx.onLeave(() => {
    left = true;
    closedByUs = true;
    observer?.disconnect();
    socket?.close();
    term?.dispose();
    term = null;
    page.classList.remove("wide");
  });
  const encoder = new TextEncoder();
  function send(data) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(typeof data === "string" ? encoder.encode(data) : data);
  }
  function setStatus(state, label) { mount(status, pill(state, label)); }

  /** Replace the page with the reason it cannot work (from the device list or the socket). */
  function fail(error) {
    if (failed) return;
    failed = true;
    closedByUs = true;
    socket?.close();
    term?.dispose();
    term = null;
    mount(page, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "终端"))),
      errorState(error, () => terminalPage(ctx, { params, query }, api)));
  }

  function connect() {
    if (!term) return;
    if (socket) { closedByUs = true; socket.close(); }
    closedByUs = false;
    ready = false;
    term.reset();
    routeTag.hidden = true;
    setStatus("connecting", "连接中");
    const ws = new WebSocket(terminalSocketURL(deviceId, { shell: shellId, cols: term.cols, rows: term.rows }, location, api.socketBase));
    ws.binaryType = "arraybuffer";
    socket = ws;
    ws.onmessage = (event) => {
      if (ws !== socket) return;
      if (typeof event.data !== "string") { term?.write(new Uint8Array(event.data)); return; }
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "route") {
        routeTag.hidden = false;
        routeTag.textContent = message.p2p ? `直连 · ${message.route}` : message.route;
        routeTag.title = message.p2p ? "打洞直连：数据不经过网关" : "经网关转发；直连可用时会自动优先使用";
        routeTag.classList.toggle("is-p2p", !!message.p2p);
      } else if (message.type === "ready") {
        ready = true;
        const opened = message.shell?.id;
        if (opened && opened !== shellId) { shellId = opened; showShells(); }
        setStatus("online", message.shell?.name || "已连接");
        term?.focus();
        if (launchOnReady) { launchOnReady = false; codex(); }
        const queued = pendingInput.get(deviceId);
        pendingInput.delete(deviceId);
        if (queued && Date.now() - queued.at < 120000) {
          send(`${[runCommand(queued.program, shellId), ...queued.args].join(" ")}\r`);
          if (queued.note) toast(queued.note, { type: "info", timeout: 12000 });
        }
      } else if (message.type === "exit") {
        ready = false;
        setStatus("offline", "已退出");
        term?.write(`\r\n\x1b[90m[会话结束${message.code !== null && message.code !== undefined ? `，退出码 ${message.code}` : ""}，点“重连”开新会话]\x1b[0m\r\n`);
      } else if (message.type === "error") {
        ready = false;
        if (/unknown shell/.test(message.error || "") && shellId) {
          // the shell remembered from last time is gone: the device's default instead
          shellId = "";
          rememberShell(deviceId, "");
          connect();
          return;
        }
        setStatus("error", "失败");
        term?.write(`\r\n\x1b[31m${message.error}\x1b[0m\r\n`);
      }
    };
    ws.onclose = (event) => {
      if (ws !== socket || closedByUs) return;
      ready = false;
      if (event.code === 4401) { window.dispatchEvent(new Event("anx:unauthorized")); return; }
      setStatus("offline", "已断开");
    };
    ws.onerror = () => { if (ws === socket) setStatus("error", "连接失败"); };
  }

  // -- what arrives meanwhile: the device list (may refuse), the shells and CLIs
  function showShells() {
    const shells = info?.shells || [];
    if (!shells.length) return;
    const current = shellId || (shells.find((s) => s.default) || shells[0]).id;
    mount(select, shells.map((s) => el("option", { value: s.id, selected: s.id === current, title: s.command }, s.name)));
  }

  const codexCli = () => (info?.clis || []).find((cli) => cli.id === "codex") || null;

  function showClis() {
    const clis = info?.clis || [];
    cliCount.textContent = info?.clis_scanning && !clis.length ? "检测中…" : `${clis.length} 个 · 点击输入`;
    const codexEntry = codexCli();
    codexButton.title = codexEntry ? `在这台设备上启动 Codex（${codexEntry.version || codexEntry.path}）`
      : "这台设备还没有装 Codex：点击用 OpenAI 官方脚本安装，再登录启动";
    mount(cliHolder, clis.length ? el("div", { class: "chips" }, clis.map((cli) =>
      el("button", { class: "chip", type: "button", title: cli.path, onclick: () => { send(`${typed(cli)} `); term?.focus(); } },
        el("span", {}, cli.name), cli.version ? el("span", { class: "mono faint" }, cli.version) : null)))
      : el("p", { class: "muted small" }, info?.clis_scanning ? "正在检测这台设备上的命令行工具，稍后点“刷新”。" : "PATH 里没有找到常见的命令行工具。"));
  }

  /** What a tool chip types: its name, or its full path when it is on no PATH (the codex inside the
   *  ChatGPT / Codex app); PowerShell runs a quoted path only after `&`. */
  const typed = (cli) => {
    if (cli.id === "python") return cli.path.split(/[\\/]/).pop().replace(/\.exe$/i, "");
    return runCommand(cli.command || cli.id, shellId);
  };

  let infoRequest = null;
  function loadInfo(refresh = false) {
    if (refresh) mount(cliHolder, el("p", { class: "muted small" }, "正在重新检测…"));
    infoRequest = fetchJSON(api.info(deviceId) + (refresh ? "?refresh=1" : ""), { quiet: true }).then((fetched) => {
      info = fetched;
      showShells();
      showClis();
      return info;
    }, (error) => {
      mount(cliHolder, el("p", { class: "muted small" }, `读取命令行工具失败：${error.message}`));
      return null;
    });
    return infoRequest;
  }

  /** The Codex button: start it, sign in first, or install it -- typed into this shell. */
  async function codex() {
    if (!ready) { launchOnReady = true; toast("连上后自动启动 Codex"); return; }
    if (!info) await infoRequest;
    if (!info) { toast("还没读到这台设备上的工具列表，稍后再试", { type: "warn" }); return; }
    const plan = codexPlan({ platform: info?.platform || device?.platform || "", shell: shellId, cli: codexCli() });
    if (plan.kind === "install") {
      const yes = await confirmDialog({ title: `在 ${device?.display_name || deviceId} 上安装 Codex？`, confirmLabel: "安装并启动",
        message: `这台设备上没有检测到 Codex。将在下面的终端里运行 OpenAI 官方安装脚本（${plan.install}），装到当前用户目录，然后用设备码登录 ChatGPT 并启动。` });
      if (!yes) return;
    }
    if (plan.kind !== "launch") {
      toast("登录用设备码：在你自己的浏览器打开终端里显示的网址，输入验证码。需先在 ChatGPT 的安全设置里开启“设备码登录”。",
        { title: "Codex 还没登录", timeout: 12000 });
    }
    send(`${plan.text}\r`);
    term?.focus();
    if (plan.kind === "install") setTimeout(() => loadInfo(true), 60000);
  }

  loadInfo();
  fetchJSON(api.devices, { quiet: true }).then((list) => {
    device = findDevice(list, deviceId) || device;
    nameNode.textContent = device?.display_name || deviceId;
    drawMark();
    const why = refusal(list, findDevice(list, deviceId));
    if (why && !ready && !left) fail(new Error(why));
  }, () => { /* the socket says what is wrong */ });

  try {
    await loadVendor();
  } catch (error) {
    fail(error);
    return;
  }
  if (failed || left) return;
  term = new window.Terminal({
    fontFamily: TERMINAL_FONT, fontSize: 14, lineHeight: 1.15, cursorBlink: true, scrollback: 5000,
    theme: TERMINAL_THEME, allowProposedApi: false, macOptionIsMeta: true,
  });
  fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(frame);
  fit.fit();
  term.onData((data) => send(data));
  term.onBinary((data) => send(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff)));
  term.onResize(({ cols, rows }) => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows }));
  });
  const refit = () => { try { fit.fit(); } catch { /* hidden */ } };
  observer = new ResizeObserver(refit);
  observer.observe(frame);
  connect();
}
