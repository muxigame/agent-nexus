// The network picture, shared by the client's 设备 page and the gateway console.
//
// scope "local": this device in the middle and how it reaches each other one right now
//   (直连 / 中转·<relay> / 网关); the gateway sits on the inner ring.
// scope "global": the gateway in the middle and every link the devices report (links/1),
//   so it shows how the devices reach *each other*, not how one of them reaches the rest.
//
// layout "rings": relays on the inner ring, the other devices on the outer ring grouped by
//   region (a sector per province / country), two staggered rows when crowded.
// layout "globe": the earth; drag to turn it, wheel or +/− to zoom. Devices and the gateway sit
//   where the gateway located them (or where the owner dragged them); links follow great circles.
// On the globe three or more devices in one place fold into a numbered bubble:
// clicking opens it around the place, 「收起」 (on the group, or 全部收起) folds it again, and
// zooming in separates them by itself.
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
const RAD = Math.PI / 180;
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

/**
 * Follow the drag that ``down`` (a pointerdown) starts: ``move(e)`` per move, ``end(e, cancelled)``
 * once. It ends on pointerup -- and also on pointercancel (the browser took the gesture over),
 * the window losing focus, or a mouse move with no button held: a release outside the window,
 * or one the page never saw, used to leave the globe turning with a mouse nobody pressed.
 */
export function followDrag(down, move, end) {
  const id = down.pointerId;
  let done = false;
  const finish = (e, cancelled) => {
    if (done) return;
    done = true;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onCancel);
    window.removeEventListener("blur", onBlur);
    end(e, cancelled);
  };
  const onMove = (e) => {
    if (e.pointerId !== id) return;
    if (e.pointerType === "mouse" && e.buttons === 0) { finish(e, true); return; }
    move(e);
  };
  const onUp = (e) => { if (e.pointerId === id) finish(e, false); };
  const onCancel = (e) => { if (e.pointerId === id) finish(e, true); };
  const onBlur = () => finish(null, true);
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onCancel);
  window.addEventListener("blur", onBlur);
  return finish;
}

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

/** The land outlines for the globe (static, loaded once). */
let world = null;
export async function loadWorld() {
  if (world) return world;
  try { world = await (await fetch("/ui/world.json")).json(); } catch { world = { rings: [] }; }
  return world;
}

/** Bubbles the viewer opened, and where the globe was zoomed or turned (kept across refreshes). */
const opened = new Set();
const views = { globe: null };

/** A cheap signature of what the picture shows, so a refresh with nothing new can skip redrawing. */
export function topologySignature({ devices = [], edges = [], gateway = {}, mesh = null }) {
  return JSON.stringify([
    devices.map((d) => [d.device_id, d.online, d.relay, d.self, d.revoked, d.geo?.lat, d.geo?.lon, d.geo?.source,
      d.gateway_rtt_ms == null ? null : Math.round(d.gateway_rtt_ms / 20)]),
    edges.map((e) => [e.a, e.b, e.kind, e.via, e.rtt_ms == null ? null : Math.round(e.rtt_ms / 10)]),
    gateway.online, gateway.rtt_ms == null ? null : Math.round(gateway.rtt_ms / 20), gateway.geo?.lat, gateway.geo?.lon,
    (mesh?.home || []).map((h) => h.device_id),
  ]);
}

// ---------------------------------------------------------------- projections

const GLOBE_K = [0.8, 6];

// Orthographic: the globe as seen from far away, turned so (view.lat, view.lon) faces us.
function globeProjection(view, W, H) {
  const R = Math.min(W, H) / 2 * 0.86 * view.k;
  const cx = W / 2, cy = H / 2;
  view.lat = clamp(view.lat, -80, 80);
  const l0 = view.lon * RAD, p0 = view.lat * RAD;
  const sp0 = Math.sin(p0), cp0 = Math.cos(p0);
  const raw = (lat, lon) => {
    const p = lat * RAD, l = lon * RAD - l0;
    const cp = Math.cos(p), sp = Math.sin(p), cl = Math.cos(l);
    return [R * cp * Math.sin(l), R * (cp0 * sp - sp0 * cp * cl), sp0 * sp + cp0 * cp * cl];
  };
  return {
    kind: "globe", R, cx, cy, raw,
    project: (lat, lon) => { const [x, y, z] = raw(lat, lon); return [cx + x, cy - y, z >= 0.02]; },
    invert: (px, py) => {
      const x = (px - cx) / R, y = (cy - py) / R;
      const rho = Math.hypot(x, y);
      if (rho > 1) return null;
      const c = Math.asin(rho), sc = Math.sin(c), cc = Math.cos(c);
      const lat = rho ? Math.asin(cc * sp0 + y * sc * cp0 / rho) : p0;
      const lon = l0 + Math.atan2(x * sc, rho * cc * cp0 - y * sc * sp0);
      return [lat / RAD, ((lon / RAD + 540) % 360) - 180];
    },
  };
}

