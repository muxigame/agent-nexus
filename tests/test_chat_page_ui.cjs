"use strict";
// The 对话 page, state keeping, context ring and notices (docs/agent-chat.md §16.1–§16.3, §16.6): pure helpers of
// ui/chat.js and ui/chat-page.js (no DOM), plus static checks of the wiring. The page itself is driven in a browser
// on docs/mockups/agent-chat.html.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const read = (rel) => fs.readFileSync(path.join(STATIC, rel), "utf8");
const load = (rel) => import(pathToFileURL(path.join(STATIC, rel)).href);

test("token counts read like the spec (872.1K / 1M)", async () => {
  const { tokenCount } = await load("ui/chat.js");
  assert.equal(tokenCount(872100), "872.1K");
  assert.equal(tokenCount(1000000), "1M");
  assert.equal(tokenCount(1500000), "1.5M");
  assert.equal(tokenCount(200000), "200K");
  assert.equal(tokenCount(999), "999");
  assert.equal(tokenCount(null), "0");
});

test("the usage ring: yellow above 80 %, red above 95 %, a count when the limit is unknown", async () => {
  const { usageView } = await load("ui/chat.js");
  const view = usageView({ used: 872100, limit: 1000000, model: "claude-opus-4-5" });
  assert.equal(view.pct, 87);
  assert.equal(view.tone, "warn");
  assert.equal(view.title, "已用 872.1K / 1M（87%）· 模型 claude-opus-4-5");
  assert.equal(usageView({ used: 500, limit: 1000 }).tone, "");
  assert.equal(usageView({ used: 800, limit: 1000 }).tone, "");          // exactly 80 % is not over it
  assert.equal(usageView({ used: 801, limit: 1000 }).tone, "warn");
  assert.equal(usageView({ used: 951, limit: 1000 }).tone, "bad");
  assert.equal(usageView({ used: 1200, limit: 1000 }).ratio > 1, true);  // over the window: the ring caps itself
  const open = usageView({ used: 48200, limit: null, model: "gpt-5.1-codex" });
  assert.equal(open.pct, null);
  assert.equal(open.title, "已用 48.2K（上限未知）· 模型 gpt-5.1-codex");
  assert.equal(usageView(null), null);
  assert.equal(usageView({}), null);
  assert.equal(usageView({ used: null, limit: 10 }), null);
});

test("API models are their own kind family", async () => {
  const { isApiKind } = await load("ui/chat.js");
  assert.equal(isApiKind("api:deepseek/deepseek-chat"), true);
  assert.equal(isApiKind("claude"), false);
  assert.equal(isApiKind(null), false);
  const chat = read("ui/chat.js");
  assert.ok(chat.includes('"API 模型"'), "the Agent picker has an API group");
});

test("#/chat URLs round-trip and 本机 is spelled `local` only in the local console", async () => {
  const { chatWant, chatHref } = await load("ui/chat-page.js");
  const q = (text) => new URLSearchParams(text);
  assert.deepEqual(chatWant(q("device=local&kind=claude&session=c-1"), { local: true }), { device: null, session: { kind: "claude", id: "c-1" } });
  assert.deepEqual(chatWant(q("device=local"), { local: false }), { device: "local" });
  assert.deepEqual(chatWant(q("kind=codex")), { kind: "codex" });
  assert.deepEqual(chatWant(q("new=1&device=pc-a")), { device: "pc-a", fresh: true });
  assert.deepEqual(chatWant(q("")), {});
  assert.equal(chatHref({ device: null, kind: "claude", session: "c 1" }, { local: true }), "#/chat?device=local&kind=claude&session=c+1");
  assert.equal(chatHref({ device: null, fresh: true }, { local: false }), "#/chat?new=1");
  assert.equal(chatHref({}), "#/chat");
  for (const [want, local] of [[{ device: "pc-a", kind: "codex", session: "x-9" }, false], [{ device: null, kind: "api:o/m", session: "a-1" }, true]]) {
    const back = chatWant(new URLSearchParams(chatHref(want, { local }).split("?")[1]), { local });
    assert.deepEqual(back, { device: want.device, session: { kind: want.kind, id: want.session } });
  }
});

