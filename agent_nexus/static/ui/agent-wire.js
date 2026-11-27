// Wiring of the agent chat into the two consoles (docs/agent-chat.md §9, §11): the transport adapters
// ui/chat.js, ui/prompts.js and ui/agent-admin.js take, the "在终端登录" flow, and the sidebar page "Agent".
//
//   cloudAgentApi()  — the gateway console: /api/devices/{id}/agents/{op}; devices from /api/devices.
//   localAgentApi()  — the local console: 本机 → /api/agents/local/{op} (in process, works with the cloud
//                      disconnected); other devices → /api/agents/remote/{id}/{op} through the gateway.
//   loginHandler({local}) → onLogin(kind, device, command, info) for mountChat / mountCliManager.
//   agentPage(ctx, route, {api, local}) — tabs 提示词 / CLI / 临时目录 (+ agent-admin.js AGENT_PAGE_TABS, e.g. API)
//                      for one runner device. The chat itself is the 对话 page (ui/chat-page.js, §16.1).

import { el, mount, icon, fetchJSON, modal, copy, emptyState } from "./ui.js";
import { mountPromptEditor } from "./prompts.js";
import * as admin from "./agent-admin.js";
import { queueTerminalInput } from "./terminal.js";

const enc = encodeURIComponent;

/** POST an op; transport failures (4xx/5xx, network) come back as {error, code} like a handler's own error. */
async function post(path, params) {
  try {
    // keepCache: the chat polls every few seconds; a POST must not throw away the cached device list
    return await fetchJSON(path, { method: "POST", body: params || {}, quiet: true, keepCache: true });
  } catch (error) {
    const data = error?.data;
    if (data && typeof data === "object" && data.error) return { error: String(data.error), code: data.code || `http_${error.status || 0}` };
    return { error: error?.message || "请求失败", code: error?.status ? `http_${error.status}` : "network" };
  }
}

function inboxCalls() {
  return {
    conversation: (address) => fetchJSON(`/api/inbox/conversation?address=${enc(address)}`, { quiet: true }),
    async answerAsk(id, answer) {
      try {
        return await fetchJSON(`/api/inbox/asks/${enc(id)}/answer`, { method: "POST", body: answer, quiet: true });
      } catch (error) {
        return { error: error?.message || "回答没有送达", code: error?.status ? `http_${error.status}` : "network" };
      }
    },
  };
}

/** Why a device of the gateway's list cannot run the chat ("" = it can). */
function deviceHint(device, locked) {
  if (device.revoked) return "已吊销";
  if (!device.online) return "离线";
  if (!device.agents) return "客户端过旧或未开放 Agent";
  if (locked) return "已锁定";
  return "";
}

export function cloudAgentApi() {
  return {
    call: (op, params, device) => (device
      ? post(`/api/devices/${enc(device)}/agents/${enc(op)}`, params)
      : Promise.resolve({ error: "先选择一台设备", code: "no_device" })),
    async devices() {
      const list = await fetchJSON("/api/devices", { quiet: true });
      return (list.devices || []).filter((d) => !d.revoked).map((d) => {
        const hint = deviceHint(d, false);
        return { id: d.device_id, name: d.display_name || d.device_id, online: !hint, hint, os: d.os_family || "" };
      });
    },
    ...inboxCalls(),
  };
}

export function localAgentApi() {
  let note = "";
  return {
    call: (op, params, device) => post(device ? `/api/agents/remote/${enc(device)}/${enc(op)}` : `/api/agents/local/${enc(op)}`, params),
    async devices() {
      const self = { id: null, name: "本机", online: true, self: true };
      note = "";
      let cloud = null;
      try { cloud = await fetchJSON("/api/cloud", { quiet: true }); } catch { cloud = null; }
      if (!cloud || cloud.state !== "connected") {
        note = !cloud ? "" : cloud.state === "not_joined" ? "加入网关后，也能在其他设备上运行 Agent" : "云端未连接：暂时只能用本机";
        if (cloud?.display_name || cloud?.hostname) self.name = cloud.display_name || cloud.hostname;
        return [self];
      }
      let list;
      try { list = await fetchJSON("/api/devices", { quiet: true }); } catch { note = "读不到网关的设备列表：暂时只能用本机"; return [self]; }
      const locked = !!list.lock?.locked;
      if (locked) note = "这台设备已锁定，解锁后才能用其他设备";
      const devices = list.devices || [];
      const me = devices.find((d) => d.self);
      if (me) self.name = me.display_name || self.name;
      const others = devices.filter((d) => !d.self && !d.revoked).map((d) => {
        const hint = deviceHint(d, locked);
        return { id: d.device_id, name: d.display_name || d.device_id, online: !hint, hint };
      });
      return [self, ...others];
    },
    devicesNote: () => note,
    ...inboxCalls(),
  };
}

// ---------------------------------------------------------------- 在终端登录

