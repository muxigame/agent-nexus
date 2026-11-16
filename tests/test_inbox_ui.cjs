"use strict";
// The shared inbox page (agent_nexus/static/inbox.js) and the PWA service
// worker (static/sw.js), run in a minimal DOM: decision D11, design §8.12.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const inboxSource = fs.readFileSync(path.join(STATIC, "inbox.js"), "utf8");
const workerSource = fs.readFileSync(path.join(STATIC, "sw.js"), "utf8");
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve)); };

class Element {
  constructor(tag = "div") {
    this.tag = tag; this.childNodes = []; this.parentNode = null; this.dataset = {}; this.handlers = new Map();
    this.value = ""; this.hidden = false; this.disabled = false; this.checked = false; this.className = ""; this._text = "";
  }
  get textContent() { return this._text + this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(...nodes) {
    for (let node of nodes) {
      if (!(node instanceof Element)) { const text = new Element("#text"); text._text = String(node); node = text; }
      if (node.parentNode) node.parentNode.childNodes.splice(node.parentNode.childNodes.indexOf(node), 1);
      this.childNodes.push(node); node.parentNode = this;
    }
  }
  replaceChildren(...nodes) { for (const node of this.childNodes) node.parentNode = null; this.childNodes = []; this._text = ""; this.append(...nodes); }
  setAttribute(name, value) {
    if (name === "disabled" || name === "checked" || name === "hidden") this[name] = true;
    else if (name.startsWith("data-")) this.dataset[name.slice(5)] = value;
    else this[name] = value;
  }
  addEventListener(name, callback) { if (!this.handlers.has(name)) this.handlers.set(name, []); this.handlers.get(name).push(callback); }
  fire(name) { for (const callback of this.handlers.get(name) || []) callback({ currentTarget: this, target: this, preventDefault() {} }); }
  all(predicate, found = []) { if (predicate(this)) found.push(this); for (const child of this.childNodes) child.all(predicate, found); return found; }
}

const ask = { ask_id: "q_1", message_id: "m_2", thread_id: "t_1", from: "claude-unity@host-alpha", question: "用哪个分支发版？",
  context: "", options: ["main", "release/0.2"], recommended: "release/0.2", kind: "single", allow_other: true, state: "open" };
const fixtures = {
  "GET /api/inbox/threads": { viewer: "me", unread: 2, open_asks: [ask], threads: [
    { thread_id: "t_1", subject: "发版", participants: ["claude-unity@host-alpha", "me"], messages: 2, unread: 2, open_asks: 1,
      last_at: 1700000000, last_from: "claude-unity@host-alpha", last_snippet: "用哪个分支发版？" }] },
  "GET /api/inbox/addresses": { addresses: [{ address: "me", address_display: "me" },
    { address: "claude-unity@pc-main", address_display: "claude-unity@host-alpha" }] },
  "GET /api/inbox/threads/t_1": { thread_id: "t_1", participants: ["claude-unity@host-alpha", "me"], asks: [ask], messages: [
    { id: "m_1", from: "claude-unity@host-alpha", kind: "message", priority: "normal", subject: "发版", body: "<img src=x onerror=alert(1)>", created_at: 1700000000, mine: false },
    { id: "m_2", from: "claude-unity@host-alpha", kind: "question", priority: "normal", subject: "用哪个分支发版？", body: "用哪个分支发版？", created_at: 1700000001, mine: false }] },
};

function mount(options = {}) {
  const requests = [];
  const document = { createElement: (tag) => new Element(tag) };
  const sandbox = { document, console, Date, setTimeout, clearTimeout };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(inboxSource, sandbox);
  const root = new Element("main");
  const api = async (method, url, body) => {
    requests.push({ method, url, body });
    const key = `${method} ${url.split("?")[0]}`;
    if (method === "POST") return { ok: true, thread_id: "t_1" };
    if (!(key in fixtures)) throw new Error(`unexpected ${key}`);
    return JSON.parse(JSON.stringify(fixtures[key]));
  };
  const view = sandbox.AnxInbox.mount(root, { api, base: "/api/inbox", toast() {}, ...options });
  return { sandbox, root, view, requests };
}

const plain = (value) => JSON.parse(JSON.stringify(value));
const byText = (root, text) => root.all((node) => node.tag === "button" && node.textContent === text);

test("question cards put the recommended option first, then the others, then a free answer", () => {
  const { sandbox } = mount();
  assert.deepEqual(Array.from(sandbox.AnxInbox.orderOptions({ options: ["a", "b"], recommended: "b" })), ["b", "a"]);
  assert.deepEqual(Array.from(sandbox.AnxInbox.cardEntries({ options: ["a", "b"], recommended: "b" })).map((e) => e.label), ["b", "a", "其他"]);
  assert.deepEqual(Array.from(sandbox.AnxInbox.cardEntries({ options: [] })).map((e) => e.label), ["其他"]);
});

test("thread list, thread view with the question card, answering and replying (375 px layout)", async () => {
  const { root, view, requests } = mount();
  await view.refresh();
  await flush();
  const items = root.all((node) => node.tag === "li" && node.dataset.thread);
  assert.equal(items.length, 1);
  assert.match(items[0].textContent, /发版/);
  assert.match(root.textContent, /待回答（1）/);
  // Phone width: the list and the thread are one screen at a time.
  assert.equal(view.root.className, "anx-inbox");
  items[0].fire("click");
  await flush();
  assert.equal(view.root.className, "anx-inbox has-thread");
  assert.ok(requests.some((r) => r.url === "/api/inbox/threads/t_1"));
  const card = root.all((node) => node.dataset.ask === "q_1")[0];
  const labels = card.all((node) => node.tag === "button").map((node) => node.textContent);
  assert.deepEqual(labels, ["release/0.2（推荐）", "main", "其他：提交"]);
  // Message bodies are text, never markup.
  assert.equal(root.all((node) => node.tag === "img").length, 0);
  assert.ok(root.textContent.includes("<img src=x onerror=alert(1)>"));

  byText(card, "release/0.2（推荐）")[0].fire("click");
  await flush();
  assert.deepEqual(plain(requests.find((r) => r.method === "POST")), { method: "POST", url: "/api/inbox/asks/q_1/answer", body: { choice: "release/0.2" } });

  const draft = root.all((node) => node.tag === "textarea")[0];
  draft.value = "LGTM";
  byText(root, "回复")[0].fire("click");
  await flush();
  assert.deepEqual(plain(requests.filter((r) => r.method === "POST").at(-1)), { method: "POST", url: "/api/inbox/send", body: { reply_to: "m_2", body: "LGTM" } });

  byText(root, "‹ 返回")[0].fire("click");
  assert.equal(view.root.className, "anx-inbox");
});

test("other mailboxes are read-only and the free answer posts text", async () => {
  const { root, view, requests } = mount({ threadId: "t_1" });
  await view.refresh();
  await flush();
  const input = root.all((node) => node.tag === "input" && node.type === "text")[0];
  input.value = "都不要";
  byText(root, "其他：提交")[0].fire("click");
  await flush();
  assert.deepEqual(plain(requests.find((r) => r.method === "POST").body), { text: "都不要" });
  view.state.viewer = "claude-unity@pc-main";
  await view.refresh();
  await flush();
  assert.ok(requests.some((r) => r.url === "/api/inbox/threads/t_1?viewer=claude-unity%40pc-main"));
  assert.ok(byText(root, "回复")[0].disabled);
  assert.ok(root.all((node) => node.dataset.ask === "q_1")[0].all((node) => node.tag === "button").every((b) => b.disabled));
});

test("the mobile stylesheet stacks list and thread below 700 px", () => {
  assert.match(inboxSource, /@media \(max-width:700px\)/);
  assert.match(fs.readFileSync(path.join(STATIC, "pwa.html"), "utf8"), /name="viewport" content="width=device-width/);
});

test("the service worker never caches /api/inbox responses", async () => {
  const listeners = {};
  const puts = [];
  const fetched = [];
  const cache = { put: async (request) => { puts.push(request.url); }, addAll: async () => {}, match: async () => undefined };
  const self = {
    location: { origin: "https://hub.example.test" },
    addEventListener: (name, callback) => { listeners[name] = callback; },
    skipWaiting() {}, clients: { claim() {} },
  };
  const sandbox = {
    self, URL, Promise, console,
    caches: { open: async () => cache, keys: async () => [], delete: async () => true, match: async () => undefined },
    fetch: async (request) => { fetched.push(request.url); return { ok: true, type: "basic", redirected: false, clone() { return this; } }; },
    Response: { error: () => ({ error: true }) },
  };
  vm.createContext(sandbox);
  vm.runInContext(workerSource, sandbox);
  async function dispatch(url, method = "GET") {
    let responded = null;
    listeners.fetch({ request: { url, method }, respondWith(promise) { responded = promise; } });
    if (responded) await responded;
    await flush();
    return responded !== null;
  }
  assert.equal(await dispatch("https://hub.example.test/api/inbox/threads"), false);
  assert.equal(await dispatch("https://hub.example.test/api/inbox/threads/t_1"), false);
  assert.equal(await dispatch("https://hub.example.test/api/inbox/send", "POST"), false);
  assert.equal(await dispatch("https://hub.example.test/inbox/inbox.js"), true);
  assert.deepEqual(puts, ["https://hub.example.test/inbox/inbox.js"]);
  assert.ok(puts.every((url) => !url.includes("/api/")));
});

// The viewer dropdown (design §8.2): grouped by device, most recent first, titles, legacy anon rows only with mail.
const VIEWER_ADDRESSES = [
  { address: "me", address_display: "me", name: "me", kind: "human" },
  { address: "alpha@pc-main", address_display: "alpha@host-alpha", name: "alpha", name_source: "explicit", agent_id: "pc-main",
    device_display: "host-alpha", workdir: "C:\\work\\unity", last_seen_s_ago: 4000, unread: 0 },
  { address: "claude-api@pc-main", address_display: "claude-api@host-alpha", name: "claude-api", name_source: "auto",
    agent_id: "pc-main", device_display: "host-alpha", workdir: "/srv/api", title: "修复登录 bug", last_seen_s_ago: 30, unread: 2 },
  { address: "codex-web@laptop", address_display: "codex-web@laptop", name: "codex-web", name_source: "auto",
    agent_id: "laptop", device_display: "laptop", workdir: "/home/u/web", last_seen_s_ago: 300, unread: 0 },
  { address: "anon-0fa510@pc-main", address_display: "anon-0fa510@host-alpha", name: "anon-0fa510", name_source: "anon",
    agent_id: "pc-main", device_display: "host-alpha", last_seen_s_ago: 10, unread: 0 },
  { address: "anon-1b2c3d@pc-main", address_display: "anon-1b2c3d@host-alpha", name: "anon-1b2c3d",
    agent_id: "pc-main", device_display: "host-alpha", last_seen_s_ago: 90000, unread: 1 },
  // Read mail only: still listed, or the owner could not open that mailbox any more.
  { address: "anon-2c3d4e@pc-main", address_display: "anon-2c3d4e@host-alpha", name: "anon-2c3d4e", name_source: "anon",
    agent_id: "pc-main", device_display: "host-alpha", last_seen_s_ago: 200000, unread: 0, mail: 1 },
];
const EXPECTED_GROUPS = [
  { label: "host-alpha", items: [
    { value: "claude-api@pc-main", text: "修复登录 bug — claude-api · 刚刚 · 2 未读（只读）", title: "claude-api@host-alpha" },
    { value: "alpha@pc-main", text: "unity — alpha · 1 小时前（只读）", title: "alpha@host-alpha" },
    { value: "anon-1b2c3d@pc-main", text: "anon-1b2c3d · 1 天前 · 1 未读（只读）", title: "anon-1b2c3d@host-alpha" },
    { value: "anon-2c3d4e@pc-main", text: "anon-2c3d4e · 2 天前（只读）", title: "anon-2c3d4e@host-alpha" }] },
  { label: "laptop", items: [{ value: "codex-web@laptop", text: "web — codex-web · 5 分钟前（只读）", title: "codex-web@laptop" }] },
];

test("the viewer dropdown groups addresses by device with titles (classic inbox)", async () => {
  const { sandbox } = mount();
  assert.deepEqual(plain(sandbox.AnxInbox.viewerGroups(VIEWER_ADDRESSES, "me")), EXPECTED_GROUPS);
  // The address being viewed stays listed even when it is a legacy row without mail.
  const kept = plain(sandbox.AnxInbox.viewerGroups(VIEWER_ADDRESSES, "anon-0fa510@pc-main"));
  assert.ok(kept[0].items.some((item) => item.value === "anon-0fa510@pc-main"));
  fixtures["GET /api/inbox/addresses"] = { addresses: VIEWER_ADDRESSES };
  try {
    const { root, view } = mount();
    await view.refresh();
    await flush();
    const groups = root.all((node) => node.tag === "optgroup");
    assert.deepEqual(groups.map((node) => node.label), ["host-alpha", "laptop"]);
    const options = root.all((node) => node.tag === "option").map((node) => node.textContent);
    assert.equal(options[0], "me（我）");
    assert.ok(options.every((text) => text === "me（我）" || text.endsWith("（只读）")));
    assert.ok(!options.some((text) => text.startsWith("anon-0fa510")));
  } finally {
    fixtures["GET /api/inbox/addresses"] = { addresses: [{ address: "me", address_display: "me" },
      { address: "claude-unity@pc-main", address_display: "claude-unity@host-alpha" }] };
  }
});

test("the design-system inbox builds the same viewer groups", async () => {
  globalThis.document = globalThis.document || { createElement: () => ({}) };
  const url = require("node:url").pathToFileURL(path.join(STATIC, "ui", "inbox.js")).href;
  const module = await import(url);
  assert.deepEqual(plain(module.viewerGroups(VIEWER_ADDRESSES, "me")), EXPECTED_GROUPS);
  assert.equal(module.viewerLabel({ address: "x@d", name: "x" }), "x（只读）");
});
