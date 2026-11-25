// The network picture, shared by the client's 设备 page and the gateway console.
//
// scope "local": this device in the middle and how it reaches each other one right now
//   (直连 / 中转·<relay> / 网关); the gateway sits in a corner on the control line.
// scope "global": the gateway in the middle and every link the devices report (links/1),
//   so it shows how the devices reach *each other*, not how one of them reaches the rest.
//
// layout "rings": relays on the inner ring, the other devices on the outer ring grouped by
//   region (a sector per province / country, labelled), two staggered rows when crowded.
// layout "map": devices where the gateway located them (ip2region, or dragged by the owner);
//   three or more devices in one place collapse into a bubble that opens on click.
//
// Traffic dots run along live links (faster for a shorter round trip). Hovering a device lights
// up its links and shows a card; clicking opens an action panel (the page passes the actions).
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
const RANK = { direct: 0, relay: 1, punching: 2, gateway: 3 };
const GATEWAY = "__gateway";

function shortName(text, max = 10) {
  const chars = Array.from(text || "");
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : chars.join("");
}

/** How long a traffic dot takes to cross a line: quicker for a shorter round trip. */
function flowSeconds(rtt) {
  if (rtt == null) return 1.6;
  return Math.min(3.2, Math.max(0.7, 0.6 + rtt / 60));
}

/** One link in words, the same everywhere: 直连 · 23ms, 中转 · 宁波 · 41ms, 网关, 打洞中. */
export function linkText(link, nameOf = (id) => id) {
  if (!link) return "";
  const rtt = link.rtt_ms != null ? ` · ${Math.round(link.rtt_ms)}ms` : "";
  if (link.kind === "direct") return `直连${rtt}`;
  if (link.kind === "relay") return `中转${link.via ? " · " + nameOf(link.via) : ""}${rtt}`;
  if (link.kind === "gateway") return `网关${rtt}`;
  if (link.kind === "punching") return "打洞中";
  return link.kind || "";
}

/** The edges of the local view from /api/remote/connections: this device to each other one. */
export function localEdges(conns = {}, me, devices = []) {
  const byName = Object.fromEntries(devices.map((d) => [d.display_name, d.device_id]));
  return Object.entries(conns.devices || {}).map(([id, link]) => ({
    a: me, b: id, kind: link.state, rtt_ms: link.rtt_ms ?? null, route: link.route || "", pinned: !!link.pinned,
    via: link.via_id || byName[link.via] || "",
  }));
}

/** The land outlines for the map (static, loaded once). */
let world = null;
export async function loadWorld() {
  if (world) return world;
  try { world = await (await fetch("/ui/world.json")).json(); } catch { world = { rings: [] }; }
  return world;
}

/** Map bubbles the viewer opened (kept across refreshes). */
const opened = new Set();

/** A cheap signature of what the picture shows, so a refresh with nothing new can skip redrawing. */
export function topologySignature({ devices = [], edges = [], gateway = {}, mesh = null }) {
  return JSON.stringify([
    devices.map((d) => [d.device_id, d.online, d.relay, d.self, d.revoked, d.geo?.lat, d.geo?.lon, d.geo?.source,
      d.gateway_rtt_ms == null ? null : Math.round(d.gateway_rtt_ms / 20)]),
    edges.map((e) => [e.a, e.b, e.kind, e.via, e.rtt_ms == null ? null : Math.round(e.rtt_ms / 10)]),
    gateway.online, gateway.rtt_ms == null ? null : Math.round(gateway.rtt_ms / 20),
    (mesh?.home || []).map((h) => h.device_id), [...opened].sort(),
  ]);
}

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

