// Console pages: overview, devices (with invites), settings (docs/ui-spec.md §3.1, §3.2, §3.6).
import {
  el, mount, icon, iconHTML, fetchJSON, toast, modal, confirmDialog, copy, tokenField, emptyState, errorState,
  skeletonRows, pill, relTime, setTheme, bytes,
} from "/ui/ui.js";
import { qrSVG } from "/ui/qr.js";
import { osMark } from "/ui/os-icons.js";
import { ubuntuServerJoinCommand } from "/console/install-commands.js";

// ---------------------------------------------------------------- shared bits

const ENROLLED = { agents_map: "令牌映射", invite: "邀请码", agent_groups: "共享令牌" };
const TTL_CHOICES = [[3600, "1 小时"], [86400, "24 小时"], [7 * 86400, "7 天"]];

function deviceIcon(device) {
  if (device.form === "phone" || device.form === "mobile") return "smartphone";
  if (device.form === "laptop") return "laptop";
  if (device.platform === "linux") return "server";
  return "monitor";
}

/** The device's avatar: its operating-system mark, else a generic device glyph. */
function deviceAvatar(device) {
  const mark = osMark(device.os_family);
  if (mark) return el("span", { class: "avatar os-mark", title: mark.label, "aria-label": mark.label, html: mark.svg });
  return el("span", { class: "avatar" }, icon(deviceIcon(device), { size: 16 }));
}

function deviceState(device) {
  if (device.revoked) return "revoked";
  return device.online ? "online" : "offline";
}

function head(title, text, actions = []) {
  return el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, title), text ? el("p", {}, text) : null),
    actions.length ? el("div", { class: "actions" }, actions) : null);
}

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "夜深了" : hour < 11 ? "早上好" : hour < 13 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
}

/** A small dropdown menu next to `anchor`: items [{label, icon, danger, onClick}]. */
function openMenu(anchor, items) {
  document.querySelector(".menu.floating")?.remove();
  const rect = anchor.getBoundingClientRect();
  const menu = el("div", { class: "menu floating", role: "menu" }, items.map((item) => item.separator ? el("div", { class: "menu-sep" }) :
    el("button", { class: `menu-item ${item.danger ? "danger" : ""}`, type: "button", role: "menuitem",
      onclick: () => { menu.remove(); item.onClick(); } }, icon(item.icon, { size: 14 }), item.label)));
  document.body.append(menu);
  const width = menu.offsetWidth || 180;
  menu.style.top = `${window.scrollY + rect.bottom + 4}px`;
  menu.style.left = `${Math.max(8, Math.min(window.scrollX + rect.right - width, window.scrollX + document.documentElement.clientWidth - width - 8))}px`;
  const close = (event) => { if (!menu.contains(event.target) && event.target !== anchor) { menu.remove(); document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close));
  menu.querySelector("button")?.focus();
}

// ---------------------------------------------------------------- overview

