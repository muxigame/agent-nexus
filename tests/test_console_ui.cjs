"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../agent_nexus/static/classic.html"), "utf8");
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const service = {
  name: "demo", kind: "managed", status: "running", port: 8399, pid: 123, listener_pid: null,
  description: "test service", health: {}, health_path: "", health_interval: 2, uptime_s: 1,
  enabled: true, supported: true, autostart: true, autorestart: true, crashes: 0,
  dir: "fixture", cwd: "fixture", command: ["fixture"], env_keys: [], platforms: ["win32"],
  source: "manifest", logs: [{ id: "stdout", label: "stdout" }, { id: "1", label: "other" }]
};
const manager = { root: "isolated-fixture", platform: "win32", pid: 999, mcpm: "test" };
const flush = () => new Promise((resolve) => setImmediate(resolve));

function browser(storage = new Map()) {
  const listeners = new Map();
  const documentListeners = new Map();
  const timers = new Map();
  const requests = [];
  const streams = [];
  let nextTimer = 1;
  let document;
  class Element {
    constructor(tag = "div") {
      this.tag = tag; this.childNodes = []; this.parentNode = null; this.dataset = {};
      this.handlers = new Map(); this.value = ""; this.scrollTop = 0; this.clientHeight = 200;
      this.hidden = false; this.disabled = false; this.style = {}; this._text = "";
    }
    get firstChild() { return this.childNodes[0] || null; }
    get scrollHeight() { return this.childNodes.length * 16; }
    get textContent() { return this._text + this.childNodes.map((node) => node.textContent).join(""); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    append(...nodes) {
      for (let node of nodes) {
        if (!(node instanceof Element)) { const text = new Element("#text"); text._text = String(node); node = text; }
        if (node.tag === "#fragment") { this.append(...Array.from(node.childNodes)); continue; }
        node.remove(); this.childNodes.push(node); node.parentNode = this;
      }
    }
    replaceChildren(...nodes) {
      for (const node of this.childNodes) node.parentNode = null;
      this.childNodes = []; this._text = ""; this.append(...nodes);
    }
    remove() {
      if (!this.parentNode) return;
      const index = this.parentNode.childNodes.indexOf(this);
      if (index >= 0) this.parentNode.childNodes.splice(index, 1);
      this.parentNode = null;
    }
    setAttribute(name, value) {
      if (name === "value") this.value = value;
      else if (name === "disabled" || name === "checked" || name === "hidden") this[name] = true;
      else if (name.startsWith("data-")) this.dataset[name.slice(5)] = value;
      else this[name] = value;
    }
    addEventListener(name, callback) {
      if (!this.handlers.has(name)) this.handlers.set(name, []);
      this.handlers.get(name).push(callback);
    }
    fire(name, properties = {}) {
      const event = { currentTarget: this, target: this, preventDefault() {}, stopPropagation() {}, ...properties };
      for (const callback of this.handlers.get(name) || []) callback(event);
    }
    contains(node) { return node === this || this.childNodes.some((child) => child.contains(node)); }
    blur() { document.activeElement = null; }
    getBoundingClientRect() { return { left: 0, bottom: 0 }; }
  }
  const nodes = new Map(["#main", "#tabs", "#meta", "#offline", "#update", "#conn", "#menu", "#rescan", "#toasts"].map((key) => [key, new Element()]));
  document = {
    hidden: false, activeElement: null,
    querySelector: (selector) => nodes.get(selector),
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element("#fragment"),
    addEventListener(name, callback) {
      if (!documentListeners.has(name)) documentListeners.set(name, []);
      documentListeners.get(name).push(callback);
    }
  };
  class EventSource {
    constructor(url) { this.url = url; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
    send(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
  }
  const sandbox = {
    document, Node: Element, EventSource, AbortController, console, innerWidth: 1200,
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} }, confirm: () => true,
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key)
    },
    setTimeout(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      // Deliberately permit a late completion after AbortSignal so the tests
      // prove identity guards, rather than relying on this mock to hide races.
      return new Promise((resolve, reject) => requests.push({
        url, options, reject,
        complete(data = { services: [service], manager }) {
          resolve({ ok: true, json: async () => data });
        }
      }));
    },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    }
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(script, context);
  return {
    context, storage, nodes, requests, streams, timers, document,
    eval: (code) => vm.runInContext(code, context),
    event(name) { for (const callback of listeners.get(name) || []) callback(); },
    visibility(hidden) {
      document.hidden = hidden;
      for (const callback of documentListeners.get("visibilitychange") || []) callback();
    },
    tick() {
      const entry = Array.from(timers.entries()).find(([, timer]) => timer.delay === 2000);
      assert.ok(entry, "refresh timer exists");
      timers.delete(entry[0]); entry[1].callback();
    }
  };
}

async function serviceBrowser() {
  const b = browser(new Map([["mcpm.tabs", '["demo"]'], ["mcpm.active", '"demo"']]));
  b.requests[0].complete();
  await flush();
  assert.equal(b.streams.length, 1);
  return b;
}

