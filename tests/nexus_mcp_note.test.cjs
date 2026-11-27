"use strict";
// The chat's 本机 MCP note (ui/chat.js nexusMcpNote): only when nothing can attach, with the reason.
const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const load = () => import(pathToFileURL(path.join(__dirname, "../agent_nexus/static/ui/chat.js")).href);

test("no note when a connector attaches or the answer is unknown", async () => {
  const { nexusMcpNote } = await load();
  for (const state of ["local", "gateway_config", "gateway_account", "both", "unknown"]) {
    assert.equal(nexusMcpNote({ nexus_mcp: { state, local_state: "ready" } }), null, state);
  }
  assert.equal(nexusMcpNote({ nexus_mcp: { state: "none", attach: "flags", local_state: "ready" } }), null);
  assert.equal(nexusMcpNote({ nexus_mcp: { state: "gateway_account", local_state: "off" } }), null);
  assert.equal(nexusMcpNote(null), null);
  assert.equal(nexusMcpNote({ gateway_mcp: true }), null);
});

test("a chip with the reason when the 本机 MCP cannot work", async () => {
  const { nexusMcpNote } = await load();
  const cases = { off: "本机 MCP 已关闭", not_joined: "未加入网关", offline: "网关未连接", revoked: "已被吊销",
    locked: "已锁定", gateway_old: "网关版本过旧", device_access_off: "网关关闭了设备接入",
    rejected: "凭据不被网关接受" };
  for (const [local, text] of Object.entries(cases)) {
    const note = nexusMcpNote({ nexus_mcp: { state: "none", local_state: local, local_reason: "原因" } });
    assert.equal(note.text, `Agent Nexus MCP：${text}`);
    assert.equal(note.down, text);
    assert.match(note.title, /本机控制台 → 设置 → 本机 MCP/);
  }
  // a CLI that cannot take the connector at all (a custom text CLI without {mcp_url}, an old Codex)
  assert.equal(nexusMcpNote({ nexus_mcp: { state: "unsupported", attach: null, local_state: "ready" } }).down,
    "这个 CLI 接不了");
  assert.equal(nexusMcpNote({ nexus_mcp: { state: "name_taken", local_state: "ready" } }), null);
  // an older device without nexus_mcp: its config check
  assert.equal(nexusMcpNote({ gateway_mcp: false }).text, "未检测到网关连接器");
});

test("tokenCount keeps its doc comment", async () => {
  const fs = require("node:fs");
  const text = fs.readFileSync(path.join(__dirname, "../agent_nexus/static/ui/chat.js"), "utf8");
  assert.match(text, /\*\/\nexport function tokenCount\(/);
});
