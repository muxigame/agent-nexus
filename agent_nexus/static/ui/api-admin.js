// Agent 设置 → API (docs/agent-chat.md §16.4, §16.5): model providers the runner device calls directly.
//   import { mountApiAdmin } from "/ui/api-admin.js";
//   mountApiAdmin(container, api, {device, local}) -> {refresh, destroy}
//
// `api` is the adapter chat.js / the CLI tab take ({call(op, params, device)}); every op goes through callOp(),
// so the local console reaches 本机 in process and other devices through the gateway, like the CLI tab.
//   agent_api_providers {}                        -> {providers: [{id, label, protocol, base_url, headers?, models, enabled,
//                                                    key: {set, last4?, env?}, source?}]}
//   agent_api_provider_save {provider}            -> {provider?}   (never carries a key)
//   agent_api_provider_delete {provider_id}
//   agent_api_key_set {provider_id, key|null}     (write-only; null clears it; "env:NAME" references a variable)
//   agent_api_test {provider_id, model}           -> {ok, latency_ms?, error?}
//   agent_api_models {provider_id}                -> {models: [{id, context_window?, owned_by?}]}   (openai protocol)
// The cloud console also offers 「同步到所有设备」: the non-secret provider + model config goes to the gateway
// (hub op api_config_put); devices pull it and merge it under their own config. Keys never leave the device.

import { el, icon, mount, toast, modal, confirmDialog, skeletonRows, errorState, emptyState, fetchJSON } from "./ui.js";
import { callOp } from "./agent-admin.js";

// ---------------------------------------------------------------- presets and pure helpers (tests/test_api_admin_ui.cjs)

export const PROTOCOLS = {
  openai: { label: "OpenAI 兼容", short: "OpenAI", mark: "OA", hint: "POST {base_url}/chat/completions" },
  anthropic: { label: "Anthropic Messages", short: "Anthropic", mark: "A", hint: "POST {base_url}/v1/messages" },
  gemini: { label: "Gemini", short: "Gemini", mark: "G", hint: "POST {base_url}/v1beta/models/{model}:streamGenerateContent" },
};

/** The dialog's templates: picking one fills protocol + base_url (and a suggested id / name). */
export const PRESETS = [
  { id: "openai", label: "OpenAI", protocol: "openai", base_url: "https://api.openai.com/v1", key_hint: "sk-…" },
  { id: "deepseek", label: "DeepSeek", protocol: "openai", base_url: "https://api.deepseek.com/v1", key_hint: "sk-…" },
  { id: "qwen", label: "通义", protocol: "openai", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", key_hint: "sk-…（DashScope）" },
  { id: "kimi", label: "Kimi", protocol: "openai", base_url: "https://api.moonshot.cn/v1", key_hint: "sk-…（Moonshot）" },
  { id: "openrouter", label: "OpenRouter", protocol: "openai", base_url: "https://openrouter.ai/api/v1", key_hint: "sk-or-…" },
  { id: "ollama", label: "Ollama", protocol: "openai", base_url: "http://localhost:11434/v1", key_hint: "本机 Ollama 不需要密钥", key_optional: true },
  { id: "anthropic", label: "Anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com", key_hint: "sk-ant-…" },
  { id: "gemini", label: "Gemini", protocol: "gemini", base_url: "https://generativelanguage.googleapis.com", key_hint: "AIza…" },
  { id: "custom", label: "自定义 OpenAI 兼容 / NewAPI", protocol: "openai", base_url: "", key_hint: "中转站给的令牌", custom: true },
];

const MODEL_PLACEHOLDER = { openai: "例如 gpt-4o-mini、deepseek-chat", anthropic: "例如 claude-sonnet-4-5", gemini: "例如 gemini-2.5-flash" };
const PROVIDER_ID = /^[a-z0-9][a-z0-9_-]{0,31}$/;
// header names that would carry a secret: keys go to the write-only key field, never into the synced config
// (the same idea as the device's check, agents/api/config.py: any *auth*, X-Auth, X-Custom-Auth too)
const SECRET_HEADER = /auth|api[-_]?key|token|secret|passw|cookie|credential|(^|[-_])key$/i;
const PROVIDER_FIELDS = ["id", "label", "protocol", "base_url", "headers", "models", "enabled"];
const MODEL_FIELDS = ["id", "label", "context_window", "max_output", "tools", "enabled"];

/** Guess the preset of a saved provider (by base_url host, then id); the custom preset when nothing matches. */
export function presetFor(provider) {
  const host = hostOf(provider?.base_url);
  const exact = PRESETS.find((p) => !p.custom && p.protocol === provider?.protocol && host && hostOf(p.base_url) === host);
  return exact || PRESETS.find((p) => p.id === provider?.id && p.protocol === provider?.protocol) || PRESETS.at(-1);
}

export function hostOf(url) {
  try { return new URL(String(url || "")).host; } catch { return ""; }
}

/** Key status from whatever shape the device answers ({key: {set, last4, env, origin_changed}} or flat key_set /
 * key_last4), with the provider's `login` ("ok" without a key: Ollama, LAN endpoints need none). */
export function keyStatus(provider) {
  const key = provider?.key && typeof provider.key === "object" ? provider.key : {};
  const set = !!(key.set ?? provider?.key_set ?? provider?.has_key);
  const last4 = String(key.last4 ?? provider?.key_last4 ?? "").replace(/[^A-Za-z0-9]/g, "").slice(-4);
  const env = String(key.env ?? provider?.key_env ?? "");
  return { set, last4, env, login: provider?.login || "", moved: !!key.origin_changed };
}

/** "● 已设置 ****1a2b" / "● 环境变量 NAME" / "● 不需要密钥" / "○ 地址变了，需重新输入" / "○ 未设置" (the list's key column). */
export function keyLabel(status) {
  if (!status.set && status.login === "ok") return "● 不需要密钥";
  if (status.moved) return "○ 地址变了，需重新输入";
  if (!status.set) return "○ 未设置";
  if (status.env) return `● 环境变量 ${status.env}`;
  return status.last4 ? `● 已设置 ****${status.last4}` : "● 已设置";
}

/** The key column's tone: set (ok), not needed (neutral), missing. */
export function keyTone(status) {
  return status.set ? "set" : status.login === "ok" ? "keyless" : "";
}

/** agent_api_providers answers {providers: [...]} or a bare list; always a list of normalized providers. */
export function normalizeProviders(result) {
  const list = Array.isArray(result) ? result : Array.isArray(result?.providers) ? result.providers : [];
  return list.filter((p) => p && typeof p === "object" && p.id).map((p) => ({
    ...p,
    label: p.label || p.id,
    protocol: PROTOCOLS[p.protocol] ? p.protocol : "openai",
    base_url: p.base_url || "",
    enabled: p.enabled !== false,
    headers: p.headers && typeof p.headers === "object" && !Array.isArray(p.headers) ? { ...p.headers } : {},
    models: (Array.isArray(p.models) ? p.models : []).filter((m) => m && m.id).map((m) => ({ ...m, enabled: m.enabled !== false, tools: m.tools !== false })),
  }));
}

/** "128K" / "1m" / "200000" / "1,000,000" → tokens; "" → null; nonsense → NaN. */
export function parseTokens(text) {
  const value = String(text ?? "").trim().replace(/[,_\s]/g, "");
  if (!value) return null;
  const match = value.match(/^(\d+(?:\.\d+)?)([kKmM]?)$/);
  if (!match) return NaN;
  const n = Number(match[1]) * ({ k: 1e3, m: 1e6 }[match[2].toLowerCase()] || 1);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : NaN;
}

/** 128000 → "128K", 1000000 → "1M", 32768 → "32768". */
export function formatTokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "";
  if (v % 1e6 === 0) return `${v / 1e6}M`;
  if (v % 1e3 === 0) return `${v / 1e3}K`;
  return String(v);
}

