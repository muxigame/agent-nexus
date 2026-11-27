"use strict";
// 本机 · 设置 → 本机 MCP (static/local/settings.js mcpCard): static checks; the card is driven end to end by the
// console smoke.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const page = fs.readFileSync(path.join(__dirname, "../agent_nexus/static/local/settings.js"), "utf8");

test("我已连接 is offered whenever the account may hold the cloud connector, not only when unknown", () => {
  const states = JSON.parse(page.match(/const MARK_CONNECTED_STATES = (\[[^\]]*\])/)[1]);
  for (const state of ["none", "local", "unknown"]) assert.ok(states.includes(state), state);
  for (const state of ["gateway_config", "gateway_account", "both"]) assert.ok(!states.includes(state), state);
  assert.match(page, /markable\(cli\)\s*\? el\("button"[^\n]*"mark_connected"/);
});

test("a switch toasts success only when the PUT succeeded, and the CLI rows are fetched again", () => {
  assert.match(page, /if \(await put\(\{ \[key\]: on \}\)\) toast\(/);
  assert.match(page, /return false;/);
  assert.match(page, /setTimeout\(refresh, \d+\)/);
});
