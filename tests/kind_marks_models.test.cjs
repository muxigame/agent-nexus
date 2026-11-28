"use strict";
// The CLI marks (ui/kind-icons.js, used by kindMark in ui/agent-admin.js and ui/chat.js) and the chat's model chip
// helpers (ui/chat.js, docs/agent-chat.md §16.7).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const UI = path.join(__dirname, "../agent_nexus/static/ui");
const load = (name) => import(pathToFileURL(path.join(UI, name)).href);

test("every CLI kind, API kinds and unknown kinds get a mark", async () => {
  const { kindGlyph, kindSvg, KIND_MARKS } = await load("kind-icons.js");
  for (const kind of ["claude", "codex", "dsh", "gemini", "api:x/y", "mycli", "", undefined]) {
    const glyph = kindGlyph(kind);
    assert.ok(glyph.body && glyph.body.length > 20, String(kind));
    const svg = kindSvg(kind, { size: 20 });
    assert.ok(svg.startsWith("<svg") && svg.includes('width="20"') && svg.includes('aria-hidden="true"'), String(kind));
    assert.ok(!/<script|\son\w+=/i.test(svg), String(kind));
  }
  for (const kind of ["claude", "codex", "dsh", "gemini"]) {
    assert.equal(kindGlyph(kind).key, kind);
    assert.ok(KIND_MARKS[kind].label);
  }
  assert.notEqual(kindGlyph("claude").body, kindGlyph("codex").body);
  assert.equal(kindGlyph("mycli").key, "custom");
  assert.equal(kindGlyph("api:deepseek/deepseek-chat").key, "api");
  assert.equal(kindGlyph("api:deepseek/deepseek-chat").vendor, "dsh");
  assert.equal(kindGlyph("api:google/gemini-2.5-pro").vendor, "gemini");
  assert.equal(kindGlyph("api:anthropic/claude-x").vendor, "anthropic");
  assert.equal(kindGlyph("api:openrouter/foo").vendor, "");
});

test("kindMark draws the mark everywhere a kind is shown; the brand marks are credited", () => {
  const admin = fs.readFileSync(path.join(UI, "agent-admin.js"), "utf8");
  assert.match(admin, /import \{ kindGlyph, kindSvg \} from "\.\/kind-icons\.js"/);
  assert.match(admin, /kindSvg\(kind, \{ size: px \}\)/);
  const chat = fs.readFileSync(path.join(UI, "chat.js"), "utf8");
  assert.match(chat, /kindMark as baseMark/);
  assert.match(chat, /function kindMark\(kind, opts = \{\}\) \{ return baseMark\(kind, opts\); \}/);
  assert.match(chat, /kindSvg\(key === "api" \? "api:" : key, \{ size: 12 \}\)/, "the session tabs carry the mark");
  assert.match(chat, /kindMark\(S\.kind, \{ size: "xs" \}\)/, "the Agent chip carries the mark");
  const css = fs.readFileSync(path.join(UI, "ui.css"), "utf8");
  for (const kind of ["claude", "codex", "dsh", "gemini", "custom", "api"]) assert.ok(css.includes(`.aa-mark[data-kind="${kind}"]`), kind);
  const tokens = fs.readFileSync(path.join(UI, "tokens.css"), "utf8");
  assert.equal((tokens.match(/--kind-claude:/g) || []).length, 3, "light and both dark blocks");
  const icons = fs.readFileSync(path.join(UI, "kind-icons.js"), "utf8");
  assert.match(icons, /Simple Icons v16\.37\.0/);
  assert.ok(fs.readFileSync(path.join(UI, "LICENSE-simple-icons.txt"), "utf8").includes("CC0 1.0 Universal"));
  const notices = fs.readFileSync(path.join(__dirname, "../THIRD_PARTY_NOTICES.md"), "utf8");
  assert.match(notices, /Simple Icons/);
});

test("the model chip: shown only when the device says the CLI can choose one", async () => {
  const { modelChoosable, modelLabel, modelMenuItems, MODEL_RE } = await load("chat.js");
  assert.equal(modelChoosable({ models: { flag: true } }, "claude"), true);
  assert.equal(modelChoosable({ models: { flag: false, protocol: true } }, "dsh"), true);
  assert.equal(modelChoosable({ models: { flag: false, protocol: false } }, "mycli"), false);
  assert.equal(modelChoosable({}, "claude"), false, "an older device: no chip");
  assert.equal(modelChoosable({ models: { flag: true } }, "api:deepseek/x"), false, "an API kind is its model");
  assert.equal(modelLabel(""), "默认模型");
  assert.equal(modelLabel("", { default: "gpt-6.1-sol" }), "默认模型（gpt-6.1-sol）");
  assert.equal(modelLabel("opus", { models: [{ id: "opus", label: "Opus" }] }), "Opus");
  assert.equal(modelLabel("my-model"), "my-model");
  const items = modelMenuItems({ models: [{ id: "opus", label: "Opus", desc: "更强" }], default: "sonnet" }, "custom-1", { cli: "Claude", last: "haiku" });
  assert.deepEqual(items.map((i) => i.id), ["", "opus", "custom-1", "haiku"]);
  assert.equal(items[0].desc, "使用 Claude 自己的设置（sonnet）");
  assert.equal(items.find((i) => i.checked).id, "custom-1");
  // the alias is not repeated under its capitalised label; the session's last model is marked where it is listed
  const listed = modelMenuItems({ models: [{ id: "sonnet", label: "Sonnet", desc: "日常编码" }, { id: "opus", label: "Opus" }] }, "sonnet", { last: "opus" });
  assert.equal(listed.find((i) => i.id === "sonnet").desc, "日常编码");
  assert.equal(listed.find((i) => i.id === "opus").desc, "这个会话上次用的");
  assert.equal(listed.length, 3, "no separate entry for a listed last model");
  for (const ok of ["opus", "gpt-5.6-sol", "gemini-2.5-pro", "sonnet[1m]", "deepseek/deepseek-v4", "a@b+c=d"]) assert.ok(MODEL_RE.test(ok), ok);
  for (const bad of ["-m", "a b", "x\"y", "%PATH%", "a!b", "", "x".repeat(101)]) assert.ok(!MODEL_RE.test(bad), bad);
});

test("the chat sends the model with the run and remembers it per CLI", () => {
  const chat = fs.readFileSync(path.join(UI, "chat.js"), "utf8");
  assert.match(chat, /model: model \|\| undefined,/, "agent_run_open carries the model");
  assert.match(chat, /sendParams\.model = model/, "agent_run_send carries a changed model");
  assert.match(chat, /store\.write\(\{ modelByKind: S\.modelByKind \}\)/);
  assert.match(chat, /function modelKey\(kind\) \{ return `\$\{S\.device \?\? ""\}:\$\{kind\}`; \}/, "model lists are per device");
  assert.doesNotMatch(chat, /S\.lastModel = ev\.model/, "an ACP start-of-session report is not the session's last model");
  assert.match(chat, /call\("agent_models", \{ kind/);
  assert.match(chat, /case "model": \{/);
  const fake = fs.readFileSync(path.join(__dirname, "../docs/mockups/agent-fake-api.js"), "utf8");
  assert.match(fake, /agent_models: /);
  assert.match(fake, /kind: "drive"/, "the mockup's Windows device lists its drives");
});
