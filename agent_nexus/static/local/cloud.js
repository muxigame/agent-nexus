// 本机 · 连接云端 (docs/ui-spec.md §4.3, design §11.5): join a gateway with an invite, or show the link.
import { el, mount, icon, fetchJSON, toast, confirmDialog, emptyState, pill, relTime } from "/ui/ui.js";
import { decodeInvite, hubHost } from "/ui/invite.js";

export function cloudPage(ctx, route) {
const shell = { setStatus: (state, label) => ctx.shell.setStatus(state, label), page: ctx.page };
let prefill = route.query.get("invite") || "";
let current = null;
let busy = false;

const STATE_TEXT = { connected: "已连接", connecting: "连接中", error: "连接错误", revoked: "已被吊销", not_joined: "未加入", disabled: "未加入" };

function statusLine(info) {
  if (info.state === "connected") shell.setStatus("online", `已连接 ${hubHost(info.hub)}`);
  else if (info.state === "not_joined") shell.setStatus("offline", "仅本机 · 未加入网关");
  else if (info.state === "revoked") shell.setStatus("revoked", "已被吊销");
  else if (info.state === "error") shell.setStatus("error", "连接错误");
  else shell.setStatus("connecting", `正在连接 ${hubHost(info.hub)}`);
}

function stepsCard() {
  const step = (title, desc) => el("div", { class: "step" }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc)));
  return el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "邀请码从哪里来？")),
    el("div", { class: "card-body" }, el("div", { class: "steps" },
      step("打开云端控制台", "在浏览器访问你的网关地址，用 owner 密码登录。"),
      step("设备 → 邀请新设备", "生成一次性的邀请码，默认 24 小时有效。"),
      step("粘贴到左边", el("span", {}, "或直接点击 ", el("span", { class: "mono" }, "agentnexus://"), " 链接，会自动填入。")))));
}

function nextCard(info) {
  const step = (title, desc) => el("div", { class: "step" }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc)));
  return el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "接下来")),
    el("div", { class: "card-body" }, el("div", { class: "steps" },
      step(el("a", { href: "#/plugins" }, "启用本机插件"), "插件页里打开开关；需要配置的会提示你填写。"),
      step("在 AI 客户端添加网关", el("span", {}, "本机的 CLI 已自动接入",
        el("a", { href: "#/settings" }, "本机 MCP"), "；其他电脑上的 AI 客户端（以及网页版 ChatGPT、Claude）再按网关设置添加，",
        info.hub ? el("a", { href: `${info.hub}/#/settings`, target: "_blank", rel: "noopener" }, "在云端设置页查看") : "见云端设置页", "。")),
      step(el("a", { href: "#/inbox" }, "查看收件箱"), "Agent 发给你的邮件和提问会出现在这里。"))));
}

function joinForm(info) {
  const textarea = el("textarea", { class: "textarea mono", id: "invite", rows: 4, spellcheck: "false", placeholder: "anx1.… 或 agentnexus://enroll?invite=…" });
  textarea.value = prefill;
  const preview = el("div");
  const error = el("span", { class: "error-text", role: "alert", hidden: true });
  const agentId = el("input", { class: "input mono", id: "agent-id", placeholder: "自动生成（按主机名）", maxlength: "100" });
  const join = el("button", { class: "btn btn-primary btn-lg", type: "submit" }, icon("link", { size: 18 }), "加入网关");
  const render = () => {
    const decoded = decodeInvite(textarea.value);
    error.hidden = true;
    textarea.removeAttribute("aria-invalid");
    if (!textarea.value.trim()) { mount(preview); return; }
    if (!decoded) {
      mount(preview, el("div", { class: "banner warn" }, icon("triangle-alert", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, "这不像是邀请码"), el("div", { class: "muted" }, "邀请码以 anx1. 开头，或是一个 agentnexus://enroll 链接。"))));
      return;
    }
    mount(preview, el("div", { class: "banner" }, icon("globe", { size: 18 }), el("div", { class: "banner-body" },
      el("div", { class: "banner-title" }, `将加入 ${hubHost(decoded.hub)}`),
      el("div", { class: "muted" }, "本机显示为 ", el("span", { class: "mono" }, info.hostname || "本机主机名"), "，加入后可以在云端控制台设置备注名。"))));
  };
  textarea.addEventListener("input", render);
  const form = el("form", { class: "form", onsubmit: async (event) => {
    event.preventDefault();
    if (busy) return;
    if (!decodeInvite(textarea.value)) { error.textContent = "请粘贴有效的邀请码"; error.hidden = false; textarea.setAttribute("aria-invalid", "true"); return; }
    busy = true;
    join.disabled = true;
    mount(join, el("span", { class: "spinner" }), "正在加入…");
    try {
      const result = await fetchJSON("/api/cloud/join", { method: "POST", quiet: true, body: { invite: textarea.value.trim(), agent_id: agentId.value.trim() || null } });
      prefill = "";
      toast(`已加入 ${hubHost(result.hub)}，设备 ID：${result.agent_id}`);
      history.replaceState(null, "", "#/cloud");
      render_(result);
    } catch (err) {
      const messages = { expired: "邀请已过期，请在云端控制台重新生成", used: "这个邀请已经使用过了，请生成一个新的", revoked: "这个邀请已被作废",
        hub_unreachable: "无法连接网关：检查网络，或确认邀请里的地址可以访问", invalid_invite: "邀请码无效", already_joined: "这台设备已经加入了网关" };
      error.textContent = messages[err.data?.code] || err.message;
      error.hidden = false;
      textarea.setAttribute("aria-invalid", "true");
    } finally {
      busy = false;
      join.disabled = false;
      mount(join, icon("link", { size: 18 }), "加入网关");
    }
  } },
    el("div", { class: "field" }, el("label", { for: "invite" }, "邀请码或邀请链接"), textarea, error,
      el("span", { class: "hint" }, "邀请码自带网关地址，只能使用一次。")),
    preview,
    el("div", { class: "field" }, el("label", { for: "agent-id" }, "设备 ID ", el("span", { class: "optional" }, "可选")), agentId),
    el("div", { class: "row wrap", style: { gap: "8px" } }, join,
      el("a", { class: "btn btn-ghost btn-lg", href: "#/" }, "暂时仅本机使用")));
  render();
  return form;
}