export async function overviewPage(ctx) {
  const { page } = ctx;
  mount(page, head(greeting(), "正在读取网关状态…"), el("div", { class: "grid cols-4" },
    Array.from({ length: 4 }, () => el("div", { class: "card stat" }, el("span", { class: "skeleton w-40" }), el("span", { class: "skeleton title w-60" })))));
  let data;
  const load = async () => {
    try {
      data = await fetchJSON("/api/console/summary", { quiet: true });
    } catch (error) {
      mount(page, head(greeting(), ""), errorState(error, () => overviewPage(ctx)));
      return;
    }
    render(data);
  };
  function render(summary) {
    const { devices, inbox, gateway, setup } = summary;
    const sentence = `你的 Agent Nexus 网关运行正常。${devices.total ? `${devices.online} / ${devices.total} 台设备在线` : "还没有设备加入"}` +
      `${inbox.unread ? `，${inbox.unread} 封未读邮件` : ""}${inbox.open_asks ? `，${inbox.open_asks} 个问题等你回答` : ""}。`;
    const alert = (summary.alerts || [])[0];
    const stat = (href, glyph, label, value, unit, meta) => el(href ? "a" : "div", { class: `card stat ${href ? "interactive" : ""}`, href },
      el("span", { class: "stat-label" }, icon(glyph, { size: 14 }), label),
      el("span", { class: "stat-value" }, String(value), unit ? el("span", { class: "unit" }, unit) : null),
      el("span", { class: "stat-meta" }, meta));
    const offline = devices.items.filter((d) => !d.online && !d.revoked);
    const steps = [
      [setup.installer_available, "下载并安装客户端", "在要接入的电脑上安装 Agent Nexus。", { label: "下载", href: "#/downloads", icon: "download" }],
      [setup.device_joined, "用邀请码把设备加入网关", "设备 → 邀请新设备，在客户端“连接云端”页粘贴。", { label: "邀请新设备", href: "#/devices?invite=1", icon: "ticket" }],
      [setup.client_authorized, "在 ChatGPT / Claude 中添加连接器", "使用右侧的 MCP 地址，授权时输入 owner 密码。", { label: "查看说明", href: "#/settings", icon: "external-link" }],
      [setup.connector_added, "接入第三方 MCP（可选）", "阿里云、Cloudflare 等远程 MCP 跑在网关上，所有 AI 客户端都能用。", { label: "添加连接器", href: "#/connectors?open=add-connector", icon: "plug" }],
    ];
    const done = steps.filter((item) => item[0]).length;
    const current = steps.findIndex((item) => !item[0]);
    mount(page,
      head(greeting(), sentence, [
        el("a", { class: "btn", href: "#/downloads" }, icon("download", { size: 16 }), "下载客户端"),
        el("a", { class: "btn", href: "#/connectors?open=add-connector" }, icon("plug", { size: 16 }), "添加连接器"),
        el("a", { class: "btn btn-primary", href: "#/devices?invite=1" }, icon("ticket", { size: 16 }), "邀请新设备"),
      ]),
      alert ? el("div", { class: `banner ${alert.level || "warn"}` }, icon("triangle-alert", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, alert.title), alert.text ? el("div", { class: "muted" }, alert.text) : null,
          summary.alerts.length > 1 ? el("div", { class: "faint xs" }, `另有 ${summary.alerts.length - 1} 项`) : null),
        alert.target && alert.kind?.startsWith("connector_") ? el("a", { class: `btn btn-sm ${alert.action === "login" ? "btn-primary" : ""}`, href: "#/connectors" },
          icon(alert.action === "login" ? "log-in" : "plug", { size: 14 }), alert.action === "login" ? "去登录" : "查看") : null) : null,
      el("div", { class: "grid cols-4" },
        stat("#/devices", "monitor", "在线设备", devices.online, `/ ${devices.total}`,
          offline.length ? (offline[0].last_connected_at ? `${offline[0].display_name} ${relTime(offline[0].last_seen_at)}离线` : `${offline[0].display_name} 尚未连接`) : devices.total ? "全部在线" : "还没有设备"),
        stat("#/connectors", "plug", "连接器", summary.connectors.total, "",
          summary.connectors.needs_login ? `${summary.connectors.needs_login} 个需要登录` : summary.connectors.error ? `${summary.connectors.error} 个连接失败`
            : summary.connectors.total ? `${summary.tools.connectors || 0} 个工具可用` : "添加阿里云、Cloudflare 等 MCP"),
        stat("#/inbox", "inbox", "未读邮件", inbox.unread, "", inbox.enabled ? (summary.recent_threads[0] ? `最新：${relTime(summary.recent_threads[0].last_at)}` : "收件箱是空的") : "邮箱未启用"),
        stat("#/inbox", "circle-help", "待回答", inbox.open_asks, "", inbox.open_asks ? "Agent 在等你的回答" : "没有待回答的问题")),
      el("div", { class: "grid side" },
        el("div", { class: "stack lg" },
          done < steps.length ? el("section", { class: "card" },
            el("div", { class: "card-head" }, el("h2", {}, "开始使用"), el("span", { class: "pill accent plain" }, `${done} / ${steps.length} 已完成`)),
            el("div", { class: "card-body" }, el("div", { class: "steps" }, steps.map(([ok, title, desc, action], index) =>
              el("div", { class: `step ${ok ? "done" : index === current ? "current" : ""}` }, el("div", { class: "step-body" },
                el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc),
                !ok && index === current ? el("div", { class: "row", style: { marginTop: "8px" } },
                  el("a", { class: "btn btn-primary btn-sm", href: action.href }, icon(action.icon, { size: 14 }), action.label)) : null)))))) : null,
          el("section", { class: "card flush" },
            el("div", { class: "card-head" }, el("h2", {}, "设备"),
              el("div", { class: "actions" }, el("a", { class: "btn btn-ghost btn-sm", href: "#/devices" }, "全部", icon("chevron-right", { size: 14 })))),
            devices.items.length ? el("div", { class: "list inset" }, devices.items.map((device) => el("a", { class: "list-item clickable", href: "#/devices" },
              deviceAvatar(device),
              el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title truncate" }, device.display_name),
                el("span", { class: "sub truncate" }, el("span", { class: "mono" }, device.device_id), device.os ? ` · ${device.os}` : "")),
              el("div", { class: "meta" }, !device.online && device.last_connected_at ? el("span", { class: "faint xs hide-sm" }, relTime(device.last_seen_at)) : null,
                pill(deviceState(device)))))) : el("div", { class: "card-body" }, emptyState({ icon: "monitor", title: "还没有设备", compact: true,
              text: "安装客户端后用邀请码加入。", action: { label: "邀请新设备", icon: "ticket", href: "#/devices?invite=1" } })))),
        el("div", { class: "stack lg" },
          el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "MCP 地址")),
            el("div", { class: "card-body stack" },
              el("p", { class: "small muted" }, "在 ChatGPT、Claude 或任何 MCP 客户端中添加这个地址，授权时输入 owner 密码。"),
              tokenField(gateway.mcp_url),
              el("dl", { class: "kv", style: { marginTop: "8px" } },
                el("dt", {}, "已授权客户端"), el("dd", {}, summary.clients.length ? summary.clients.map((c) => c.name).slice(0, 4).join("、") : "还没有"),
                el("dt", {}, "网关版本"), el("dd", { class: "mono" }, gateway.version),
                el("dt", {}, "可用工具"), el("dd", {}, `${summary.tools.total} 个`)),
              el("a", { class: "btn btn-sm", href: "#/settings", style: { alignSelf: "flex-start" } }, "各客户端的添加方法", icon("chevron-right", { size: 14 })))),
          el("section", { class: "card flush" },
            el("div", { class: "card-head" }, el("h2", {}, "收件箱"),
              el("div", { class: "actions" }, el("a", { class: "btn btn-ghost btn-sm", href: "#/inbox" }, "打开", icon("chevron-right", { size: 14 })))),
            summary.recent_threads.length ? el("div", { class: "list inset" }, summary.recent_threads.map((thread) =>
              el("a", { class: "list-item clickable", href: `#/inbox/${encodeURIComponent(thread.thread_id)}` },
                el("span", { class: `avatar sm ${thread.open_asks ? "accent" : ""}` }, thread.open_asks ? "?" : String(thread.last_from || "?")[0].toUpperCase()),
                el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title truncate" }, thread.subject || "（无主题）"),
                  el("span", { class: "sub truncate" }, `${thread.last_from || ""} · ${thread.open_asks ? "等待你的回答" : thread.last_snippet || ""}`)),
                el("div", { class: "meta" }, el("span", { class: "faint xs" }, relTime(thread.last_at)))))) :
              el("div", { class: "card-body" }, el("p", { class: "muted small" }, "还没有邮件。Agent 用 mail_send 发给 me 或调用 ask_user 时会出现在这里。"))))));
  }
  await load();
  ctx.every(10000, load);
}

// ---------------------------------------------------------------- devices