/** A free provider id from a preset or label: "deepseek", then "deepseek-2", … */
export function slugId(seed, taken = []) {
  const base = String(seed || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 28) || "provider";
  const used = new Set(taken);
  if (!used.has(base)) return base;
  for (let i = 2; ; i++) if (!used.has(`${base}-${i}`)) return `${base}-${i}`;
}

/** Only the non-secret fields of a provider (what agent_api_provider_save and the gateway sync send). */
export function providerPayload(provider) {
  const out = {};
  for (const field of PROVIDER_FIELDS) if (provider[field] !== undefined) out[field] = provider[field];
  out.headers = Object.fromEntries(Object.entries(provider.headers || {}).filter(([name, value]) => name && !SECRET_HEADER.test(name) && value !== undefined)
    .map(([name, value]) => [name, String(value)]));
  if (!Object.keys(out.headers).length) delete out.headers;
  out.models = (provider.models || []).map((m) => {
    const model = {};
    for (const field of MODEL_FIELDS) if (m[field] !== undefined && m[field] !== null && m[field] !== "") model[field] = m[field];
    return model;
  });
  return out;
}

/**
 * The gateway sync body: api_config_put replaces the gateway's whole list, so it is a merge -- this device's local
 * providers, plus the gateway's (other devices' syncs) that this device does not override by id. Non-secret only.
 */
export function syncPayload(providers) {
  const local = providers.filter((p) => p.source !== "gateway");
  const ids = new Set(local.map((p) => p.id));
  const kept = providers.filter((p) => p.source === "gateway" && !ids.has(p.id) && (ids.add(p.id), true));
  return { providers: [...local, ...kept].map(providerPayload) };
}

/** Problems with a draft before saving ({field: text}; empty = fine). */
export function validateProvider(draft, { taken = [], isNew = false } = {}) {
  const errors = {};
  if (!PROVIDER_ID.test(draft.id || "")) errors.id = "小写字母、数字、- 或 _，最多 32 个字符";
  else if (isNew && taken.includes(draft.id)) errors.id = "已经有这个 ID 的服务商";
  if (!String(draft.label || "").trim()) errors.label = "填一个名称";
  if (!PROTOCOLS[draft.protocol]) errors.protocol = "选择协议";
  if (!/^https?:\/\/[^\s/]+/i.test(draft.base_url || "")) errors.base_url = "以 http:// 或 https:// 开头的地址";
  const ids = new Set();
  for (const m of draft.models || []) {
    if (!String(m.id || "").trim()) { errors.models = "有模型没有填 ID"; break; }
    if (ids.has(m.id)) { errors.models = `模型 ${m.id} 重复了`; break; }
    ids.add(m.id);
    if (m.context_window !== undefined && m.context_window !== null && !(Number(m.context_window) > 0)) { errors.models = `模型 ${m.id} 的上下文窗口不是有效的数字`; break; }
  }
  for (const name of Object.keys(draft.headers || {})) {
    if (SECRET_HEADER.test(name)) { errors.headers = `${name} 像是密钥：请填在「API 密钥」里，它不会被同步`; break; }
  }
  return errors;
}