// What each CLI's sign-in types after the program (registry login_command is the bare default; codex signs in
// with a device code: the browser sign-in would open on that computer's screen and call back to its localhost).
const LOGIN_ARGS = { claude: ["auth", "login"], codex: ["login", "--device-auth"], gemini: [], dsh: ["web"] };
const LOGIN_NOTE = {
  claude: "按终端里的提示完成登录：打开它给出的网址，用你的 Claude 账号授权。",
  codex: "用设备码登录：在你自己的浏览器打开终端里显示的网址并输入验证码（需先在 ChatGPT 安全设置里开启“设备码登录”）。",
  gemini: "首次运行会引导登录：选择用 Google 账号登录，或者设置 GEMINI_API_KEY。",
  dsh: "dsh web 会在那台电脑上打开配置页，填好模型凭据后回来点“重新检测”。",
};

/** program + args for kind; the full path when the CLI was found outside PATH (e.g. the dsh desktop app). */
function loginPlan(kind, command, info) {
  const words = String(command || "").trim().split(/\s+/).filter(Boolean);
  const path = typeof info?.path === "string" && /[\\/]/.test(info.path) ? info.path : "";
  const bare = words[0] || kind;
  const args = LOGIN_ARGS[kind] || words.slice(1);
  const quote = (value) => (/\s/.test(value) ? `"${value}"` : value);
  // typed into the device's terminal: the full path (the dsh desktop app's launcher is on no PATH);
  // shown to a person: the short command, with the full path as the fallback
  return { program: path || bare, args, text: [bare, ...args].join(" "), full: path ? [quote(path), ...args].join(" ") : "" };
}

function commandBlock(text) {
  return el("div", { class: "row", style: { gap: "8px", alignItems: "stretch" } },
    el("code", { class: "mono grow", style: { padding: "8px 10px", borderRadius: "var(--radius-sm)", background: "var(--surface-2)", overflowX: "auto", whiteSpace: "nowrap" } }, text),
    el("button", { class: "btn btn-sm", type: "button", onclick: () => copy(text) }, icon("copy", { size: 14 }), "复制"));
}

/**
 * onLogin for the chat and the CLI manager. ``local``: the local console (device null = 本机, signed in at
 * this computer's own terminal); otherwise the device's 终端 page opens and the command is typed there.
 */
export function loginHandler({ local = false } = {}) {
  return (kind, device, command, info) => {
    const plan = loginPlan(kind, command, info);
    const name = kindLabel(kind, info);
    const openTerminal = () => {
      queueTerminalInput(device, { program: plan.program, args: plan.args, note: LOGIN_NOTE[kind] || "" });
      location.hash = `#/terminal/${enc(device)}`;
    };
    const here = local && !device;
    if (kind !== "dsh" && !here) { openTerminal(); return; }
    const body = el("div", { class: "stack" });
    const append = (...nodes) => body.append(...nodes.filter(Boolean));
    if (kind === "dsh") {
      append(
        el("p", { class: "muted" }, "DeepSeek Harness 需要模型凭据。两种方式任选一种："),
        el("div", { class: "strong" }, "方式一：在配置页里填写"),
        el("p", { class: "muted small" }, here ? "在这台电脑的终端里运行下面的命令，会在浏览器里打开 dsh 的配置页。"
          : "在那台设备的终端里运行下面的命令，会在那台电脑的浏览器里打开 dsh 的配置页（需要有人在那台电脑前）。"),
        commandBlock(plan.text),
        plan.full ? el("p", { class: "faint xs" }, "提示找不到命令时，用完整路径：") : null,
        plan.full ? commandBlock(plan.full) : null,
        el("div", { class: "strong" }, "方式二：设置环境变量"),
        el("p", { class: "muted small" }, "给运行 Agent 的执行账户设置 DEEPSEEK_API_KEY（或 OPENROUTER_API_KEY），重启 Agent Nexus 后生效。"),
        commandBlock("DEEPSEEK_API_KEY=sk-…"),
        el("p", { class: "faint xs" }, "配置好以后，在 Agent 菜单里点“重新检测”。"));
    } else {
      append(
        el("p", { class: "muted" }, `在这台电脑上打开一个终端（PowerShell、终端.app 等），运行：`),
        commandBlock(plan.text),
        plan.full ? el("p", { class: "faint xs" }, "提示找不到命令时，用完整路径：") : null,
        plan.full ? commandBlock(plan.full) : null,
        el("p", { class: "muted small" }, LOGIN_NOTE[kind] || ""),
        el("p", { class: "faint xs" }, "登录好以后，在 Agent 菜单里点“重新检测”。"));
    }
    modal({
      title: `登录 ${name}`, size: "sm", body,
      actions: [
        { label: "关闭" },
        here ? { label: "复制命令", variant: "primary", onClick: () => { copy(plan.text); return true; } }
          : { label: "在终端运行", variant: "primary", onClick: () => { openTerminal(); return true; } },
      ],
    });
  };
}

// ---------------------------------------------------------------- the 「Agent」 page