export async function devicesPage(ctx, route) {
  const { page } = ctx;
  const inviteButton = el("button", { class: "btn btn-primary", type: "button", onclick: () => inviteDialog(reload) }, icon("ticket", { size: 16 }), "邀请新设备");
  const devicesHolder = el("div", {}, skeletonRows(3));
  const invitesHolder = el("section");
  mount(page, head("设备", "运行 Agent Nexus 客户端并加入网关的电脑。每台设备有自己的凭据，可以随时吊销。", [inviteButton]), devicesHolder, invitesHolder);

  async function reload() {
    let devices, invites;
    try {
      [devices, invites] = await Promise.all([fetchJSON("/api/devices", { quiet: true }), fetchJSON("/api/invites", { quiet: true })]);
    } catch (error) {
      mount(devicesHolder, errorState(error, reload));
      return;
    }
    renderDevices(devices.devices || []);
    renderInvites(invites.invites || []);
  }

  function renderDevices(devices) {
    if (!devices.length) {
      mount(devicesHolder, el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({
        icon: "monitor", title: "还没有设备", text: "在电脑上安装 Agent Nexus，然后用邀请码加入这个网关。",
        action: { label: "邀请新设备", icon: "ticket", onClick: () => inviteDialog(reload) },
        secondary: { label: "下载客户端", href: "#/downloads" } }))));
      return;
    }
    mount(devicesHolder, el("div", { class: "table-wrap" }, el("table", { class: "table stack-sm" },
      el("thead", {}, el("tr", {}, ["设备", "状态", "设备 ID", "版本", "入网方式", "最后在线", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, devices.map((device) => el("tr", { dataset: { device: device.device_id } },
        el("td", { class: "cell-first" }, el("div", { class: "cell-main" },
          deviceAvatar(device),
          el("div", { class: "stack", style: { gap: 0 } }, el("span", { class: "title" }, device.display_name),
            el("span", { class: "sub" }, device.hostname ? ["主机名 ", el("span", { class: "mono" }, device.hostname)] : "未上报主机名",
              device.os ? ` · ${device.os}` : "")))),
        el("td", { "data-label": "状态" }, pill(deviceState(device))),
        el("td", { "data-label": "设备 ID" }, el("span", { class: "mono" }, device.device_id)),
        el("td", { "data-label": "版本" }, el("span", { class: "mono" }, device.version || "—")),
        el("td", { "data-label": "入网方式" }, el("span", { class: "tag" }, ENROLLED[device.enrolled_via] || device.enrolled_via || "—")),
        el("td", { "data-label": "最后在线", class: "muted" }, device.online ? "现在" : device.last_connected_at ? relTime(device.last_seen_at) : "从未连接"),
        el("td", { class: "actions" }, el("div", { class: "btn-group" },
          device.online && device.terminal ? el("a", { class: "btn btn-sm", href: `#/terminal/${encodeURIComponent(device.device_id)}`, title: "打开远程终端" },
            icon("terminal", { size: 14 }), "终端") : null,
          el("button", { class: "btn btn-sm", type: "button", onclick: () => renameDialog(device, reload) }, icon("pencil", { size: 14 }), "备注名"),
          el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "更多操作", html: iconHTML("ellipsis", { size: 16 }),
            onclick: (event) => openMenu(event.currentTarget, [
              { label: "复制设备 ID", icon: "copy", onClick: () => copy(device.device_id) },
              { separator: true },
              { label: device.revoked ? "已吊销" : "吊销", icon: "circle-x", danger: true, onClick: () => revoke(device, reload) },
              ...(device.online ? [] : [{ label: "删除", icon: "trash", danger: true, onClick: () => removeDevice(device, reload) }]),
            ]) })))))))));
  }

  function renderInvites(invites) {
    mount(invitesHolder,
      el("div", { class: "section-head" }, el("h2", {}, "未使用的邀请"), el("span", { class: "faint small hide-sm" }, "邀请只能使用一次，过期后自动失效")),
      invites.length ? el("div", { class: "list" }, invites.map((invite) => el("div", { class: "list-item", dataset: { invite: invite.id } },
        el("span", { class: "avatar accent" }, icon("ticket", { size: 16 })),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title" }, invite.label || "未备注的邀请"),
          el("span", { class: "sub" }, el("span", { class: "mono" }, invite.id), ` · 创建于 ${relTime(invite.created_at)}`,
            invite.agent_id ? ["，设备 ID ", el("span", { class: "mono" }, invite.agent_id)] : "")),
        el("div", { class: "meta" }, el("span", { class: "faint xs hide-sm" }, `${relTime(invite.expires_at)}过期`), pill(invite.state),
          el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "作废", title: "作废", html: iconHTML("trash", { size: 14 }),
            onclick: async () => {
              if (!await confirmDialog({ title: "作废这个邀请？", message: "作废后这个邀请码不能再用来加入。", confirmLabel: "作废", danger: true })) return;
              try { await fetchJSON(`/api/invites/${encodeURIComponent(invite.id)}`, { method: "DELETE" }); toast("邀请已作废"); } catch { /* toasted */ }
              reload();
            } }))))) :
        el("p", { class: "muted small" }, "没有未使用的邀请。点击“邀请新设备”生成一个。"));
  }

  await reload();
  ctx.every(10000, reload);
  if (route.query.get("invite")) {
    history.replaceState(null, "", "#/devices");
    inviteDialog(reload);
  }
}