function assertLogMatches(b) {
  const snapshot = JSON.parse(b.eval("JSON.stringify({lines:view.lines,characters:view.characters,filter:view.filter,dom:view.pre.childNodes.map(n=>n.textContent)})"));
  assert.ok(snapshot.lines.length <= 5000);
  assert.ok(snapshot.characters <= 2 * 1024 * 1024);
  assert.equal(snapshot.characters, snapshot.lines.reduce((sum, line) => sum + line.length + 1, 0));
  assert.deepEqual(snapshot.dom, snapshot.lines.filter((line) => !snapshot.filter || line.toLowerCase().includes(snapshot.filter)).map((line) => line + "\n"));
  return snapshot;
}

test("visible/pageshow events share one refresh chain; hiding invalidates a late response", async () => {
  const b = browser();
  b.event("pageshow"); b.visibility(false); b.event("pageshow");
  assert.equal(b.requests.length, 1);
  b.visibility(true);
  assert.equal(b.requests[0].options.signal.aborted, true);
  b.requests[0].complete();
  await flush();
  assert.equal(b.eval("state.manager"), null);
  assert.equal(b.timers.size, 0);
  assert.equal(b.streams.length, 0);
});

test("native hiding pauses WebView2 even when document.hidden stays false", async () => {
  const b = await serviceBrowser();
  b.tick();
  assert.equal(b.requests.length, 2);
  b.eval("window.__mcpmNativeVisible = false");
  b.event("mcpmvisibilitychange");
  assert.equal(b.document.hidden, false);
  assert.equal(b.requests[1].options.signal.aborted, true);
  assert.equal(b.streams[0].closed, true);
  b.event("pageshow"); b.visibility(false); b.event("mcpmvisibilitychange");
  b.requests[1].complete();
  await flush();
  assert.equal(b.requests.length, 2);
  assert.equal(b.timers.size, 0);
  b.visibility(true);
  b.eval("window.__mcpmNativeVisible = true");
  b.event("mcpmvisibilitychange");
  assert.equal(b.requests.length, 2);
  b.visibility(false);
  b.event("mcpmvisibilitychange"); b.event("pageshow");
  assert.equal(b.requests.length, 3);
  assert.equal(b.streams.length, 2);
  assert.equal(b.streams[1].closed, false);
});

test("hide/show prevents old success and finally from clobbering the new request", async () => {
  const b = browser();
  b.visibility(true); b.visibility(false); b.event("pageshow");
  assert.equal(b.requests.length, 2);
  b.requests[0].complete({ services: [], manager: { ...manager, pid: 1 } });
  await flush();
  assert.equal(b.eval("state.manager"), null);
  assert.equal(b.eval("refreshRequest !== null"), true);
  assert.equal(b.timers.size, 0);
  b.requests[1].complete({ services: [], manager: { ...manager, pid: 2 } });
  await flush();
  assert.equal(b.eval("state.manager.pid"), 2);
  assert.equal(b.timers.size, 1);
});

test("a failed refresh retries without overlapping pending requests", async () => {
  const b = browser();
  b.eval("refresh(); refresh()");
  assert.equal(b.requests.length, 1);
  b.requests[0].reject(new Error("offline"));
  await flush();
  assert.equal(b.nodes.get("#offline").hidden, false);
  b.tick();
  b.eval("refresh()");
  assert.equal(b.requests.length, 2);
  b.requests[1].complete();
  await flush();
  assert.equal(b.nodes.get("#offline").hidden, true);
  assert.equal(b.timers.size, 1);
});

test("pagehide closes SSE; resume keeps one stream and reset replaces the snapshot", async () => {
  const b = await serviceBrowser();
  const first = b.streams[0];
  const stale = first.onmessage;
  first.send({ reset: true, text: "one\ntwo\n" });
  b.event("pagehide");
  assert.equal(first.closed, true);
  assert.equal(b.eval("view.es"), null);
  b.event("pageshow"); b.event("pageshow"); b.visibility(false);
  assert.equal(b.streams.length, 2);
  b.streams[1].send({ reset: true, text: "one\ntwo\n" });
  stale({ data: JSON.stringify({ text: "stale\n" }) });
  assert.deepEqual(assertLogMatches(b).lines, ["one", "two"]);
  b.requests[1].complete(); await flush();
  assert.equal(b.streams.length, 2);
});

test("switching log source rejects queued callbacks from the old source", async () => {
  const b = await serviceBrowser();
  const stale = b.streams[0].onmessage;
  b.eval("view.select.value = '1'; view.select.fire('change')");
  assert.equal(b.streams[0].closed, true);
  b.streams[1].send({ reset: true, text: "other source\n" });
  stale({ data: JSON.stringify({ text: "old source\n" }) });
  assert.deepEqual(assertLogMatches(b).lines, ["other source"]);
});

for (const oldCount of [0, 4999]) {
  test("a 10000-line batch keeps only its newest bounded suffix after " + oldCount + " old lines", async () => {
    const b = await serviceBrowser();
    const es = b.streams[0];
    es.send({ reset: true, text: Array.from({ length: oldCount }, (_, i) => "old " + i).join("\n") });
    es.send({ text: Array.from({ length: 10000 }, (_, i) => "new " + i).join("\n") });
    const snapshot = assertLogMatches(b);
    assert.equal(snapshot.lines.length, 5000);
    assert.equal(snapshot.lines[0], "new 5000");
    assert.equal(snapshot.lines.at(-1), "new 9999");
  });
}

