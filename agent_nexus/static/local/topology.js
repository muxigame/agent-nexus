// The 设备 page's topology view: this device in the middle, the gateway in a corner, relay
// devices on the inner ring, every other device on the outer ring. Lines show how this device
// reaches each one right now; traffic dots run along live connections (faster when the round
// trip is shorter). Hovering a device lights up its paths and shows a card; clicking it opens
// an action panel (终端, 文件, 远程桌面, 建立连接) -- the page passes the actions in.
import { el, icon } from "/ui/ui.js";
import { osMark } from "/ui/os-icons.js";

const NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const TONE = { direct: "var(--ok)", relay: "var(--info)", gateway: "var(--warn)", punching: "var(--warn)",
  idle: "var(--border-strong)", control: "var(--accent)" };
const STATE_TEXT = { direct: "直连", relay: "经中转", gateway: "网关中转", punching: "打洞中" };

function shortName(text, max = 10) {
  const chars = Array.from(text || "");
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : chars.join("");
}

/** How long a traffic dot takes to cross a line: quicker for a shorter round trip. */
function flowSeconds(rtt) {
  if (rtt == null) return 1.6;
  return Math.min(3.2, Math.max(0.7, 0.6 + rtt / 60));
}

/** A cheap signature of what the picture shows, so a refresh with nothing new can skip redrawing. */
export function topologySignature({ devices = [], conns = {} }) {
  const links = conns.devices || {};
  return JSON.stringify([
    devices.map((d) => [d.device_id, d.online, d.relay, d.self, d.revoked]),
    Object.entries(links).map(([id, l]) => [id, l.state, l.via, l.rtt_ms == null ? null : Math.round(l.rtt_ms / 10)]),
    conns.gateway?.online, (conns.mesh?.home || []).map((h) => h.device_id),
  ]);
}

/**
 * @param {object} data     {devices (from /api/devices), conns (from /api/remote/connections)}
 * @param {object} actions  {can(device) -> {terminal, files, desktop, why}, terminal, files, desktop, connect}
 */