function renameDialog(device, reload) {
  const input = el("input", { class: "input", id: "alias", value: device.alias || "", maxlength: "64", placeholder: device.hostname || device.device_id });
  modal({
    title: "设置备注名", size: "sm", description: "Agent 在 device_list、地址和提醒中看到的都是备注名；agent_id 不变。",
    body: el("div", { class: "form" }, el("div", { class: "field" }, el("label", { for: "alias" }, "备注名"), input,
      el("span", { class: "hint" }, `留空则显示主机名 ${device.hostname || device.device_id}`))),
    actions: [{ label: "取消" }, { label: "保存", variant: "primary", onClick: async () => {
      try {
        await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}`, { method: "PUT", body: { alias: input.value.trim() || null } });
        toast("备注名已更新");
        reload();
      } catch { return false; }
      return true;
    } }],
  });
}

async function revoke(device, reload) {
  if (device.revoked) { toast("这台设备已经吊销了", { type: "info" }); return; }
  const ok = await confirmDialog({ title: `吊销 ${device.display_name}？`, danger: true, confirmLabel: "吊销",
    message: "吊销后该设备立即断开，它的凭据失效；需要新的邀请才能重新加入。" });
  if (!ok) return;
  try {
    await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}/revoke`, { method: "POST", quiet: true });
    toast(`${device.display_name} 已吊销`);
  } catch (error) {
    if (error.data?.code === "agents_map") {
      modal({ title: "这台设备不能在这里吊销", size: "sm", body: el("div", { class: "stack" },
        el("p", { class: "muted" }, error.message),
        el("p", { class: "small" }, "它是用 gateway.env 里的固定令牌接入的（0.2 之前的方式）。删除对应的变量并重启网关后，再用邀请码重新加入，以后就能在这里一键吊销。")),
        actions: [{ label: "知道了", variant: "primary" }] });
    } else {
      toast(error.message, { type: "bad" });
    }
  }
  reload();
}

async function removeDevice(device, reload) {
  if (device.enrolled_via === "invite" && !device.revoked) {
    modal({ title: "先吊销再删除", size: "sm", body: el("p", { class: "muted" },
      `${device.display_name} 的凭据仍然有效，删除后它还能重新连上来。先吊销，再删除。`),
      actions: [{ label: "取消" }, { label: "吊销", variant: "danger", onClick: () => { revoke(device, reload); return true; } }] });
    return;
  }
  const comesBack = device.enrolled_via && device.enrolled_via !== "invite";
  const ok = await confirmDialog({ title: `删除 ${device.display_name}？`, danger: true, confirmLabel: "删除",
    message: comesBack
      ? "从设备列表中移除这条记录。它用 gateway.env 里的令牌入网，如果再次连接会重新出现。"
      : "从设备列表中移除这条记录和它的备注名。删除后不能恢复；要重新接入需要新的邀请。" });
  if (!ok) return;
  try {
    await fetchJSON(`/api/devices/${encodeURIComponent(device.device_id)}`, { method: "DELETE", quiet: true });
    toast(`${device.display_name} 已删除`);
  } catch (error) {
    toast(error.message, { type: "bad" });
  }
  reload();
}

export function inviteDialog(reload) {
  const label = el("input", { class: "input", id: "invite-label", maxlength: "64", placeholder: "例如：家里的台式机" });
  const ttl = el("select", { class: "select", id: "invite-ttl" }, TTL_CHOICES.map(([value, text]) => el("option", { value, selected: value === 86400 }, text)));
  const agentId = el("input", { class: "input mono", id: "invite-agent", maxlength: "100", placeholder: "默认按主机名生成" });
  modal({
    title: "邀请新设备", description: "生成一次性的邀请码。新电脑装好 Agent Nexus 后，在“连接云端”页粘贴即可加入。",
    body: el("div", { class: "form" },
      el("div", { class: "field" }, el("label", { for: "invite-label" }, "备注 ", el("span", { class: "optional" }, "可选")), label),
      el("div", { class: "form-row" },
        el("div", { class: "field" }, el("label", { for: "invite-ttl" }, "有效期"), ttl),
        el("div", { class: "field" }, el("label", { for: "invite-agent" }, "指定 agent_id ", el("span", { class: "optional" }, "可选")), agentId))),
    actions: [{ label: "取消" }, { label: "生成邀请", variant: "primary", icon: "ticket", onClick: async () => {
      let created;
      try {
        created = await fetchJSON("/api/invites", { method: "POST", body: { label: label.value.trim() || null, ttl_s: Number(ttl.value), agent_id: agentId.value.trim() || null } });
      } catch { return false; }
      showInvite(created);
      reload?.();
      return true;
    } }],
  });
}

function showInvite(created) {
  const expires = new Date(created.invite.expires_at * 1000);
  const ttlText = TTL_CHOICES.find(([value]) => Math.abs(created.invite.expires_at - created.invite.created_at - value) < 5)?.[1] || "限定时间";
  modal({
    title: "邀请已创建", description: "在新电脑上安装 Agent Nexus，打开“连接云端”，粘贴下面的邀请码。",
    body: el("div", { class: "stack lg" },
      el("div", { class: "banner ok" }, icon("circle-check", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, "只显示这一次"), el("div", { class: "muted" }, `邀请码 ${ttlText}内有效，只能使用一次。关闭后无法再查看。`))),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "邀请码"), tokenField(created.code, { multiline: true }),
        el("span", { class: "hint" }, "邀请码里已经带有网关地址，客户端粘贴即可；也可以在命令行运行 anx join <邀请码>。")),
      el("div", { class: "invite-qr-row" },
        el("div", { class: "qr-box", html: qrSVG(created.link, { label: "邀请链接二维码" }) }),
        el("div", { class: "stack grow" },
          el("span", { class: "field-label" }, "或者打开邀请链接"),
          tokenField(created.link, { copyLabel: "复制链接" }),
          el("span", { class: "hint" }, "已装好客户端的电脑上点击链接会自动填入；手机扫码可把链接带到电脑上。"))),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "Ubuntu 服务器：下载、安装、加入并作为服务运行"),
        tokenField(ubuntuServerJoinCommand(created.code), { multiline: true, copyLabel: "复制命令" }),
        el("span", { class: "hint" }, "在服务器上以要运行它的用户（直接 SSH 登录，不要 su）粘贴执行；命令里含邀请码，同样只能用一次。")),
      el("dl", { class: "kv" },
        el("dt", {}, "备注"), el("dd", {}, created.invite.label || "—"),
        el("dt", {}, "过期时间"), el("dd", {}, expires.toLocaleString("zh-CN", { hour12: false })),
        el("dt", {}, "还没装客户端？"), el("dd", {}, el("a", { href: "#/downloads" }, "下载安装包")))),
    actions: [{ label: "完成", variant: "primary" }],
  });
}

// ---------------------------------------------------------------- settings