const { mountTmpManager, mountCliManager, kindLabel, xicon } = admin;
// Tabs other modules add ({id, label, mount(container, api, {device, local})}, e.g. API §16.5); read through the
// namespace so a build without them still loads.
const EXTRA_TABS = Array.isArray(admin.AGENT_PAGE_TABS) ? admin.AGENT_PAGE_TABS.filter((t) => t && t.id && typeof t.mount === "function") : [];
const TABS = [["prompts", "提示词"], ["cli", "CLI"], ["tmp", "临时目录"], ...EXTRA_TABS.map((t) => [t.id, t.label || t.id])];
const PAGE_KEY = "anx.agentpage.";

function remember(key, value) { try { localStorage.setItem(PAGE_KEY + key, JSON.stringify(value)); } catch { /* private mode */ } }
function recall(key, fallback) { try { const raw = localStorage.getItem(PAGE_KEY + key); return raw ? JSON.parse(raw) : fallback; } catch { return fallback; } }

/**
 * #/agent?tab=prompts|cli|tmp&device=<id>. The device picker sits above the tabs: prompts, CLIs and temp
 * folders all live on the runner device (本机 = this console's device in the local console).
 */
export async function agentPage(ctx, { query } = {}, { api, local = false } = {}) {
  const { page } = ctx;
  let tab = TABS.some(([id]) => id === query?.get?.("tab")) ? query.get("tab") : recall("tab", "prompts");
  const asked = query?.get?.("device");
  let device = asked !== null && asked !== undefined ? (asked || null) : recall(local ? "device.local" : "device.cloud", local ? null : undefined);
  let devices = [];
  let current = null;   // the mounted component ({destroy})
  ctx.onLeave(() => current?.destroy?.());

  const select = el("select", { class: "select", "aria-label": "运行设备", style: { minWidth: "180px" },
    onchange: () => { device = select.value === "" ? null : select.value; show(); } });
  const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": "Agent 设置" });
  const note = el("p", { class: "faint xs", hidden: true });
  const body = el("div", { class: "aa-page-body" });
  mount(page,
    el("div", { class: "page-head" },
      el("div", { class: "titles" }, el("h1", {}, "Agent"),
        el("p", {}, `系统提示词片段、各设备上的 CLI（安装与登录）、对话的临时目录${EXTRA_TABS.some((t) => t.id === "api") ? "、API 模型" : ""}。对话本身在“对话”页。`), note),
      el("div", { class: "actions", style: { alignItems: "center" } },
        el("label", { class: "row", style: { gap: "8px", flexWrap: "nowrap" } }, el("span", { class: "muted small hide-sm", style: { whiteSpace: "nowrap" } }, "运行设备"), select),
        el("a", { class: "btn", href: "#/chat" }, xicon("bot", { size: 16 }), "去对话"))),
    tabs, body);

  function renderTabs() {
    mount(tabs, TABS.map(([id, label]) => el("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(tab === id),
      onclick: () => { if (tab !== id) { tab = id; show(); } } }, label)));
  }

  function bound() {
    const id = device;
    return { ...api, call: (op, params) => api.call(op, params, id) };
  }

  function show() {
    remember("tab", tab);
    if (device !== undefined) remember(local ? "device.local" : "device.cloud", device);
    const params = new URLSearchParams({ tab });
    if (device) params.set("device", device);
    history.replaceState(null, "", `#/agent?${params}`);
    renderTabs();
    current?.destroy?.();
    current = null;
    const holder = el("section", { class: "card" }, el("div", { class: "card-body" }));
    mount(body, holder);
    const inner = holder.firstElementChild;
    if (device === undefined) {
      mount(inner, emptyState({ icon: "monitor", title: "还没有能运行 Agent 的设备", compact: true,
        text: "设备需要在线，且客户端版本支持 Agent 对话。", action: { label: "查看设备", icon: "monitor", href: "#/devices" } }));
      return;
    }
    if (tab === "prompts") {
      holder.classList.add("flush");
      current = mountPromptEditor(inner, bound(), {});
    } else if (tab === "cli") {
      current = mountCliManager(inner, api, { device, onLogin: loginHandler({ local }) });
    } else if (EXTRA_TABS.some((t) => t.id === tab)) {
      current = EXTRA_TABS.find((t) => t.id === tab).mount(inner, api, { device, local }) || null;
    } else {
      current = mountTmpManager(inner, api, { device });
    }
  }

  renderTabs();
  try { devices = (await api.devices()) || []; } catch { devices = local ? [{ id: null, name: "本机", online: true, self: true }] : []; }
  const usable = devices.filter((d) => d.online !== false);
  if (device !== undefined && !usable.some((d) => d.id === device)) device = usable[0] ? usable[0].id : (local ? null : undefined);
  if (device === undefined && usable[0]) device = usable[0].id;
  mount(select, devices.length ? devices.map((d) => el("option", { value: d.id ?? "", selected: d.id === device, disabled: d.online === false },
    d.self ? `本机${d.name && d.name !== "本机" ? `（${d.name}）` : ""}` : `${d.name}${d.hint ? `（${d.hint}）` : ""}`))
    : el("option", { value: "" }, "没有可用的设备"));
  select.disabled = !usable.length;
  const text = typeof api.devicesNote === "function" ? api.devicesNote() : "";
  note.hidden = !text;
  note.textContent = text;
  show();
}

