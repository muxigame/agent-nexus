// 本机 · 概览 (docs/ui-spec.md §4.1, docs/agent-chat.md §16.1): the 对话 card (recent sessions → #/chat), cloud
// link, plugins, app, recent mail. The 10 s poll redraws the head and the summary; the card reads every 30 s.
import { el, mount, icon, fetchJSON, toast, pill, relTime, errorState, supervisorText } from "/ui/ui.js";
import { hubHost } from "/ui/invite.js";
import { chatCard } from "/ui/chat-page.js";
import { localAgentApi } from "/ui/agent-wire.js";

const JOIN_HINT_KEY = "anx-local-join-hint-hidden";
const PROBLEM = new Set(["needs_config", "crashed", "conflict", "invalid", "unhealthy"]);

/** The Overview's head: on a phone only its primary action stays. */
function overviewHead(node) { node.classList.add("overview-head"); return node; }

function head(title, text, actions = []) {
  return el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, title), text ? el("p", {}, text) : null),
    actions.length ? el("div", { class: "actions" }, actions) : null);
}

export async function overviewPage(ctx) {
  const { page } = ctx;
  const headHolder = el("div", { style: { display: "contents" } }, overviewHead(head("这台电脑", "正在读取…")));
  const summaryHolder = el("div", { style: { display: "contents" } }, el("div", { class: "grid cols-4" },
    Array.from({ length: 4 }, () => el("div", { class: "card stat" }, el("span", { class: "skeleton w-40" }), el("span", { class: "skeleton title w-60" })))));
  mount(page, headHolder, summaryHolder);
  // Created once (in process for 本机, so it works with the cloud disconnected); each redraw re-places it.
  const chat = chatCard({ api: localAgentApi(), local: true });

  async function load() {
    let services, plugins, cloud, inbox = null, autostart = { enabled: null };
    try {
      [services, { plugins }, cloud] = await Promise.all([
        fetchJSON("/api/services", { quiet: true }), fetchJSON("/api/plugins", { quiet: true }), fetchJSON("/api/cloud", { quiet: true })]);
    } catch (error) {
      mount(headHolder, overviewHead(head("这台电脑", "")));
      mount(summaryHolder, errorState(error, load));
      return;
    }
    if (cloud.state === "connected") { try { inbox = await fetchJSON("/api/inbox/threads", { quiet: true }); } catch { inbox = null; } }
    try { autostart = await fetchJSON("/api/app/autostart", { quiet: true }); } catch { /* not the desktop app */ }
    // No redirect to 连接云端 on a first visit: the chat with this computer's CLIs works without a gateway, so
    // the Overview stays, with a dismissible line about what joining adds.
    chat.refresh();
    render(services.manager, plugins, cloud, inbox, autostart);
  }

  function render(manager, plugins, cloud, inbox, autostart) {
    const installed = plugins.filter((p) => p.installed);
    const running = installed.filter((p) => p.status === "running");
    const disabled = installed.filter((p) => p.status === "disabled");
    const problems = installed.filter((p) => PROBLEM.has(p.status));
    const missing = plugins.filter((p) => !p.installed);
    const update = manager.app_update || {};
    const joined = cloud.state !== "not_joined";
    const sentence = `${running.length} 个插件在运行` + (problems.length ? `，${problems.length} 个需要处理` : "") +
      (joined ? (cloud.state === "connected" ? `，已连接 ${hubHost(cloud.hub)}。` : "，正在连接网关。") : "，还没有加入网关。");
    const stat = (href, glyph, label, value, unit, meta) => el("a", { class: "card stat interactive", href },
      el("span", { class: "stat-label" }, icon(glyph, { size: 14 }), label),
      el("span", { class: "stat-value" }, String(value), unit ? el("span", { class: "unit" }, unit) : null),
      el("span", { class: "stat-meta" }, meta));
    const unread = inbox ? (inbox.unread || 0) : 0;
    const asks = inbox ? (inbox.open_asks || []).length : 0;

    const cloudCard = el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, "云端连接"),
        pill(cloud.state === "connected" ? "online" : cloud.state === "not_joined" ? "not_joined" : cloud.state === "revoked" ? "revoked" : cloud.state === "error" ? "error" : "connecting",
          { connected: "已连接", not_joined: "未加入", revoked: "已被吊销", error: "连接错误" }[cloud.state] || "连接中")),
      el("div", { class: "card-body stack" },
        joined ? el("dl", { class: "kv" },
          el("dt", {}, "网关"), el("dd", {}, cloud.hub ? hubHost(cloud.hub) : "—"),
          el("dt", {}, "设备 ID"), el("dd", { class: "mono" }, cloud.agent_id || "—"),
          el("dt", {}, "显示名"), el("dd", {}, cloud.display_name || cloud.hostname || "—"))
          : el("p", { class: "muted small" }, "加入网关后，ChatGPT、Claude 和其他设备上的 Agent 就能用这台电脑的插件，并给你发邮件。"),
        el("div", { class: "row wrap" }, joined
          ? el("a", { class: "btn btn-sm", href: "#/cloud" }, "查看连接", icon("chevron-right", { size: 14 }))
          : el("a", { class: "btn btn-primary btn-sm", href: "#/cloud" }, icon("link", { size: 14 }), "加入网关"))));

    const appCard = el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, "应用"), el("span", { class: "tag mono" }, `v${manager.anx}`)),
      el("div", { class: "card-body stack" },
        update.available ? el("div", { class: "banner accent" }, icon("arrow-up-right", { size: 18 }), el("div", { class: "banner-body" },
          el("div", { class: "banner-title" }, `Agent Nexus v${update.version} 已可用`), el("div", { class: "muted" }, `当前 v${update.current}`)),
          el("div", { class: "banner-actions" }, el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: async () => {
            try { await fetchJSON("/api/app/update/apply", { method: "POST" }); toast("正在安装更新，完成后会自动重启"); } catch { /* toasted */ }
          } }, `更新到 v${update.version}`))) : el("p", { class: "muted small" }, update.checked_at ? `已是最新版本 · 上次检查 ${relTime(update.checked_at)}` : "已是最新版本"),
        autostart.enabled === null || autostart.enabled === undefined ? null : el("label", { class: "switch" },
          el("input", { type: "checkbox", role: "switch", checked: autostart.enabled, disabled: Boolean(autostart.system),
            title: autostart.system ? "核心是系统服务：开机即运行，界面在登录后自动出现" : "", onchange: async (event) => {
            const box = event.currentTarget;
            try {
              const result = await fetchJSON("/api/app/autostart", { method: "PUT", body: { enabled: box.checked } });
              box.checked = Boolean(result.enabled);
              toast(result.enabled ? "已开启开机自启" : "已关闭开机自启");
            } catch { box.checked = !box.checked; }
          } }), el("span"), "开机自动启动"),
        el("a", { class: "btn btn-ghost btn-sm", href: "#/settings", style: { alignSelf: "flex-start" } }, "设置", icon("chevron-right", { size: 14 }))));

    const pluginList = installed.slice(0, 6);
    const pluginsCard = el("section", { class: "card flush" },
      el("div", { class: "card-head" }, el("h2", {}, "插件"),
        el("div", { class: "actions" }, el("a", { class: "btn btn-ghost btn-sm", href: "#/plugins" }, "全部", icon("chevron-right", { size: 14 })))),
      pluginList.length ? el("div", { class: "list inset" }, pluginList.map((p) => el("a", { class: "list-item clickable", href: `#/plugins/${encodeURIComponent(p.name)}` },
        el("span", { class: "avatar" }, p.icon_url ? el("img", { src: p.icon_url, alt: "" }) : (p.title || p.name)[0].toUpperCase()),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title truncate" }, p.title || p.name),
          el("span", { class: "sub truncate" }, supervisorText(p.message) || p.description || "")),
        el("div", { class: "meta" }, pill(p.status))))) : null,
      missing.length ? el("div", { class: "card-body" }, el("div", { class: "banner accent" }, icon("package", { size: 18 }),
        el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, `${missing.length} 个插件还没有安装`),
          el("div", { class: "muted" }, missing.map((p) => p.title || p.name).join("、"))),
        el("div", { class: "banner-actions" }, el("a", { class: "btn btn-primary btn-sm", href: "#/plugins?open=install" }, "安装插件")))) : null);

    const threads = inbox ? (inbox.threads || []).slice(0, 3) : [];
    const inboxCard = el("section", { class: "card flush" },
      el("div", { class: "card-head" }, el("h2", {}, "收件箱"),
        el("div", { class: "actions" }, el("a", { class: "btn btn-ghost btn-sm", href: "#/inbox" }, "打开", icon("chevron-right", { size: 14 })))),
      threads.length ? el("div", { class: "list inset" }, threads.map((thread) => el("a", { class: "list-item clickable", href: `#/inbox/${encodeURIComponent(thread.thread_id)}` },
        el("span", { class: `avatar sm ${thread.open_asks ? "accent" : ""}` }, thread.open_asks ? "?" : String(thread.last_from || "?")[0].toUpperCase()),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title truncate" }, thread.subject || "（无主题）"),
          el("span", { class: "sub truncate" }, `${thread.last_from || ""} · ${thread.open_asks ? "等待你的回答" : thread.last_snippet || ""}`)),
        el("div", { class: "meta" }, el("span", { class: "faint xs" }, relTime(thread.last_at)))))) :
        el("div", { class: "card-body" }, el("p", { class: "muted small" }, inbox ? "还没有邮件。" : "收件箱需要连接网关。")));

    mount(headHolder, overviewHead(
      head("这台电脑", sentence, [
        el("a", { class: "btn", href: "#/chat" }, icon("message-square", { size: 16 }), "对话"),
        el("a", { class: "btn", href: "#/agent" }, icon("sliders-horizontal", { size: 16 }), "Agent 设置"),
        el("a", { class: "btn", href: "#/cloud" }, icon("cloud", { size: 16 }), "连接云端"),
        el("a", { class: "btn btn-primary", href: "#/plugins?open=install" }, icon("plus", { size: 16 }), "安装插件")])));
    let joinHidden = false;
    try { joinHidden = localStorage.getItem(JOIN_HINT_KEY) === "1"; } catch { /* storage unavailable */ }
    const joinHint = !joined && !joinHidden ? el("div", { class: "banner accent overview-join" }, icon("cloud", { size: 18 }),
      el("div", { class: "banner-body" }, el("div", {}, "加入网关后，还能在其他设备上运行 Agent、收发邮件，ChatGPT 和 Claude 也能用这台电脑的插件。")),
      el("div", { class: "banner-actions" },
        el("a", { class: "btn btn-sm btn-primary", href: "#/cloud" }, icon("link", { size: 14 }), "连接云端"),
        el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "不再提示", title: "不再提示",
          onclick: (event) => { try { localStorage.setItem(JOIN_HINT_KEY, "1"); } catch { /* storage unavailable */ } event.currentTarget.closest(".banner")?.remove(); } },
          icon("x", { size: 14 })))) : null;
    mount(summaryHolder,
      joinHint,
      problems.length ? el("div", { class: "banner warn" }, icon("triangle-alert", { size: 18 }), el("div", { class: "banner-body" },
        el("div", { class: "banner-title" }, `${problems.length} 个插件需要处理`),
        el("div", { class: "muted" }, problems.map((p) => `${p.title || p.name}：${supervisorText(p.message) || p.status}`).join("；"))),
        el("div", { class: "banner-actions" }, el("a", { class: "btn btn-sm", href: "#/plugins?tab=attention" }, "查看"))) : null,
      el("div", { class: "grid cols-4" },
        stat("#/plugins", "puzzle", "运行中的插件", running.length, `/ ${installed.length}`, disabled.length ? `${disabled.length} 个已停用` : "全部已启用"),
        stat("#/plugins?tab=attention", "triangle-alert", "需要处理", problems.length, "", problems.length ? problems.map((p) => p.title || p.name).slice(0, 3).join("、") : "一切正常"),
        stat("#/inbox", "inbox", "未读邮件", unread, "", inbox ? "Agent 发给 me 的" : "需要连接网关"),
        stat("#/inbox", "circle-question-mark", "待回答", asks, "", asks ? "Agent 在等你的回答" : "没有待回答的问题")),
      el("div", { class: "grid side" },
        el("div", { class: "stack lg" }, chat.node, pluginsCard, inboxCard),
        el("div", { class: "stack lg" }, cloudCard, appCard)));
  }

  await load();
  ctx.every(10000, load);
}
