// The client's 远程桌面: noVNC over the P2P tunnel to another device's built-in VNC (agent_nexus/vnc.py).
// The page fills the content area: a slim bar (device, route, live frame rate and bit rate, quality,
// view, keys, full screen) over the remote screen. A dropped connection reconnects by itself.
import { el, mount, icon, fetchJSON, cachedJSON, toast, errorState, pill } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";
import { SteadyH264Decoder } from "/local/desktop-h264.js";

export const ENCODING_H264 = 50;
/** the quality levels offered (noVNC 0..9; the server maps them to a bit-rate ceiling and a size) */
export const QUALITIES = [
  [8, "自动", "清晰优先，网速不够时自动降低码率；大屏缩到约 1080p"],
  [9, "原画", "原始分辨率、最高码率（需要好网络）"],
  [6, "均衡", "码率上限 8 Mbps"],
  [3, "省流", "码率上限 4 Mbps"],
  [1, "极省", "码率上限 2 Mbps，适合很差的网络"],
];
const DEFAULT_QUALITY = 8;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

/** WebCodecs can decode H.264 here (hardware where the GPU allows). noVNC's own probe also
 *  decodes a tiny test frame, which Windows' hardware decoders often refuse -- and then every
 *  session fell back to JPEG -- so the configuration check is what decides. */
export async function canDecodeH264() {
  if (!("VideoDecoder" in window)) return false;
  for (const hardwareAcceleration of ["prefer-hardware", "no-preference"]) {
    try {
      const result = await VideoDecoder.isConfigSupported({ codec: "avc1.42401f", codedWidth: 1920, codedHeight: 1080,
        optimizeForLatency: true, hardwareAcceleration });
      if (result.supported) return true;
    } catch { /* try the next */ }
  }
  return false;
}

let novnc = null;
/** noVNC's RFB (57 small modules: once per window; the shell preloads them while idle). */
export function loadNoVNC() {
  if (!novnc) {
    novnc = Promise.all([import("/vendor/novnc/core/rfb.js"), import("/vendor/novnc/core/decoders/h264.js")])
      .then(([rfb, h264]) => withH264(rfb.default, h264.H264Parser));
    novnc.catch(() => { novnc = null; });
  }
  return novnc;
}

/** noVNC with H.264 offered whenever this client can decode it, our steady decoder, and counters. */
function withH264(RFB, Parser) {
  return class extends RFB {
    constructor(...args) {
      super(...args);
      this.anxStats = { updates: 0 };
      this.anxEvents = {};
      if ("VideoDecoder" in window) {
        this.anxDecoder = new SteadyH264Decoder({ Parser, VideoDecoder: window.VideoDecoder, EncodedVideoChunk: window.EncodedVideoChunk }, {
          keyFrame: () => this.anxRequestKeyFrame(),
          software: () => this.anxEvents.software?.(),
          giveUp: () => this.anxEvents.giveUp?.(),
        });
        this._decoders[ENCODING_H264] = this.anxDecoder;
      }
    }

    /** A full update request: the server answers with a key frame. */
    anxRequestKeyFrame() {
      try {
        if (this._rfbConnectionState === "connected") {
          RFB.messages.fbUpdateRequest(this._sock, false, 0, 0, this._fbWidth, this._fbHeight);
        }
      } catch { /* closing */ }
    }

    _framebufferUpdate() {
      const done = super._framebufferUpdate();
      if (done) this.anxStats.updates++;
      return done;
    }

    _sendEncodings() {
      const original = RFB.messages.clientEncodings;
      const offer = this.anxH264;
      RFB.messages.clientEncodings = (sock, encodings) => {
        if (offer && !encodings.includes(ENCODING_H264)) encodings.splice(1, 0, ENCODING_H264);
        if (!offer) encodings = encodings.filter((e) => e !== ENCODING_H264);
        return original.call(RFB.messages, sock, encodings);
      };
      try { super._sendEncodings(); } finally { RFB.messages.clientEncodings = original; }
    }

    disconnect() {
      this.anxDecoder?.close();
      super.disconnect();
    }
  };
}