/**
 * Save a provider, then apply the key change: keyChange undefined = leave the key alone, null = clear it,
 * a string = set it. The key goes only to agent_api_key_set, never into the provider payload.
 */
export async function saveProvider(api, device, provider, keyChange) {
  const result = await callOp(api, "agent_api_provider_save", { provider: providerPayload(provider) }, device);
  if (keyChange !== undefined) await callOp(api, "agent_api_key_set", { provider_id: provider.id, key: keyChange }, device);
  return result;
}

/** Path of the gateway op api_config_put for the cloud console (an adapter may bring its own apiConfigPut). */
export const GATEWAY_API_CONFIG_PUT = "/api/agents/api_config_put";

/** 同步到所有设备: put the non-secret config on the gateway. Returns {error, code} instead of throwing. */
export async function syncToGateway(api, providers) {
  const body = syncPayload(providers);
  if (typeof api?.apiConfigPut === "function") return (await api.apiConfigPut(body)) ?? {};
  try {
    return await fetchJSON(GATEWAY_API_CONFIG_PUT, { method: "POST", body, quiet: true, keepCache: true });
  } catch (error) {
    const data = error?.data;
    if (data && typeof data === "object" && data.error) return { error: String(data.error), code: data.code || `http_${error.status || 0}` };
    return { error: error?.message || "同步失败", code: error?.status ? `http_${error.status}` : "network" };
  }
}

/** The model a 测试 uses: the first enabled one, else the first. */
export function testModel(provider) {
  const models = provider?.models || [];
  return (models.find((m) => m.enabled !== false) || models[0])?.id || "";
}