test("single-line and total-character bounds also bound retained DOM", async () => {
  const b = await serviceBrowser();
  b.streams[0].send({ reset: true, text: "a".repeat(1_000_000) });
  let snapshot = assertLogMatches(b);
  assert.equal(snapshot.lines[0].length, 32768);
  assert.match(snapshot.lines[0], /原始日志保留/);
  b.streams[0].send({ text: Array.from({ length: 100 }, (_, i) => String(i).padStart(3, "0") + "x".repeat(32765)).join("\n") });
  snapshot = assertLogMatches(b);
  assert.ok(snapshot.lines.length < 100);
  assert.ok(snapshot.lines.at(-1).startsWith("099"));
});

test("filtered DOM stays aligned when character trimming removes old and incoming lines", async () => {
  const b = await serviceBrowser();
  b.eval("view.filter = 'error'; redraw(view)");
  const text = Array.from({ length: 6000 }, (_, i) => (i % 2 ? "info " : "error ") + i + "x".repeat(1000)).join("\n");
  b.streams[0].send({ reset: true, text });
  assertLogMatches(b);
  b.streams[0].send({ text });
  assertLogMatches(b);
});

test("pause freezes display while bounded collection continues; resume redraws current model", async () => {
  const b = await serviceBrowser();
  b.streams[0].send({ reset: true, text: "initial\n" });
  b.eval("view.pause.fire('click')");
  b.streams[0].send({ text: Array.from({ length: 6000 }, (_, i) => "later " + i).join("\n") });
  assert.equal(b.eval("view.pre.textContent"), "initial\n");
  assert.equal(b.streams[0].closed, false);
  assert.equal(b.eval("view.lines.length"), 5000);
  b.eval("view.pause.fire('click')");
  assertLogMatches(b);
});

test("port draft, filter and pause survive UI recreation without persisting log text", async () => {
  const b = await serviceBrowser();
  b.context.fixtureService = service;
  b.eval("globalThis.port = portEditor(fixtureService).childNodes[0]; port.value='8401'; port.fire('input'); view.filterInput.value='WARNING'; view.filterInput.fire('input'); view.pause.fire('click')");
  b.streams[0].send({ reset: true, text: "private synthetic log content\n" });
  b.event("pagehide");
  const c = browser(b.storage);
  c.requests[0].complete(); await flush();
  c.context.fixtureService = service;
  assert.equal(c.eval("portEditor(fixtureService).childNodes[0].value"), "8401");
  assert.equal(c.eval("view.filter"), "warning");
  assert.equal(c.eval("view.follow"), false);
  assert.ok(!Array.from(b.storage.values()).some((value) => value.includes("private synthetic log content")));
});

test("empty port drafts survive refresh and Escape removes them", async () => {
  const b = await serviceBrowser();
  b.context.fixtureService = service;
  b.eval("globalThis.port = portEditor(fixtureService).childNodes[0]; port.value=''; port.fire('input')");
  assert.equal(b.eval("portEditor(fixtureService).childNodes[0].value"), "");
  b.eval("port.fire('keydown', {key:'Escape'})");
  assert.equal(b.eval("portEditor(fixtureService).childNodes[0].value"), "8399");
});

test("the inbox is a fixed tab and #/inbox/<thread> opens it", async () => {
  const b = browser();
  b.requests[0].complete(); await flush();
  assert.match(b.nodes.get("#tabs").textContent, /总览收件箱/);
  b.eval("location.hash = '#/inbox/t_42'; takeHashTab()");
  assert.equal(b.eval("state.active"), "inbox");
  assert.equal(b.eval("state.inboxThread"), "t_42");
  // inbox.js is a separate file; without it the tab says so instead of failing.
  assert.match(b.nodes.get("#main").textContent, /收件箱脚本没有加载/);
  b.eval("activate('overview')");
  assert.equal(b.nodes.get("#main").dataset.view, "overview");
});

test("a known desktop update shows a banner whose button asks the shell to install it", async () => {
  const b = browser();
  const update = { available: true, version: "0.2.1", current: "0.2.0" };
  b.requests[0].complete({ services: [service], manager: { ...manager, app_update: update } });
  await flush();
  const bar = b.nodes.get("#update");
  assert.equal(bar.hidden, false);
  assert.match(bar.textContent, /Agent Nexus v0\.2\.1 已可用（当前 v0\.2\.0）/);
  assert.match(bar.textContent, /更新到 v0\.2\.1/);
  const button = bar.childNodes[1];
  button.fire("click");
  const apply = b.requests.at(-1);
  assert.equal(apply.url, "/api/app/update/apply");
  assert.equal(apply.options.method, "POST");
  // Without an update (or after installing it) the banner goes away.
  b.tick();
  b.requests.at(-1).complete({ services: [service], manager: { ...manager, app_update: { available: false } } });
  await flush();
  assert.equal(bar.hidden, true);
});
