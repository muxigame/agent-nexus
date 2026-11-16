"use strict";
// The local console's plugins page and the schema form (docs/ui-spec.md §4.2, design §6.6).
// schema-form.js runs here in a minimal DOM; the page itself is driven end to end by
// tests/test_plugins_smoke.py (Playwright + Edge).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const read = (rel) => fs.readFileSync(path.join(STATIC, rel), "utf8");

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null; this.attributes = {};
    this.dataset = {}; this.style = {}; this.handlers = {}; this.value = ""; this.checked = false; this.disabled = false;
    this.hidden = false; this.className = ""; this._text = "";
  }
  get firstElementChild() { return this.childNodes[0] || null; }
  set innerHTML(html) { this.childNodes = []; const svg = new Node("svg"); svg._text = ""; this.append(svg); this.content = this; }
  get textContent() { return this._text + this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = []; this._text = String(v); }
  append(...nodes) { for (const n of nodes) { const node = typeof n === "string" ? Object.assign(new Node("#text"), { _text: n }) : n; node.parentNode = this; this.childNodes.push(node); } }
  replaceChildren(...nodes) { this.childNodes = []; this._text = ""; this.append(...nodes); }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "for") this.htmlFor = v; if (k === "id") this.id = v; if (["type", "placeholder", "min", "max", "step", "role"].includes(k)) this[k] = v; }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  click() { for (const fn of this.handlers.click || []) fn({ currentTarget: this, target: this, preventDefault() {} }); }
  all(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.childNodes) c.all(pred, out); return out; }
  querySelector(sel) { return this.all((n) => n !== this && n.tagName === sel.toUpperCase())[0] || null; }
  querySelectorAll(sel) { return this.all((n) => n !== this && n.tagName === sel.toUpperCase()); }
}
global.document = { createElement: (t) => new Node(t), createTextNode: (t) => Object.assign(new Node("#text"), { _text: String(t) }) };
global.Node = Node;

async function load() {
  return import(pathToFileURL(path.join(STATIC, "ui/schema-form.js")).href);
}

const SCHEMA = {
  type: "object", required: ["team_id"],
  properties: {
    base_url: { type: "string", format: "uri", title: "实例地址", default: "https://ones.example" },
    team_id: { type: "string", title: "团队 ID" },
    profile: { type: "string", enum: ["official", "full"], default: "official", title: "工具集" },
    data_dir: { type: "string", format: "path", title: "数据目录" },
    timeout: { type: "number", minimum: 1, title: "超时", "x-anx-advanced": true },
    verbose: { type: "boolean", title: "详细日志" },
  },
};

test("groups, select, folder, number and switch fields render and collect typed values", async () => {
  const { schemaForm } = await load();
  const form = schemaForm({ schema: SCHEMA, values: { team_id: "T1", profile: "full" },
    ui: { groups: [{ title: "常用", fields: ["base_url", "team_id", "profile", "data_dir", "verbose"] }, { title: "高级", fields: ["timeout"], collapsed: true }] },
    secrets: [{ name: "token", title: "Token", required: true, set: false }] });
  const details = form.root.all((n) => n.tagName === "DETAILS");
  assert.equal(details.length, 1, "the collapsed group is a <details>");
  assert.ok(details[0].textContent.includes("超时"));
  assert.equal(form.fields.profile.input.tagName, "SELECT");
  assert.equal(form.fields.profile.input.value, "full");
  assert.ok(form.fields.data_dir.node.textContent.includes("数据目录"));
  assert.ok(form.fields.data_dir.node.all((n) => n.className === "input-icon").length, "a folder field has the folder icon");
  form.fields.timeout.input.value = "0";
  form.fields.verbose.input.checked = true;
  const collected = form.collect();
  assert.deepEqual(collected.errors, [{ path: "timeout", message: "不能小于 1" }]);
  assert.equal(collected.values.verbose, true);
  assert.equal(collected.values.team_id, "T1");
  assert.equal(collected.values.base_url, null, "an empty field with a default means: use the default");
  assert.deepEqual(collected.secrets, {}, "an untouched secret is not sent");
});

test("secret fields are password inputs that never show a value", async () => {
  const { schemaForm } = await load();
  const form = schemaForm({ schema: { type: "object", properties: {} },
    secrets: [{ name: "token", title: "Token", required: true, set: true, updated_at: 1700000000 }, { name: "cookie", title: "Cookie", set: false }] });
  const [token, cookie] = form.secretFields;
  assert.equal(token.input.type, "password");
  assert.equal(token.input.value, "");
  assert.match(token.input.placeholder, /已设置/);
  cookie.input.value = " abc ";
  assert.deepEqual(form.collect().secrets, { cookie: "abc" });
  const clear = token.node.all((n) => n.tagName === "BUTTON")[0];
  clear.click();
  assert.deepEqual(form.collect().secrets, { token: null, cookie: "abc" }, "清除 sends null");
  form.setErrors([{ path: "token", message: "必填" }]);
  assert.equal(token.error.hidden, false);
});

test("the plugins page uses local modules, the plugin API and the status vocabulary", () => {
  const page = read("local/plugins.js");
  for (const match of page.matchAll(/from "([^"]+)"/g)) assert.ok(match[1].startsWith("/ui/"), match[1]);
  for (const endpoint of ["/api/plugins", "/api/plugins/install", "/api/plugins/inspect", "/api/plugins/uploads", "/api/plugins/jobs/",
    "/api/plugins/enable", "/api/plugins/suggestions", "/config", "/logs/stream"]) assert.ok(page.includes(endpoint), endpoint);
  for (const word of ["安装插件", "本地文件夹", "Git 地址", "压缩包", "安装后立即启用", "链接而不是复制", "全部启用", "需要处理", "卸载", "同时删除数据和凭据", "not_installed"]) {
    assert.ok(page.includes(word), word);
  }
  const ui = read("ui/ui.js");
  for (const state of ["needs_config", "not_installed", "running", "stopped", "disabled", "crashed", "invalid"]) assert.ok(ui.includes(` ${state}: [`), state);
  const app = read("local/app.js");
  for (const route of ['"/plugins/:name?"', '"/cloud"', '"/inbox/:thread?"', '"/settings"']) assert.ok(app.includes(route), route);
  assert.match(app, /__mcpmNativeVisible/, "polling pauses while the Tauri window is hidden");
});