test("state is kept per console under anx-chat:<console> (§16.2)", async () => {
  const { chatStore } = await load("ui/chat.js");
  const memory = new Map();
  global.localStorage = { getItem: (k) => (memory.has(k) ? memory.get(k) : null), setItem: (k, v) => memory.set(k, String(v)) };
  try {
    const store = chatStore("anx-chat:local");
    assert.deepEqual(store.read(), {});
    store.write({ device: null, draft: "草稿" });
    store.write({ sideCollapsed: true });
    assert.deepEqual(JSON.parse(memory.get("anx-chat:local")), { device: null, draft: "草稿", sideCollapsed: true });
    memory.set("anx-chat:local", "{broken");
    assert.deepEqual(store.read(), {});
  } finally {
    delete global.localStorage;
  }
  const page = read("ui/chat-page.js");
  assert.match(page, /`anx-chat:\$\{local \? "local" : `cloud:\$\{location\.origin\}`\}`/);
});

test("one chat instance per console that keeps polling while detached; hidden > 10 min pauses", async () => {
  const { PAUSE_HIDDEN_MS } = await load("ui/chat.js");
  assert.equal(PAUSE_HIDDEN_MS, 10 * 60 * 1000);
  const chat = read("ui/chat.js");
  assert.match(chat, /if \(!opts\.persistent && !root\.isConnected\) \{ destroy\(\); return false; \}/);
  assert.match(chat, /const nap = asleep\(\);/);
  const page = read("ui/chat-page.js");
  assert.match(page, /persistent: true/);
  assert.match(page, /entry\.host\.remove\(\)/);
});

test("notices are grey, once per session; warn is yellow (§16.6)", () => {
  const chat = read("ui/chat.js");
  assert.match(chat, /ev\.level === "notice"/);
  assert.match(chat, /notices\.has\(text\)/);
  assert.match(chat, /chat-error \$\{warn \? "warn" : ""\}/);
  const css = read("ui/ui.css");
  for (const rule of [".chat-notice", ".chat-error.warn", ".chat-usage.warn .chat-usage-bar", ".chat-usage.bad .chat-usage-bar", ".page.page-chat"]) assert.ok(css.includes(rule), rule);
});

test("both consoles route #/chat and the Overviews show the 对话 card instead of the chat", () => {
  for (const app of ["console/app.js", "local/app.js"]) {
    const text = read(app);
    assert.match(text, /\{ path: "\/chat", label: "对话", icon: "message-square", primary: true \}/, app);
    assert.match(text, /"\/chat": \(route\) => chatPage\(context\("对话"/, app);
    assert.ok(text.includes('import { chatPage } from "/ui/chat-page.js"'), app);
    // at most five primary entries: with 更多 the phone tab bar keeps six slots
    assert.ok((text.match(/primary: true/g) || []).length <= 5, app);
  }
  for (const overview of ["console/pages.js", "local/overview.js"]) {
    const text = read(overview);
    assert.ok(!text.includes("mountChat("), overview);
    assert.match(text, /chatCard\(\{ api: (cloud|local)AgentApi\(\), local: (true|false) \}\)/, overview);
    assert.ok(text.includes("chat.node"), overview);
  }
  assert.ok(read("ui/agent-wire.js").includes('href: "#/chat"'));
});

test("switching the run device does not carry the previous device's workdir over (e2e regression)", async () => {
  const { devicePrefs } = await load("ui/chat.js");
  const stored = { device: null, kind: "claude", workdir: "/srv/work/proj", byDevice: { local: { kind: "claude", workdir: "/srv/work/proj" } } };
  assert.deepEqual(devicePrefs(stored, "host-b"), {});                       // never used: the device's own defaults
  assert.equal(devicePrefs(stored, null).workdir, "/srv/work/proj");
  assert.equal(devicePrefs({ device: "host-b", workdir: "/b" }, "host-b").workdir, "/b");   // older state, no byDevice
  assert.deepEqual(devicePrefs({ device: "host-b", workdir: "/b" }, null), {});
  // the device was switched (top-level `device` already says host-b) but nothing was chosen there yet
  assert.deepEqual(devicePrefs({ ...stored, device: "host-b" }, "host-b"), {});
  // selectDevice reads the prefs before it re-attributes the store's top-level fields to the new device
  const src = read("ui/chat.js");
  const body = src.slice(src.indexOf("async function selectDevice("));
  assert.ok(body.indexOf("prefsFor(id)") > 0 && body.indexOf("prefsFor(id)") < body.indexOf("store.write({ device: id })"));
});
