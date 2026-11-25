// The 设备 page's topology view: this device in the middle, the gateway above, relay devices on
// an inner ring, every other device on the outer ring; lines show how this device reaches each
// one right now (direct, through a relay, through the gateway, punching).
import { el } from "/ui/ui.js";

const NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const TONE = { direct: "var(--ok)", relay: "var(--info)", gateway: "var(--warn)", punching: "var(--warn)", idle: "var(--border-strong)" };

function shortName(text, max = 10) {
  const chars = Array.from(text || "");
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : chars.join("");
}

/**
 * @param {object} data  {me, devices: [...] (from /api/devices), conns: (from /api/remote/connections)}
 * @param {(device) => void} onPick  a node was clicked
 */
export function topology({ devices = [], conns = {} }, onPick) {
  const W = 760, H = 480, cx = W / 2, cy = H / 2 - 10;
  const me = devices.find((d) => d.self);
  const others = devices.filter((d) => !d.self && !d.revoked);
  const byId = Object.fromEntries(devices.map((d) => [d.device_id, d]));
  const links = conns.devices || {};
  const relays = others.filter((d) => d.relay && d.online);
  const rest = others.filter((d) => !(d.relay && d.online))
    .sort((a, b) => (b.online - a.online) || (a.display_name || "").localeCompare(b.display_name || ""));

  const pos = {};
  if (me) pos[me.device_id] = [cx, cy];
  // the gateway sits in the top-right corner: straight above would run this device's line to it
  // through the middle relay
  const gateway = [W - 70, 44];
  relays.forEach((d, i) => {
    const angle = Math.PI * (0.15 + 0.7 * ((i + 0.5) / relays.length)) + Math.PI; // the upper half
    pos[d.device_id] = [cx + 150 * Math.cos(angle), cy + 105 * Math.sin(angle)];
  });
  // ordinary devices on the lower arc (both sides and the bottom), clear of the gateway and relays
  rest.forEach((d, i) => {
    const angle = -0.12 * Math.PI + 1.24 * Math.PI * ((i + 0.5) / Math.max(rest.length, 1));
    pos[d.device_id] = [cx + 330 * Math.cos(angle), cy + 190 * Math.sin(angle)];
  });

  const lines = svg("g", { "stroke-linecap": "round" });
  const labels = svg("g", { "font-size": 11, "text-anchor": "middle" });
  const line = (a, b, kind, title, dash) => {
    if (!a || !b) return;
    lines.append(svg("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1],
      style: `stroke:${TONE[kind] || TONE.idle};stroke-width:${kind === "idle" ? 1 : 2}`,
      "stroke-dasharray": dash || null }, svg("title", {}, title || "")));
  };

  // this device -> gateway: the always-on control link
  if (me) line(pos[me.device_id], gateway, conns.gateway?.online === false ? "idle" : "direct",
    `与网关的长连接${conns.gateway?.origin ? "：" + conns.gateway.origin : ""}`, "2 4");

  for (const device of others) {
    const here = pos[device.device_id];
    const link = links[device.device_id];
    if (!me || !here) continue;
    const mine = pos[me.device_id];
    if (!link) continue;
    if (link.state === "direct") {
      line(mine, here, "direct", link.route);
      if (link.rtt_ms != null) {
        // near this device's end of the line, outlined so it stays readable over other lines
        const mx = mine[0] + (here[0] - mine[0]) * 0.42, my = mine[1] + (here[1] - mine[1]) * 0.42;
        labels.append(svg("text", { x: mx, y: my + 4,
          style: "fill:var(--ok);paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round" },
          `${link.rtt_ms}ms`));
      }
    } else if (link.state === "relay") {
      const via = Object.values(byId).find((d) => d.display_name === link.via || d.device_id === link.via);
      const hop = via ? pos[via.device_id] : null;
      if (hop) {
        if (!links[via.device_id]) line(mine, hop, "relay", link.route, "6 4");
        line(hop, here, "relay", link.route, "6 4");
      } else line(mine, here, "relay", link.route, "6 4");
    } else if (link.state === "gateway") {
      line(gateway, here, "gateway", link.route, "1 5");
    } else if (link.state === "punching") {
      line(mine, here, "punching", "正在打洞直连", "3 6");
    }
  }

  const nodes = svg("g", {});
  const node = (xy, { title, sub, kind, big, device }) => {
    const [x, y] = xy;
    const r = big ? 26 : 18;
    const stroke = kind === "offline" ? "var(--border-strong)" : kind === "relay" ? "var(--info)"
      : kind === "gateway" ? "var(--accent)" : "var(--ok)";
    const group = svg("g", { style: device ? "cursor:pointer" : null, tabindex: device ? 0 : null },
      svg("title", {}, [title, sub].filter(Boolean).join("\n")),
      svg("circle", { cx: x, cy: y, r, style: `fill:var(--surface);stroke:${stroke};stroke-width:${big ? 3 : 2}` }),
      svg("text", { x, y: y + 4, "text-anchor": "middle", "font-size": big ? 12 : 10,
        style: `fill:${kind === "offline" ? "var(--text-3)" : "var(--text-2)"}` },
        kind === "gateway" ? "网关" : kind === "relay" ? "中转" : big ? "本机" : ""),
      svg("text", { x, y: y + r + 14, "text-anchor": "middle", "font-size": 11,
        style: `fill:${kind === "offline" ? "var(--text-3)" : "var(--text)"}` }, shortName(title, big ? 14 : 10)));
    if (device && onPick) {
      group.addEventListener("click", () => onPick(device));
      group.addEventListener("keydown", (event) => { if (event.key === "Enter") onPick(device); });
    }
    nodes.append(group);
  };

  let host = "";
  try { host = conns.gateway?.origin ? new URL(conns.gateway.origin).host : ""; } catch { /* not a URL */ }
  node(gateway, { title: host || "网关", sub: conns.gateway?.origin || "", kind: "gateway" });
  for (const device of relays) node(pos[device.device_id], { title: device.display_name, sub: "中转设备 · " + device.device_id, kind: "relay", device });
  for (const device of rest) node(pos[device.device_id], { title: device.display_name,
    sub: (device.online ? "" : "离线 · ") + device.device_id, kind: device.online ? "device" : "offline", device });
  if (me) node(pos[me.device_id], { title: me.display_name, sub: "本机", kind: "device", big: true });

  const picture = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "网络拓扑",
    style: "display:block;max-height:560px;font-family:var(--font)" }, lines, labels, nodes);

  const key = (kind, text, dash) => el("span", { class: "row", style: { gap: "6px", alignItems: "center" } },
    el("span", { html: `<svg width="28" height="8"><line x1="1" y1="4" x2="27" y2="4" style="stroke:${TONE[kind]};stroke-width:2" ${dash ? `stroke-dasharray="${dash}"` : ""}/></svg>` }),
    el("span", { class: "muted" }, text));
  const legend = el("div", { class: "row wrap", style: { gap: "16px", fontSize: "var(--fs-sm)" } },
    key("direct", "直连（数字为往返延迟）"), key("relay", "经中转（端到端加密）", "6 4"),
    key("gateway", "网关中转", "1 5"), key("punching", "正在打洞", "3 6"));
  return el("div", { class: "stack", style: { gap: "12px" } }, picture, legend);
}