function latencyText(result) {
  const ms = Number(result.latency_ms ?? result.latency ?? result.ms);
  return Number.isFinite(ms) ? `${Math.round(ms)} ms` : "";
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

// ---------------------------------------------------------------- the tab

/** mountApiAdmin(container, api, {device, local}) — the provider list of one runner device. */
export function mountApiAdmin(container, api, { device = null, local = false } = {}) {
  let alive = true;
  let providers = [];
  const tests = new Map();   // provider id -> {state: running|ok|bad, text}
  const root = el("div", { class: "aapi" });
  mount(container, root);

  async function load({ quiet = false } = {}) {
    if (!quiet) mount(root, skeletonRows(3));
    try {
      providers = normalizeProviders(await callOp(api, "agent_api_providers", {}, device));
    } catch (error) {
      if (alive) mount(root, errorState(error, () => load()));
      return;
    }
    if (alive) render();
  }

  async function runTest(provider) {
    const model = testModel(provider);
    if (!model) { toast("先给这个服务商添加一个模型", { type: "warn" }); return; }
    tests.set(provider.id, { state: "running", text: `正在用 ${model} 测试…` });
    render();
    let next;
    try {
      const result = await callOp(api, "agent_api_test", { provider_id: provider.id, model }, device);
      next = result.ok === false
        ? { state: "bad", text: result.message || "测试没有通过" }
        : { state: "ok", text: [latencyText(result), model].filter(Boolean).join(" · ") };
    } catch (error) {
      next = { state: "bad", text: error.message };
    }
    if (!alive) return;
    tests.set(provider.id, next);
    render();
  }

  async function toggle(provider, on) {
    try {
      await saveProvider(api, device, { ...provider, enabled: on });
      provider.enabled = on;
      toast(on ? `已启用 ${provider.label}` : `已停用 ${provider.label}`);
    } catch (error) {
      toast(error.message, { type: "bad", title: "保存失败" });
    }
    if (alive) render();
  }

  async function remove(provider) {
    const ok = await confirmDialog({ title: `删除 ${provider.label}？`, message: "服务商配置和这台设备上保存的密钥都会删除；用它的历史会话还在，但不能再续接。", confirmLabel: "删除", danger: true });
    if (!ok) return;
    try {
      await callOp(api, "agent_api_provider_delete", { provider_id: provider.id }, device);
      toast(`已删除 ${provider.label}`);
    } catch (error) {
      toast(error.message, { type: "bad", title: "删除失败" });
    }
    if (alive) load({ quiet: true });
  }

  async function sync(button) {
    // the gateway's current list first (another device may have synced since the last pull): it is merged, not lost
    let current = providers;
    button.disabled = true;
    try { current = normalizeProviders(await callOp(api, "agent_api_providers", { refresh: true }, device)); } catch { /* the last list */ }
    button.disabled = false;
    const mine = current.filter((p) => p.source !== "gateway");
    if (!mine.length) { toast("这台设备上没有可同步的服务商", { type: "info" }); return; }
    const kept = current.filter((p) => p.source === "gateway").length;
    const message = `把这台设备上的 ${mine.length} 个服务商和它们的模型放到网关（网关上同 ID 的会被这台设备的替换）`
      + (kept ? `，网关上其他设备同步的 ${kept} 个保留` : "")
      + "。其他设备拉取后合并（各设备自己的配置优先）。密钥不会同步，每台设备要单独设置。";
    const ok = await confirmDialog({ title: "同步到所有设备？", message, confirmLabel: "同步" });
    if (!ok) return;
    button.disabled = true;
    const result = await syncToGateway(api, current);
    button.disabled = false;
    if (result?.error) toast(result.error, { type: "bad", title: "同步失败" });
    else toast(`已同步 ${mine.length} 个服务商到网关`, { title: "同步完成" });
  }

  function providerRow(provider) {
    const status = keyStatus(provider);
    const preset = presetFor(provider);
    const enabledModels = provider.models.filter((m) => m.enabled).length;
    const test = tests.get(provider.id);
    const sw = el("input", { type: "checkbox", role: "switch", checked: provider.enabled, "aria-label": `启用 ${provider.label}`,
      onchange: (event) => toggle(provider, event.currentTarget.checked) });
    return el("div", { class: `aapi-row${provider.enabled ? "" : " off"}`, role: "listitem", dataset: { id: provider.id } },
      el("span", { class: "aapi-mark", dataset: { protocol: provider.protocol }, "aria-hidden": "true" }, PROTOCOLS[provider.protocol].mark),
      el("div", { class: "aapi-main" },
        el("div", { class: "aapi-title" },
          el("span", { class: "strong truncate" }, provider.label),
          el("span", { class: "aapi-tag" }, PROTOCOLS[provider.protocol].short),
          provider.source === "gateway" ? el("span", { class: "aapi-tag", title: "来自网关的同步配置，本机没有单独修改" }, "网关同步") : null,
          !preset.custom && preset.label !== provider.label ? el("span", { class: "aapi-tag faint" }, preset.label) : null),
        el("div", { class: "aapi-url mono truncate", title: provider.base_url }, provider.base_url || "—"),
        el("div", { class: "aapi-facts" },
          el("span", { class: `aapi-key ${keyTone(status)}`, title: status.set ? "密钥只保存在这台设备上，不会显示"
            : status.login === "ok" ? "这个地址在本机或局域网，不需要密钥" : status.moved ? "密钥是为原来的地址设置的，不会发给新地址" : "还没有在这台设备上设置密钥" }, keyLabel(status)),
          el("span", { class: "faint" }, provider.models.length ? `${provider.models.length} 个模型${enabledModels !== provider.models.length ? `（${enabledModels} 个启用）` : ""}` : "还没有模型"),
          test ? el("span", { class: `aapi-test ${test.state}`, role: "status" },
            test.state === "running" ? el("span", { class: "spinner" }) : icon(test.state === "ok" ? "circle-check" : "circle-alert", { size: 13 }), test.text) : null)),
      el("div", { class: "aapi-actions" },
        el("label", { class: "switch", title: provider.enabled ? "已启用：模型出现在 Agent 选择器里" : "已停用" }, sw, el("span")),
        el("button", { class: "btn btn-sm", type: "button", disabled: test?.state === "running", onclick: () => runTest(provider) }, icon("zap", { size: 14 }), "测试"),
        el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => openEditor(provider) }, icon("pencil", { size: 14 }), "编辑"),
        el("button", { class: "btn btn-sm btn-ghost btn-icon", type: "button", "aria-label": `删除 ${provider.label}`, title: "删除", onclick: () => remove(provider) }, icon("trash", { size: 14 }))));
  }

  function render() {
    const add = el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => openEditor(null) }, icon("plus", { size: 14 }), "新增服务商");
    const syncButton = local ? null : el("button", { class: "btn btn-sm", type: "button", title: "把服务商和模型（不含密钥）放到网关，所有设备都能用",
      onclick: (event) => sync(event.currentTarget) }, icon("cloud", { size: 14 }), "同步到所有设备");
    const head = el("div", { class: "aapi-head" },
      el("div", { class: "aapi-head-text" },
        el("span", { class: "strong" }, "API 服务商"),
        el("span", { class: "faint xs" }, "启用的模型会作为「API 模型」出现在对话的 Agent 菜单里。")),
      el("div", { class: "row" },
        el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "刷新", title: "刷新", onclick: () => load() }, icon("refresh-cw", { size: 14 })),
        syncButton, add));
    const note = el("p", { class: "faint xs aapi-note" }, icon("lock", { size: 12 }),
      "请求从运行设备直接发给服务商，不经过网关；密钥只保存在这台设备上，界面只显示末 4 位。", local ? "" : "「同步到所有设备」只同步服务商和模型。");
    if (!providers.length) {
      mount(root, head, emptyState({ icon: "key-round", title: "还没有 API 服务商", compact: true,
        text: "添加 OpenAI、DeepSeek、通义、Kimi、OpenRouter、Ollama、Anthropic、Gemini 或任意 OpenAI 兼容的中转（NewAPI / OneAPI），就能不装 CLI 直接对话。",
        action: { label: "新增服务商", icon: "plus", onClick: () => openEditor(null) } }), note);
      return;
    }
    mount(root, head, el("div", { class: "list aapi-list", role: "list" }, providers.map(providerRow)), note);
  }

  function openEditor(existing) {
    const editor = providerEditor(api, device, existing, providers, {
      onSaved: () => { if (existing) tests.delete(existing.id); if (alive) load({ quiet: true }); },
    });
    return editor;
  }

  load();
  return { refresh: () => load({ quiet: true }), destroy() { alive = false; } };
}

// ---------------------------------------------------------------- add / edit dialog

