// The client's 设备 page (private-network model): every device of the gateway, with 终端,
// 在本机终端打开, 吊销 and 删除 -- as this device, unless it is locked.
import { el, mount, icon, iconHTML, fetchJSON, toast, modal, confirmDialog, emptyState, errorState, pill, relTime } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";
import { openFiles } from "/local/files.js";
import { topology, topologySignature, localEdges, loadWorld, linkText } from "/ui/topology.js";

/** How this device reaches another one right now (/api/remote/connections). */
function connPill(device, conn) {
  // no pill until there is a connection: they are built on demand, so "none yet" is the normal case
  if (!device.online || device.self || !conn) return null;
  const link = { kind: conn.state, via: conn.via, rtt_ms: conn.rtt_ms };
  const tone = { direct: "ok", relay: "info", punching: "warn pulse", gateway: "warn" }[conn.state] || "neutral";
  const text = conn.state === "direct" && conn.pinned ? linkText(link).replace("直连", "常驻直连") : linkText(link);
  return el("span", { class: `pill ${tone}`, title: conn.route || "" }, text);
}

/** 在线 with this device's round trip to the gateway (links/1), 离线, 已吊销. */
function presencePill(device, gatewayRtt) {
  if (device.revoked) return pill("revoked");
  if (!device.online) return pill("offline");
  const rtt = device.self ? gatewayRtt : device.gateway_rtt_ms;
  return pill("online", rtt != null ? `在线 · ${rtt}ms` : "在线");
}

function savedView() {
  try {
    const value = localStorage.getItem("anx.devices.view");
    if (value === "map") return "globe";  // the flat map is gone; the globe shows the same
    return ["topology", "globe"].includes(value) ? value : "list";
  } catch { return "list"; }
}

function savedScope() {
  try { return localStorage.getItem("anx.devices.scope") === "global" ? "global" : "local"; } catch { return "local"; }
}

function avatar(device) {
  const mark = osMark(device.os_family);
  if (mark) return el("span", { class: "avatar os-mark", title: mark.label, "aria-label": mark.label, html: mark.svg });
  return el("span", { class: "avatar" }, icon(device.platform === "linux" ? "server" : "monitor", { size: 16 }));
}

function menu(anchor, items) {
  document.querySelector(".menu.floating")?.remove();
  const rect = anchor.getBoundingClientRect();
  const node = el("div", { class: "menu floating", role: "menu" }, items.map((item) => el("button", {
    class: `menu-item ${item.danger ? "danger" : ""}`, type: "button", role: "menuitem", disabled: item.disabled || null,
    onclick: () => { node.remove(); item.onClick(); } }, icon(item.icon, { size: 14 }), item.label)));
  document.body.append(node);
  const width = node.offsetWidth || 180;
  node.style.top = `${window.scrollY + rect.bottom + 4}px`;
  node.style.left = `${Math.max(8, Math.min(window.scrollX + rect.right - width, window.scrollX + document.documentElement.clientWidth - width - 8))}px`;
  const close = (event) => { if (!node.contains(event.target) && event.target !== anchor) { node.remove(); document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close));
}

