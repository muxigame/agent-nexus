// The 设备 page's topology view: this device in the middle, the gateway in a corner, relay
// devices on the inner ring, every other device on the outer ring. Lines show how this device
// reaches each one right now; traffic dots run along live connections (faster when the round
// trip is shorter). Hovering a device lights up its paths and shows a card; clicking it opens
// an action panel (终端, 文件, 远程桌面, 建立连接) -- the page passes the actions in.
//
// layout "map": the same picture on a world map, each device where the gateway located it
// (ip2region, or where the owner dragged it); dragging a device fixes its position.
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
    devices.map((d) => [d.device_id, d.online, d.relay, d.self, d.revoked, d.geo?.lat, d.geo?.lon, d.geo?.source]),
    Object.entries(links).map(([id, l]) => [id, l.state, l.via, l.rtt_ms == null ? null : Math.round(l.rtt_ms / 10)]),
    conns.gateway?.online, (conns.mesh?.home || []).map((h) => h.device_id),
  ]);
}

/**
 * @param {object} data     {devices (from /api/devices), conns (from /api/remote/connections)}
 * @param {object} actions  {can(device) -> {terminal, files, desktop, why}, terminal, files, desktop, connect}
 */
// -- map projection (Mercator, fitted to the located devices)
const RAD = Math.PI / 180;
const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + Math.max(-80, Math.min(80, lat)) * RAD / 2));

function fitProjection(points, W, H, margin) {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const [lat, lon] of points) {
    x0 = Math.min(x0, lon * RAD); x1 = Math.max(x1, lon * RAD);
    y0 = Math.min(y0, mercY(lat)); y1 = Math.max(y1, mercY(lat));
  }
  if (!points.length) { x0 = 70 * RAD; x1 = 140 * RAD; y0 = mercY(15); y1 = mercY(50); }
  // never zoom into one city: at least ~24 degrees of longitude and the matching height
  const minX = 24 * RAD, minY = minX * (H - 2 * margin) / (W - 2 * margin);
  const xc = (x0 + x1) / 2, yc = (y0 + y1) / 2;
  const sx = Math.max(x1 - x0, minX) * 1.25, sy = Math.max(y1 - y0, minY) * 1.35;
  const scale = Math.min((W - 2 * margin) / sx, (H - 2 * margin) / sy);
  const cx = W / 2, cy = H / 2;
  return {
    project: (lat, lon) => [cx + (lon * RAD - xc) * scale, cy - (mercY(lat) - yc) * scale],
    invert: (x, y) => {
      const lon = (xc + (x - cx) / scale) / RAD;
      const my = yc - (y - cy) / scale;
      return [(2 * Math.atan(Math.exp(my)) - Math.PI / 2) / RAD, lon];
    },
  };
}