function field(label, control, { hint = null, optional = false, error = null } = {}) {
  return el("div", { class: "field" },
    el("label", {}, label, optional ? el("span", { class: "optional" }, "（可选）") : null), control,
    typeof hint === "string" ? (hint ? el("span", { class: "hint" }, hint) : null) : hint, error);
}

function errorSlot() { return el("span", { class: "error-text", hidden: true }); }
function setError(slot, input, text) {
  slot.hidden = !text;
  slot.textContent = text || "";
  if (input) input.setAttribute("aria-invalid", text ? "true" : "false");
}

/** The 新增 / 编辑 dialog. Returns the modal handle. */
export function providerEditor(api, device, existing, providers, { onSaved = null } = {}) {
  const isNew = !existing;
  const taken = providers.map((p) => p.id);
  const draft = existing ? clone({ ...existing, key: undefined }) : { id: "", label: "", protocol: "openai", base_url: "", headers: {}, models: [], enabled: true };
  delete draft.key;
  const status = existing ? keyStatus(existing) : { set: false, last4: "", env: "" };
  let preset = existing ? presetFor(existing) : null;
  let keyChange;           // undefined = unchanged; null = clear; string = new key (kept only in the input)
  let idTouched = false;
  let saved = !isNew;      // 测试 / 拉取模型 need the provider on the device first

  // -- basic fields
  const presets = el("div", { class: "aapi-presets", role: "radiogroup", "aria-label": "预设模板" });
  const idInput = el("input", { class: "input mono", value: draft.id, disabled: !isNew, spellcheck: "false", autocomplete: "off", placeholder: "deepseek",
    oninput: () => { idTouched = true; draft.id = idInput.value.trim(); } });
  const labelInput = el("input", { class: "input", value: draft.label, placeholder: "DeepSeek", autocomplete: "off",
    oninput: () => { draft.label = labelInput.value; if (isNew && !idTouched) { draft.id = slugId(preset && !preset.custom ? preset.id : labelInput.value, taken); idInput.value = draft.id; } } });
  const protocolSelect = el("select", { class: "select", onchange: () => { draft.protocol = protocolSelect.value; syncProtocol(); } },
    Object.entries(PROTOCOLS).map(([id, p]) => el("option", { value: id, selected: draft.protocol === id }, p.label)));
  const urlInput = el("input", { class: "input mono", value: draft.base_url, placeholder: "https://api.example.com/v1", spellcheck: "false", autocomplete: "off",
    oninput: () => { draft.base_url = urlInput.value.trim(); } });
  const protocolHint = el("span", { class: "hint mono" });
  const errs = { id: errorSlot(), label: errorSlot(), protocol: errorSlot(), base_url: errorSlot(), models: errorSlot(), headers: errorSlot() };

  // -- key (write-only: the input starts empty and its value is never read back from the device)
  const keyInput = el("input", { class: "input mono", type: "password", autocomplete: "new-password", spellcheck: "false", "data-lpignore": "true",
    placeholder: status.set ? "已设置；留空则不修改" : "粘贴 API 密钥",
    oninput: () => { keyChange = keyInput.value.trim() || undefined; renderKeyState(); } });
  const keyState = el("div", { class: "aapi-keystate" });
  function renderKeyState() {
    let text, cls = "";
    if (keyChange === null) { text = "保存后清除这台设备上的密钥"; cls = "warn"; }
    else if (typeof keyChange === "string") { text = "保存后写入新密钥（只保存在这台设备上）"; cls = "ok"; }
    else text = keyLabel(status);
    const clear = status.set && keyChange !== null
      ? el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { keyChange = null; keyInput.value = ""; renderKeyState(); } }, "清除密钥")
      : keyChange === null ? el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { keyChange = undefined; renderKeyState(); } }, "撤销") : null;
    mount(keyState, el("span", { class: `aapi-key ${keyChange === undefined ? keyTone(status) : ""} ${cls}` }, text), clear);
  }
  const keyHint = el("span", { class: "hint" });

  // -- headers (non-secret extras, e.g. HTTP-Referer for OpenRouter)
  const headerList = Object.entries(draft.headers || {});   // [name, value] rows while editing
  const commitHeaders = () => { draft.headers = Object.fromEntries(headerList.filter(([name]) => name.trim()).map(([name, value]) => [name.trim(), value])); };
  const headerRows = el("div", { class: "stack aapi-headers" });
  function renderHeaders() {
    mount(headerRows, headerList.map((row, index) => el("div", { class: "header-row" },
      el("input", { class: "input mono", value: row[0], placeholder: "X-Title", "aria-label": "请求头名称", spellcheck: "false", oninput: (event) => { row[0] = event.currentTarget.value; } }),
      el("input", { class: "input mono", value: row[1], placeholder: "值", "aria-label": "请求头的值", spellcheck: "false", oninput: (event) => { row[1] = event.currentTarget.value; } }),
      el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "删除这个请求头",
        onclick: () => { headerList.splice(index, 1); renderHeaders(); } }, icon("x", { size: 14 })))),
    el("div", {}, el("button", { class: "btn btn-ghost btn-sm", type: "button",
      onclick: () => { headerList.push(["", ""]); renderHeaders(); headerRows.querySelector(".header-row:last-of-type input")?.focus(); } }, icon("plus", { size: 14 }), "添加请求头")));
  }

  // -- models
  const modelList = el("div", { class: "aapi-models", role: "list" });
  const pullButton = el("button", { class: "btn btn-sm", type: "button", onclick: () => pullModels() }, icon("download", { size: 14 }), "从服务商拉取模型");
  function renderModels() {
    if (!draft.models.length) {
      mount(modelList, el("div", { class: "aapi-models-empty faint small" }, draft.protocol === "openai" ? "还没有模型：手动添加，或者从服务商拉取。" : "还没有模型：添加一个模型 ID。"));
      return;
    }
    mount(modelList,
      el("div", { class: "aapi-model aapi-model-head", "aria-hidden": "true" },
        el("span", {}, "启用"), el("span", {}, "模型 ID"), el("span", {}, "显示名"), el("span", {}, "上下文窗口"), el("span", {}, "工具"), el("span")),
      draft.models.map((model, index) => modelRow(model, index)));
  }
  function modelRow(model, index) {
    const ctx = el("input", { class: "input mono aapi-ctx", value: formatTokens(model.context_window), placeholder: "自动", inputmode: "text", "aria-label": `${model.id || "模型"} 的上下文窗口`,
      title: "例如 128K、1M；留空则按内置表，查不到时不显示上限",
      oninput: () => {
        const n = parseTokens(ctx.value);
        ctx.setAttribute("aria-invalid", Number.isNaN(n) ? "true" : "false");
        if (n === null) delete model.context_window; else model.context_window = Number.isNaN(n) ? -1 : n;
      } });
    const idCell = model.fresh
      ? el("input", { class: "input mono", value: model.id, placeholder: MODEL_PLACEHOLDER[draft.protocol], "aria-label": "模型 ID", spellcheck: "false",
        oninput: (event) => { model.id = event.currentTarget.value.trim(); } })
      : el("span", { class: "mono truncate aapi-model-id", title: model.id }, model.id);
    return el("div", { class: `aapi-model${model.enabled ? "" : " off"}`, role: "listitem" },
      el("label", { class: "switch aapi-cell-enable", title: "启用后出现在 Agent 菜单里" },
        el("input", { type: "checkbox", role: "switch", checked: model.enabled, "aria-label": `启用 ${model.id || "模型"}`,
          onchange: (event) => { model.enabled = event.currentTarget.checked; event.currentTarget.closest(".aapi-model").classList.toggle("off", !model.enabled); } }), el("span"),
        el("span", { class: "only-sm small" }, "启用")),
      el("div", { class: "aapi-cell-id" }, idCell),
      el("label", { class: "aapi-cell-label" }, el("span", { class: "only-sm aapi-cap" }, "显示名"),
        el("input", { class: "input", value: model.label || "", placeholder: model.id || "显示名", "aria-label": `${model.id || "模型"} 的显示名`,
          oninput: (event) => { const v = event.currentTarget.value.trim(); if (v) model.label = v; else delete model.label; } })),
      el("label", { class: "aapi-cell-ctx" }, el("span", { class: "only-sm aapi-cap" }, "上下文窗口"), ctx),
      el("label", { class: "check aapi-cell-tools", title: "允许模型调用读写文件、执行命令等工具（仍按权限模式审批）" },
        el("input", { type: "checkbox", checked: model.tools !== false, "aria-label": `${model.id || "模型"} 允许工具`,
          onchange: (event) => { model.tools = event.currentTarget.checked; } }), el("span", { class: "only-sm small" }, "允许工具")),
      el("button", { class: "btn btn-ghost btn-icon btn-sm aapi-cell-del", type: "button", "aria-label": `移除 ${model.id || "模型"}`, title: "移除",
        onclick: () => { draft.models.splice(index, 1); renderModels(); } }, icon("x", { size: 14 })));
  }
  const addModel = el("button", { class: "btn btn-ghost btn-sm", type: "button",
    onclick: () => { draft.models.push({ id: "", enabled: true, tools: true, fresh: true }); renderModels(); modelList.querySelector(".aapi-model:last-child input.mono")?.focus(); } },
  icon("plus", { size: 14 }), "添加模型");

  // -- 测试
  const testOut = el("span", { class: "aapi-test", role: "status" });
  const testModelSelect = el("select", { class: "select aapi-test-model", "aria-label": "测试用的模型" });
  function renderTestModels() {
    const ids = draft.models.map((m) => m.id).filter(Boolean);
    const keep = testModelSelect.value;
    mount(testModelSelect, ids.length ? ids.map((id) => el("option", { value: id, selected: id === keep }, id)) : el("option", { value: "" }, "先添加模型"));
    testModelSelect.disabled = !ids.length;
  }
  const testButton = el("button", { class: "btn btn-sm", type: "button", onclick: () => test() }, icon("zap", { size: 14 }), "测试");

  function syncProtocol() {
    protocolHint.textContent = PROTOCOLS[draft.protocol]?.hint || "";
    pullButton.disabled = draft.protocol !== "openai";
    pullButton.title = draft.protocol === "openai" ? "调用 {base_url}/models 列出可选模型" : "只有 OpenAI 兼容协议能列出模型，请手动添加";
    const p = preset && preset.protocol === draft.protocol ? preset : null;
    keyHint.textContent = `${p?.key_optional ? "可以不填。" : ""}只写不读：保存后不会再显示，只能替换或清除。也可以填 env:变量名 引用设备上的环境变量。${p?.key_hint && !p.key_optional ? ` 格式：${p.key_hint}` : ""}`;
    renderModels();
  }

  function renderPresets() {
    mount(presets, PRESETS.map((p) => el("button", { class: `aapi-preset${preset?.id === p.id ? " on" : ""}`, type: "button", role: "radio", "aria-checked": String(preset?.id === p.id),
      onclick: () => choosePreset(p) },
    el("span", { class: "aapi-mark sm", dataset: { protocol: p.protocol }, "aria-hidden": "true" }, PROTOCOLS[p.protocol].mark),
    el("span", { class: "aapi-preset-name" }, p.label))));
  }
  function choosePreset(p) {
    preset = p;
    draft.protocol = p.protocol;
    protocolSelect.value = p.protocol;
    if (!p.custom || !draft.base_url || PRESETS.some((x) => x.base_url && x.base_url === draft.base_url)) { draft.base_url = p.base_url; urlInput.value = p.base_url; }
    if (isNew) {
      const known = !draft.label || PRESETS.some((x) => x.label === draft.label);
      if (known) { draft.label = p.custom ? "" : p.label; labelInput.value = draft.label; }
      if (!idTouched) { draft.id = slugId(p.custom ? (draft.label || "custom") : p.id, taken); idInput.value = draft.id; }
    }
    renderPresets();
    syncProtocol();
    (p.custom ? (draft.label ? urlInput : labelInput) : keyInput).focus();
  }

  function check() {
    commitHeaders();
    const errors = validateProvider(draft, { taken, isNew });
    for (const [name, slot] of Object.entries(errs)) setError(slot, { id: idInput, label: labelInput, protocol: protocolSelect, base_url: urlInput }[name], errors[name]);
    const first = Object.keys(errors)[0];
    if (first) ({ id: idInput, label: labelInput, base_url: urlInput }[first] || errs[first]).scrollIntoView?.({ block: "nearest" });
    return !first;
  }

  function cleanModels() {
    draft.models = draft.models.filter((m) => m.id).map((m) => { const copy = { ...m }; delete copy.fresh; return copy; });
  }

  let implicit = false;
  /** A new provider was saved by 测试 / 拉取模型: the dialog becomes an editor (取消 → 关闭, 添加 → 保存). */
  function savedNote() {
    implicit = true;
    const panel = handle?.root;
    if (!panel) return;
    const buttons = panel.querySelectorAll(".modal-foot > .btn");
    if (buttons[0]) buttons[0].replaceChildren("关闭");
    if (buttons[1]) buttons[1].replaceChildren("保存");
    const titles = panel.querySelector(".modal-head .titles");
    titles?.querySelector("h2")?.replaceChildren(`编辑 ${draft.label || draft.id}`);
    const note = titles?.querySelector("p");
    if (note) note.textContent = "已保存到这台设备（测试前需要先保存）；关闭不会删除它，要删除请在列表里点删除。";
  }

  /** Save the draft on the device (provider first, then the key change). Returns false when it failed. */
  async function persist({ quiet = false } = {}) {
    for (const m of draft.models) if (m.fresh) m.id = String(m.id || "").trim();
    if (!check()) return false;
    const models = draft.models;
    cleanModels();
    try {
      await saveProvider(api, device, draft, keyChange);
    } catch (error) {
      draft.models = models;
      toast(error.message, { type: "bad", title: "保存失败" });
      return false;
    }
    if (keyChange !== undefined) {
      status.set = keyChange !== null;
      status.env = typeof keyChange === "string" && keyChange.startsWith("env:") ? keyChange.slice(4) : "";
      status.last4 = typeof keyChange === "string" && !status.env ? keyChange.replace(/[^A-Za-z0-9]/g, "").slice(-4) : "";
      keyChange = undefined;
      keyInput.value = "";
      keyInput.placeholder = status.set ? "已设置；留空则不修改" : "粘贴 API 密钥";
    }
    if (isNew && !saved && quiet) savedNote();   // 测试 / 拉取模型 saved it: say so, 取消 no longer discards it
    saved = true;
    idInput.disabled = true;
    renderKeyState();
    renderModels();
    renderTestModels();
    if (!quiet) toast(isNew && !implicit ? `已添加 ${draft.label}` : `已保存 ${draft.label}`);
    onSaved?.();
    return true;
  }

  async function test() {
    renderTestModels();
    const model = testModelSelect.value || testModel(draft);
    if (!model) { toast("先添加一个模型再测试", { type: "warn" }); return; }
    testButton.disabled = true;
    mount(testOut, el("span", { class: "spinner" }), saved ? "测试中…" : "先保存，再测试…");
    testOut.className = "aapi-test running";
    try {
      if (!(await persist({ quiet: true }))) { mount(testOut); return; }
      const result = await callOp(api, "agent_api_test", { provider_id: draft.id, model }, device);
      if (result.ok === false) throw new Error(result.message || "测试没有通过");
      testOut.className = "aapi-test ok";
      mount(testOut, icon("circle-check", { size: 14 }), `连接正常${latencyText(result) ? ` · ${latencyText(result)}` : ""}`);
    } catch (error) {
      testOut.className = "aapi-test bad";
      mount(testOut, icon("circle-alert", { size: 14 }), error.message);
    } finally {
      testButton.disabled = false;
    }
  }

  async function pullModels() {
    pullButton.disabled = true;
    const original = Array.from(pullButton.childNodes);
    pullButton.replaceChildren(el("span", { class: "spinner" }), "拉取中…");
    let list;
    try {
      if (!(await persist({ quiet: true }))) return;
      const result = await callOp(api, "agent_api_models", { provider_id: draft.id }, device);
      list = (Array.isArray(result) ? result : result.models || result.data || []).map((m) => (typeof m === "string" ? { id: m } : m)).filter((m) => m && m.id);
    } catch (error) {
      toast(error.message, { type: "bad", title: "拉取模型失败" });
      return;
    } finally {
      pullButton.replaceChildren(...original);
      pullButton.disabled = draft.protocol !== "openai";
    }
    if (!list.length) { toast("服务商没有返回模型", { type: "info" }); return; }
    pickModels(list, draft.models.map((m) => m.id), (picked) => {
      for (const m of picked) {
        const model = { id: m.id, enabled: true, tools: true };
        const ctx = Number(m.context_window ?? m.context_length);
        if (ctx > 0) model.context_window = ctx;
        draft.models.push(model);
      }
      renderModels();
      renderTestModels();
      if (picked.length) toast(`已添加 ${picked.length} 个模型，记得保存`, { type: "info" });
    });
  }

  // -- layout
  renderPresets();
  renderHeaders();
  renderKeyState();
  syncProtocol();
  renderTestModels();
  testModelSelect.addEventListener("focus", renderTestModels);

  const body = el("div", { class: "form aapi-editor" },
    field("预设模板", presets, { hint: "选一个模板会自动填好协议和地址；中转站（NewAPI / OneAPI 等）选「自定义」。" }),
    el("div", { class: "form-row" },
      field("名称", labelInput, { error: errs.label }),
      field("ID", idInput, { hint: isNew ? "Agent 名为 api:<ID>/<模型>，保存后不能改" : "保存后不能改", error: errs.id })),
    el("div", { class: "aapi-row2" },
      field("协议", protocolSelect, { error: errs.protocol }),
      field("API 地址（base_url）", urlInput, { hint: protocolHint, error: errs.base_url })),
    field("API 密钥", el("div", { class: "stack aapi-keybox" }, keyInput, keyState), { hint: keyHint }),
    el("div", { class: "field" },
      el("div", { class: "row between aapi-models-bar" },
        el("span", { class: "field-label" }, "模型"),
        el("div", { class: "row" }, pullButton, addModel)),
      modelList, errs.models),
    el("details", { class: "advanced", open: Object.keys(draft.headers || {}).length > 0 },
      el("summary", {}, "高级：附加请求头"),
      el("p", { class: "faint xs", style: { margin: "8px 0" } }, "非机密的请求头，例如 OpenRouter 的 HTTP-Referer / X-Title。会随配置同步，所以不要放密钥。"),
      headerRows, errs.headers));

  const handle = modal({
    title: isNew ? "新增 API 服务商" : `编辑 ${existing.label}`,
    description: "请求从运行设备直接发给服务商；密钥只保存在这台设备上。",
    size: "lg", body,
    footLeft: el("div", { class: "aapi-testbar" }, testButton, testModelSelect, testOut),
    actions: [
      { label: "取消" },
      { label: isNew ? "添加" : "保存", variant: "primary", onClick: async () => persist() },
    ],
    onClose: () => { keyInput.value = ""; keyChange = undefined; },
  });
  handle.root.classList.add("aapi-modal");
  if (isNew && !preset) requestAnimationFrame(() => presets.querySelector("button")?.focus());
  return handle;
}