function joinedCard(info) {
  const leave = el("button", { class: "btn btn-danger", type: "button", onclick: async () => {
    if (!await confirmDialog({ title: "退出网关？", danger: true, confirmLabel: "退出",
      message: "会删除本机的设备凭据并断开连接。网关控制台里的设备记录保留，之后可以用新邀请重新加入。" })) return;
    try { await fetchJSON("/api/cloud/leave", { method: "POST" }); toast("已退出网关"); } catch { /* toasted */ }
    load();
  } }, icon("log-out", { size: 16 }), "退出网关");
  const since = info.state === "connected" && info.since ? relTime(info.since).replace("前", "") : "—";
  return el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, "云端连接"), pill(info.state === "connected" ? "online" : info.state === "error" ? "error" : "connecting", STATE_TEXT[info.state] || info.state)),
    el("div", { class: "card-body stack lg" },
      info.state === "error" ? el("div", { class: "banner bad" }, icon("circle-alert", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, "连接不上网关"), el("div", { class: "muted" }, `原因：${info.error || "未知"}。客户端会自动重试。`))) : null,
      el("dl", { class: "kv" },
        el("dt", {}, "网关"), el("dd", {}, info.hub ? el("a", { href: info.hub, target: "_blank", rel: "noopener" }, hubHost(info.hub)) : "—"),
        el("dt", {}, "设备 ID"), el("dd", { class: "mono" }, info.agent_id || "—"),
        el("dt", {}, "显示名"), el("dd", {}, info.display_name || info.hostname || "—", " ",
          info.hub ? el("a", { class: "small", href: `${info.hub}/#/devices`, target: "_blank", rel: "noopener" }, "在云端控制台修改备注名") : null),
        el("dt", {}, "已连接"), el("dd", {}, since)),
      (info.features || []).length ? el("details", { class: "advanced" }, el("summary", {}, "网关特性"),
        el("div", { class: "row wrap", style: { marginTop: "8px" } }, info.features.map((f) => el("span", { class: "tag mono" }, f)))) : null,
      el("div", { class: "row wrap" },
        info.hub ? el("a", { class: "btn btn-primary", href: info.hub, target: "_blank", rel: "noopener" }, icon("external-link", { size: 16 }), "打开云端控制台") : null,
        info.managed ? el("span", { class: "faint small" }, "由私有入网配置管理") : leave)));
}

function render_(info) {
  current = info;
  statusLine(info);
  const page = shell.page;
  if (info.state === "not_joined" || info.state === "revoked") {
    mount(page,
      el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "加入你的 Agent Nexus"),
        el("p", {}, "把这台电脑接入网关后，ChatGPT、Claude 和其他设备上的 Agent 就能使用这里的插件，并互相收发邮件。"))),
      info.state === "revoked" ? el("div", { class: "banner bad" }, icon("circle-x", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, "此设备已被吊销"), el("div", { class: "muted" }, "网关不再接受这台设备的凭据。可以用新的邀请码重新加入。"))) : null,
      el("div", { class: "grid side" }, el("section", { class: "card" }, el("div", { class: "card-body stack xl", style: { paddingTop: "24px" } }, joinForm(info))), stepsCard()));
    return;
  }
  mount(page,
    el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "连接云端"),
      el("p", {}, "这台电脑已经加入网关。它的插件会出现在 ChatGPT、Claude 等客户端的 device_list 和 toolsearch 里。"))),
    el("div", { class: "grid side" }, joinedCard(info), nextCard(info)));
}

async function load() {
  if (busy) return;
  try {
    const info = await fetchJSON("/api/cloud", { quiet: true });
    // Don't throw away what the user is typing in the join form.
    if (current && current.state === info.state && (info.state === "not_joined" || info.state === "revoked")) { statusLine(info); return; }
    render_(info);
  } catch (error) {
    mount(shell.page, emptyState({ icon: "circle-alert", title: "读取云端连接失败", text: error.message, action: { label: "重试", onClick: load } }));
  }
}
load();
ctx.every(4000, load);
}
