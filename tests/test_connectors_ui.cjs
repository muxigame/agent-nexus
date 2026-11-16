"use strict";
// The console's connectors page (docs/ui-spec.md §3.4): static checks. The page itself is
// driven end to end by the Playwright smoke (scripts/console_screens.py).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const read = (rel) => fs.readFileSync(path.join(STATIC, rel), "utf8");

test("the connectors route is a real page, not the placeholder", () => {
  const app = read("console/app.js");
  assert.match(app, /import \{ connectorsPage \} from "\/console\/connectors\.js"/);
  assert.match(app, /"\/connectors": \(route\) => connectorsPage\(/);
});

test("the connectors page only uses local modules and write-only secret fields", () => {
  const page = read("console/connectors.js");
  for (const match of page.matchAll(/from "([^"]+)"/g)) assert.ok(match[1].startsWith("/ui/"), match[1]);
  assert.ok(!/https?:\/\/(?!mcp\.cloudflare\.com|docs\.mcp\.cloudflare\.com|openapi-mcp\.cn-hangzhou\.aliyuncs\.com|mcp\.example\.com)[a-z]/i.test(page.replace(/"https:\/\/<[^"]*"/g, "")));
  for (const id of ["c-token", "c-client-secret"]) assert.match(page, new RegExp(`id: "${id}", type: "password"`));
  assert.match(page, /"Header 值", placeholder|type: "password", "aria-label": "Header 值"/);
  for (const word of ["添加连接器", "探测", "测试连接", "保存并连接", "登录", "退出登录", "即将推出", "已设置，留空保持不变"]) assert.ok(page.includes(word), word);
  for (const endpoint of ["/api/connectors/probe", "/api/connectors", "/login", "/test", "/logout"]) assert.ok(page.includes(endpoint), endpoint);
});

test("the status vocabulary covers every connector state", () => {
  const ui = read("ui/ui.js");
  for (const state of ["ok", "needs_login", "error", "testing", "disabled", "connecting"]) assert.ok(ui.includes(` ${state}: [`), state);
});
