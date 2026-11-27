// In-memory stand-in for the API-provider ops (docs/agent-chat.md §16.4) on top of agent-fake-api.js, for the
// agent-prompts mockup's ?tab=api. withApiProviders(api) answers agent_api_* itself and passes every other op through;
// apiConfigPut(body) plays the gateway's api_config_put (refuses a key field, like the real one).
// ?providers=none → no providers yet; ?test=fail → 测试 answers 401.

const params = new URLSearchParams(location.search);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

const PROVIDERS = params.get("providers") === "none" ? [] : [
  { id: "deepseek", label: "DeepSeek", protocol: "openai", base_url: "https://api.deepseek.com/v1", enabled: true,
    models: [
      { id: "deepseek-chat", label: "DeepSeek V3", context_window: 128000, tools: true, enabled: true },
      { id: "deepseek-reasoner", label: "DeepSeek R1", context_window: 128000, tools: false, enabled: true },
    ] },
  { id: "openrouter", label: "OpenRouter", protocol: "openai", base_url: "https://openrouter.ai/api/v1", enabled: true,
    headers: { "HTTP-Referer": "https://anx.example", "X-Title": "Agent Nexus" },
    models: [{ id: "qwen/qwen3-coder", context_window: 262144, tools: true, enabled: true }, { id: "moonshotai/kimi-k2", tools: true, enabled: false }] },
  { id: "anthropic", label: "Anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com", enabled: true, source: "gateway",
    models: [{ id: "claude-sonnet-4-5", label: "Claude Sonnet", context_window: 1000000, tools: true, enabled: true }] },
  { id: "ollama", label: "家里的 Ollama", protocol: "openai", base_url: "http://192.168.1.20:11434/v1", enabled: false,
    models: [{ id: "qwen2.5-coder:14b", context_window: 32768, tools: true, enabled: true }] },
];
// provider id -> {last4} | {env}; the fake never hands a key back either
const KEYS = new Map(params.get("providers") === "none" ? [] : [["deepseek", { last4: "1a2b" }], ["openrouter", { last4: "9f0c" }], ["anthropic", { env: "ANTHROPIC_API_KEY" }]]);

const OPENAI_MODELS = {
  deepseek: ["deepseek-chat", "deepseek-reasoner"],
  openrouter: ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro", "openai/gpt-4o-mini", "qwen/qwen3-coder", "moonshotai/kimi-k2", "deepseek/deepseek-chat-v3.1",
    "meta-llama/llama-3.3-70b-instruct", "mistralai/mistral-small-3.2", "z-ai/glm-4.6", "x-ai/grok-code-fast-1"],
};
const CONTEXT = { "anthropic/claude-sonnet-4.5": 1000000, "google/gemini-2.5-pro": 1048576, "openai/gpt-4o-mini": 128000, "z-ai/glm-4.6": 200000 };

export const gatewayConfig = { providers: [], updated: null };

function view(provider) {
  const key = KEYS.get(provider.id);
  return { ...clone(provider), key: key ? { set: true, ...(key.env ? { env: key.env } : { last4: key.last4 }) } : { set: false } };
}

const ops = {
  agent_api_providers: () => ({ providers: PROVIDERS.map(view) }),
  agent_api_provider_save: ({ provider }) => {
    if (!provider || !provider.id) return { error: "缺少服务商 ID", code: "bad_request" };
    if ("key" in provider || "api_key" in provider) return { error: "密钥请用 agent_api_key_set 设置", code: "bad_request" };
    const index = PROVIDERS.findIndex((p) => p.id === provider.id);
    const stored = { ...clone(provider), source: undefined };
    if (index >= 0) PROVIDERS[index] = stored; else PROVIDERS.push(stored);
    return { provider: view(stored) };
  },
  agent_api_provider_delete: ({ provider_id }) => {
    const index = PROVIDERS.findIndex((p) => p.id === provider_id);
    if (index < 0) return { error: "没有这个服务商", code: "not_found" };
    PROVIDERS.splice(index, 1);
    KEYS.delete(provider_id);
    return { ok: true };
  },
  agent_api_key_set: ({ provider_id, key }) => {
    if (key === null) { KEYS.delete(provider_id); return { ok: true, key: { set: false } }; }
    const text = String(key || "");
    if (text.startsWith("env:")) KEYS.set(provider_id, { env: text.slice(4) });
    else KEYS.set(provider_id, { last4: text.replace(/[^A-Za-z0-9]/g, "").slice(-4) });
    return { ok: true, key: { set: true } };
  },
  agent_api_test: async ({ provider_id, model }) => {
    await wait(500 + Math.random() * 400);
    const provider = PROVIDERS.find((p) => p.id === provider_id);
    if (!provider) return { error: "没有这个服务商", code: "not_found" };
    if (params.get("test") === "fail" || (!KEYS.has(provider_id) && !/localhost|192\.168\./.test(provider.base_url))) return { error: "密钥无效或无权限（HTTP 401）", code: "auth" };
    if (provider.id === "ollama") return { error: `连不上 ${new URL(provider.base_url).host}：连接被拒绝`, code: "network" };
    return { ok: true, latency_ms: 280 + Math.round(Math.random() * 300), model };
  },
  agent_api_models: async ({ provider_id }) => {
    await wait(450);
    const provider = PROVIDERS.find((p) => p.id === provider_id);
    if (!provider) return { error: "没有这个服务商", code: "not_found" };
    if (provider.protocol !== "openai") return { error: "只有 OpenAI 兼容协议支持列出模型", code: "unsupported" };
    const ids = OPENAI_MODELS[provider_id] || ["gpt-4o-mini", "gpt-4.1", "o4-mini", "text-embedding-3-small"];
    return { models: ids.map((id) => ({ id, context_window: CONTEXT[id], owned_by: id.split("/")[0] })) };
  },
};

/** api + the agent_api_* ops (any device) + apiConfigPut for 「同步到所有设备」. */
export function withApiProviders(api) {
  return {
    ...api,
    async call(op, p = {}, device) {
      if (!ops[op]) return api.call(op, p, device);
      await wait(80 + Math.random() * 80);
      if (device === "dev-nas") return { error: "设备离线", code: "offline" };
      try { return await ops[op](p, device); } catch (error) { return { error: String(error.message || error), code: "internal" }; }
    },
    async apiConfigPut(body) {
      await wait(300);
      const text = JSON.stringify(body);
      if (/"(key|api_key)"\s*:/.test(text)) return { error: "同步内容里不能有密钥", code: "bad_request" };
      gatewayConfig.providers = body.providers;
      gatewayConfig.updated = Date.now();
      return { ok: true, providers: body.providers.length };
    },
  };
}