function worldPaths(world, projection) {
  const group = svg("g", { class: "topo-land" });
  for (const ring of world?.rings || []) {
    let d = "", last = null;
    for (let i = 0; i < ring.length; i += 2) {
      const lon = ring[i], lat = ring[i + 1];
      const [x, y] = projection.project(lat, lon);
      const jump = last !== null && Math.abs(lon - last) > 180; // the antimeridian: lift the pen
      d += `${d && !jump ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
      last = lon;
    }
    if (d) group.append(svg("path", { d: d + "Z" }));
  }
  return group;
}

export function topology({ devices = [], conns = {}, world = null }, actions = {}, { layout = "rings" } = {}) {
  const W = 800, H = 500, cx = W / 2, cy = H / 2 - 6;
  const isMap = layout === "map";
  const me = devices.find((d) => d.self);
  const others = devices.filter((d) => !d.self && !d.revoked);
  const byId = Object.fromEntries(devices.map((d) => [d.device_id, d]));
  const links = conns.devices || {};
  const relays = others.filter((d) => d.relay && d.online);
  const rest = others.filter((d) => !(d.relay && d.online))
    .sort((a, b) => (b.online - a.online) || (a.display_name || "").localeCompare(b.display_name || ""));

  const pos = {};
  const far = new Set();  // map: devices outside the fitted region, pinned to the edge
  const gatewayAt = [W - 74, 50];
  let backdrop, projection = null;
  if (!isMap) {
    if (me) pos[me.device_id] = [cx, cy];
    relays.forEach((d, i) => {
      const angle = Math.PI * (1.15 + 0.7 * ((i + 0.5) / relays.length)); // upper arc
      pos[d.device_id] = [cx + 160 * Math.cos(angle), cy + 112 * Math.sin(angle)];
    });
    rest.forEach((d, i) => {
      const angle = -0.12 * Math.PI + 1.24 * Math.PI * ((i + 0.5) / Math.max(rest.length, 1)); // lower arc
      pos[d.device_id] = [cx + 335 * Math.cos(angle), cy + 195 * Math.sin(angle)];
    });
    // soft rings for the two tiers
    backdrop = svg("g", { class: "topo-rings" },
      svg("ellipse", { cx, cy, rx: 160, ry: 112 }),
      svg("ellipse", { cx, cy, rx: 335, ry: 195 }));
  } else {
    const placed = [me, ...others].filter((d) => d && d.geo && d.geo.lat != null && d.geo.lon != null);
    // fit the region around this device (or the middle of everything); devices far outside it
    // are pinned to the map's edge in their direction instead of shrinking the map to a dot
    const home = me?.geo?.lat != null ? me.geo : placed[0]?.geo;
    const near = (d) => !home || (Math.abs(d.geo.lat - home.lat) < 28 && Math.abs(d.geo.lon - home.lon) < 40);
    const core = placed.filter(near);
    projection = fitProjection(core.map((d) => [d.geo.lat, d.geo.lon]), W, H, 70);
    const anchor = {};
    const margin = 46;
    for (const device of placed) {
      let [x, y] = projection.project(device.geo.lat, device.geo.lon);
      if (x < margin || x > W - margin || y < margin || y > H - 70) {
        // pinned to the edge, along the line from the middle
        const dx = x - W / 2, dy = y - H / 2;
        const t = Math.min((W / 2 - margin) / Math.abs(dx || 1e-9), (H / 2 - 60) / Math.abs(dy || 1e-9));
        x = W / 2 + dx * t; y = H / 2 + dy * t;
        far.add(device.device_id);
      }
      anchor[device.device_id] = [x, y];
      pos[device.device_id] = [x, y];
    }
    // push apart what overlaps, and keep each one near its place
    const ids = Object.keys(pos);
    for (let round = 0; round < 120; round++) {
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const a = pos[ids[i]], b = pos[ids[j]];
          let dx = b[0] - a[0], dy = b[1] - a[1];
          let dist = Math.hypot(dx, dy);
          if (dist >= 66) continue;
          if (dist < 0.01) { dx = Math.cos(i + j); dy = Math.sin(i + j); dist = 1; }
          const push = (66 - dist) / 2;
          a[0] -= dx / dist * push; a[1] -= dy / dist * push;
          b[0] += dx / dist * push; b[1] += dy / dist * push;
        }
      }
      for (const id of ids) {
        const [ax, ay] = anchor[id];
        pos[id][0] += (ax - pos[id][0]) * 0.04;
        pos[id][1] += (ay - pos[id][1]) * 0.04;
        pos[id][0] = Math.max(30, Math.min(W - 30, pos[id][0]));
        pos[id][1] = Math.max(30, Math.min(H - 60, pos[id][1]));
      }
    }
    // not located (LAN addresses, unknown): a row along the bottom
    const lost = [me, ...others].filter((d) => d && !pos[d.device_id]);
    lost.forEach((device, i) => { pos[device.device_id] = [60 + i * 84, H - 34]; });
    backdrop = svg("g", {}, worldPaths(world, projection),
      lost.length ? svg("text", { x: 24, y: H - 62, class: "topo-unknown" }, "位置未知") : null);
  }

  // -- edges: a faint base line plus an animated flow on top
  const edges = svg("g", { "stroke-linecap": "round" });
  const labels = svg("g", { "font-size": 11, "text-anchor": "middle" });
  const touching = {}; // device id -> edges that belong to it (for hover highlighting)
  const edge = (from, to, kind, { owners = [], dash = null, flow = true, rtt = null, title = "" } = {}) => {
    if (!from || !to) return;
    const color = TONE[kind] || TONE.idle;
    // a gentle arc on the map (like a flight route), straight in the ring view
    let d = `M${from[0]},${from[1]} L${to[0]},${to[1]}`;
    if (isMap) {
      const mx = (from[0] + to[0]) / 2, my = (from[1] + to[1]) / 2;
      const dx = to[0] - from[0], dy = to[1] - from[1];
      d = `M${from[0]},${from[1]} Q${mx - dy * 0.18},${my + dx * 0.18 - Math.hypot(dx, dy) * 0.08} ${to[0]},${to[1]}`;
    }
    const group = svg("g", { class: "topo-edge" },
      svg("title", {}, title),
      svg("path", { d, class: "topo-base", fill: "none", style: `stroke:${color}`, "stroke-dasharray": dash }),
      flow ? svg("path", { d, fill: "none", class: `topo-flow${kind === "punching" ? " topo-blink" : ""}`,
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
        shortName(title, big ? 16 : 11)),
      device && far.has(id) ? svg("text", { x, y: y + r + 28, "text-anchor": "middle", class: "topo-far" },
        `→ ${shortName(device.geo?.label || "", 14)}`) : null);
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
  const stage = el("div", { class: `topo${isMap ? " map" : ""}` }, picture, card, panel);

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
      return [device.display_name, "本机", mesh?.role === "relay" ? "本机是中转设备" : home.length ? `家中转：${home.join("、")}` : "正在选择家中转",
        isMap && device.geo?.lat != null ? `${device.geo.label || ""}（${device.geo.source === "manual" ? "手动设置" : "按 IP 推测，可拖动修正"}）` : ""];
    }
    const link = links[id];
    const lines = [device.display_name, device.device_id];
    if (!device.online) lines.push("离线");
    else if (!link) lines.push("未连接：打开终端、文件或桌面时自动选路");
    else lines.push(`${STATE_TEXT[link.state] || link.state}${link.via ? " " + link.via : ""}${link.rtt_ms != null ? " · " + link.rtt_ms + "ms" : ""}`,
      (link.route || "").replace(/^[^（]*（?/, "").replace(/）$/, ""));
    if (device.relay) lines.push("中转设备：转发其他设备的加密流量");
    if (isMap) lines.push(device.geo?.lat == null ? "位置未知：拖到地图上即可设置"
      : `${device.geo.label || ""}（${device.geo.source === "manual" ? "手动设置" : "按 IP 推测，可拖动修正"}）`);
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
        button("plug", link ? "重新连接" : "建立连接", can.terminal && actions.connect, actions.connect, can.why),
        isMap && device.geo?.source === "manual" && actions.locate
          ? button("map-pin", "恢复自动位置", true, (d) => actions.locate(d, null)) : null));
    panel.hidden = false;
    card.hidden = true;
    light(id);
    place(panel, id);
    panel.querySelector(".topo-panel-actions button:not(.disabled)")?.focus();
    setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", escape); });
  };

  const toSvg = (event) => {
    const point = picture.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(picture.getScreenCTM().inverse());
  };
  for (const [id, group] of Object.entries(nodeOf)) {
    const device = byId[id];
    if (isMap && device && actions.locate) {
      // drag to fix where a device is; a press without moving is a click
      group.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const start = toSvg(event);
        const origin = pos[id];
        let moved = false;
        group.setPointerCapture(event.pointerId);
        const move = (e) => {
          const now = toSvg(e);
          const dx = now.x - start.x, dy = now.y - start.y;
          if (!moved && Math.hypot(dx, dy) < 4) return;
          moved = true;
          card.hidden = true;
          group.setAttribute("transform", `translate(${dx} ${dy})`);
        };
        const up = (e) => {
          group.removeEventListener("pointermove", move);
          group.removeEventListener("pointerup", up);
          if (!moved) { if (!device.self) openPanel(id); return; }
          const now = toSvg(e);
          const [lat, lon] = projection.invert(origin[0] + now.x - start.x, origin[1] + now.y - start.y);
          group.classList.add("topo-saving");
          actions.locate(device, { lat: Math.round(lat * 100) / 100, lon: Math.round(lon * 100) / 100,
            label: "手动设置" });
        };
        group.addEventListener("pointermove", move);
        group.addEventListener("pointerup", up);
      });
    }
    group.addEventListener("mouseenter", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("mouseleave", hide);
    group.addEventListener("focus", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("blur", hide);
    if (byId[id] && !byId[id].self) {
      if (!(isMap && actions.locate)) group.addEventListener("click", () => openPanel(id));
      group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openPanel(id); } });
    }
  }

  const key = (kind, text, dash) => el("span", { class: "row", style: { gap: "6px", alignItems: "center" } },
    el("span", { html: `<svg width="28" height="8"><line x1="1" y1="4" x2="27" y2="4" style="stroke:${TONE[kind]};stroke-width:2" ${dash ? `stroke-dasharray="${dash}"` : ""}/></svg>` }),
    el("span", { class: "muted" }, text));
  const legend = el("div", { class: "row wrap", style: { gap: "16px", fontSize: "var(--fs-sm)" } },
    key("direct", "直连（数字为往返延迟，流动越快延迟越低）"), key("relay", "经中转（端到端加密）", "6 5"),
    key("gateway", "网关中转", "1 6"), key("punching", "正在打洞", "3 7"),
    el("span", { class: "muted" }, isMap ? "点设备打开操作，拖动设备修正位置（位置按 IP 推测，城市级）" : "点设备可直接打开终端、文件或桌面"));
  return el("div", { class: "stack", style: { gap: "12px" } }, stage, legend);
}