export async function devicesPage(ctx) {
  const { page } = ctx;
  const head = el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "设备"),
    el("p", {}, "同一个网关下的设备组成一个私有网络：任何一台都可以打开其他设备的终端、吊销或删除它们。锁定后这台电脑只能被操作。")));
  const holder = el("div", { class: "stack lg" });
  mount(page, head, holder);

  let view = savedView();
  let scope = savedScope();  // the picture: how this device reaches the others, or the whole network
  let last = null;
  let drawn = "";  // what the topology view currently shows (redraw only when it changed)

  async function load() {
    let data;
    try {
      const pictured = view !== "list";
      const [devices, conns, network] = await Promise.all([
        fetchJSON("/api/devices", { quiet: true }),
        fetchJSON("/api/remote/connections", { quiet: true }).catch(() => ({})),
        pictured && scope === "global" ? fetchJSON("/api/topology", { quiet: true }).catch((error) => ({ error })) : null,
      ]);
      data = { ...devices, conns, network };
    } catch (error) {
      if (error.status === 409) {
        mount(holder, el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({
          icon: "cloud-off", title: "还没有连接网关", text: "加入网关后，这里会列出同一个网关下的所有设备。",
          action: { label: "连接云端", icon: "cloud", href: "#/cloud" } }))));
      } else mount(holder, errorState(error, load));
      return;
    }
    render(data);
  }

  async function setView(next) {
    view = next;
    drawn = "";
    if (next === "globe") await loadWorld();
    try { localStorage.setItem("anx.devices.view", next); } catch { /* private mode */ }
    if (last) render(last);
    if (scope === "global" && next !== "list") load();  // the network picture comes from the gateway
  }

  function setScope(next) {
    scope = next;
    drawn = "";
    try { localStorage.setItem("anx.devices.scope", next); } catch { /* private mode */ }
    load();
  }

  function switcher() {
    const option = (key, label, glyph) => el("button", { type: "button", "aria-pressed": view === key ? "true" : "false",
      onclick: () => setView(key) }, icon(glyph, { size: 14 }), label);
    const views = el("div", { class: "segmented", role: "group", "aria-label": "显示方式" },
      option("list", "列表", "list"), option("topology", "拓扑", "network"), option("globe", "地球", "globe"));
    if (view === "list") return views;
    const lens = (key, label, title) => el("button", { type: "button", "aria-pressed": scope === key ? "true" : "false", title,
      onclick: () => setScope(key) }, label);
    return el("div", { class: "row", style: { gap: "8px" } },
      el("div", { class: "segmented", role: "group", "aria-label": "视角" },
        lens("local", "本机", "本机怎样连到每台设备"), lens("global", "全网", "所有设备之间实际在用的连接（网关汇总）")), views);
  }

  /** What this device may do to another one right now, and why not. */
  function abilities(device, locked, remote) {
    const why = locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.terminal ? "对方客户端不支持远程终端（需 0.3.0 以上）" : !remote ? "网关需要 0.4.0 以上" : "";
    return {
      terminal: device.online && device.terminal && remote && !locked,
      files: device.online && device.files && remote && !locked,
      desktop: device.online && device.desktop && remote && !locked,
      why,
      whyFiles: locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.files ? "对方客户端不支持文件传输（需 0.8.8 以上）" : why,
      whyDesktop: locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.desktop ? "对方没有可共享的桌面" : why,
    };
  }

  function render(data) {
    last = data;
    const { devices = [], lock = {}, remote = true, conns = {}, network = null, gateway: listed = {} } = data;
    const links = conns.devices || {};
    const locked = !!lock.locked;
    const global = scope === "global" && network && !network.error;
    const me = devices.find((d) => d.self);
    const picture = global
      ? { devices: network.devices || devices, edges: network.edges || [], scope: "global",
          gateway: { ...(conns.gateway || {}), geo: network.gateway?.geo || listed.geo || null } }
      : { devices, edges: localEdges(conns, me?.device_id, devices), mesh: conns.mesh, scope: "local",
          gateway: { ...(conns.gateway || {}), geo: listed.geo || null } };
    // a refresh that changes nothing in the picture keeps it (and its animations, open panel)
    const signature = `${view}|${scope}|${locked}|${remote}|${topologySignature(picture)}`;
    const pictured = view !== "list";
    if (pictured && signature === drawn && holder.querySelector(".topo")) return;
    drawn = pictured ? signature : "";
    const others = devices.filter((d) => !d.self);
    mount(holder,
      locked ? el("div", { class: "banner warn" }, icon("lock", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, "这台设备已锁定"),
          el("div", { class: "muted" }, "锁定时不能操作其他设备，其他设备仍然可以操作这台。点右上角的锁解锁。"))) : null,
      !remote ? el("div", { class: "banner info" }, icon("info", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, "网关版本较旧"),
          el("div", { class: "muted" }, "网关升级到 0.4.0 以上后，才能从客户端打开其他设备的终端。"))) : null,
      me ? el("section", { class: "card" }, el("div", { class: "card-body row", style: { gap: "12px" } }, avatar(me),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title" }, me.display_name, el("span", { class: "tag", style: { marginLeft: "8px" } }, "本机")),
          el("span", { class: "sub mono" }, me.device_id)),
        presencePill(me, conns.gateway?.rtt_ms), pill(locked ? "revoked" : "online", locked ? "已锁定" : "未锁定"))) : null,
      others.length ? el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "center" } },
        el("span", { class: "muted" }, meshSummary(conns)), switcher()) : null,
      scope === "global" && network?.error && view !== "list" ? el("div", { class: "banner info" }, icon("info", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, "暂时看不到全网连接"),
          el("div", { class: "muted" }, network.error.message || "网关需要 0.8.18 以上"))) : null,
      others.length && view !== "list" ? el("section", { class: "card" }, el("div", { class: "card-body" },
        topology(picture, {
          can: (device) => abilities(device, locked, remote),
          terminal: (device) => { location.hash = `#/terminal/${encodeURIComponent(device.device_id)}`; },
          files: (device) => openFiles(device),
          desktop: (device) => { location.hash = `#/desktop/${encodeURIComponent(device.device_id)}`; },
          connect: (device) => connect(device),
          locate: locked ? null : (device, location) => locate(device, location),
        }, { layout: view === "topology" ? "rings" : view })))
      : others.length ? el("div", { class: "list" }, others.map((device) => row(device, locked, remote, links[device.device_id])))
        : el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({ icon: "monitor", title: "网关里还没有其他设备",
          text: "在网关控制台或“连接云端”页生成邀请码，让其他电脑加入。", compact: true }))));
  }

  function meshSummary(conns) {
    const mesh = conns.mesh;
    if (!mesh || !mesh.enabled) return "";
    if (mesh.role === "relay") return "本机是中转设备，转发其他设备的加密流量";
    const home = (mesh.home || []).map((h) => `${nameOf(h.device_id)}（${h.rtt_ms}ms）`);
    return home.length ? `家中转：${home.join("、")}` : "正在选择家中转…";
  }

  function nameOf(id) {
    return (last?.devices || []).find((d) => d.device_id === id)?.display_name || id;
  }

  function focusRow(id) {
    requestAnimationFrame(() => {
      const node = holder.querySelector(`[data-device="${CSS.escape(id)}"]`);
      if (!node) return;
      node.scrollIntoView({ block: "center", behavior: "smooth" });
      node.classList.add("flash");
      setTimeout(() => node.classList.remove("flash"), 1600);
    });
  }

  async function locate(device, location) {
    try {
      await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}/location`, { method: "PUT", body: { location } });
      toast(location ? `已把 ${device.display_name} 放到这里` : `${device.display_name} 改回按 IP 自动定位`);
    } catch { /* toasted */ }
    drawn = "";
    load();
  }

  async function connect(device) {
    try {
      const result = await fetchJSON(`/api/remote/devices/${encodeURIComponent(device.device_id)}/connect`, { method: "POST" });
      toast(`已连上 ${device.display_name}：${(result.route || "").split("（")[0]}`);
    } catch { /* toasted */ }
    load();
  }

  function row(device, locked, remote, conn) {
    const canTerminal = device.online && device.terminal && remote && !locked;
    const canDesktop = device.online && device.desktop && remote && !locked;
    const canFiles = device.online && device.files && remote && !locked;
    const whyFiles = locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.files ? "对方客户端不支持文件传输（需 0.8.8 以上）" : !remote ? "网关需要 0.8.8 以上" : "";
    const whyDesktop = locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.desktop ? "对方没有可共享的桌面（无桌面服务器只有终端），或客户端低于 0.5.0" : "";
    const why = locked ? "这台设备已锁定" : !device.online ? "设备不在线" : !device.terminal ? "对方客户端不支持远程终端（需 0.3.0 以上）" : !remote ? "网关需要 0.4.0 以上" : "";
    return el("div", { class: "list-item", dataset: { device: device.device_id } },
      avatar(device),
      el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } },
        el("span", { class: "title truncate" }, device.display_name, device.locked ? el("span", { class: "tag", style: { marginLeft: "8px" } }, icon("lock", { size: 11 }), "已锁定") : null),
        el("span", { class: "sub truncate" }, el("span", { class: "mono" }, device.device_id), device.os ? ` · ${device.os}` : "",
          !device.online && device.last_connected_at ? ` · ${relTime(device.last_seen_at)}在线` : "")),
      el("div", { class: "meta row", style: { gap: "6px" } },
        presencePill(device),
        connPill(device, conn),
        device.desktop || device.platform !== "linux" ? el("a", { class: `btn btn-sm ${canDesktop ? "" : "disabled"}`,
          href: canDesktop ? `#/desktop/${encodeURIComponent(device.device_id)}` : null,
          title: canDesktop ? "查看并控制对方桌面（打洞直连）" : whyDesktop, "aria-disabled": canDesktop ? null : "true" },
          icon("monitor", { size: 14 }), "远程桌面") : null,
        el("button", { class: `btn btn-sm ${canFiles ? "" : "disabled"}`, type: "button",
          title: canFiles ? "浏览、下载对方的文件，或发送文件过去" : whyFiles, "aria-disabled": canFiles ? null : "true",
          onclick: () => { if (canFiles) openFiles(device); } }, icon("folder-open", { size: 14 }), "文件"),
        el("a", { class: `btn btn-sm ${canTerminal ? "" : "disabled"}`, href: canTerminal ? `#/terminal/${encodeURIComponent(device.device_id)}` : null,
          title: canTerminal ? "在这个窗口里打开终端" : why, "aria-disabled": canTerminal ? null : "true" }, icon("terminal", { size: 14 }), "终端"),
        el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "更多操作", html: iconHTML("ellipsis", { size: 16 }),
          onclick: (event) => menu(event.currentTarget, [
            { label: "在本机终端打开", icon: "external-link", disabled: !canTerminal, onClick: () => external(device) },
            { label: "预先建立连接", icon: "plug", disabled: !canTerminal, onClick: () => connect(device) },
            { label: device.revoked ? "已吊销" : "吊销", icon: "circle-x", danger: true, disabled: locked || device.revoked, onClick: () => revoke(device) },
            { label: "删除", icon: "trash", danger: true, disabled: locked || device.online, onClick: () => remove(device) },
          ]) })));
  }

  async function external(device) {
    try {
      const result = await fetchJSON(`/api/remote/devices/${encodeURIComponent(device.device_id)}/terminal/external`,
        { method: "POST", body: { title: device.display_name } });
      toast(result.opened === "windows-terminal" ? "已在 Windows Terminal 中打开" : "已在新终端窗口中打开");
    } catch { /* toasted */ }
  }

  async function revoke(device) {
    if (!await confirmDialog({ title: `吊销 ${device.display_name}？`, danger: true, confirmLabel: "吊销",
      message: "吊销后该设备立即断开，它的凭据失效；需要新的邀请才能重新加入。" })) return;
    try {
      await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}/revoke`, { method: "POST" });
      toast(`${device.display_name} 已吊销`);
    } catch { /* toasted */ }
    load();
  }

  async function remove(device) {
    if (device.enrolled_via === "invite" && !device.revoked) {
      modal({ title: "先吊销再删除", size: "sm", body: el("p", { class: "muted" }, `${device.display_name} 的凭据仍然有效，删除后它还能重新连上来。先吊销，再删除。`),
        actions: [{ label: "知道了", variant: "primary" }] });
      return;
    }
    if (!await confirmDialog({ title: `删除 ${device.display_name}？`, danger: true, confirmLabel: "删除",
      message: "从设备列表里移除这条记录。" })) return;
    try {
      await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}`, { method: "DELETE" });
      toast(`${device.display_name} 已删除`);
    } catch { /* toasted */ }
    load();
  }

  if (view === "globe") await loadWorld();
  await load();
  ctx.every(10000, load);
  window.addEventListener("anx:lockchange", load);
  ctx.onLeave(() => window.removeEventListener("anx:lockchange", load));
}

