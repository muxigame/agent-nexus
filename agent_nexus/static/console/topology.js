// The gateway console's 拓扑 page: the whole network as the devices report it (links/1) --
// the gateway in the middle, relays around it, every device grouped by region, and the links
// the devices actually use between each other (直连 / 中转 / 网关), on rings or on a map.
import { el, mount, icon, fetchJSON, toast, errorState, skeletonRows } from "/ui/ui.js";
import { topology, topologySignature, loadWorld } from "/ui/topology.js";

function savedView() {
  try {
    const value = localStorage.getItem("anx.console.topology");
    return value === "map" || value === "globe" ? "globe" : "rings";  // the flat map is gone
  } catch { return "rings"; }
}

function summary(devices, edges) {
  const online = devices.filter((d) => d.online && !d.revoked).length;
  const count = (kind) => edges.filter((e) => e.kind === kind).length;
  const rtts = devices.filter((d) => d.online && d.gateway_rtt_ms != null).map((d) => d.gateway_rtt_ms);
  const relays = devices.filter((d) => d.online && d.relay).length;
  const stat = (label, value, sub) => el("div", { class: "card stat" },
    el("span", { class: "stat-label" }, label), el("span", { class: "stat-value" }, String(value)), el("span", { class: "stat-meta" }, sub || ""));
  return el("div", { class: "grid cols-4" },
    stat("在线设备", online, `共 ${devices.filter((d) => !d.revoked).length} 台`),
    stat("中转设备", relays, relays ? "转发其他设备的加密流量" : "还没有设备开启中转"),
    stat("在用连接", edges.length, `直连 ${count("direct")} · 中转 ${count("relay")} · 网关 ${count("gateway")}`),
    stat("到网关延迟", rtts.length ? `${Math.round(rtts.reduce((a, b) => a + b, 0) / rtts.length)}ms` : "—",
      rtts.length ? `中位 ${rtts.sort((a, b) => a - b)[Math.floor(rtts.length / 2)]}ms · 最高 ${Math.max(...rtts)}ms` : "设备升级到 0.8.18 后显示"));
}

export async function topologyPage(ctx) {
  const { page } = ctx;
  let view = savedView();
  let drawn = "";
  let last = null;
  const holder = el("div", { class: "stack lg" }, skeletonRows(3));
  const option = (key, label, glyph) => el("button", { type: "button", "aria-pressed": view === key ? "true" : "false",
    dataset: { view: key },
    onclick: async () => {
      view = key; drawn = "";
      try { localStorage.setItem("anx.console.topology", key); } catch { /* private mode */ }
      for (const b of switcher.children) b.setAttribute("aria-pressed", b.dataset.view === key ? "true" : "false");
      if (key !== "rings") await loadWorld();
      if (last) render(last);
    } }, icon(glyph, { size: 14 }), label);
  const switcher = el("div", { class: "segmented", role: "group", "aria-label": "显示方式" },
    option("rings", "拓扑", "network"), option("globe", "地球", "globe"));
  mount(page,
    el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "拓扑"),
      el("p", {}, "所有设备之间实际在用的连接，由各设备每 30 秒上报。连接按需建立：没打开过终端、文件或桌面的两台设备之间没有连线是正常的。")),
      el("div", { class: "actions" }, switcher)),
    holder);

  async function locate(device, location) {
    try {
      await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}`, { method: "PUT", body: { location } });
      toast(location ? `已把 ${device.display_name} 放到这里` : `${device.display_name} 改回按 IP 自动定位`);
    } catch { /* toasted */ }
    drawn = "";
    load();
  }

  function render(data) {
    last = data;
    const picture = { devices: data.devices || [], edges: data.edges || [], scope: "global",
      gateway: { origin: data.origin, online: true, geo: data.gateway?.geo || null } };
    const signature = `${view}|${topologySignature(picture)}`;
    if (signature === drawn && holder.querySelector(".topo")) return;
    drawn = signature;
    mount(holder,
      summary(picture.devices, picture.edges),
      el("section", { class: "card" }, el("div", { class: "card-body" },
        topology(picture, {
          can: (device) => ({ terminal: device.online && device.terminal, why: !device.online ? "设备不在线" : "设备不支持远程终端" }),
          terminal: (device) => { location.hash = `#/terminal/${encodeURIComponent(device.device_id)}`; },
          locate,
        }, { layout: view }))));
  }

  async function load() {
    try {
      render(await fetchJSON("/api/topology", { quiet: true }));
    } catch (error) {
      mount(holder, errorState(error, load));
    }
  }

  if (view !== "rings") await loadWorld();
  await load();
  ctx.every(10000, load);
}