export function topology({ devices = [], conns = {} }, actions = {}) {
  const W = 800, H = 500, cx = W / 2, cy = H / 2 - 6;
  const me = devices.find((d) => d.self);
  const others = devices.filter((d) => !d.self && !d.revoked);
  const byId = Object.fromEntries(devices.map((d) => [d.device_id, d]));
  const links = conns.devices || {};
  const relays = others.filter((d) => d.relay && d.online);
  const rest = others.filter((d) => !(d.relay && d.online))
    .sort((a, b) => (b.online - a.online) || (a.display_name || "").localeCompare(b.display_name || ""));

  const pos = {};
  if (me) pos[me.device_id] = [cx, cy];
  const gatewayAt = [W - 74, 50];
  relays.forEach((d, i) => {
    const angle = Math.PI * (1.15 + 0.7 * ((i + 0.5) / relays.length)); // upper arc
    pos[d.device_id] = [cx + 160 * Math.cos(angle), cy + 112 * Math.sin(angle)];
  });
  rest.forEach((d, i) => {
    const angle = -0.12 * Math.PI + 1.24 * Math.PI * ((i + 0.5) / Math.max(rest.length, 1)); // lower arc
    pos[d.device_id] = [cx + 335 * Math.cos(angle), cy + 195 * Math.sin(angle)];
  });

  // -- backdrop: soft rings for the two tiers
  const backdrop = svg("g", { class: "topo-rings" },
    svg("ellipse", { cx, cy, rx: 160, ry: 112 }),
    svg("ellipse", { cx, cy, rx: 335, ry: 195 }));

  // -- edges: a faint base line plus an animated flow on top
  const edges = svg("g", { "stroke-linecap": "round" });
  const labels = svg("g", { "font-size": 11, "text-anchor": "middle" });
  const touching = {}; // device id -> edges that belong to it (for hover highlighting)
  const edge = (from, to, kind, { owners = [], dash = null, flow = true, rtt = null, title = "" } = {}) => {
    if (!from || !to) return;
    const color = TONE[kind] || TONE.idle;
    const group = svg("g", { class: "topo-edge" },
      svg("title", {}, title),
      svg("line", { x1: from[0], y1: from[1], x2: to[0], y2: to[1], class: "topo-base",
        style: `stroke:${color}`, "stroke-dasharray": dash }),
      flow ? svg("line", { x1: from[0], y1: from[1], x2: to[0], y2: to[1],
        class: `topo-flow${kind === "punching" ? " topo-blink" : ""}`,
        style: `stroke:${color};animation-duration:${flowSeconds(rtt)}s` }) : null);
    edges.append(group);
    for (const id of owners) (touching[id] ||= []).push(group);
  };

  if (me) edge(pos[me.device_id], gatewayAt, conns.gateway?.online === false ? "idle" : "control",
    { owners: ["__gateway"], dash: "2 5", flow: conns.gateway?.online !== false,
      title: `与网关的长连接${conns.gateway?.origin ? "：" + conns.gateway.origin : ""}` });

  for (const device of others) {
    const here = pos[device.device_id];
    const link = links[device.device_id];
    if (!me || !here || !link) continue;
    const mine = pos[me.device_id];
    const id = device.device_id;
    if (link.state === "direct") {
      edge(mine, here, "direct", { owners: [id], rtt: link.rtt_ms, title: link.route });
      if (link.rtt_ms != null) {
        const mx = mine[0] + (here[0] - mine[0]) * 0.42, my = mine[1] + (here[1] - mine[1]) * 0.42;
        labels.append(svg("text", { x: mx, y: my + 4, class: "topo-rtt" }, `${link.rtt_ms}ms`));
      }
    } else if (link.state === "relay") {
      const via = Object.values(byId).find((d) => d.display_name === link.via || d.device_id === link.via);
      const hop = via ? pos[via.device_id] : null;
      if (hop) {
        if (!links[via.device_id]) edge(mine, hop, "relay", { owners: [id, via.device_id], dash: "6 5", title: link.route });
        else (touching[id] ||= []).push(...(touching[via.device_id] || []));
        edge(hop, here, "relay", { owners: [id], dash: "6 5", title: link.route });
      } else edge(mine, here, "relay", { owners: [id], dash: "6 5", title: link.route });
    } else if (link.state === "gateway") {
      edge(gatewayAt, here, "gateway", { owners: [id], dash: "1 6", title: link.route });
      (touching[id] ||= []).push(...(touching.__gateway || []));
    } else if (link.state === "punching") {
      edge(mine, here, "punching", { owners: [id], dash: "3 7", title: "正在打洞直连" });
    }
  }

  // -- nodes
  const nodes = svg("g", {});
  const nodeOf = {};
  const node = (xy, { id, title, kind, big = false, device = null, osFamily = null }) => {
    const [x, y] = xy;
    const r = big ? 28 : kind === "relay" ? 21 : 19;
    const ring = kind === "offline" ? "var(--border-strong)" : kind === "relay" ? "var(--info)"
      : kind === "gateway" ? "var(--accent)" : "var(--ok)";
    const mark = osFamily ? osMark(osFamily, { size: 18 }) : null;
    const glyph = svg("g", { transform: `translate(${x - (big ? 11 : 9)} ${y - (big ? 11 : 9)}) scale(${big ? 0.92 : 0.75})`,
      class: "topo-glyph", style: `color:${kind === "offline" ? "var(--text-3)" : "var(--text-2)"}` });
    if (mark) glyph.innerHTML = mark.svg.replace(/^<svg[^>]*>|<\/svg>$/g, "");
    const group = svg("g", { class: `topo-node ${kind}`, "data-id": id, tabindex: device ? 0 : null,
      role: device ? "button" : null, "aria-label": device ? `${title}，点开操作` : null },
      big ? svg("circle", { cx: x, cy: y, r, class: "topo-halo", style: `stroke:${ring}` }) : null,
      svg("circle", { cx: x, cy: y, r, class: "topo-disc", style: `stroke:${ring}` }),
      mark ? glyph : svg("text", { x, y: y + 4, "text-anchor": "middle", "font-size": big ? 12 : 10, class: "topo-inner" },
        kind === "gateway" ? "网关" : kind === "relay" ? "中转" : big ? "本机" : ""),
      kind === "relay" ? svg("circle", { cx: x + r * 0.72, cy: y - r * 0.72, r: 5, class: "topo-badge" }) : null,
      svg("text", { x, y: y + r + 15, "text-anchor": "middle", class: `topo-label${kind === "offline" ? " off" : ""}` },
        shortName(title, big ? 16 : 11)));
    nodes.append(group);
    nodeOf[id] = group;
    return group;
  };

  let host = "";
  try { host = conns.gateway?.origin ? new URL(conns.gateway.origin).host : ""; } catch { /* not a URL */ }
  node(gatewayAt, { id: "__gateway", title: host || "网关", kind: "gateway" });
  for (const device of relays) node(pos[device.device_id], { id: device.device_id, title: device.display_name,
    kind: "relay", device, osFamily: device.os_family });
  for (const device of rest) node(pos[device.device_id], { id: device.device_id, title: device.display_name,
    kind: device.online ? "device" : "offline", device, osFamily: device.os_family });
  if (me) node(pos[me.device_id], { id: me.device_id, title: me.display_name, kind: "device", big: true, osFamily: me.os_family });

  const picture = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "网络拓扑", class: "topo-svg" },
    backdrop, edges, labels, nodes);
  const card = el("div", { class: "topo-card", hidden: true });
  const panel = el("div", { class: "topo-panel", hidden: true, role: "dialog" });
  const stage = el("div", { class: "topo" }, picture, card, panel);

  // -- hover: light up a device's paths and show a card
  const place = (box, id, below = true) => {
    const target = nodeOf[id];
    if (!target) return;
    const rect = target.getBoundingClientRect();
    const host = stage.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left - host.left + rect.width / 2 - box.offsetWidth / 2), host.width - box.offsetWidth - 8);
    let top = rect.top - host.top + (below ? rect.height + 8 : -box.offsetHeight - 8);
    if (top + box.offsetHeight > host.height - 4) top = rect.top - host.top - box.offsetHeight - 8;
    box.style.left = `${left}px`;
    box.style.top = `${Math.max(4, top)}px`;
  };
  const describe = (id) => {
    if (id === "__gateway") return [host || "网关", "设备之间的信令与兜底中转", conns.gateway?.online === false ? "正在重连" : "已连接"];
    const device = byId[id];
    if (!device) return [];
    if (device.self) {
      const mesh = conns.mesh;
      const home = (mesh?.home || []).map((h) => `${byId[h.device_id]?.display_name || h.device_id} ${h.rtt_ms}ms`);
      return [device.display_name, "本机", mesh?.role === "relay" ? "本机是中转设备" : home.length ? `家中转：${home.join("、")}` : "正在选择家中转"];
    }
    const link = links[id];
    const lines = [device.display_name, device.device_id];
    if (!device.online) lines.push("离线");
    else if (!link) lines.push("未连接：打开终端、文件或桌面时自动选路");
    else lines.push(`${STATE_TEXT[link.state] || link.state}${link.via ? " " + link.via : ""}${link.rtt_ms != null ? " · " + link.rtt_ms + "ms" : ""}`,
      (link.route || "").replace(/^[^（]*（?/, "").replace(/）$/, ""));
    if (device.relay) lines.push("中转设备：转发其他设备的加密流量");
    return lines.filter(Boolean);
  };
  const light = (id) => {
    stage.classList.toggle("dim", !!id);
    for (const g of stage.querySelectorAll(".hot")) g.classList.remove("hot");
    if (!id) return;
    nodeOf[id]?.classList.add("hot");
    if (me) nodeOf[me.device_id]?.classList.add("hot");
    for (const g of touching[id] || []) g.classList.add("hot");
    const link = links[id];
    if (link?.state === "relay") {
      const via = Object.values(byId).find((d) => d.display_name === link.via || d.device_id === link.via);
      if (via) nodeOf[via.device_id]?.classList.add("hot");
    }
    if (link?.state === "gateway" || id === "__gateway") nodeOf.__gateway?.classList.add("hot");
  };
  const showCard = (id) => {
    if (!panel.hidden) return;
    const [title, ...rest] = describe(id);
    card.replaceChildren(el("div", { class: "title" }, title || ""), ...rest.map((line) => el("div", { class: "muted" }, line)));
    card.hidden = false;
    place(card, id);
  };
  const hide = () => { card.hidden = true; if (panel.hidden) light(null); };

  // -- click: the action panel
  const closePanel = () => { panel.hidden = true; light(null); document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", escape); };
  const outside = (event) => { if (!panel.contains(event.target) && !event.target.closest?.(".topo-node")) closePanel(); };
  const escape = (event) => { if (event.key === "Escape") closePanel(); };
  const openPanel = (id) => {
    const device = byId[id];
    if (!device || device.self) return;
    const can = actions.can ? actions.can(device) : {};
    const link = links[id];
    const button = (glyph, label, enabled, run, why) => el("button", { type: "button", class: `btn btn-sm ${enabled ? "" : "disabled"}`,
      "aria-disabled": enabled ? null : "true", title: enabled ? "" : (why || ""),
      onclick: () => { if (!enabled) return; closePanel(); run(device); } }, icon(glyph, { size: 14 }), label);
    const [, ...info] = describe(id);
    panel.replaceChildren(
      el("div", { class: "topo-panel-head" }, el("span", { class: "title truncate" }, device.display_name),
        el("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", "aria-label": "关闭", onclick: closePanel }, icon("x", { size: 14 }))),
      el("div", { class: "muted topo-panel-info" }, info.slice(1).join(" · ")),
      el("div", { class: "topo-panel-actions" },
        button("terminal", "终端", can.terminal, actions.terminal, can.why),
        button("folder-open", "文件", can.files, actions.files, can.whyFiles || can.why),
        device.desktop || device.platform !== "linux" ? button("monitor", "远程桌面", can.desktop, actions.desktop, can.whyDesktop || can.why) : null,
        button("plug", link ? "重新连接" : "建立连接", can.terminal && actions.connect, actions.connect, can.why)));
    panel.hidden = false;
    card.hidden = true;
    light(id);
    place(panel, id);
    panel.querySelector(".topo-panel-actions button:not(.disabled)")?.focus();
    setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", escape); });
  };

  for (const [id, group] of Object.entries(nodeOf)) {
    group.addEventListener("mouseenter", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("mouseleave", hide);
    group.addEventListener("focus", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("blur", hide);
    if (byId[id] && !byId[id].self) {
      group.addEventListener("click", () => openPanel(id));
      group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openPanel(id); } });
    }
  }

  const key = (kind, text, dash) => el("span", { class: "row", style: { gap: "6px", alignItems: "center" } },
    el("span", { html: `<svg width="28" height="8"><line x1="1" y1="4" x2="27" y2="4" style="stroke:${TONE[kind]};stroke-width:2" ${dash ? `stroke-dasharray="${dash}"` : ""}/></svg>` }),
    el("span", { class: "muted" }, text));
  const legend = el("div", { class: "row wrap", style: { gap: "16px", fontSize: "var(--fs-sm)" } },
    key("direct", "直连（数字为往返延迟，流动越快延迟越低）"), key("relay", "经中转（端到端加密）", "6 5"),
    key("gateway", "网关中转", "1 6"), key("punching", "正在打洞", "3 7"),
    el("span", { class: "muted" }, "点设备可直接打开终端、文件或桌面"));
  return el("div", { class: "stack", style: { gap: "12px" } }, stage, legend);
}