// ---------------------------------------------------------------- 锁定

/** The lock button in the client's header; unlocking asks for the password only when one is set. */
export function lockButton() {
  const button = el("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "锁定", title: "锁定" });
  let current = { locked: false, has_password: false };
  function paint() {
    mount(button, icon(current.locked ? "lock" : "lock-open", { size: 18 }));
    button.title = current.locked ? "已锁定：不能操作其他设备。点击解锁" : "未锁定。点击锁定（锁定后只能被其他设备操作）";
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("is-locked", current.locked);
  }
  async function refresh() {
    try { current = await fetchJSON("/api/lock", { quiet: true }); } catch { /* old manager */ }
    paint();
  }
  async function set(body) {
    current = await fetchJSON("/api/lock", { method: "POST", body, quiet: true });
    paint();
    window.dispatchEvent(new Event("anx:lockchange"));
  }
  button.addEventListener("click", async () => {
    if (!current.locked) {
      try { await set({ action: "lock" }); toast("已锁定：这台设备现在只能被操作"); } catch (error) { toast(error.message, { type: "bad" }); }
      return;
    }
    if (!current.has_password) {
      try { await set({ action: "unlock" }); toast("已解锁"); } catch (error) { toast(error.message, { type: "bad" }); }
      return;
    }
    const input = el("input", { class: "input", type: "password", autocomplete: "current-password", placeholder: "锁定密码" });
    const error = el("p", { class: "small", style: { color: "var(--bad)" } });
    const dialog = modal({ title: "解锁", size: "sm", body: el("div", { class: "stack" }, input, error),
      actions: [{ label: "取消" }, { label: "解锁", variant: "primary", submit: true, onClick: async () => {
        try { await set({ action: "unlock", password: input.value }); toast("已解锁"); }
        catch (failure) { error.textContent = failure.message; input.select(); return false; }
      } }] });
    setTimeout(() => input.focus(), 50);
    return dialog;
  });
  paint();
  refresh();
  window.addEventListener("anx:lockchange", refresh);
  return button;
}

/** 设置 → 锁定密码: set, change or remove; never required. */
export function lockPasswordCard() {
  const body = el("div", { class: "card-body stack" });
  const card = el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "锁定密码")), body);
  async function render() {
    let state = { has_password: false, locked: false };
    try { state = await fetchJSON("/api/lock", { quiet: true }); } catch { /* shown below */ }
    const current = el("input", { class: "input", type: "password", autocomplete: "current-password", placeholder: "当前锁定密码" });
    const next = el("input", { class: "input", type: "password", autocomplete: "new-password", placeholder: state.has_password ? "新密码（留空则移除密码）" : "新密码（至少 4 个字符）" });
    const save = el("button", { class: "btn btn-primary", type: "button", onclick: async () => {
      try {
        await fetchJSON("/api/lock/password", { method: "PUT", body: { current: current.value || null, new: next.value || null }, quiet: true });
        toast(next.value ? "锁定密码已保存" : "锁定密码已移除");
        render();
      } catch (error) { toast(error.message, { type: "bad" }); }
    } }, state.has_password ? "修改" : "设置");
    mount(body,
      el("p", { class: "small muted" }, "可选。设置后，解锁这台设备需要输入它；不设置时锁定按钮直接切换。锁定期间这台电脑不能操作其他设备，但其他设备仍然可以操作它。"),
      el("p", { class: "small" }, state.has_password ? "已设置锁定密码。" : "还没有设置锁定密码。"),
      state.has_password ? current : null, next, el("div", { class: "row" }, save));
  }
  render();
  return card;
}