const CLIENT_GUIDES = [
  { id: "chatgpt", label: "ChatGPT", steps: (url) => [
    ["打开连接器设置", "ChatGPT → 设置 → 应用与连接器（Apps & Connectors）→ 高级设置，打开“开发者模式”。"],
    ["创建连接器", el("span", {}, "点“创建”，名称填 Agent Nexus，MCP 服务器 URL 填 ", el("span", { class: "mono" }, url), "，身份验证选 OAuth。")],
    ["授权", "ChatGPT 打开授权页时输入网关的 owner 密码并点“授权”。之后在对话里选择 Agent Nexus 即可使用。"],
  ] },
  { id: "claude", label: "Claude.ai", steps: (url) => [
    ["添加自定义连接器", "Claude → 设置 → 连接器（Connectors）→ 添加自定义连接器。"],
    ["填写地址", el("span", {}, "名称 Agent Nexus，远程 MCP 服务器 URL 填 ", el("span", { class: "mono" }, url), "。")],
    ["连接并授权", "点“连接”，在授权页输入 owner 密码。对话里从“搜索和工具”启用它。"],
  ] },
  { id: "claude-code", label: "Claude Code", steps: (url) => [
    ["添加服务器", el("div", { class: "stack" }, "在终端运行：", el("pre", { class: "code-block" }, `claude mcp add --transport http agent-nexus ${url}`))],
    ["登录", "在 Claude Code 里输入 /mcp，选择 agent-nexus → Authenticate，浏览器里输入 owner 密码。"],
    ["只用静态令牌的网关", el("pre", { class: "code-block" }, `claude mcp add --transport http agent-nexus ${url} --header "Authorization: Bearer <网关令牌>"`)],
  ] },
  { id: "codex", label: "Codex", steps: (url) => [
    ["添加服务器", el("div", { class: "stack" }, "在终端运行：", el("pre", { class: "code-block" }, `codex mcp add agent-nexus --url ${url}`))],
    ["登录", el("div", { class: "stack" }, "然后运行下面的命令，在浏览器里输入 owner 密码：", el("pre", { class: "code-block" }, "codex mcp login agent-nexus"))],
    ["或者直接写配置", el("pre", { class: "code-block" }, `# ~/.codex/config.toml\n[mcp_servers.agent-nexus]\nurl = "${url}"`)],
  ] },
];

export async function settingsPage(ctx) {
  const { page } = ctx;
  mount(page, head("设置", "网关信息、AI 客户端的接入方法和本浏览器的外观。"), skeletonRows(3));
  let data;
  try {
    data = await fetchJSON("/api/settings", { quiet: true });
  } catch (error) {
    mount(page, head("设置", ""), errorState(error, () => settingsPage(ctx)));
    return;
  }
  const gateway = data.gateway;
  const guideBody = el("div");
  const tabs = el("div", { class: "segmented", role: "tablist" }, CLIENT_GUIDES.map((guide, index) =>
    el("button", { type: "button", "aria-pressed": String(index === 0), dataset: { guide: guide.id }, onclick: () => showGuide(guide.id) }, guide.label)));
  function showGuide(id) {
    for (const button of tabs.children) button.setAttribute("aria-pressed", String(button.dataset.guide === id));
    const guide = CLIENT_GUIDES.find((item) => item.id === id);
    mount(guideBody, el("div", { class: "steps" }, guide.steps(gateway.mcp_url).map(([title, desc]) =>
      el("div", { class: "step" }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc))))));
  }
  showGuide("chatgpt");
  const themeNow = (() => { try { return localStorage.getItem("anx-theme") || "system"; } catch { return "system"; } })();
  const theme = el("div", { class: "segmented" }, [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]].map(([value, text]) =>
    el("button", { type: "button", "aria-pressed": String(themeNow === value), onclick: (event) => {
      setTheme(value);
      for (const button of event.currentTarget.parentElement.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
    } }, text)));
  const hours = (s) => (s % 86400 === 0 ? `${s / 86400} 天` : `${Math.round(s / 3600)} 小时`);
  mount(page,
    head("设置", "网关信息、AI 客户端的接入方法和本浏览器的外观。"),
    el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "网关")),
      el("div", { class: "card-body stack lg" },
        el("div", { class: "field" }, el("span", { class: "field-label" }, "公网地址"), tokenField(gateway.public_url),
          gateway.public_url_configured ? null : el("span", { class: "error-text" }, "gateway.json 没有设置 public_url：这里显示的是浏览器访问的地址。")),
        el("div", { class: "field" }, el("span", { class: "field-label" }, "MCP 地址"), tokenField(gateway.mcp_url),
          el("span", { class: "hint" }, "所有 AI 客户端都添加这一个地址；工具 device_list / use_device / toolsearch 会把请求路由到各台设备。")),
        el("dl", { class: "kv" },
          el("dt", {}, "版本"), el("dd", { class: "mono" }, gateway.version),
          el("dt", {}, "登录方式"), el("dd", {}, data.auth.mode === "oauth" ? "OAuth（owner 密码）" : "静态令牌"),
          el("dt", {}, "状态目录"), el("dd", { class: "mono" }, gateway.state_dir || "—"),
          el("dt", {}, "数据库"), el("dd", {}, gateway.db_mb == null ? "—" : `${gateway.db_mb} MB `, gateway.db_warning ? pill("needs_config", "超过警戒") : null),
          el("dt", {}, "安装包目录"), el("dd", { class: "mono" }, data.downloads.dir)))),
    el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "在 AI 客户端中添加 Agent Nexus")),
      el("div", { class: "card-body stack lg" }, el("div", { class: "scroll-x" }, tabs), guideBody)),
    el("section", { class: "card flush" }, el("div", { class: "card-head" }, el("h2", {}, "已授权的客户端")),
      data.clients.length ? el("div", { class: "list inset" }, data.clients.map((client) => el("div", { class: "list-item" },
        el("span", { class: "avatar" }, icon(client.kind === "gpt" || client.kind === "claude" ? "message-square" : "key-round", { size: 16 })),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title truncate" }, client.client_name),
          el("span", { class: "sub mono truncate" }, client.client_id)),
        el("div", { class: "meta" }, client.created_at ? el("span", { class: "faint xs" }, `注册于 ${relTime(client.created_at)}`) : null)))) :
        el("div", { class: "card-body" }, el("p", { class: "muted small" }, data.auth.mode === "oauth" ? "还没有客户端通过 OAuth 授权。" : "静态令牌网关不记录客户端。"))),
    el("div", { class: "grid cols-2" },
      el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "邀请")),
        el("div", { class: "card-body" }, el("dl", { class: "kv" },
          el("dt", {}, "默认有效期"), el("dd", {}, hours(data.invites.default_ttl_s)),
          el("dt", {}, "最长有效期"), el("dd", {}, hours(data.invites.max_ttl_s))),
          el("p", { class: "hint", style: { marginTop: "8px" } }, "在 gateway.json 的 invites.default_ttl_s / max_ttl_s 修改。"))),
      el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "外观")),
        el("div", { class: "card-body stack" }, theme, el("span", { class: "hint" }, "只影响这个浏览器。")))));
}

