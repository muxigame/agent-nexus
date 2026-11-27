"use strict";
// Agent 设置 → API (agent_nexus/static/ui/api-admin.js, docs/agent-chat.md §16.4/§16.5): the pure helpers and the
// op sequences, against a recording fake adapter (no DOM, no network).
const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const load = () => import(pathToFileURL(path.join(__dirname, "../agent_nexus/static/ui/api-admin.js")).href);

function recorder(answers = {}) {
  const calls = [];
  return {
    calls,
    call: async (op, params, device) => { calls.push({ op, params: JSON.parse(JSON.stringify(params)), device }); return answers[op] ?? { ok: true }; },
  };
}

test("presets cover §16.5 and fill protocol + base_url", async () => {
  const { PRESETS, PROTOCOLS, presetFor } = await load();
  const labels = PRESETS.map((p) => p.label);
  for (const name of ["OpenAI", "DeepSeek", "通义", "Kimi", "OpenRouter", "Ollama", "Anthropic", "Gemini"]) assert.ok(labels.includes(name), name);
  assert.ok(PRESETS.at(-1).custom && /NewAPI/.test(PRESETS.at(-1).label));
  for (const p of PRESETS) {
    assert.ok(PROTOCOLS[p.protocol], p.id);
    if (!p.custom) assert.match(p.base_url, /^https?:\/\//, p.id);
  }
  assert.equal(PRESETS.find((p) => p.id === "anthropic").protocol, "anthropic");
  assert.equal(PRESETS.find((p) => p.id === "gemini").protocol, "gemini");
  assert.equal(presetFor({ protocol: "openai", base_url: "https://api.deepseek.com/v1/" }).id, "deepseek");
  assert.equal(presetFor({ protocol: "openai", base_url: "https://relay.example/v1" }).id, "custom");
});

test("key status never shows more than the last four characters", async () => {
  const { keyStatus, keyLabel, normalizeProviders } = await load();
  assert.equal(keyLabel(keyStatus({ key: { set: true, last4: "1a2b" } })), "● 已设置 ****1a2b");
  assert.equal(keyLabel(keyStatus({ key: { set: false } })), "○ 未设置");
  assert.equal(keyLabel(keyStatus({})), "○ 未设置");
  assert.equal(keyLabel(keyStatus({ key_set: true, key_last4: "sk-abcdef123456" })), "● 已设置 ****3456");
  assert.equal(keyLabel(keyStatus({ key: { set: true, env: "OPENAI_API_KEY" } })), "● 环境变量 OPENAI_API_KEY");
  const [p] = normalizeProviders({ providers: [{ id: "x", protocol: "weird", models: [{ id: "m" }, { label: "no id" }] }, { label: "no id" }] });
  assert.equal(p.label, "x");
  assert.equal(p.protocol, "openai");
  assert.deepEqual(p.models.map((m) => [m.id, m.enabled, m.tools]), [["m", true, true]]);
});

test("token sizes parse and format", async () => {
  const { parseTokens, formatTokens } = await load();
  assert.equal(parseTokens("128K"), 128000);
  assert.equal(parseTokens("1m"), 1000000);
  assert.equal(parseTokens("1,000,000"), 1000000);
  assert.equal(parseTokens(""), null);
  assert.ok(Number.isNaN(parseTokens("lots")));
  assert.ok(Number.isNaN(parseTokens("0")));
  assert.equal(formatTokens(128000), "128K");
  assert.equal(formatTokens(1000000), "1M");
  assert.equal(formatTokens(32768), "32768");
  assert.equal(formatTokens(undefined), "");
});

test("ids and validation", async () => {
  const { slugId, validateProvider } = await load();
  assert.equal(slugId("deepseek", []), "deepseek");
  assert.equal(slugId("deepseek", ["deepseek", "deepseek-2"]), "deepseek-3");
  assert.equal(slugId("我的 NewAPI", []), "newapi");
  assert.equal(slugId("通义", []), "provider");
  const good = { id: "kimi", label: "Kimi", protocol: "openai", base_url: "https://api.moonshot.cn/v1", models: [{ id: "kimi-k2", context_window: 128000 }], headers: {} };
  assert.deepEqual(validateProvider(good), {});
  assert.ok(validateProvider({ ...good, id: "a/b" }).id);
  assert.ok(validateProvider(good, { taken: ["kimi"], isNew: true }).id);
  assert.ok(validateProvider({ ...good, base_url: "api.moonshot.cn" }).base_url);
  assert.ok(validateProvider({ ...good, models: [{ id: "a" }, { id: "a" }] }).models);
  assert.ok(validateProvider({ ...good, models: [{ id: "a", context_window: -1 }] }).models);
  assert.ok(validateProvider({ ...good, headers: { Authorization: "Bearer x" } }).headers);
});

test("the provider payload and the gateway sync carry no secrets", async () => {
  const { providerPayload, syncPayload } = await load();
  const provider = { id: "or", label: "OR", protocol: "openai", base_url: "https://openrouter.ai/api/v1", enabled: true, source: "local",
    key: { set: true, last4: "9f0c" }, key_last4: "9f0c", api_key: "sk-nope",
    headers: { "X-Title": "anx", Authorization: "Bearer sk-nope", "x-api-key": "sk-nope" },
    models: [{ id: "a", label: "", context_window: 1000, tools: true, enabled: false, fresh: true }] };
  const payload = providerPayload(provider);
  assert.deepEqual(Object.keys(payload).sort(), ["base_url", "enabled", "headers", "id", "label", "models", "protocol"]);
  assert.deepEqual(payload.headers, { "X-Title": "anx" });
  assert.deepEqual(payload.models, [{ id: "a", context_window: 1000, tools: true, enabled: false }]);
  assert.ok(!JSON.stringify(payload).includes("sk-nope"));
  const body = syncPayload([provider, { ...provider, id: "gw", source: "gateway" }]);
  // a merge: the gateway's providers (other devices' syncs) this device does not override are kept
  assert.deepEqual(body.providers.map((p) => p.id), ["or", "gw"]);
  assert.ok(!/"key"|last4|sk-nope/.test(JSON.stringify(body)));
  const shadowed = syncPayload([{ ...provider, source: "gateway", label: "old" }, provider]);
  assert.deepEqual(shadowed.providers.map((p) => [p.id, p.label]), [["or", "OR"]]);   // local wins by id
  assert.deepEqual(providerPayload({ ...provider, headers: { "X-Auth": "s", "X-Custom-Auth": "s", "X-Title": "t" } }).headers, { "X-Title": "t" });
});

test("keyless providers and moved addresses are labelled", async () => {
  const { keyStatus, keyLabel, keyTone } = await load();
  const ollama = keyStatus({ key: { set: false }, login: "ok" });
  assert.equal(keyLabel(ollama), "● 不需要密钥");
  assert.equal(keyTone(ollama), "keyless");
  assert.equal(keyLabel(keyStatus({ key: { set: false, origin_changed: true }, login: "missing" })), "○ 地址变了，需重新输入");
  assert.equal(keyTone(keyStatus({ key: { set: true, last4: "abcd" } })), "set");
});

test("saveProvider: save first, key only through agent_api_key_set, null clears", async () => {
  const { saveProvider } = await load();
  const provider = { id: "ds", label: "DS", protocol: "openai", base_url: "https://api.deepseek.com/v1", models: [], key: { set: true, last4: "1a2b" } };
  let api = recorder();
  await saveProvider(api, "dev-1", provider, undefined);
  assert.deepEqual(api.calls.map((c) => c.op), ["agent_api_provider_save"]);
  assert.equal(api.calls[0].device, "dev-1");
  assert.ok(!("key" in api.calls[0].params.provider));

  api = recorder();
  await saveProvider(api, null, provider, "sk-new-secret");
  assert.deepEqual(api.calls.map((c) => c.op), ["agent_api_provider_save", "agent_api_key_set"]);
  assert.deepEqual(api.calls[1].params, { provider_id: "ds", key: "sk-new-secret" });
  assert.ok(!JSON.stringify(api.calls[0]).includes("sk-new-secret"));

  api = recorder();
  await saveProvider(api, null, provider, null);
  assert.deepEqual(api.calls[1].params, { provider_id: "ds", key: null });

  api = recorder({ agent_api_provider_save: { error: "保存失败了", code: "bad" } });
  await assert.rejects(saveProvider(api, null, provider, "sk-x"), /保存失败了/);
  assert.deepEqual(api.calls.map((c) => c.op), ["agent_api_provider_save"]);   // no key write after a failed save
});

test("syncToGateway uses the adapter's apiConfigPut with the non-secret body", async () => {
  const { syncToGateway } = await load();
  let seen = null;
  const api = { apiConfigPut: async (body) => { seen = body; return { ok: true }; } };
  const result = await syncToGateway(api, [{ id: "a", label: "A", protocol: "openai", base_url: "https://x/v1", models: [], key: { set: true, last4: "abcd" } }]);
  assert.deepEqual(result, { ok: true });
  assert.deepEqual(seen.providers.map((p) => p.id), ["a"]);
  assert.ok(!JSON.stringify(seen).includes("abcd"));
});

test("testModel prefers the first enabled model", async () => {
  const { testModel } = await load();
  assert.equal(testModel({ models: [{ id: "a", enabled: false }, { id: "b" }] }), "b");
  assert.equal(testModel({ models: [{ id: "a", enabled: false }] }), "a");
  assert.equal(testModel({ models: [] }), "");
});