function worldPaths(outline, projection) {
  const group = svg("g", { class: "topo-land" });
  for (const ring of outline?.rings || []) {
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

/** The region a device is grouped under in the ring view: its province / country, else 位置未知. */
function regionOf(device) {
  const label = (device.geo?.label || "").trim();
  if (!label || device.geo?.lat == null) return "位置未知";
  return label.split(/[\s·,，]+/)[0] || label;
}

/** Push apart overlapping points, each pulled back towards its anchor. */
function relax(pos, anchor, W, H, gap = 66, rounds = 120) {
  const ids = Object.keys(pos);
  for (let round = 0; round < rounds; round++) {
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = pos[ids[i]], b = pos[ids[j]];
        let dx = b[0] - a[0], dy = b[1] - a[1];
        let dist = Math.hypot(dx, dy);
        if (dist >= gap) continue;
        if (dist < 0.01) { dx = Math.cos(i + j); dy = Math.sin(i + j); dist = 1; }
        const push = (gap - dist) / 2;
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
}

/**
 * @param {object} data     {devices, edges, gateway: {origin, online, rtt_ms}, mesh, world, scope}
 * @param {object} actions  {can(device) -> {terminal, files, desktop, why...}, terminal, files, desktop, connect, locate}
 * @param {object} options  {layout: "rings" | "map", onChange(): redraw after a bubble opened or closed}
 */
export function topology(data, actions = {}, { layout = "rings", onChange = null } = {}) {
  const { devices = [], edges = [], gateway = {}, mesh = null, scope = "local" } = data;
  const outline = data.world || world;
  const global = scope === "global";
  const isMap = layout === "map";
  const W = 800, H = global && !isMap ? 560 : 500, cx = W / 2, cy = H / 2 - 6;
  const me = global ? null : devices.find((d) => d.self);
  const shown = devices.filter((d) => !d.revoked);
  const others = shown.filter((d) => d !== me);
  const byId = Object.fromEntries(devices.map((d) => [d.device_id, d]));
  const nameOf = (id) => id === GATEWAY ? "网关" : byId[id]?.display_name || id;
  const relays = others.filter((d) => d.relay && d.online);
  const rest = others.filter((d) => !(d.relay && d.online));

  // links per device, for cards and highlighting
  const linksOf = {};
  for (const edge of edges) {
    (linksOf[edge.a] ||= []).push({ ...edge, other: edge.b });
    (linksOf[edge.b] ||= []).push({ ...edge, other: edge.a });
  }

  const pos = {};
  const far = new Set();     // map: devices outside the fitted region, pinned to the edge
  const hiddenIn = {};       // map: device id -> the bubble it is folded into
  const bubbles = {};        // bubble id -> {members, label, at}
  const regionLabels = [];
  const gatewayAt = isMap ? [W - 74, 50] : global ? [cx, cy] : null;  // local rings: on the inner ring
  let backdrop, projection = null;

  if (!isMap) {
    if (me) pos[me.device_id] = [cx, cy];
    // the inner ring: the relays, and (local view) the gateway at the top among them
    const inner = global ? relays.map((d) => d.device_id) : [GATEWAY, ...relays.map((d) => d.device_id)];
    inner.forEach((id, i) => {
      const angle = -Math.PI / 2 + 2 * Math.PI * (global ? i + 0.5 : i) / inner.length;
      pos[id] = [cx + 150 * Math.cos(angle), cy + 100 * Math.sin(angle)];
    });
    // the outer ring: a sector per region, biggest first, a gap between sectors
    const groups = {};
    for (const d of rest) (groups[regionOf(d)] ||= []).push(d);
    const ordered = Object.entries(groups).sort(([an, a], [bn, b]) =>
      (an === "位置未知") - (bn === "位置未知") || b.length - a.length || an.localeCompare(bn));
    for (const [, members] of ordered) {
      members.sort((a, b) => (b.online - a.online) || (a.display_name || "").localeCompare(b.display_name || ""));
    }
    const gap = ordered.length > 1 ? 1.2 : 0;
    const slots = rest.length + gap * ordered.length || 1;
    const crowded = rest.length > 18;
    const rx = global ? 345 : 335, ry = global ? 230 : 195;
    let slot = gap / 2;
    let k = 0;
    for (const [name, members] of ordered) {
      const first = slot;
      for (const d of members) {
        const angle = -Math.PI / 2 + 2 * Math.PI * (slot + 0.5) / slots;
        const inset = crowded && k % 2 ? 0.86 : 1;  // stagger two rows when crowded
        pos[d.device_id] = [cx + rx * inset * Math.cos(angle), cy + ry * inset * Math.sin(angle)];
        slot += 1; k += 1;
      }
      if (members.length > 1 && ordered.length > 1) {
        const mid = -Math.PI / 2 + 2 * Math.PI * ((first + slot) / 2) / slots;
        // inside the ring, between it and the relays: outside it would run into the device names
        const inner = crowded ? 0.72 : 0.8;
        const lx = cx + rx * inner * Math.cos(mid), ly = cy + ry * inner * Math.sin(mid) + 4;
        regionLabels.push(svg("text", { x: Math.max(40, Math.min(W - 40, lx)), y: Math.max(14, Math.min(H - 6, ly)),
          "text-anchor": "middle", class: "topo-region" }, `${shortName(name, 8)}${members.length > 1 ? ` ${members.length}` : ""}`));
      }
      slot += gap;
    }
    backdrop = svg("g", { class: "topo-rings" },
      svg("ellipse", { cx, cy, rx: 150, ry: 100 }),
      svg("ellipse", { cx, cy, rx, ry }), regionLabels);
  } else {
    const located = shown.filter((d) => d.geo && d.geo.lat != null && d.geo.lon != null);
    // fit the region around this device (or the middle of everything); devices far outside it
    // are pinned to the map's edge in their direction instead of shrinking the map to a dot
    const home = me?.geo?.lat != null ? me.geo : located[0]?.geo;
    const near = (d) => !home || (Math.abs(d.geo.lat - home.lat) < 28 && Math.abs(d.geo.lon - home.lon) < 40);
    projection = fitProjection(located.filter(near).map((d) => [d.geo.lat, d.geo.lon]), W, H, 70);
    const margin = 46;
    const anchor = {};
    for (const device of located) {
      let [x, y] = projection.project(device.geo.lat, device.geo.lon);
      if (x < margin || x > W - margin || y < margin || y > H - 70) {
        const dx = x - W / 2, dy = y - H / 2;
        const t = Math.min((W / 2 - margin) / Math.abs(dx || 1e-9), (H / 2 - 60) / Math.abs(dy || 1e-9));
        x = W / 2 + dx * t; y = H / 2 + dy * t;
        far.add(device.device_id);
      }
      anchor[device.device_id] = [x, y];
    }
    // bubbles: three or more devices within ~36px of each other (this device and relays never fold)
    const groups = [];
    const solo = (d) => d === me || (d.relay && d.online);
    for (const device of located.filter((d) => !solo(d)).sort((a, b) => a.device_id.localeCompare(b.device_id))) {
      const [x, y] = anchor[device.device_id];
      const group = groups.find((g) => Math.hypot(g.x - x, g.y - y) < 36);
      if (group) { group.members.push(device); group.x = (group.x * (group.members.length - 1) + x) / group.members.length; group.y = (group.y * (group.members.length - 1) + y) / group.members.length; }
      else groups.push({ x, y, members: [device] });
    }
    const placeAnchor = {};
    for (const group of groups) {
      const labels = new Set(group.members.map((m) => m.geo?.label || ""));
      const label = labels.size === 1 ? group.members[0].geo?.label || regionOf(group.members[0]) : regionOf(group.members[0]);
      const id = `__bubble:${label}`;
      if (group.members.length >= 3 && !opened.has(id)) {
        bubbles[id] = { members: group.members, label, at: [group.x, group.y] };
        for (const m of group.members) hiddenIn[m.device_id] = id;
        placeAnchor[id] = [group.x, group.y];
      } else if (group.members.length >= 3) {
        // an opened bubble: its devices on a small circle around the place
        bubbles[id] = { members: group.members, label, at: [group.x, group.y], open: true };
        const r = Math.max(60, group.members.length * 84 / (2 * Math.PI));  // ~84px between neighbours
        group.members.forEach((m, i) => {
          const angle = -Math.PI / 2 + 2 * Math.PI * i / group.members.length;
          placeAnchor[m.device_id] = [group.x + r * Math.cos(angle), group.y + r * Math.sin(angle)];
        });
      } else {
        for (const m of group.members) placeAnchor[m.device_id] = anchor[m.device_id];
      }
    }
    for (const d of located) if (solo(d)) placeAnchor[d.device_id] = anchor[d.device_id];
    for (const [id, xy] of Object.entries(placeAnchor)) pos[id] = [...xy];
    relax(pos, placeAnchor, W, H, 66);
    // not located (LAN addresses, unknown): a row along the bottom
    const lost = shown.filter((d) => !pos[d.device_id] && !hiddenIn[d.device_id]);
    lost.forEach((device, i) => { pos[device.device_id] = [60 + i * 84, H - 34]; });
    backdrop = svg("g", {}, worldPaths(outline, projection),
      lost.length ? svg("text", { x: 24, y: H - 62, class: "topo-unknown" }, "位置未知") : null);
  }
  if (gatewayAt) pos[GATEWAY] = gatewayAt;
  const at = (id) => pos[hiddenIn[id] || id];
  const owner = (id) => hiddenIn[id] || id;

  // -- edges: a faint base line plus an animated flow on top
  const spokes = svg("g", { "stroke-linecap": "round" });
  const lines = svg("g", { "stroke-linecap": "round" });
  const labels = svg("g", { "font-size": 11, "text-anchor": "middle" });
  const touching = {}; // node id -> edges that belong to it (for hover highlighting)
  const drawn = new Set();
  const line = (fromId, toId, kind, { owners = [], dash = null, flow = true, rtt = null, title = "", into = lines, cls = "" } = {}) => {
    const from = pos[fromId], to = pos[toId];
    if (!from || !to || fromId === toId) return;
    const key = [fromId, toId].sort().join("|") + kind;
    if (drawn.has(key)) { for (const id of owners) (touching[id] ||= []).push(...(touching[key] || [])); return; }
    drawn.add(key);
    const color = TONE[kind] || TONE.idle;
    // a gentle arc on the map (like a flight route), straight in the ring view
    let d = `M${from[0]},${from[1]} L${to[0]},${to[1]}`;
    if (isMap) {
      const mx = (from[0] + to[0]) / 2, my = (from[1] + to[1]) / 2;
      const dx = to[0] - from[0], dy = to[1] - from[1];
      d = `M${from[0]},${from[1]} Q${mx - dy * 0.18},${my + dx * 0.18 - Math.hypot(dx, dy) * 0.08} ${to[0]},${to[1]}`;
    }
    const group = svg("g", { class: `topo-edge ${cls}` },
      svg("title", {}, title),
      svg("path", { d, class: "topo-base", fill: "none", style: `stroke:${color}`, "stroke-dasharray": dash }),
      flow ? svg("path", { d, fill: "none", class: `topo-flow${kind === "punching" ? " topo-blink" : ""}`,
        style: `stroke:${color};animation-duration:${flowSeconds(rtt)}s` }) : null);
    into.append(group);
    touching[key] = [group];
    for (const id of new Set([...owners, fromId, toId])) (touching[id] ||= []).push(group);
  };

  // control lines to the gateway: this device's (local), or every online device's (global, faint)
  if (me) line(me.device_id, GATEWAY, gateway.online === false ? "idle" : "control",
    { dash: "2 5", flow: gateway.online !== false, into: spokes,
      title: `与网关的长连接${gateway.rtt_ms != null ? ` · ${gateway.rtt_ms}ms` : ""}` });
  if (global) for (const d of others) if (d.online) line(d.device_id, GATEWAY, "control",
    { flow: false, into: spokes, cls: "topo-spoke", title: `${d.display_name} 与网关的长连接${d.gateway_rtt_ms != null ? ` · ${d.gateway_rtt_ms}ms` : ""}` });

  // the links, best kind first so a pair shows its best path when both ends fold into bubbles
  for (const edge of [...edges].sort((x, y) => (RANK[x.kind] ?? 9) - (RANK[y.kind] ?? 9))) {
    const a = owner(edge.a), b = owner(edge.b);
    if (!pos[a] || !pos[b] || a === b) continue;
    const owners = [edge.a, edge.b, a, b];
    const title = `${nameOf(edge.a)} ↔ ${nameOf(edge.b)}：${linkText(edge, nameOf)}${edge.route ? "\n" + edge.route : ""}`;
    if (edge.kind === "direct") {
      line(a, b, "direct", { owners, rtt: edge.rtt_ms, title });
      if (edge.rtt_ms != null) {
        const [ax, ay] = pos[a], [bx, by] = pos[b];
        const t = me && a === me.device_id ? 0.42 : me && b === me.device_id ? 0.58 : 0.5;
        labels.append(svg("text", { x: ax + (bx - ax) * t, y: ay + (by - ay) * t + 4, class: "topo-rtt" }, `${Math.round(edge.rtt_ms)}ms`));
      }
    } else if (edge.kind === "relay") {
      const hop = edge.via && pos[owner(edge.via)] ? owner(edge.via) : null;
      if (hop && hop !== a && hop !== b) {
        line(a, hop, "relay", { owners: [...owners, edge.via], dash: "6 5", title });
        line(hop, b, "relay", { owners: [...owners, edge.via], dash: "6 5", title });
      } else line(a, b, "relay", { owners, dash: "6 5", title });
    } else if (edge.kind === "gateway") {
      line(a, GATEWAY, "gateway", { owners: [...owners, GATEWAY], dash: "1 6", title });
      line(GATEWAY, b, "gateway", { owners: [...owners, GATEWAY], dash: "1 6", title });
    } else if (edge.kind === "punching") {
      line(a, b, "punching", { owners, dash: "3 7", title });
    }
  }

  // -- nodes
  const nodes = svg("g", {});
  const nodeOf = {};
  const node = (id, { title, kind, big = false, device = null, osFamily = null, count = 0, sub = "" }) => {
    const xy = pos[id];
    if (!xy) return null;
    const [x, y] = xy;
    const r = big ? 28 : count ? 22 + Math.min(10, count) : kind === "relay" ? 21 : 19;
    const ring = kind === "offline" ? "var(--border-strong)" : kind === "relay" ? "var(--info)"
      : kind === "gateway" ? "var(--accent)" : kind === "bubble" ? "var(--text-3)" : "var(--ok)";
    const mark = osFamily ? osMark(osFamily, { size: 18 }) : null;
    const glyph = svg("g", { transform: `translate(${x - (big ? 11 : 9)} ${y - (big ? 11 : 9)}) scale(${big ? 0.92 : 0.75})`,
      class: "topo-glyph", style: `color:${kind === "offline" ? "var(--text-3)" : "var(--text-2)"}` });
    if (mark) glyph.innerHTML = mark.svg.replace(/^<svg[^>]*>|<\/svg>$/g, "");
    const clickable = device || kind === "bubble";
    const group = svg("g", { class: `topo-node ${kind}`, "data-id": id, tabindex: clickable ? 0 : null,
      role: clickable ? "button" : null, "aria-label": clickable ? `${title}，点开${kind === "bubble" ? "展开" : "操作"}` : null },
      big || (global && kind === "gateway") ? svg("circle", { cx: x, cy: y, r, class: "topo-halo", style: `stroke:${ring}` }) : null,
      svg("circle", { cx: x, cy: y, r, class: `topo-disc${kind === "bubble" ? " topo-bubble" : ""}`, style: `stroke:${ring}` }),
      kind === "bubble" ? svg("text", { x, y: y + 5, "text-anchor": "middle", class: "topo-count" }, `×${count}`)
        : mark ? glyph : svg("text", { x, y: y + 4, "text-anchor": "middle", "font-size": big ? 12 : 10, class: "topo-inner" },
          kind === "gateway" ? "网关" : kind === "relay" ? "中转" : big ? "本机" : ""),
      kind === "relay" ? svg("circle", { cx: x + r * 0.72, cy: y - r * 0.72, r: 5, class: "topo-badge" }) : null,
      svg("text", { x, y: y + r + 15, "text-anchor": "middle", class: `topo-label${kind === "offline" ? " off" : ""}` },
        shortName(title, big ? 16 : 11)),
      sub ? svg("text", { x, y: y + r + 28, "text-anchor": "middle", class: "topo-far" }, sub) : null);
    nodes.append(group);
    nodeOf[id] = group;
    return group;
  };

  let host = "";
  try { host = gateway.origin ? new URL(gateway.origin).host : ""; } catch { /* not a URL */ }
  node(GATEWAY, { title: host || "网关", kind: "gateway" });
  for (const [id, bubble] of Object.entries(bubbles)) {
    if (bubble.open) continue;
    pos[id] ||= bubble.at;
    const online = bubble.members.filter((m) => m.online).length;
    node(id, { title: shortName(bubble.label, 10), kind: "bubble", count: bubble.members.length, sub: `${online} 在线` });
  }
  const subOf = (device) => {
    if (isMap && far.has(device.device_id)) return `→ ${shortName(device.geo?.label || "", 14)}`;
    if (global && device.online && device.gateway_rtt_ms != null) return `${device.gateway_rtt_ms}ms`;
    return "";
  };
  for (const device of relays) if (!hiddenIn[device.device_id]) node(device.device_id, { title: device.display_name,
    kind: "relay", device, osFamily: device.os_family, sub: subOf(device) });
  for (const device of rest) if (!hiddenIn[device.device_id]) node(device.device_id, { title: device.display_name,
    kind: device.online ? "device" : "offline", device, osFamily: device.os_family, sub: subOf(device) });
  if (me) node(me.device_id, { title: me.display_name, kind: "device", big: true, osFamily: me.os_family });

  const picture = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "网络拓扑", class: "topo-svg" },
    backdrop, spokes, lines, labels, nodes);
  const card = el("div", { class: "topo-card", hidden: true });
  const panel = el("div", { class: "topo-panel", hidden: true, role: "dialog" });
  const stage = el("div", { class: `topo${isMap ? " map" : ""}${global ? " global" : ""}` }, picture, card, panel);

  // -- hover: light up a node's links and show a card
  const place = (box, id, below = true) => {
    const target = nodeOf[id];
    if (!target) return;
    const rect = target.getBoundingClientRect();
    const frame = stage.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left - frame.left + rect.width / 2 - box.offsetWidth / 2), frame.width - box.offsetWidth - 8);
    let top = rect.top - frame.top + (below ? rect.height + 8 : -box.offsetHeight - 8);
    if (top + box.offsetHeight > frame.height - 4) top = rect.top - frame.top - box.offsetHeight - 8;
    box.style.left = `${left}px`;
    box.style.top = `${Math.max(4, top)}px`;
  };
  const geoLine = (device) => isMap ? (device.geo?.lat == null ? "位置未知：拖到地图上即可设置"
    : `${device.geo.label || ""}（${device.geo.source === "manual" ? "手动设置" : "按 IP 推测，可拖动修正"}）`) : "";
  const describe = (id) => {
    if (id === GATEWAY) {
      const online = others.filter((d) => d.online).length;
      return [host || "网关", global ? `${online} 台设备在线；负责信令、发放中转票据，其他路都不通时兜底转发` : "设备之间的信令与兜底中转",
        gateway.online === false ? "正在重连" : gateway.rtt_ms != null ? `本机到网关 ${gateway.rtt_ms}ms` : "已连接"];
    }
    if (bubbles[id]) {
      const bubble = bubbles[id];
      const online = bubble.members.filter((m) => m.online);
      return [`${bubble.label} · ${bubble.members.length} 台`, `${online.length} 台在线`,
        bubble.members.slice(0, 6).map((m) => m.display_name).join("、") + (bubble.members.length > 6 ? " …" : ""), "点击展开"];
    }
    const device = byId[id];
    if (!device) return [];
    const lines = [device.display_name];
    const presence = !device.online ? "离线" : device.gateway_rtt_ms != null ? `在线 · 到网关 ${device.gateway_rtt_ms}ms` : "在线";
    if (device.self && !global) {
      const home = (mesh?.home || []).map((h) => `${nameOf(h.device_id)} ${h.rtt_ms}ms`);
      lines.push(`本机 · ${gateway.rtt_ms != null ? `到网关 ${gateway.rtt_ms}ms` : "在线"}`,
        mesh?.role === "relay" ? "本机是中转设备" : home.length ? `家中转：${home.join("、")}` : "正在选择家中转");
      return [...lines, geoLine(device)].filter(Boolean);
    }
    lines.push(device.device_id, presence);
    const mine = linksOf[id] || [];
    if (global) {
      const count = (kind) => mine.filter((l) => l.kind === kind).length;
      if (mine.length) {
        lines.push([["直连", count("direct")], ["中转", count("relay")], ["网关", count("gateway")]]
          .filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join(" · "));
        for (const link of mine.slice(0, 5)) lines.push(`↔ ${nameOf(link.other)}：${linkText(link, nameOf)}`);
        if (mine.length > 5) lines.push(`… 还有 ${mine.length - 5} 条`);
      } else if (device.online) lines.push(device.p2p === false ? "只能经网关：客户端没有 P2P 组件"
        : "和其他设备之间暂时没有连接（按需建立）");
    } else if (device.online) {
      const link = mine.find((l) => l.other === me?.device_id) || mine[0];
      lines.push(link ? linkText(link, nameOf) : device.p2p === false ? "只能经网关：对方客户端没有 P2P 组件"
        : "按需：打开终端、文件或桌面时自动选路",
        link?.route ? link.route.replace(/^[^（]*（?/, "").replace(/）$/, "") : "");
    }
    if (device.relay) lines.push("中转设备：转发其他设备的加密流量");
    return [...lines, geoLine(device)].filter(Boolean);
  };
  const light = (id) => {
    stage.classList.toggle("dim", !!id);
    for (const g of stage.querySelectorAll(".hot")) g.classList.remove("hot");
    if (!id) return;
    nodeOf[id]?.classList.add("hot");
    if (me) nodeOf[me.device_id]?.classList.add("hot");
    for (const g of touching[id] || []) g.classList.add("hot");
    const members = bubbles[id] ? bubbles[id].members.map((m) => m.device_id) : [id];
    for (const member of members) {
      for (const link of linksOf[member] || []) {
        nodeOf[owner(link.other)]?.classList.add("hot");
        if (link.via) nodeOf[owner(link.via)]?.classList.add("hot");
        if (link.kind === "gateway") nodeOf[GATEWAY]?.classList.add("hot");
      }
    }
    if (id === GATEWAY) for (const g of spokes.children) g.classList.add("hot");
  };
  const showCard = (id) => {
    if (!panel.hidden) return;
    const [title, ...rest] = describe(id);
    card.replaceChildren(el("div", { class: "title" }, title || ""), ...rest.map((text) => el("div", { class: "muted" }, text)));
    card.hidden = false;
    place(card, id);
  };
  const hide = () => { card.hidden = true; if (panel.hidden) light(null); };

  // -- click: the action panel, or open a bubble
  const closePanel = () => { panel.hidden = true; light(null); document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", escape); };
  const outside = (event) => { if (!panel.contains(event.target) && !event.target.closest?.(".topo-node")) closePanel(); };
  const escape = (event) => { if (event.key === "Escape") closePanel(); };
  const toggleBubble = (id) => {
    if (opened.has(id)) opened.delete(id); else opened.add(id);
    onChange?.();
  };
  const openPanel = (id) => {
    if (bubbles[id]) { toggleBubble(id); return; }
    const device = byId[id];
    if (!device || device.self) return;
    const can = actions.can ? actions.can(device) : {};
    const button = (glyph, label, enabled, run, why) => run ? el("button", { type: "button", class: `btn btn-sm ${enabled ? "" : "disabled"}`,
      "aria-disabled": enabled ? null : "true", title: enabled ? "" : (why || ""),
      onclick: () => { if (!enabled) return; closePanel(); run(device); } }, icon(glyph, { size: 14 }), label) : null;
    const [, ...info] = describe(id);
    const linked = !global && (linksOf[id] || []).length;
    const bubble = Object.entries(bubbles).find(([, b]) => b.open && b.members.includes(device));
    panel.replaceChildren(
      el("div", { class: "topo-panel-head" }, el("span", { class: "title truncate" }, device.display_name),
        el("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", "aria-label": "关闭", onclick: closePanel }, icon("x", { size: 14 }))),
      el("div", { class: "muted topo-panel-info" }, info.slice(1, 4).join(" · ")),
      el("div", { class: "topo-panel-actions" },
        button("terminal", "终端", can.terminal, actions.terminal, can.why),
        button("folder-open", "文件", can.files, actions.files, can.whyFiles || can.why),
        device.desktop || device.platform !== "linux" ? button("monitor", "远程桌面", can.desktop, actions.desktop, can.whyDesktop || can.why) : null,
        button("plug", linked ? "重新连接" : "建立连接", can.terminal, actions.connect, can.why),
        isMap && device.geo?.source === "manual" && actions.locate
          ? button("map-pin", "恢复自动位置", true, (d) => actions.locate(d, null)) : null,
        bubble ? button("minimize-2", "收起这一组", true, () => toggleBubble(bubble[0])) : null));
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
    const draggable = isMap && device && actions.locate;
    if (draggable) {
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
          if (!moved) { openPanel(id); return; }
          const now = toSvg(e);
          const [lat, lon] = projection.invert(origin[0] + now.x - start.x, origin[1] + now.y - start.y);
          group.classList.add("topo-saving");
          actions.locate(device, { lat: Math.round(lat * 100) / 100, lon: Math.round(lon * 100) / 100, label: "手动设置" });
        };
        group.addEventListener("pointermove", move);
        group.addEventListener("pointerup", up);
      });
    }
    group.addEventListener("mouseenter", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("mouseleave", hide);
    group.addEventListener("focus", () => { if (panel.hidden) { light(id); showCard(id); } });
    group.addEventListener("blur", hide);
    if (bubbles[id] || (device && !device.self)) {
      if (!draggable) group.addEventListener("click", () => openPanel(id));
      group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openPanel(id); } });
    }
  }

  const key = (kind, text, dash, faint) => el("span", { class: "row", style: { gap: "6px", alignItems: "center" } },
    el("span", { html: `<svg width="28" height="8"><line x1="1" y1="4" x2="27" y2="4" style="stroke:${TONE[kind]};stroke-width:2;opacity:${faint ? 0.4 : 1}" ${dash ? `stroke-dasharray="${dash}"` : ""}/></svg>` }),
    el("span", { class: "muted" }, text));
  const legend = el("div", { class: "row wrap", style: { gap: "16px", fontSize: "var(--fs-sm)" } },
    key("direct", "直连（数字为往返延迟）"), key("relay", "中转（经中转设备，端到端加密）", "6 5"),
    key("gateway", "网关（兜底转发）", "1 6"), key("punching", "正在打洞", "3 7"),
    global ? key("control", "与网关的长连接", null, true) : null,
    el("span", { class: "muted" }, isMap ? "点设备打开操作，拖动设备修正位置；数字气泡点开看同一地点的设备"
      : global ? "连线是设备之间实际在用的连接（按需建立，空闲一段时间后断开）" : "点设备可直接打开终端、文件或桌面"));
  return el("div", { class: "stack", style: { gap: "12px" } }, stage, legend);
}
