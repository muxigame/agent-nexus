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
    store.write({ sideClosed: true });
    assert.deepEqual(JSON.parse(memory.get("anx-chat:local")), { device: null, draft: "草稿", sideClosed: true });
    store.write({ sideClosed: undefined });   // undefined drops a key (how the old sideCollapsed is cleared)
    assert.deepEqual(JSON.parse(memory.get("anx-chat:local")), { device: null, draft: "草稿" });
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


test("the settings are one chip row inside the composer; a light head line replaces the toolbar band", () => {
  const chat = read("ui/chat.js");
  assert.ok(!chat.includes('"chat-bar"') && !chat.includes("chat-bar-picks") && !chat.includes("function pick("), "the old toolbar is gone");
  assert.match(chat, /const chips = el\("div", \{ class: "chat-chips", role: "toolbar", "aria-label": "对话设置" \}\)/);
  // the chips sit in the composer's foot, left of usage / stop / send
  assert.match(chat, /el\("div", \{ class: "chat-composer-foot" \}, chips, el\("div", \{ class: "chat-composer-actions" \}, usageNode, stopBtn, sendBtn\)\)/);
  // each chip: a button with the full text as its name, reusing the existing menus
  assert.match(chat, /class: `chat-chip \$\{cls\}`, type: "button", "aria-haspopup": "true", "aria-expanded": "false", title: name, "aria-label": name/);
  for (const [cls, opener] of [["chat-chip-device", "deviceMenu("], ["chat-chip-agent", "agentMenu("], ["chat-chip-dir", "dirMenu("], ["chat-chip-perm", "permissionMenu("]]) {
    const at = chat.indexOf(`"${cls}`) >= 0 ? chat.indexOf(`"${cls}`) : chat.indexOf(`\`${cls}`);
    assert.ok(at > 0, cls);
    assert.ok(chat.slice(at, at + 600).includes(opener), `${cls} opens ${opener}`);
  }
  assert.match(chat, /mount\(chips, device, agent, dir, perm\)/);
  assert.match(chat, /`dot \$\{status\.tone/, "the Agent chip has a status dot");
  // head line: session toggle · current title · ⋯ (the ⋯ menu keeps presets / context / temp dirs / 新对话)
  assert.match(chat, /const head = el\("div", \{ class: "chat-head" \}, sideBtn, headTitle, moreBtn\)/);
  assert.match(chat, /const main = el\("section", \{ class: "chat-main", "aria-label": "对话" \}, head, ctxLine,/);
  for (const item of ["编辑提示词片段…", "工作上下文…", "临时目录…"]) assert.ok(chat.includes(item), item);
  const css = read("ui/ui.css");
  for (const rule of [".chat-head {", ".chat-chips {", ".chat-chip {", ".chat-chips.fade-end", ".chat-chip.danger", ".chat-chip-dir.no-dir"]) assert.ok(css.includes(rule), rule);
  assert.ok(!/\.chat-bar|\.chat-pick\b/.test(css), "no leftover toolbar rules");
});

test("the composer note keeps only state that matters; key hints are in the placeholder", () => {
  const chat = read("ui/chat.js");
  const body = chat.slice(chat.indexOf("function renderComposer("), chat.indexOf("// ============================================================ the event stream"));
  assert.ok(!body.includes("kbd") && !body.includes("chat-note-keys"), "no always-visible key hints");
  assert.ok(body.includes("Enter 发送") && body.includes("Shift+Enter 换行") && body.includes("input.title"));
  assert.ok(body.includes('"将续接这个会话"'));
  assert.ok(!body.includes("不会逐项询问") && !body.includes("chat-note-danger"), "the permission shows in its chip");
  assert.match(body, /composerNote\.hidden = !composerNote\.childElementCount/);
});

test("the chat is not a card: messages on the page background, the composer is the raised surface", () => {
  const chat = read("ui/chat.js");
  assert.match(chat, /const root = el\("div", \{ class: "chat" \}, side, scrim, main\)/);
  assert.ok(!chat.includes('class: "chat card"'));
  const css = read("ui/ui.css");
  assert.match(css, /\.chat \{ --chat-side-w: 272px; --chat-col: 820px;/);
  const composer = css.slice(css.indexOf(".chat-composer {"), css.indexOf("}", css.indexOf(".chat-composer {")));
  assert.ok(composer.includes("border:") && composer.includes("box-shadow:"));
  // the skins do not give the chat back a card surface
  for (const skin of ["ui/style-pure.css", "ui/style-mica.css"]) assert.ok(!/\.chat[\s.{,]/.test(read(skin)), skin);
});

test("the session list is open by default; only this version's collapse keeps it closed", () => {
  const chat = read("ui/chat.js");
  assert.ok(chat.includes('if ("sideCollapsed" in stored) store.write({ sideCollapsed: undefined });'), "the old preference is dropped once");
  assert.ok(chat.includes("if (stored.sideClosed === true) root.classList.add(\"side-collapsed\")"));
  assert.ok(chat.includes("store.write({ sideClosed: on })"));
  assert.ok(!/read\(\)\.sideCollapsed/.test(chat), "sideCollapsed is never read");
  // the drawer breakpoint is the same in the script and the stylesheet
  const css = read("ui/ui.css");
  assert.ok(css.includes("@container chat (max-width: 719px)") && css.includes("@container chat (min-width: 720px)"));
  assert.ok(chat.includes("root.clientWidth < 720"));
});

test("chip text ellipsizes; on wide layouts the chips shrink instead of scrolling 权限 out of view", () => {
  const chat = read("ui/chat.js");
  // a plain string value is wrapped in .truncate (text-overflow needs a block, not a bare text node in a flex box)
  assert.match(chat, /typeof value === "string" \? el\("span", \{ class: "truncate" \}, value\) : value/);
  const css = read("ui/ui.css");
  assert.ok(css.includes(".chat-chip-device, .chat-chip-agent, .chat-chip-dir { flex: 0 1 auto; min-width: 48px; }"));
  assert.ok(css.includes(".chat-chip.fit { flex-shrink: 0; }") && chat.includes('deviceName().length <= 6 ? "fit" : ""'));
  // phones keep the chips at size and scroll the row sideways
  const phone = css.slice(css.indexOf("@container chat (max-width: 640px)"));
  assert.ok(phone.slice(0, phone.indexOf("\n}")).includes(".chat-chip, .chat-chip-device, .chat-chip-agent, .chat-chip-dir { flex: none;"));
});

test("a Codex session row shows its sub-agent count; the key hint is only for keyboards", () => {
  const chat = read("ui/chat.js");
  assert.match(chat, /s\.subagents > 0 \? el\("span", \{ class: "chat-sess-subagents", title: `\$\{s\.subagents\} 个子代理会话` \}, `子代理 \$\{s\.subagents\}`\) : null/);
  assert.ok(read("ui/ui.css").includes(".chat-sess-subagents {"));
  assert.ok(chat.includes('matchMedia?.("(pointer: fine)")'));
});