// ---------------------------------------------------------------- plugins: 插件库 + per device

const REPO_FEATURE = "plugin_repo/1";
const REPO_REASON = { offline: "离线", revoked: "已吊销", old: "客户端版本太旧，先更新" };
/** Install progress, read from the device's job log (docs/ui-spec.md §3.3). */
const INSTALL_STEPS = [
  ["download", "下载插件包", "设备用自己的凭据从网关下载"],
  ["verify", "校验 sha256", "和插件库记录的摘要比对，不一致就拒绝"],
  ["setup", "安装依赖", "执行插件的安装步骤（uv sync / npm ci）"],
  ["start", "启动并检查健康", "通过健康检查才算装好；失败会自动回滚"],
];

function repoDeviceState(device) {
  if (device.revoked) return "revoked";
  if (!device.online) return "offline";
  return (device.features || []).includes(REPO_FEATURE) ? "ok" : "old";
}

function groupPackages(packages) {
  const groups = new Map();
  for (const item of packages) {
    if (!groups.has(item.name)) groups.set(item.name, []);
    groups.get(item.name).push(item);  // the API lists newest first within a name
  }
  return [...groups.values()];
}

/** 插件: the gateway's 插件库 (one-click install on a device) and what each device publishes now. */
export async function pluginsPage(ctx, route = { params: {}, query: new URLSearchParams() }) {
  const { page } = ctx;
  const state = { packages: null, devices: [], repoError: null };
  const repoHolder = el("section", { class: "card flush repo-card" }, el("div", { class: "card-body" }, skeletonRows(2)));
  const holder = el("div", {}, skeletonRows(3));
  const fileInput = el("input", { type: "file", accept: ".zip,application/zip", hidden: true, "aria-label": "选择插件包",
    onchange: () => { const file = fileInput.files[0]; fileInput.value = ""; uploadPackage(file); } });
  const upload = () => fileInput.click();
  const step = (title, desc) => el("div", { class: "step" }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc)));
  const howTo = el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "怎么把插件放进插件库")),
    el("div", { class: "card-body stack lg" }, el("div", { class: "steps" },
      step("在一台电脑上打包", "anx plugin pack <插件文件夹>：不带 .venv、node_modules、日志、凭据和数据，含疑似凭据的文件夹会被拒绝。"),
      step("上传到插件库", "在这里点“上传插件包”，或在已加入网关的设备上 anx plugin publish <文件夹>。"),
      step("安装到设备", "点“安装到…”选设备。设备从网关下载、校验后安装，需要账号的插件装好后在设备上填写。")),
    el("div", { class: "banner" }, icon("lock", { size: 18 }), el("div", { class: "banner-body" },
      el("div", { class: "banner-title" }, "插件包只保存在网关上"),
      el("div", { class: "muted" }, "不出现在下载页；只有你和已加入的设备能下载。凭据从不随包上传。"))),
    el("div", { class: "banner" }, icon("plug", { size: 18 }), el("div", { class: "banner-body" },
      el("div", { class: "banner-title" }, "远程 MCP 不用装在设备上"),
      el("div", { class: "muted" }, "阿里云、Cloudflare 等 HTTP / SSE 服务在", el("a", { href: "#/connectors" }, "连接器"), "页添加，运行在网关上。")))));
  mount(page, head("插件", "插件库里的私有插件保存在网关上，一键安装到任意在线设备；下面列出每台设备现在提供的插件。",
    [el("button", { class: "btn btn-primary", type: "button", onclick: upload }, icon("upload", { size: 16 }), "上传插件包")]),
  fileInput, el("div", { class: "grid side" }, el("div", { class: "stack lg" }, repoHolder, holder), howTo));

  // -- 插件库 ---------------------------------------------------------------------------------
  function renderRepo() {
    const groups = groupPackages(state.packages || []);
    const headNode = el("div", { class: "card-head" }, el("span", { class: "avatar" }, icon("package", { size: 16 })),
      el("h2", {}, "插件库"), state.packages ? el("span", { class: "tag" }, `${groups.length} 个插件`) : null,
      el("div", { class: "actions" }, el("button", { class: "btn btn-sm", type: "button", onclick: upload }, icon("upload", { size: 14 }), el("span", { class: "hide-sm" }, "上传"))));
    if (state.repoError && !state.packages) { mount(repoHolder, headNode, el("div", { class: "card-body" }, errorState(state.repoError, reload))); return; }
    if (!groups.length) {
      mount(repoHolder, headNode, el("div", { class: "card-body" }, emptyState({
        icon: "package", title: "插件库还是空的", compact: true,
        text: "在电脑上用 anx plugin pack 打包插件文件夹，再上传到这里；之后就能一键安装到任意设备。",
        action: { label: "上传插件包", icon: "upload", onClick: upload } })));
      return;
    }
    mount(repoHolder, headNode, el("div", { class: "list inset" }, groups.map(repoRow)));
  }

  function repoRow(versions) {
    const latest = versions[0];
    const ready = state.devices.filter((d) => repoDeviceState(d) === "ok").length;
    const remove = el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": `删除 ${latest.name} 的版本`, title: "删除版本",
      onclick: (event) => openMenu(event.currentTarget, versions.map((v) => ({ label: `删除 ${v.version}`, icon: "trash", danger: true, onClick: () => removePackage(v) }))) },
    icon("trash", { size: 16 }));
    return el("div", { class: "list-item wrap-sm repo-row", dataset: { package: latest.name } },
      el("span", { class: "avatar" }, String(latest.title || latest.name)[0].toUpperCase()),
      el("div", { class: "stack grow", style: { gap: "2px", minWidth: 0 } },
        el("div", { class: "row wrap", style: { gap: "6px" } }, el("span", { class: "title" }, latest.title || latest.name),
          latest.title !== latest.name ? el("span", { class: "tag mono hide-sm" }, latest.name) : null,
          el("span", { class: "tag mono" }, latest.version),
          versions.length > 1 ? el("span", { class: "tag" }, `${versions.length} 个版本`) : null),
        latest.description ? el("span", { class: "sub truncate" }, latest.description) : null,
        el("span", { class: "faint xs" }, `${bytes(latest.size)} · ${latest.files} 个文件 · 上传于 ${relTime(latest.uploaded_at)}`)),
      el("div", { class: "meta" }, remove,
        el("button", { class: "btn btn-sm btn-primary", type: "button", disabled: !state.devices.length,
          title: ready ? "选择设备安装" : "没有可安装的在线设备", onclick: () => openInstall(versions) },
        icon("download", { size: 14 }), "安装到…")));
  }

  async function uploadPackage(file, replace = false) {
    if (!file) return;
    const pending = toast(`正在上传 ${file.name}（${bytes(file.size)}）…`, { type: "info", timeout: 0 });
    let response, data = {};
    try {
      response = await fetch(`/api/plugin-repo${replace ? "?replace=1" : ""}`, { method: "POST", body: file, credentials: "same-origin",
        headers: { "X-ANX": "1", "Content-Type": "application/zip", Accept: "application/json" } });
      data = await response.json().catch(() => ({}));
    } catch {
      pending.dismiss();
      toast("网络不可用，上传失败", { type: "bad" });
      return;
    }
    pending.dismiss();
    if (response.status === 409 && data.code === "exists") {
      if (await confirmDialog({ title: "覆盖已有的版本？", message: `${data.error}。覆盖后，之后的安装都会用新上传的包。`, confirmLabel: "覆盖", danger: true })) {
        await uploadPackage(file, true);
      }
      return;
    }
    if (!response.ok) {
      modal({ title: "插件包被拒绝", size: "sm", body: el("div", { class: "stack" },
        el("p", { class: "muted" }, data.error || `上传失败（HTTP ${response.status}）`),
        (data.findings || []).length ? el("div", { class: "list inset" }, data.findings.slice(0, 12).map((f) => el("div", { class: "list-item" },
          icon("triangle-alert", { size: 14 }), el("span", { class: "mono small truncate grow" }, f.path), el("span", { class: "faint xs" }, f.rule)))) : null),
      actions: [{ label: "知道了", variant: "primary" }] });
      return;
    }
    const item = data.package;
    toast(`已${item.replaced ? "覆盖" : "上传"} ${item.title || item.name} ${item.version}`, { title: "插件库" });
    await reload();
  }

  async function removePackage(item) {
    if (!await confirmDialog({ title: `删除 ${item.title || item.name} ${item.version}？`, message: "只删除网关上的插件包；已经装到设备上的插件不受影响。", confirmLabel: "删除", danger: true })) return;
    try {
      await fetchJSON(`/api/plugin-repo/${encodeURIComponent(item.name)}/${encodeURIComponent(item.version)}`, { method: "DELETE" });
      toast(`已删除 ${item.name} ${item.version}`);
    } catch { /* toasted */ }
    await reload();
  }

  // -- 安装到… ------------------------------------------------------------------------------
  function openInstall(versions, preferred = route.params?.device) {
    const latest = versions[0];
    const devices = state.devices.filter((d) => !d.revoked)
      .sort((a, b) => Number(repoDeviceState(a) !== "ok") - Number(repoDeviceState(b) !== "ok") || String(a.display_name).localeCompare(String(b.display_name)));
    const firstOk = devices.find((d) => d.device_id === preferred && repoDeviceState(d) === "ok") || devices.find((d) => repoDeviceState(d) === "ok");
    const version = el("select", { class: "select mono", id: "repo-version", "aria-label": "版本" },
      versions.map((v) => el("option", { value: v.version }, `${v.version}${v === latest ? "（最新）" : ""} · ${bytes(v.size)}`)));
    const choices = el("div", { class: "choices device-choices", role: "radiogroup", "aria-label": "目标设备" }, devices.length ? devices.map((device) => {
      const s = repoDeviceState(device);
      const input = el("input", { type: "radio", name: "repo-device", value: device.device_id, disabled: s !== "ok", checked: device === firstOk });
      return el("label", { class: `choice ${s !== "ok" ? "disabled" : ""}`, dataset: { device: device.device_id } }, input,
        icon(deviceIcon(device), { size: 16 }),
        el("div", { class: "stack", style: { gap: 0, minWidth: 0 } }, el("span", { class: "choice-title truncate" }, device.display_name),
          el("span", { class: "choice-sub" }, s === "ok" ? `在线${device.version ? ` · v${device.version}` : ""}` : REPO_REASON[s])));
    }) : el("p", { class: "muted small" }, "还没有设备加入网关。"));
    const steps = el("div", { class: "steps install-steps", hidden: true }, INSTALL_STEPS.map(([id, title, desc]) =>
      el("div", { class: "step", dataset: { step: id } }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc)))));
    const outcome = el("div");
    const logBox = el("pre", { class: "log install-log", hidden: true, "data-empty": "等待设备开始安装…" });
    const picker = el("div", { class: "form" },
      el("div", { class: "field" }, el("label", { for: "repo-version" }, "版本"), version),
      el("div", { class: "field" }, el("label", {}, "安装到"), choices,
        devices.some((d) => repoDeviceState(d) === "old") ? el("span", { class: "hint" }, "灰色的设备运行的是旧版 Agent Nexus，更新后才能从插件库安装。") : null));
    let finished = false;
    const dialog = modal({
      title: `安装 ${latest.title || latest.name}`, description: "设备用自己的凭据从网关下载插件包，校验 sha256 后安装；失败会自动回滚。",
      body: el("div", { class: "stack lg" }, picker, steps, outcome, logBox),
      actions: [{ label: "关闭" }, { label: "安装", variant: "primary", icon: "download", onClick: submit }],
    });
    dialog.root.classList.add("repo-install");

    function markSteps(lines, done) {
      const text = lines.join("\n");
      const reached = done ? 4 : text.includes("等待健康检查") ? 3 : /\n\$ |^\$ |没有安装步骤/.test(text) ? 2 : text.includes("已下载") ? 2 : text.includes("下载") ? 0 : -1;
      steps.querySelectorAll(".step").forEach((node, index) => {
        node.classList.toggle("done", index < reached || (done && index <= 3));
        node.classList.toggle("current", !done && index === reached);
      });
    }

    async function submit({ root }) {
      if (finished) return true;
      const target = choices.querySelector("input:checked");
      if (!target) { mount(outcome, banner("bad", "先选择一台在线设备", "")); return false; }
      let started;
      try {
        started = await fetchJSON(`/api/devices/${encodeURIComponent(target.value)}/plugins/install`, { method: "POST", body: { name: latest.name, version: version.value }, quiet: true });
      } catch (error) {
        mount(outcome, banner("bad", "没有开始安装", error.message));
        return false;
      }
      picker.hidden = true;
      steps.hidden = false;
      logBox.hidden = false;
      mount(outcome, el("div", { class: "row small" }, el("span", { class: "spinner" }), `正在 ${started.display_name} 上安装 ${started.name} ${started.version}…`));
      const lines = [];
      let since = 0, job = null, misses = 0;
      while (root.isConnected) {
        try {
          job = await fetchJSON(`/api/devices/${encodeURIComponent(started.device_id)}/plugins/jobs/${encodeURIComponent(started.job_id)}?since=${since}`, { quiet: true });
          misses = 0;
        } catch (error) {
          if (++misses >= 8) { mount(outcome, banner("bad", "看不到安装进度", error.message)); finished = true; break; }
          await new Promise((r) => setTimeout(r, 1500));
          continue;
        }
        if (job.log.length) { lines.push(...job.log); logBox.textContent = lines.join("\n"); logBox.scrollTop = logBox.scrollHeight; }
        since = job.log_total;
        markSteps(lines, job.state === "done");
        if (job.state !== "running") break;
        await new Promise((r) => setTimeout(r, 700));
      }
      finished = true;
      if (job && job.state === "done") {
        const r = job.result || {};
        const text = { running: "已安装并在运行", needs_config: "已安装，需要在设备上填写配置", conflict: "已安装，但端口被占用：在设备上换一个端口",
          disabled: "已安装（未启用）", installed: "已安装" }[r.state] || "已安装";
        mount(outcome, banner(r.state === "running" || r.state === "disabled" || r.state === "installed" ? "ok" : "warn",
          `${r.name} ${r.version} ${text}`, r.state === "needs_config" ? "在那台设备的客户端“插件”页打开它，填写账号或令牌后会自动启动。" : started.display_name));
        toast(`${started.display_name}：${r.name} ${text}`, { type: r.state === "running" ? "ok" : "warn" });
      } else if (job) {
        steps.querySelectorAll(".step.current").forEach((node) => node.classList.replace("current", "failed"));
        mount(outcome, banner("bad", "安装失败，已回滚", String(job.error || "").split("\n")[0]));
      }
      setTimeout(() => {
        const button = root.querySelector(".modal-foot .btn-primary");
        if (button) button.replaceChildren(icon("check", { size: 16 }), "完成");
      });
      reload();
      return false;
    }
    return dialog;
  }

  function banner(tone, title, text) {
    const glyph = { ok: "circle-check", warn: "triangle-alert", bad: "circle-alert" }[tone] || "info";
    return el("div", { class: `banner ${tone}` }, icon(glyph, { size: 18 }), el("div", { class: "banner-body" },
      el("div", { class: "banner-title" }, title), text ? el("div", { class: "muted" }, text) : null));
  }

  // -- devices ------------------------------------------------------------------------------
  function deviceCard(device) {
    const plugins = device.plugins || [];
    let body;
    if (device.revoked) body = el("p", { class: "muted small" }, "这台设备已被吊销。");
    else if (!device.online) body = el("p", { class: "muted small" }, "设备离线，上线后显示它的插件。");
    else if (!plugins.length) body = el("p", { class: "muted small" }, "还没有启用插件。从插件库安装，或在这台设备的客户端“插件”页打开开关。");
    return el("section", { class: "card", dataset: { device: device.device_id } },
      el("div", { class: "card-head" },
        deviceAvatar(device),
        el("h2", {}, device.display_name),
        pill(deviceState(device)),
        el("div", { class: "actions" }, el("span", { class: "faint small" }, device.online ? `${plugins.length} 个插件` : ""))),
      body ? el("div", { class: "card-body" }, body) :
        el("div", { class: "list inset" }, plugins.map((plugin) => el("div", { class: "list-item", dataset: { plugin: plugin.name } },
          el("span", { class: "avatar sm" }, (plugin.name[0] || "?").toUpperCase()),
          el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } }, el("span", { class: "title mono" }, plugin.name),
            plugin.description ? el("span", { class: "sub truncate" }, plugin.description) : null),
          el("div", { class: "meta" }, plugin.error ? pill("error", "不可用") : el("span", { class: "faint small" }, `${plugin.tools} 个工具`))))));
  }

  async function reload() {
    const [devices, repo] = await Promise.allSettled([fetchJSON("/api/devices", { quiet: true }), fetchJSON("/api/plugin-repo", { quiet: true })]);
    if (repo.status === "fulfilled") { state.packages = repo.value.packages || []; state.repoError = null; } else state.repoError = repo.reason;
    if (devices.status === "fulfilled") state.devices = devices.value.devices || [];
    renderRepo();
    if (devices.status === "rejected") { mount(holder, errorState(devices.reason, reload)); return; }
    const list = [...state.devices];
    if (!list.length) {
      mount(holder, el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({
        icon: "puzzle", title: "还没有设备", text: "插件运行在设备上。先在电脑上安装 Agent Nexus，再用邀请码加入网关。",
        action: { label: "下载客户端", icon: "download", href: "#/downloads" }, secondary: { label: "邀请新设备", href: "#/devices?invite=1" } }))));
      return;
    }
    list.sort((a, b) => Number(b.online) - Number(a.online) || String(a.display_name).localeCompare(String(b.display_name)));
    mount(holder, el("div", { class: "stack lg" }, list.map(deviceCard)));
  }
  await reload();
  if (route.query?.get?.("install")) {
    const versions = groupPackages(state.packages || []).find((group) => group[0].name === route.query.get("install"));
    if (versions) openInstall(versions);
  }
  ctx.every(15000, () => { if (!document.querySelector(".repo-install")) reload(); });
}
