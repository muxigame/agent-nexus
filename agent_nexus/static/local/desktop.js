// The client's 远程桌面: noVNC over the P2P tunnel to another device's built-in VNC.
import { el, mount, icon, fetchJSON, toast, errorState, pill } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";

export function desktopSocketURL(deviceId, loc = location) {
  const scheme = loc.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${loc.host}/api/remote/devices/${encodeURIComponent(deviceId)}/desktop/ws`;
}

export async function desktopPage(ctx, { params }) {
  const deviceId = params.device;
  const { page, shell: appShell } = ctx;
  appShell.setTitle("远程桌面", [{ label: "设备", href: "#/devices" }]);
  page.classList.add("wide");
  mount(page, el("div", { class: "stack" }, el("span", { class: "skeleton w-40" }), el("div", { class: "term-frame skeleton-block" })));

  let device;
  let RFB;
  try {
    const list = await fetchJSON("/api/devices", { quiet: true });
    if (list.lock?.locked) throw new Error("这台设备已锁定，解锁后才能操作其他设备（右上角的锁）");
    device = (list.devices || []).find((d) => d.device_id === deviceId);
    if (!device) throw new Error("没有这台设备");
    if (!device.online) throw new Error(`${device.display_name} 现在不在线`);
    if (!device.desktop) throw new Error(`${device.display_name} 没有可共享的桌面（无桌面的服务器只能用终端），或客户端低于 0.5.0`);
    ({ default: RFB } = await import("/vendor/novnc/core/rfb.js"));
  } catch (error) {
    mount(page, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "远程桌面"))),
      errorState(error, () => desktopPage(ctx, { params })));
    return;
  }

  const mark = osMark(device.os_family);
  const status = el("span", { class: "term-status" }, pill("connecting", "正在直连"));
  const routeTag = el("span", { class: "tag term-route is-p2p", hidden: true });
  const screen = el("div", { class: "desktop-screen" });
  const quality = el("select", { class: "select", "aria-label": "画质", title: "画质（越低越省流量）",
    onchange: () => { if (rfb) rfb.qualityLevel = Number(quality.value); } },
  [[9, "画质 · 最佳"], [6, "画质 · 均衡"], [3, "画质 · 省流"], [1, "画质 · 极省"]].map(([v, label]) => el("option", { value: v, selected: v === 6 }, label)));
  const toolbar = el("div", { class: "term-toolbar" },
    el("div", { class: "row grow", style: { gap: "10px", minWidth: 0 } },
      mark ? el("span", { class: "avatar os-mark", html: mark.svg, title: mark.label }) : el("span", { class: "avatar" }, icon("monitor", { size: 16 })),
      el("div", { class: "stack", style: { gap: 0, minWidth: 0 } }, el("span", { class: "title truncate" }, device.display_name),
        el("span", { class: "sub mono truncate" }, device.device_id)),
      status, routeTag),
    el("div", { class: "row", style: { gap: "6px" } }, quality,
      el("button", { class: "btn btn-sm", type: "button", title: "发送 Ctrl+Alt+Del", onclick: () => rfb?.sendCtrlAltDel() }, "Ctrl+Alt+Del"),
      el("button", { class: "btn btn-sm", type: "button", title: "重新连接", onclick: () => connect() }, icon("refresh-cw", { size: 14 }), "重连"),
      el("button", { class: "btn btn-sm btn-icon", type: "button", title: "全屏", "aria-label": "全屏",
        onclick: () => (document.fullscreenElement ? document.exitFullscreen() : screen.parentElement.requestFullscreen?.()) }, icon("maximize", { size: 14 }))));
  mount(page, el("div", { class: "term-shell desktop-shell" }, toolbar, screen),
    el("p", { class: "faint xs" }, "画面和键鼠经打洞直连传输，不经过网关。点击画面后键盘输入会发到对方电脑。"));

  let rfb = null;
  let leaving = false;
  function setStatus(state, label) { mount(status, pill(state, label)); }

  async function connect() {
    if (rfb) { try { rfb.disconnect(); } catch { /* already closed */ } rfb = null; }
    mount(screen);
    setStatus("connecting", "正在直连");
    routeTag.hidden = true;
    try {
      const prepared = await fetchJSON(`/api/remote/devices/${encodeURIComponent(deviceId)}/desktop/prepare`, { method: "POST", quiet: true });
      routeTag.hidden = false;
      routeTag.textContent = `直连 · ${prepared.route}`;
    } catch (error) {
      setStatus("error", "无法直连");
      mount(screen, el("div", { class: "desktop-message" }, error.message));
      return;
    }
    rfb = new RFB(screen, desktopSocketURL(deviceId), { wsProtocols: ["binary"] });
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.qualityLevel = Number(quality.value);
    rfb.compressionLevel = 2;
    rfb.focusOnClick = true;
    rfb.addEventListener("connect", () => { setStatus("online", "已连接"); rfb.focus(); });
    rfb.addEventListener("disconnect", (event) => {
      if (leaving) return;
      setStatus("offline", event.detail?.clean ? "已断开" : "连接中断");
    });
    rfb.addEventListener("securityfailure", (event) => toast(`远程桌面被拒绝：${event.detail?.reason || ""}`, { type: "bad" }));
  }
  connect();

  ctx.onLeave(() => {
    leaving = true;
    try { rfb?.disconnect(); } catch { /* closed */ }
    page.classList.remove("wide");
  });
}