/** A searchable checklist of the models the provider listed; models already added are ticked and locked. */
function pickModels(list, have, onPick) {
  const chosen = new Set();
  const search = el("input", { class: "input", type: "search", placeholder: `在 ${list.length} 个模型里搜索`, "aria-label": "搜索模型", oninput: () => draw() });
  const box = el("div", { class: "aapi-pick", role: "list" });
  const count = el("span", { class: "faint small" });
  function draw() {
    const q = search.value.trim().toLowerCase();
    const shown = list.filter((m) => !q || m.id.toLowerCase().includes(q)).slice(0, 300);
    mount(box, shown.length ? shown.map((m) => {
      const already = have.includes(m.id);
      const ctx = formatTokens(m.context_window ?? m.context_length);
      return el("label", { class: `check aapi-pick-item${already ? " have" : ""}`, role: "listitem" },
        el("input", { type: "checkbox", checked: already || chosen.has(m.id), disabled: already,
          onchange: (event) => { if (event.currentTarget.checked) chosen.add(m.id); else chosen.delete(m.id); count.textContent = chosen.size ? `已选 ${chosen.size} 个` : ""; } }),
        el("span", { class: "mono truncate grow" }, m.id),
        already ? el("span", { class: "faint xs" }, "已添加") : ctx ? el("span", { class: "faint xs mono" }, ctx) : m.owned_by ? el("span", { class: "faint xs" }, m.owned_by) : null);
    }) : el("div", { class: "faint small aapi-models-empty" }, "没有匹配的模型"));
  }
  draw();
  modal({
    title: "选择要添加的模型", size: "sm",
    body: el("div", { class: "stack" }, search, box),
    footLeft: count,
    actions: [{ label: "取消" }, { label: "添加所选", variant: "primary", onClick: () => onPick(list.filter((m) => chosen.has(m.id))) }],
  });
}