export function desktopSocketURL(deviceId, loc = location) {
  const scheme = loc.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${loc.host}/api/remote/devices/${encodeURIComponent(deviceId)}/desktop/ws`;
}

/** "2.4 Mbps" / "860 kbps" */
export function bitrateLabel(bitsPerSecond) {
  if (!(bitsPerSecond > 0)) return "0 kbps";
  return bitsPerSecond >= 1e6 ? `${(bitsPerSecond / 1e6).toFixed(bitsPerSecond >= 1e7 ? 0 : 1)} Mbps` : `${Math.round(bitsPerSecond / 1e3)} kbps`;
}

const prefKey = (deviceId) => `anx.desktop.${deviceId}`;
function loadPrefs(deviceId) {
  try { return JSON.parse(localStorage.getItem(prefKey(deviceId)) || "{}") || {}; } catch { return {}; }
}
function savePrefs(deviceId, prefs) {
  try { localStorage.setItem(prefKey(deviceId), JSON.stringify(prefs)); } catch { /* private mode */ }
}

/** Why this device's desktop cannot open, from the device list ("" = it can). */
function refusal(list, device) {
  if (list.lock?.locked) return "这台设备已锁定，解锁后才能操作其他设备（右上角的锁）";
  if (!device) return "没有这台设备";
  if (!device.online) return `${device.display_name} 现在不在线`;
  if (!device.desktop) return `${device.display_name} 没有可共享的桌面（无桌面的服务器只能用终端），或客户端低于 0.5.0`;
  return "";
}

/** The page draws at once and connects as soon as the tunnel is prepared: the device list (a
 *  gateway round trip) is checked meanwhile instead of first. */
export async function desktopPage(ctx, { params }) {
  const deviceId = params.device;
  const { page, shell: appShell } = ctx;
  appShell.setTitle("远程桌面", [{ label: "设备", href: "#/devices" }]);
  page.classList.add("wide", "page-desktop");

  const findDevice = (list) => (list?.devices || []).find((d) => d.device_id === deviceId) || null;
  let device = findDevice(cachedJSON("/api/devices"));
  // the tunnel (and the socket's ticket) is prepared while the page loads, not after it
  let preparing = fetchJSON(`/api/remote/devices/${encodeURIComponent(deviceId)}/desktop/prepare`, { method: "POST", quiet: true, keepCache: true })
    .catch((error) => ({ failed: error }));
  const h264Check = canDecodeH264();
  const prefs = { quality: DEFAULT_QUALITY, fit: true, ...loadPrefs(deviceId) };
  /** 自动 (8) needs a device of this release (desktop/2: flow control, adaptive rate, scaling); an older
   *  one would take 8 as a fixed 14 Mbps at full size, so it gets 均衡 (6), as the old viewer sent. */
  const sentQuality = () => (prefs.quality === DEFAULT_QUALITY && !device?.desktop_auto ? 6 : prefs.quality);
  let failed = false;
  let leaving = false;
  fetchJSON("/api/devices", { quiet: true }).then((list) => {
    device = findDevice(list) || device;
    nameNode.textContent = device?.display_name || deviceId;
    drawMark();
    if (rfb) rfb.qualityLevel = sentQuality();
    const why = refusal(list, findDevice(list));
    if (why && !connected && !leaving) fail(new Error(why));
  }, () => { /* the connection says what is wrong */ });

  function fail(error) {
    if (failed) return;
    failed = true;
    stop();
    mount(page, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "远程桌面"))),
      errorState(error, () => { page.classList.remove("page-desktop"); desktopPage(ctx, { params }); }));
    page.classList.remove("page-desktop");
  }

  let h264 = false;
  let jpegOnly = false;  // H.264 gave up on this page: JPEG from now on
  let connected = false;
  let rfb = null;
  let attempts = 0;
  /** each connect() takes a number; one that a newer call overtook stops at its next step */
  let connecting = 0;
  let retryTimer = null;
  let statsTimer = null;

  // -- the bar
  const nameNode = el("span", { class: "desktop-name truncate" }, device?.display_name || deviceId);
  const markNode = el("span", { class: "desktop-mark" }, icon("monitor", { size: 15 }));
  const drawMark = () => {
    const mark = device ? osMark(device.os_family) : null;
    if (mark) { markNode.innerHTML = mark.svg; markNode.title = mark.label; }
  };
  drawMark();
  const status = el("span", { class: "desktop-status" }, pill("connecting", "正在直连"));
  const routeTag = el("span", { class: "desktop-chip", hidden: true });
  const statsTag = el("span", { class: "desktop-chip mono", hidden: true, title: "画面帧率与码率（每秒刷新）" });
  const quality = el("select", { class: "desktop-select", "aria-label": "画质", title: "画质",
    onchange: () => {
      prefs.quality = Number(quality.value);
      savePrefs(deviceId, prefs);
      if (rfb) rfb.qualityLevel = sentQuality();
      quality.title = QUALITIES.find(([v]) => v === prefs.quality)?.[2] || "画质";
    } },
  QUALITIES.map(([v, label, hint]) => el("option", { value: v, selected: v === prefs.quality, title: hint }, `画质 · ${label}`)));
  quality.title = QUALITIES.find(([v]) => v === prefs.quality)?.[2] || "画质";
  const fitButton = el("button", { class: "desktop-btn", type: "button", onclick: () => {
    prefs.fit = !prefs.fit;
    savePrefs(deviceId, prefs);
    applyFit();
  } });
  const fullButton = el("button", { class: "desktop-btn", type: "button", "aria-label": "全屏",
    onclick: () => (document.fullscreenElement ? document.exitFullscreen() : shell.requestFullscreen?.()) });
  const keysMenu = el("div", { class: "desktop-menu", hidden: true },
    keyItem("Ctrl + Alt + Del", () => rfb?.sendCtrlAltDel()),
    keyItem("Win（开始菜单）", () => tap(0xFFEB, "MetaLeft")),
    keyItem("Alt + Tab", () => combo([[0xFFE9, "AltLeft"], [0xFF09, "Tab"]])),
    keyItem("Win + D（显示桌面）", () => combo([[0xFFEB, "MetaLeft"], [0x0064, "KeyD"]])),
    keyItem("PrintScreen", () => tap(0xFF61, "PrintScreen")));
  const keysButton = el("button", { class: "desktop-btn", type: "button", title: "发送组合键", "aria-haspopup": "menu",
    onclick: (event) => { event.stopPropagation(); keysMenu.hidden = !keysMenu.hidden; } }, icon("keyboard", { size: 15 }), el("span", { class: "label" }, "按键"));
  document.addEventListener("click", closeMenu);
  function closeMenu() { keysMenu.hidden = true; }
  function keyItem(label, run) {
    return el("button", { class: "desktop-menu-item", type: "button", onclick: () => { run(); closeMenu(); rfb?.focus(); } }, label);
  }
  function tap(keysym, code) { rfb?.sendKey(keysym, code, true); rfb?.sendKey(keysym, code, false); }
  function combo(keys) {
    for (const [keysym, code] of keys) rfb?.sendKey(keysym, code, true);
    for (const [keysym, code] of [...keys].reverse()) rfb?.sendKey(keysym, code, false);
  }

  const bar = el("div", { class: "desktop-bar" },
    el("div", { class: "desktop-bar-left" }, markNode,
      el("div", { class: "desktop-titles" }, nameNode, el("span", { class: "desktop-id mono truncate" }, deviceId)),
      status, routeTag, statsTag),
    el("div", { class: "desktop-bar-right" }, quality, fitButton,
      el("div", { class: "desktop-keys" }, keysButton, keysMenu),
      el("button", { class: "desktop-btn", type: "button", title: "重新连接", onclick: () => { attempts = 0; connect(); } },
        icon("refresh-cw", { size: 15 }), el("span", { class: "label" }, "重连")),
      fullButton,
      el("button", { class: "desktop-btn danger", type: "button", title: "断开远程桌面并返回设备列表", "data-action": "exit",
        onclick: () => {
          stop();
          if (document.fullscreenElement) document.exitFullscreen();
          location.hash = "#/devices";
        } }, icon("log-out", { size: 15 }), el("span", { class: "label" }, "退出"))));

  // -- the screen, with an overlay for every state that is not a live picture
  const screen = el("div", { class: "desktop-screen" });
  const overlayText = el("div", { class: "desktop-overlay-text" });
  const overlayHint = el("div", { class: "desktop-overlay-hint" });
  const overlayAction = el("div", { class: "desktop-overlay-action" });
  const spinner = el("span", { class: "desktop-spinner" }, icon("loader-circle", { size: 22 }));
  const overlay = el("div", { class: "desktop-overlay" }, spinner, overlayText, overlayHint, overlayAction);
  const reveal = el("div", { class: "desktop-reveal", "aria-hidden": "true" });
  const shell = el("div", { class: "desktop-shell" }, reveal, bar, el("div", { class: "desktop-stage" }, screen, overlay));
  mount(page, shell);

  function showOverlay(text, { hint = "", busy = true, action = null } = {}) {
    overlay.hidden = false;
    spinner.hidden = !busy;
    overlayText.textContent = text;
    overlayHint.textContent = hint;
    mount(overlayAction, action);
  }
  function hideOverlay() { overlay.hidden = true; }
  showOverlay("正在建立直连…", { hint: "首次连接要打洞，可能需要几秒" });

  function applyFit() {
    fitButton.title = prefs.fit ? "当前：适应窗口（点击改为原始大小）" : "当前：原始大小（点击改为适应窗口）";
    mount(fitButton, icon(prefs.fit ? "minimize" : "maximize", { size: 15 }), el("span", { class: "label" }, prefs.fit ? "适应" : "1:1"));
    screen.classList.toggle("native", !prefs.fit);
    if (rfb) { rfb.scaleViewport = prefs.fit; rfb.clipViewport = false; }
  }
  applyFit();
  const fullscreenChanged = () => {
    const on = document.fullscreenElement === shell;
    mount(fullButton, icon(on ? "minimize" : "maximize", { size: 15 }));
    fullButton.title = on ? "退出全屏（Esc）" : "全屏";
  };
  document.addEventListener("fullscreenchange", fullscreenChanged);
  fullscreenChanged();

  // -- live numbers: updates per second and bits per second, from the socket and the decoder
  let sample = null;
  function readStats() {
    if (!rfb || !connected) return;
    const now = performance.now();
    const bytes = rfb.anxBytes || 0;
    const updates = rfb.anxStats.updates;
    const decoded = rfb.anxDecoder?.decoded ?? 0;
    rfb.anxDecoder?.watch();
    if (sample) {
      const seconds = (now - sample.at) / 1000;
      const fps = Math.round(((h264 ? decoded - sample.decoded : updates - sample.updates)) / seconds);
      const bits = ((bytes - sample.bytes) * 8) / seconds;
      statsTag.hidden = false;
      statsTag.textContent = `${h264 ? (rfb.anxDecoder?.software ? "H.264 软解" : "H.264") : "JPEG"} · ${fps} fps · ${bitrateLabel(bits)}`;
    }
    sample = { at: now, bytes, updates, decoded };
  }

  function stop() {
    leaving = true;
    clearTimeout(retryTimer);
    clearInterval(statsTimer);
    try { rfb?.disconnect(); } catch { /* closed */ }
    rfb = null;
  }

  function scheduleRetry(reason) {
    if (leaving || failed) return;
    if (attempts >= RECONNECT_DELAYS.length) {
      setStatus("offline", "连接中断");
      showOverlay("连接中断", { busy: false, hint: reason || "多次重连没有成功：对方可能已离线或网络不通",
        action: el("button", { class: "btn btn-primary", type: "button", onclick: () => { attempts = 0; connect(); } }, icon("refresh-cw", { size: 14 }), "重新连接") });
      return;
    }
    const delay = RECONNECT_DELAYS[attempts++];
    setStatus("connecting", "重连中");
    showOverlay(`连接中断，${Math.round(delay / 1000)} 秒后重连（第 ${attempts} 次）…`, { hint: reason || "" });
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, delay);
  }

  function setStatus(state, label) { mount(status, pill(state, label)); }

  async function connect() {
    const turn = ++connecting;
    clearTimeout(retryTimer);
    clearInterval(statsTimer);
    if (rfb) { const old = rfb; rfb = null; try { old.disconnect(); } catch { /* already closed */ } }
    connected = false;
    sample = null;
    statsTag.hidden = true;
    mount(screen);
    setStatus("connecting", "正在直连");
    showOverlay(attempts ? `正在重连（第 ${attempts} 次）…` : "正在建立直连…", { hint: attempts ? "" : "首次连接要打洞，可能需要几秒" });
    let RFB;
    try {
      const prepared = preparing ? await preparing
        : await fetchJSON(`/api/remote/devices/${encodeURIComponent(deviceId)}/desktop/prepare`, { method: "POST", quiet: true, keepCache: true });
      preparing = null;  // a reconnect prepares again
      if (prepared.failed) throw prepared.failed;
      routeTag.hidden = false;
      routeTag.textContent = prepared.relayed ? `中转 · ${prepared.route}` : `直连 · ${prepared.route}`;
      routeTag.title = prepared.relayed ? "经中继设备转发（端到端加密）；画质会按网速自动调整" : "打洞直连：数据不经过网关";
      routeTag.classList.toggle("is-p2p", !prepared.relayed);
      [RFB, h264] = await Promise.all([loadNoVNC(), h264Check.then((ok) => ok && !jpegOnly)]);
    } catch (error) {
      if (leaving || turn !== connecting) return;
      if (attempts > 0) { scheduleRetry(error.message); return; }  // reconnecting: keep trying by itself
      setStatus("error", "无法直连");
      showOverlay("无法连接这台设备的桌面", { busy: false, hint: error.message,
        action: el("button", { class: "btn btn-primary", type: "button", onclick: () => { attempts = 0; connect(); } }, icon("refresh-cw", { size: 14 }), "重试") });
      return;
    }
    if (leaving || turn !== connecting) return;
    showOverlay("正在打开画面…");
    const session = new RFB(screen, desktopSocketURL(deviceId), { wsProtocols: ["binary"] });
    rfb = session;
    session.anxH264 = h264;
    session.scaleViewport = prefs.fit;
    session.resizeSession = false;
    session.qualityLevel = sentQuality();
    session.compressionLevel = 2;
    session.focusOnClick = true;
    session.background = "#000";
    session.anxEvents.software = () => toast("这台电脑的硬件解码出错，已改用软件解码", { type: "warn" });
    session.anxEvents.giveUp = () => {
      if (session !== rfb) return;
      jpegOnly = true;
      toast("这台电脑解不了 H.264 画面，已改用 JPEG", { type: "warn" });
      connect();
    };
    let firstPicture = false;
    session.addEventListener("connect", () => {
      if (session !== rfb) return;
      connected = true;
      setStatus("online", "已连接");
      showOverlay("等待第一帧画面…");
      const socket = session._sock?._websocket;
      session.anxBytes = 0;
      socket?.addEventListener("message", (event) => { session.anxBytes += event.data?.byteLength || 0; });
      statsTimer = setInterval(() => {
        readStats();
        if (!firstPicture && session.anxStats.updates > 0) {
          firstPicture = true;
          hideOverlay();
          session.focus();
        }
      }, 250);
      setTimeout(() => { if (session === rfb && connected) attempts = 0; }, 30000);
    });
    session.addEventListener("disconnect", (event) => {
      if (session !== rfb || leaving) return;
      clearInterval(statsTimer);
      connected = false;
      statsTag.hidden = true;
      if (event.detail?.clean) {
        setStatus("offline", "已断开");
        showOverlay("远程桌面已断开", { busy: false,
          action: el("button", { class: "btn btn-primary", type: "button", onclick: () => { attempts = 0; connect(); } }, icon("refresh-cw", { size: 14 }), "重新连接") });
        return;
      }
      scheduleRetry();
    });
    session.addEventListener("securityfailure", (event) => toast(`远程桌面被拒绝：${event.detail?.reason || ""}`, { type: "bad" }));
  }
  connect();

  ctx.onLeave(() => {
    stop();
    document.removeEventListener("click", closeMenu);
    document.removeEventListener("fullscreenchange", fullscreenChanged);
    page.classList.remove("wide", "page-desktop");
  });
}