function landOnGlobe(outline, P) {
  // the far side is pushed onto the rim, so a coast that turns away is clipped along the edge
  const group = svg("g", { class: "topo-land" });
  for (const ring of outline?.rings || []) {
    let d = "", seen = 0;
    for (let i = 0; i < ring.length; i += 2) {
      let [x, y, z] = P.raw(ring[i + 1], ring[i]);
      if (z < 0) { const n = Math.hypot(x, y) || 1; x = x / n * P.R; y = y / n * P.R; } else seen += 1;
      d += `${d ? "L" : "M"}${(P.cx + x).toFixed(1)},${(P.cy - y).toFixed(1)}`;
    }
    if (seen > 1) group.append(svg("path", { d: d + "Z" }));
  }
  return group;
}

function graticule(P) {
  const group = svg("g", { class: "topo-graticule" });
  const trace = (points) => {
    let d = "", up = true;
    for (const [lat, lon] of points) {
      const [x, y, visible] = P.project(lat, lon);
      if (!visible) { up = true; continue; }
      d += `${up ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      up = false;
    }
    if (d) group.append(svg("path", { d }));
  };
  for (let lon = -180; lon < 180; lon += 30) trace(Array.from({ length: 33 }, (_, i) => [-80 + i * 5, lon]));
  for (let lat = -60; lat <= 60; lat += 30) trace(Array.from({ length: 73 }, (_, i) => [lat, -180 + i * 5]));
  return group;
}

// ---------------------------------------------------------------- places

/** The region a device is grouped under: its country abroad, its province at home, else 位置未知. */
function regionOf(device) {
  const geo = device.geo || {};
  if (geo.lat == null) return "位置未知";
  if (geo.country && geo.country !== "中国") return geo.country;
  const province = (geo.province || "").replace(/(省|市|特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区)$/, "");
  return province || (geo.label || "").split(/[\s·,，]+/)[0] || "位置未知";
}

/** What a bubble is called: the shared city, else province, else country, else 多个地区. */
function placeName(members) {
  const shared = (pick) => { const values = new Set(members.map(pick)); return values.size === 1 ? [...values][0] : ""; };
  return shared((m) => m.geo?.label || "") || shared(regionOf) || shared((m) => m.geo?.country || "") || "多个地区";
}

/** Where the devices are, on average (a unit-vector mean, so it works across the antimeridian). */
function centreOf(list) {
  let x = 0, y = 0, z = 0;
  for (const d of list) {
    const p = d.geo.lat * RAD, l = d.geo.lon * RAD;
    x += Math.cos(p) * Math.cos(l); y += Math.cos(p) * Math.sin(l); z += Math.sin(p);
  }
  if (!list.length) return { lat: 25, lon: 110 };
  return { lat: Math.atan2(z, Math.hypot(x, y)) / RAD, lon: Math.atan2(y, x) / RAD };
}

/** Push apart overlapping points, each pulled back towards its anchor. */
function relax(pos, anchor, W, bottom, gap, rounds = 160, pull = 0.02) {
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
      pos[id][0] = clamp(pos[id][0] + (ax - pos[id][0]) * pull, 24, W - 24);
      pos[id][1] = clamp(pos[id][1] + (ay - pos[id][1]) * pull, 22, bottom);
    }
  }
}

// ---------------------------------------------------------------- the picture

/**
 * @param {object} data     {devices, edges, gateway: {origin, online, rtt_ms}, mesh, world, scope}
 * @param {object} actions  {can(device) -> {terminal, files, desktop, why...}, terminal, files, desktop, connect, locate}
 * @param {object} options  {layout: "rings" | "globe"}
 */
export function topology(data, actions = {}, { layout = "rings" } = {}) {
  const { devices = [], edges = [], gateway = {}, mesh = null, scope = "local" } = data;
  const outline = data.world || world;
  const global = scope === "global";
  const isGlobe = layout === "globe";
  const geoMode = isGlobe;
  const W = 800;
  const H = isGlobe ? 520 : global ? 560 : 500;
  const SIZE = geoMode ? { device: 9, relay: 11, me: 13, font: 10 } : { device: 15, relay: 17, me: 22, font: 11 };
  const me = global ? null : devices.find((d) => d.self);
  const shown = devices.filter((d) => !d.revoked);
  const others = shown.filter((d) => d !== me);
  const byId = Object.fromEntries(devices.map((d) => [d.device_id, d]));
  const nameOf = (id) => id === GATEWAY ? "网关" : byId[id]?.display_name || id;
  const relays = others.filter((d) => d.relay && d.online);
  const rest = others.filter((d) => !(d.relay && d.online));
  const located = shown.filter((d) => d.geo && d.geo.lat != null && d.geo.lon != null);

  // links per device, for cards and highlighting
  const linksOf = {};
  for (const edge of edges) {
    (linksOf[edge.a] ||= []).push({ ...edge, other: edge.b });
    (linksOf[edge.b] ||= []).push({ ...edge, other: edge.a });
  }

  let host = "";
  try { host = gateway.origin ? new URL(gateway.origin).host : ""; } catch { /* not a URL */ }

  const view = isGlobe ? (views.globe ||= { k: 1, ...centreOf(me?.geo?.lat != null ? [me] : located) }) : null;
  const card = el("div", { class: "topo-card", hidden: true });
  const panel = el("div", { class: "topo-panel", hidden: true, role: "dialog" });
  const stage = el("div", { class: `topo${geoMode ? " map" : ""}${isGlobe ? " globe" : ""}${global ? " global" : ""}` });
  let frame = 0;
  let teardown = () => {};
  const redraw = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; build(); }); };

  function build() {
    teardown();
    const pos = {};
    const offside = [];        // globe: devices on the far side
    const hiddenIn = {};       // device id -> the bubble it is folded into
    const geoOf = {};          // globe: node id -> {lat, lon}, for the great-circle routes
    const bubbles = {};        // bubble id -> {members, label, at, open, r}
    const bottom = geoMode ? H - 22 : H - 60;
    let backdrop, P = null;
    // the gateway: where it is (it locates itself) on the globe, else top left
    const gatewayGeo = gateway.geo && gateway.geo.lat != null && gateway.geo.lon != null ? gateway.geo : null;
    const gatewayAt = geoMode ? (gatewayGeo ? null : [40, 34]) : global ? [W / 2, H / 2 - 6] : null;

    if (!geoMode) {
      const cx = W / 2, cy = H / 2 - 6;
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
      const regionLabels = [];
      let slot = gap / 2, k = 0;
      for (const [name, members] of ordered) {
        const first = slot;
        for (const d of members) {
          const angle = -Math.PI / 2 + 2 * Math.PI * (slot + 0.5) / slots;
          const inset = crowded && k % 2 ? 0.86 : 1;  // stagger two rows when crowded
          pos[d.device_id] = [cx + rx * inset * Math.cos(angle), cy + ry * inset * Math.sin(angle)];
          slot += 1; k += 1;
        }
        if (members.length > 1 && ordered.length > 1) {
          // inside the ring, between it and the relays: outside it would run into the device names
          const mid = -Math.PI / 2 + 2 * Math.PI * ((first + slot) / 2) / slots;
          const inset = crowded ? 0.72 : 0.8;
          regionLabels.push(svg("text", { x: cx + rx * inset * Math.cos(mid), y: cy + ry * inset * Math.sin(mid) + 4,
            "text-anchor": "middle", class: "topo-region" }, `${shortName(name, 8)} ${members.length}`));
        }
        slot += gap;
      }
      backdrop = svg("g", { class: "topo-rings" },
        svg("ellipse", { cx, cy, rx: 150, ry: 100 }), svg("ellipse", { cx, cy, rx, ry }), regionLabels);
    } else {
      P = globeProjection(view, W, H);
      const anchor = {};
      const spots = gatewayGeo ? [...located, { device_id: GATEWAY, display_name: "网关", geo: gatewayGeo }] : located;
      for (const device of spots) {
        const [x, y, visible] = P.project(device.geo.lat, device.geo.lon);
        if (!visible) { offside.push(device); continue; }
        anchor[device.device_id] = [x, y];
      }
      // bubbles: three or more devices within ~26px of each other (this device and relays never fold)
      const solo = (d) => d === me || (d.relay && d.online);
      const groups = [];
      for (const device of located.filter((d) => anchor[d.device_id] && !solo(d)).sort((a, b) => a.device_id.localeCompare(b.device_id))) {
        const [x, y] = anchor[device.device_id];
        const group = groups.find((g) => Math.hypot(g.x - x, g.y - y) < 26);
        if (!group) { groups.push({ x, y, members: [device] }); continue; }
        group.members.push(device);
        const n = group.members.length;
        group.x = (group.x * (n - 1) + x) / n; group.y = (group.y * (n - 1) + y) / n;
      }
      const placeAnchor = {};
      for (const group of groups) {
        const n = group.members.length;
        if (n < 3) { for (const m of group.members) placeAnchor[m.device_id] = anchor[m.device_id]; continue; }
        const label = placeName(group.members);
        let id = `__bubble:${label}`;
        while (bubbles[id]) id += "+";
        if (!opened.has(id)) {
          bubbles[id] = { members: group.members, label, at: [group.x, group.y] };
          for (const m of group.members) hiddenIn[m.device_id] = id;
          placeAnchor[id] = [group.x, group.y];
        } else {
          // an opened bubble: its devices on a ring around the place
          const r = Math.max(42, n * 58 / (2 * Math.PI));
          bubbles[id] = { members: group.members, label, at: [group.x, group.y], open: true, r };
          group.members.forEach((m, i) => {
            const angle = -Math.PI / 2 + 2 * Math.PI * (i + 0.5) / n;
            placeAnchor[m.device_id] = [group.x + r * Math.cos(angle), group.y + r * Math.sin(angle)];
          });
        }
      }
      for (const d of located) if (anchor[d.device_id] && solo(d)) placeAnchor[d.device_id] = anchor[d.device_id];
      if (anchor[GATEWAY]) placeAnchor[GATEWAY] = anchor[GATEWAY];
      for (const [id, xy] of Object.entries(placeAnchor)) pos[id] = [...xy];
      for (const d of located) geoOf[d.device_id] = d.geo;
      if (gatewayGeo) geoOf[GATEWAY] = gatewayGeo;
      for (const [id, bubble] of Object.entries(bubbles)) geoOf[id] = centreOf(bubble.members);
      relax(pos, placeAnchor, W, bottom, 40);
      // not located (LAN addresses, unknown): not on the picture but in the tray under it
      const behind = offside.filter((d) => d.device_id !== GATEWAY).length;
      const notes = [
        offside.length ? svg("text", { x: W - 14, y: H - 10, "text-anchor": "end", class: "topo-unknown" },
          `背面还有 ${[behind ? `${behind} 台设备` : "", offside.some((d) => d.device_id === GATEWAY) ? "网关" : ""].filter(Boolean).join("和")}，拖动地球查看`) : null,
      ];
      {
        const shade = svg("radialGradient", { id: "topo-sea", cx: "38%", cy: "34%", r: "70%" },
          svg("stop", { offset: "0%", style: "stop-color:var(--surface)" }), svg("stop", { offset: "100%", style: "stop-color:var(--surface-2)" }));
        backdrop = svg("g", {}, svg("defs", {}, shade),
          svg("circle", { cx: P.cx, cy: P.cy, r: P.R + 6, class: "topo-air" }),
          svg("circle", { cx: P.cx, cy: P.cy, r: P.R, class: "topo-sea", fill: "url(#topo-sea)" }),
          graticule(P), landOnGlobe(outline, P),
          svg("circle", { cx: P.cx, cy: P.cy, r: P.R, class: "topo-rim" }), notes);
      }
    }
    if (gatewayAt) pos[GATEWAY] = gatewayAt;
    const owner = (id) => hiddenIn[id] || id;

    // -- edges: a faint base line plus an animated flow on top
    const halos = svg("g", {});
    const spokes = svg("g", { "stroke-linecap": "round" });
    const lines = svg("g", { "stroke-linecap": "round" });
    const labels = svg("g", { "font-size": SIZE.font, "text-anchor": "middle" });
    const touching = {}; // node id -> edges that belong to it (for hover highlighting)
    const drawn = new Set();
    const curve = (from, to) => {
      const mx = (from[0] + to[0]) / 2, my = (from[1] + to[1]) / 2;
      const dx = to[0] - from[0], dy = to[1] - from[1];
      if (!geoMode) return `M${from[0]},${from[1]} L${to[0]},${to[1]}`;
      {
        // lifted off the surface, away from the globe's middle (like a flight route)
        const ox = mx - P.cx, oy = my - P.cy, n = Math.hypot(ox, oy) || 1, lift = Math.hypot(dx, dy) * 0.22;
        return `M${from[0]},${from[1]} Q${mx + ox / n * lift},${my + oy / n * lift} ${to[0]},${to[1]}`;
      }
    };
    // globe: the great circle between two places, lifted off the surface (higher for a longer
    // route), so a link to the far side climbs over the horizon instead of vanishing
    const route = (fromId, toId) => {
      const ga = geoOf[fromId], gb = geoOf[toId];
      if (!ga || !gb) return null;
      const unit = (g) => [Math.cos(g.lat * RAD) * Math.cos(g.lon * RAD), Math.cos(g.lat * RAD) * Math.sin(g.lon * RAD), Math.sin(g.lat * RAD)];
      const A = unit(ga), B = unit(gb);
      const w = Math.acos(clamp(A[0] * B[0] + A[1] * B[1] + A[2] * B[2], -1, 1));
      const steps = Math.max(10, Math.ceil(w / RAD / 3));
      const lift = Math.min(0.28, 0.04 + w * 0.12);
      let d = "", up = true;
      for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        let p = A;
        if (w > 1e-6) {
          const sw = Math.sin(w), f1 = Math.sin((1 - t) * w) / sw, f2 = Math.sin(t * w) / sw;
          p = [f1 * A[0] + f2 * B[0], f1 * A[1] + f2 * B[1], f1 * A[2] + f2 * B[2]];
        }
        const [x, y, z] = P.raw(Math.asin(clamp(p[2], -1, 1)) / RAD, Math.atan2(p[1], p[0]) / RAD);
        const h = 1 + lift * Math.sin(Math.PI * t);
        if (z < 0 && Math.hypot(x * h, y * h) <= P.R) { up = true; continue; }  // behind the globe
        let X = P.cx + x * h, Y = P.cy - y * h;
        if (i === 0 && pos[fromId]) [X, Y] = pos[fromId];
        if (i === steps && pos[toId]) [X, Y] = pos[toId];
        d += `${up ? "M" : "L"}${X.toFixed(1)},${Y.toFixed(1)}`;
        up = false;
      }
      return d.includes("L") ? d : null;
    };
    const placed = (id) => isGlobe ? !!geoOf[id] || !!pos[id] : !!pos[id];
    const line = (fromId, toId, kind, { owners = [], dash = null, flow = true, rtt = null, title = "", into = lines, cls = "" } = {}) => {
      const from = pos[fromId], to = pos[toId];
      if (fromId === toId) return;
      const routed = isGlobe && geoOf[fromId] && geoOf[toId] ? route(fromId, toId) : null;
      if (!routed && (!from || !to)) return;
      const key = [fromId, toId].sort().join("|") + kind;
      if (drawn.has(key)) { for (const id of owners) (touching[id] ||= []).push(...(touching[key] || [])); return; }
      drawn.add(key);
      const color = TONE[kind] || TONE.idle;
      const d = routed || curve(from, to);
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
    if (global) for (const d of others) if (d.online) line(owner(d.device_id), GATEWAY, "control",
      { flow: false, into: spokes, cls: "topo-spoke", owners: [d.device_id],
        title: `${d.display_name} 与网关的长连接${d.gateway_rtt_ms != null ? ` · ${d.gateway_rtt_ms}ms` : ""}` });

    // the links, best kind first so a pair shows its best path when both ends fold into bubbles
    for (const edge of [...edges].sort((x, y) => (RANK[x.kind] ?? 9) - (RANK[y.kind] ?? 9))) {
      const a = owner(edge.a), b = owner(edge.b);
      if (!placed(a) || !placed(b) || a === b) continue;
      const owners = [edge.a, edge.b, a, b];
      const title = `${nameOf(edge.a)} ↔ ${nameOf(edge.b)}：${linkText(edge, nameOf)}${edge.route ? "\n" + edge.route : ""}`;
      if (edge.kind === "direct") {
        line(a, b, "direct", { owners, rtt: edge.rtt_ms, title });
        const [ax, ay] = pos[a] || [0, 0], [bx, by] = pos[b] || [0, 0];
        if (edge.rtt_ms != null && pos[a] && pos[b] && Math.hypot(bx - ax, by - ay) > 70) {
          const t = me && a === me.device_id ? 0.42 : me && b === me.device_id ? 0.58 : 0.5;
          labels.append(svg("text", { x: ax + (bx - ax) * t, y: ay + (by - ay) * t + 4, class: "topo-rtt" }, `${Math.round(edge.rtt_ms)}ms`));
        }
      } else if (edge.kind === "relay") {
        const hop = edge.via && placed(owner(edge.via)) ? owner(edge.via) : null;
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
      const r = big ? SIZE.me : count ? SIZE.relay + 2 + Math.min(6, count) : kind === "relay" || kind === "gateway" ? SIZE.relay : SIZE.device;
      const ring = kind === "offline" ? "var(--border-strong)" : kind === "relay" ? "var(--info)"
        : kind === "gateway" ? "var(--accent)" : kind === "bubble" ? "var(--text-3)" : "var(--ok)";
      const mark = osFamily ? osMark(osFamily, { size: 24 }) : null;
      const s = r * (big ? 0.9 : 1.05) / 24;
      const glyph = svg("g", { transform: `translate(${x - 12 * s} ${y - 12 * s}) scale(${s})`,
        class: "topo-glyph", style: `color:${kind === "offline" ? "var(--text-3)" : "var(--text-2)"}` });
      if (mark) glyph.innerHTML = mark.svg.replace(/^<svg[^>]*>|<\/svg>$/g, "");
      const clickable = (device && !device.self) || kind === "bubble";
      const group = svg("g", { class: `topo-node ${kind}`, "data-id": id, tabindex: clickable ? 0 : null,
        role: clickable ? "button" : null, "aria-label": clickable ? `${title}，点开${kind === "bubble" ? "展开" : "操作"}` : null },
        big || (global && kind === "gateway") ? svg("circle", { cx: x, cy: y, r, class: "topo-halo", style: `stroke:${ring}` }) : null,
        svg("circle", { cx: x, cy: y, r, class: `topo-disc${kind === "bubble" ? " topo-bubble" : ""}`, style: `stroke:${ring}` }),
        kind === "bubble" ? svg("text", { x, y: y + 4, "text-anchor": "middle", class: "topo-count" }, String(count))
          : mark ? glyph : svg("text", { x, y: y + (geoMode ? 3 : 4), "text-anchor": "middle", "font-size": geoMode ? 7 : big ? 11 : 9, class: "topo-inner" },
            kind === "gateway" ? "网关" : kind === "relay" ? "中转" : big ? "本机" : ""),
        kind === "relay" ? svg("circle", { cx: x + r * 0.72, cy: y - r * 0.72, r: geoMode ? 2.5 : 3.5, class: "topo-badge" }) : null,
        svg("text", { x, y: y + r + SIZE.font + 2, "text-anchor": "middle", "font-size": SIZE.font,
          class: `topo-label${kind === "offline" ? " off" : ""}` }, shortName(title, big ? 16 : geoMode ? 9 : 11)),
        sub ? svg("text", { x, y: y + r + SIZE.font * 2 + 3, "text-anchor": "middle", class: "topo-far" }, sub) : null);
      nodes.append(group);
      nodeOf[id] = group;
      return group;
    };

    if (pos[GATEWAY]) node(GATEWAY, { title: host || "网关", kind: "gateway" });
    for (const [id, bubble] of Object.entries(bubbles)) {
      if (bubble.open) {
        // the opened group: a dashed ring around it and a 收起 chip on top
        const [gx, gy] = bubble.at, r = bubble.r + 22;
        halos.append(svg("circle", { cx: gx, cy: gy, r, class: "topo-group" }));
        const chip = svg("g", { class: "topo-chip", tabindex: 0, role: "button", "aria-label": `收起 ${bubble.label}` },
          svg("rect", { x: gx - 34, y: gy - r - 11, width: 68, height: 20, rx: 10 }),
          svg("text", { x: gx, y: gy - r + 3, "text-anchor": "middle" }, `收起 ${shortName(bubble.label.split(/\s+/).pop(), 5)}`));
        const fold = (event) => { event.stopPropagation(); opened.delete(id); redraw(); };
        chip.addEventListener("click", fold);
        chip.addEventListener("pointerdown", (event) => event.stopPropagation());
        chip.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") fold(event); });
        halos.append(chip);
        continue;
      }
      const online = bubble.members.filter((m) => m.online).length;
      node(id, { title: bubble.label, kind: "bubble", count: bubble.members.length, sub: `${online} 在线` });
    }
    const subOf = (device) => {
      if (global && !geoMode && device.online && device.gateway_rtt_ms != null) return `${device.gateway_rtt_ms}ms`;
      return "";
    };
    for (const device of relays) if (!hiddenIn[device.device_id]) node(device.device_id, { title: device.display_name,
      kind: "relay", device, osFamily: device.os_family, sub: subOf(device) });
    for (const device of rest) if (!hiddenIn[device.device_id]) node(device.device_id, { title: device.display_name,
      kind: device.online ? "device" : "offline", device, osFamily: device.os_family, sub: subOf(device) });
    if (me) node(me.device_id, { title: me.display_name, kind: "device", big: true, device: me, osFamily: me.os_family });
    // a chip on top of the nodes, so it stays clickable
    const chips = [...halos.querySelectorAll(".topo-chip")];
    const picture = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "网络拓扑", class: "topo-svg" },
      backdrop, halos, spokes, lines, labels, nodes, chips);

    // -- hover: light up a node's links and show a card
    const place = (box, id, below = true) => {
      const target = nodeOf[id];
      if (!target) return;
      const rect = target.getBoundingClientRect();
      const frameRect = stage.getBoundingClientRect();
      const left = Math.min(Math.max(8, rect.left - frameRect.left + rect.width / 2 - box.offsetWidth / 2), frameRect.width - box.offsetWidth - 8);
      let top = rect.top - frameRect.top + (below ? rect.height + 8 : -box.offsetHeight - 8);
      if (top + box.offsetHeight > frameRect.height - 4) top = rect.top - frameRect.top - box.offsetHeight - 8;
      box.style.left = `${left}px`;
      box.style.top = `${Math.max(4, top)}px`;
    };
    const geoLine = (device) => geoMode ? (device.geo?.lat == null ? "位置未知：拖到地球上即可设置"
      : `${device.geo.label || ""}（${device.geo.source === "manual" ? "手动设置" : "按 IP 推测，可拖动修正"}）`) : "";
    const describe = (id) => {
      if (id === GATEWAY) {
        const online = others.filter((d) => d.online).length;
        return [host || "网关", global ? `${online} 台设备在线；负责信令、发放中转票据，其他路都不通时兜底转发` : "设备之间的信令与兜底中转",
          gateway.online === false ? "正在重连" : gateway.rtt_ms != null ? `本机到网关 ${gateway.rtt_ms}ms` : "已连接",
          geoMode && gatewayGeo ? `${gatewayGeo.label || ""}（按网关的公网地址推测）` : ""].filter(Boolean);
      }
      if (bubbles[id]) {
        const bubble = bubbles[id];
        const online = bubble.members.filter((m) => m.online);
        return [`${bubble.label} · ${bubble.members.length} 台`, `${online.length} 台在线`,
          bubble.members.slice(0, 6).map((m) => m.display_name).join("、") + (bubble.members.length > 6 ? " …" : ""),
          "点击展开，或放大地球"];
      }
      const device = byId[id];
      if (!device) return [];
      const text = [device.display_name];
      if (device.self && !global) {
        const home = (mesh?.home || []).map((h) => `${nameOf(h.device_id)} ${h.rtt_ms}ms`);
        text.push(`本机 · ${gateway.rtt_ms != null ? `到网关 ${gateway.rtt_ms}ms` : "在线"}`,
          mesh?.role === "relay" ? "本机是中转设备" : home.length ? `家中转：${home.join("、")}` : "正在选择家中转");
        return [...text, geoLine(device)].filter(Boolean);
      }
      text.push(device.device_id, !device.online ? "离线" : device.gateway_rtt_ms != null ? `在线 · 到网关 ${device.gateway_rtt_ms}ms` : "在线");
      const mine = linksOf[id] || [];
      if (global) {
        const count = (kind) => mine.filter((l) => l.kind === kind).length;
        if (mine.length) {
          text.push([["直连", count("direct")], ["中转", count("relay")], ["网关", count("gateway")]]
            .filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join(" · "));
          for (const link of mine.slice(0, 5)) text.push(`↔ ${nameOf(link.other)}：${linkText(link, nameOf)}`);
          if (mine.length > 5) text.push(`… 还有 ${mine.length - 5} 条`);
        } else if (device.online) text.push(device.p2p === false ? "只能经网关：客户端没有 P2P 组件"
          : "和其他设备之间暂时没有连接（按需建立）");
      } else if (device.online) {
        const link = mine.find((l) => l.other === me?.device_id) || mine[0];
        text.push(link ? linkText(link, nameOf) : device.p2p === false ? "只能经网关：对方客户端没有 P2P 组件"
          : "按需：打开终端、文件或桌面时自动选路",
        link?.route ? link.route.replace(/^[^（]*（?/, "").replace(/）$/, "") : "");
      }
      if (device.relay) text.push("中转设备：转发其他设备的加密流量");
      return [...text, geoLine(device)].filter(Boolean);
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
        for (const g of touching[member] || []) g.classList.add("hot");
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
      const [title, ...more] = describe(id);
      card.replaceChildren(el("div", { class: "title" }, title || ""), ...more.map((line) => el("div", { class: "muted" }, line)));
      card.hidden = false;
      place(card, id);
    };
    const hide = () => { card.hidden = true; if (panel.hidden) light(null); };

    // -- click: the action panel, or open a bubble
    const closePanel = () => { panel.hidden = true; light(null); document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", escape); };
    const outside = (event) => { if (!panel.contains(event.target) && !event.target.closest?.(".topo-node")) closePanel(); };
    const escape = (event) => { if (event.key === "Escape") closePanel(); };
    const openPanel = (id) => {
      if (bubbles[id] && !bubbles[id].open) { opened.add(id); card.hidden = true; redraw(); return; }
      const device = byId[id];
      if (!device || device.self) return;
      const can = actions.can ? actions.can(device) : {};
      const button = (glyph, label, enabled, run, why) => run ? el("button", { type: "button", class: `btn btn-sm ${enabled ? "" : "disabled"}`,
        "aria-disabled": enabled ? null : "true", title: enabled ? "" : (why || ""),
        onclick: () => { if (!enabled) return; closePanel(); run(device); } }, icon(glyph, { size: 14 }), label) : null;
      const [, ...info] = describe(id);
      const linked = !global && (linksOf[id] || []).length;
      const group = Object.entries(bubbles).find(([, b]) => b.open && b.members.includes(device));
      panel.replaceChildren(
        el("div", { class: "topo-panel-head" }, el("span", { class: "title truncate" }, device.display_name),
          el("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", "aria-label": "关闭", onclick: closePanel }, icon("x", { size: 14 }))),
        el("div", { class: "muted topo-panel-info" }, info.slice(1, 4).join(" · ")),
        el("div", { class: "topo-panel-actions" },
          button("terminal", "终端", can.terminal, actions.terminal, can.why),
          button("folder-open", "文件", can.files, actions.files, can.whyFiles || can.why),
          device.desktop || device.platform !== "linux" ? button("monitor", "远程桌面", can.desktop, actions.desktop, can.whyDesktop || can.why) : null,
          button("plug", linked ? "重新连接" : "建立连接", can.terminal, actions.connect, can.why),
          geoMode && device.geo?.source === "manual" && actions.locate
            ? button("map-pin", "恢复自动位置", true, (d) => actions.locate(d, null)) : null,
          group ? button("minimize-2", "收起这一组", true, () => { opened.delete(group[0]); redraw(); }) : null));
      panel.hidden = false;
      card.hidden = true;
      light(id);
      place(panel, id);
      panel.querySelector(".topo-panel-actions button:not(.disabled)")?.focus();
      setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", escape); });
    };

    const toSvg = (event) => {
      // the picture on screen now: panning replaces it while the pointer is down
      const current = stage.querySelector(".topo-svg") || picture;
      const point = current.createSVGPoint();
      point.x = event.clientX; point.y = event.clientY;
      return point.matrixTransform(current.getScreenCTM().inverse());
    };
    for (const [id, group] of Object.entries(nodeOf)) {
      const device = byId[id];
      const draggable = geoMode && device && actions.locate;
      group.addEventListener("pointerdown", (event) => event.stopPropagation()); // not a pan
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
          const up = (e, cancelled) => {
            if (cancelled) { group.removeAttribute("transform"); return; }
            if (!moved) { openPanel(id); return; }
            const now = toSvg(e);
            const at = P.invert(origin[0] + now.x - start.x, origin[1] + now.y - start.y);
            if (!at) { group.removeAttribute("transform"); return; }  // dropped off the globe
            group.classList.add("topo-saving");
            actions.locate(device, { lat: Math.round(at[0] * 100) / 100, lon: Math.round(at[1] * 100) / 100, label: "手动设置" });
          };
          followDrag(event, move, up);
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

    // -- devices without a place: a tray under the globe; drag one onto it to place it
    let tray = null;
    const unplaced = geoMode ? shown.filter((d) => !d.geo || d.geo.lat == null || d.geo.lon == null) : [];
    if (unplaced.length) {
      tray = el("div", { class: "topo-tray" }, el("span", { class: "muted" }, actions.locate ? "位置未知（拖到图上设置）：" : "位置未知："),
        unplaced.map((device) => {
          const mark = osMark(device.os_family, { size: 14 });
          const chip = el("button", { type: "button", class: `topo-tray-item${device.online ? "" : " off"}`, "data-id": device.device_id },
            mark ? el("span", { html: mark.svg }) : null, shortName(device.display_name, 14));
          nodeOf[device.device_id] = chip;
          chip.addEventListener("mouseenter", () => { if (panel.hidden) { light(device.device_id); showCard(device.device_id); } });
          chip.addEventListener("mouseleave", hide);
          chip.addEventListener("pointerdown", (event) => {
            if (event.button !== 0) return;
            const x0 = event.clientX, y0 = event.clientY;
            let ghost = null;
            const move = (e) => {
              if (!ghost && Math.hypot(e.clientX - x0, e.clientY - y0) < 5) return;
              if (!actions.locate) return;
              ghost ||= document.body.appendChild(el("div", { class: "topo-ghost" }, device.display_name));
              ghost.style.left = `${e.clientX + 8}px`; ghost.style.top = `${e.clientY + 8}px`;
              card.hidden = true;
            };
            const up = (e, cancelled) => {
              if (!ghost) { if (!cancelled && !device.self) openPanel(device.device_id); return; }
              ghost.remove();
              if (cancelled) return;
              const box = picture.getBoundingClientRect();
              if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) return;
              const at = P.invert(toSvg(e).x, toSvg(e).y);
              if (at) actions.locate(device, { lat: Math.round(at[0] * 100) / 100, lon: Math.round(at[1] * 100) / 100, label: "手动设置" });
            };
            followDrag(event, move, up);
          });
          return chip;
        }));
    }

    // -- globe: drag the empty area to turn it, wheel or the buttons to zoom
    let tools = null;
    if (geoMode) {
      const zoom = (factor) => {
        const k = clamp(view.k * factor, GLOBE_K[0], GLOBE_K[1]);
        if (k === view.k) return;
        view.k = k;
        redraw();
      };
      const fit = () => {
        if (!located.length) return;
        Object.assign(view, { k: 1.15, ...centreOf(located) });
        redraw();
      };
      const tool = (label, title, run, glyph = null) => el("button", { type: "button", class: "btn btn-sm", title, "aria-label": title,
        onclick: run }, glyph ? icon(glyph, { size: 14 }) : null, label);
      tools = el("div", { class: "topo-tools" },
        tool("+", "放大", () => zoom(1.6)), tool("−", "缩小", () => zoom(1 / 1.6)),
        tool("整个地球", "回到整个地球", () => { view.k = 1; redraw(); }, "globe"),
        located.length ? tool("对准设备", "把设备所在的一面转过来", fit, "maximize") : null,
        opened.size ? tool("全部收起", "把展开的设备组都收起", () => { opened.clear(); redraw(); }, "minimize-2") : null);
      picture.addEventListener("wheel", (event) => {
        event.preventDefault();
        zoom(Math.exp(-clamp(event.deltaY, -120, 120) * 0.0025));
      }, { passive: false });
      picture.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const start = toSvg(event);
        const from = { ...view };
        let moved = false;
        const move = (e) => {
          const now = toSvg(e);
          const dx = now.x - start.x, dy = now.y - start.y;
          if (!moved && Math.hypot(dx, dy) < 3) return;
          moved = true;
          stage.classList.add("panning");
          card.hidden = true;
          view.lon = from.lon - dx / P.R / RAD;
          view.lat = from.lat + dy / P.R / RAD;
          redraw();
        };
        // on window: the picture is rebuilt while dragging
        followDrag(event, move, () => stage.classList.remove("panning"));
      });
    }

    panel.hidden = true;
    card.hidden = true;
    stage.classList.remove("dim");
    stage.replaceChildren(...[picture, tray, card, panel, tools].filter(Boolean));  // (null would become the text "null")
    teardown = closePanel;
  }

  build();
  const key = (kind, text, dash, faint) => el("span", { class: "row", style: { gap: "6px", alignItems: "center" } },
    el("span", { html: `<svg width="28" height="8"><line x1="1" y1="4" x2="27" y2="4" style="stroke:${TONE[kind]};stroke-width:2;opacity:${faint ? 0.4 : 1}" ${dash ? `stroke-dasharray="${dash}"` : ""}/></svg>` }),
    el("span", { class: "muted" }, text));
  const legend = el("div", { class: "row wrap", style: { gap: "16px", fontSize: "var(--fs-sm)" } },
    key("direct", "直连（数字为往返延迟）"), key("relay", "中转（经中转设备，端到端加密）", "6 5"),
    key("gateway", "网关（兜底转发）", "1 6"), key("punching", "正在打洞", "3 7"),
    global ? key("control", "与网关的长连接", null, true) : null,
    el("span", { class: "muted" }, geoMode
      ? "滚轮或 +/− 缩放，拖动空白处转动地球；数字气泡是同一地点的设备，点开展开，「收起」还原；拖动设备可修正位置"
      : global ? "连线是设备之间实际在用的连接（按需建立，空闲一段时间后断开）" : "点设备可直接打开终端、文件或桌面"));
  // where the positions come from (DB-IP and GeoNames are CC BY 4.0: they are credited where shown)
  const source = (label, href) => el("a", { href, target: "_blank", rel: "noopener noreferrer" }, label);
  const credits = geoMode ? el("div", { class: "muted", style: { fontSize: "var(--fs-xs)" } },
    "位置按 IP 推测（在网关上离线查询）：", source("ip2region", "https://github.com/lionsoul2014/ip2region"), "（境内）、",
    source("DB-IP", "https://db-ip.com"), "（境外，CC BY 4.0）；城市坐标 ",
    source("GeoNames", "https://www.geonames.org"), "（CC BY 4.0）") : null;
  return el("div", { class: "stack", style: { gap: "12px" } }, stage, legend, credits);
}
