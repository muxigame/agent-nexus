"use strict";
// The gateway console's pure modules (docs/ui-spec.md §3, design §11.5): the QR encoder
// against matrices from the npm package qrcode 1.5.4, invite decoding, question-card order,
// and static checks on the console pages. The full pages are covered by the Playwright
// smoke (scripts/console_screens.py, tests/test_console_smoke.py).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const load = (rel) => import(pathToFileURL(path.join(STATIC, rel)).href);

test("the QR encoder matches qrcode 1.5.4 module for module", async () => {
  const { qrMatrix } = await load("ui/qr.js");
  const golden = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/qr_golden.json"), "utf8"));
  assert.ok(golden.cases.length >= 4);
  for (const item of golden.cases) {
    const mine = qrMatrix(item.text, { ecc: item.ecc, mask: item.mask });
    assert.equal(mine.version, item.version, `version for ${item.ecc}/${item.mask}`);
    const rows = mine.modules.map((row) => row.map((dark) => (dark ? "1" : "0")).join(""));
    assert.deepEqual(rows, item.rows, `matrix for ${item.text.slice(0, 16)} ${item.ecc}/${item.mask}`);
  }
});

test("the QR SVG is black on white with a quiet zone, and long input is refused", async () => {
  const { qrMatrix, qrSVG } = await load("ui/qr.js");
  const link = "agentnexus://enroll?invite=anx1." + "A".repeat(180);
  const { size, mask } = qrMatrix(link);
  assert.ok(mask >= 0 && mask < 8);
  const svg = qrSVG(link, { label: "邀请<链接>" });
  assert.match(svg, new RegExp(`viewBox="0 0 ${size + 8} ${size + 8}"`));
  assert.match(svg, /fill="#fff"/);
  assert.match(svg, /aria-label="邀请链接"/);
  assert.throws(() => qrMatrix("x".repeat(3000), { ecc: "H" }), /too long/);
});

test("invite codes decode in the browser like on the gateway", async () => {
  const { decodeInvite, inviteFromText, hubHost } = await load("ui/invite.js");
  const payload = { hub: "https://hub.example.test", id: "inv_4fK9q2ab", secret: "z-YIYCcsy9zJ6LUB_qpoCqFvrZ5W9pbw9QxX7_pjka0" }; // gitleaks:allow (synthetic)
  const code = "anx1." + Buffer.from(JSON.stringify(payload)).toString("base64url");
  assert.deepEqual(decodeInvite(code), payload);
  assert.deepEqual(decodeInvite(`agentnexus://enroll?invite=${code}`), payload);
  assert.deepEqual(decodeInvite(`  ${code.slice(0, 30)}\n${code.slice(30)}  `), payload);
  assert.equal(inviteFromText(`agentnexus://enroll?invite=${code}`), code);
  for (const bad of ["", "anx1.", "anx1.!!", "anx2." + code.slice(5), "anx1." + Buffer.from('{"hub":"ftp://x","id":"a","secret":"b"}').toString("base64url")]) {
    assert.equal(decodeInvite(bad), null, bad);
  }
  assert.equal(hubHost("https://hub.example.test/base"), "hub.example.test");
});

test("question cards put the recommended option first", async () => {
  const { orderOptions } = await load("ui/inbox.js");
  assert.deepEqual(orderOptions({ options: ["a", "b", "c"], recommended: "b" }), ["b", "a", "c"]);
  assert.deepEqual(orderOptions({ options: ["a", "b"], recommended: "x" }), ["a", "b"]);
  assert.deepEqual(orderOptions({}), []);
});

test("the console routes every nav entry and uses only local modules", () => {
  const app = fs.readFileSync(path.join(STATIC, "console/app.js"), "utf8");
  for (const route of ['"/"', '"/devices"', '"/connectors"', '"/plugins/:device?"', '"/inbox/:thread?"', '"/downloads"', '"/settings"']) {
    assert.ok(app.includes(route), route);
  }
  for (const label of ["概览", "设备", "连接器", "插件", "收件箱", "下载", "设置", "退出登录"]) assert.ok(app.includes(label), label);
  const pages = fs.readFileSync(path.join(STATIC, "console/pages.js"), "utf8");
  for (const text of ["邀请新设备", "只显示这一次", "吊销", "备注名", "/api/invites", "/api/console/summary", "ChatGPT", "Claude Code", "Codex", "qrSVG("]) {
    assert.ok(pages.includes(text), text);
  }
  const html = fs.readFileSync(path.join(STATIC, "console.html"), "utf8");
  assert.match(html, /name="viewport" content="width=device-width/);
  assert.ok(!/https?:\/\//.test(html.replace(/http:\/\/www\.w3\.org\/2000\/svg/g, "")));
  const join = fs.readFileSync(path.join(STATIC, "local/cloud.js"), "utf8");
  for (const text of ["加入你的 Agent Nexus", "/api/cloud/join", "/api/cloud/leave", "此设备已被吊销", "暂时仅本机使用"]) assert.ok(join.includes(text), text);
});

test("every os_family the gateway reports has a mark, and the device page offers delete", async () => {
  const { osMark, OS_FAMILIES } = await load("ui/os-icons.js");
  const py = fs.readFileSync(path.join(STATIC, "../osinfo.py"), "utf8");
  const families = JSON.parse(py.match(/FAMILIES = (\([^)]*\))/)[1].replace(/\(/, "[").replace(/,?\s*\)$/, "]"));
  assert.deepEqual([...OS_FAMILIES].sort(), [...families].sort());
  for (const family of OS_FAMILIES) {
    const mark = osMark(family, { size: 20 });
    assert.ok(mark.label && mark.svg.startsWith("<svg") && mark.svg.includes('width="20"'), family);
    assert.ok(!/<script|on\w+=/i.test(mark.svg), family);
  }
  assert.equal(osMark("plan9"), null);
  assert.equal(osMark(undefined), null);
  const pages = fs.readFileSync(path.join(STATIC, "console/pages.js"), "utf8");
  for (const text of ["removeDevice(", "先吊销再删除", "method: \"DELETE\"", "osMark("]) assert.ok(pages.includes(text), text);
  const connectors = fs.readFileSync(path.join(STATIC, "console/connectors.js"), "utf8");
  for (const text of ["iconDialog(", "恢复自动获取", "/icon`", "connectorAvatar("]) assert.ok(connectors.includes(text), text);
});

test("deviceAvatar falls back to a generic glyph for a device with no os_family", async () => {
  const { osMark } = await load("ui/os-icons.js");
  const pages = fs.readFileSync(path.join(STATIC, "console/pages.js"), "utf8");
  const pick = (name) => { const start = pages.indexOf(`function ${name}(device) {`); return pages.slice(start, pages.indexOf(String.fromCharCode(10) + "}", start) + 2); };
  const el = (tag, attrs, ...children) => ({ tag, attrs, children });
  const icon = (name) => ({ icon: name });
  const make = new Function("el", "icon", "osMark", `${pick("deviceIcon")}
${pick("deviceAvatar")}
return deviceAvatar;`);
  const deviceAvatar = make(el, icon, osMark);
  assert.equal(deviceAvatar({ os_family: "ubuntu" }).attrs.class, "avatar os-mark");
  for (const device of [{}, { os_family: null }, { os_family: "plan9", form: "laptop" }, { platform: "linux" }]) {
    const node = deviceAvatar(device);
    assert.equal(node.attrs.class, "avatar");
    assert.ok(node.children[0].icon, JSON.stringify(device));
  }
});

test("Ubuntu install commands download the newest package from this gateway", async () => {
  globalThis.location = { origin: "https://hub.example.test" };
  const { ubuntuInstallCommand, ubuntuServerJoinCommand } = await load("console/install-commands.js");
  const desktop = ubuntuInstallCommand();
  assert.ok(desktop.includes("https://hub.example.test/downloads/latest/amd64.deb") && desktop.includes("sudo apt install -y ./agent-nexus.deb"));
  assert.ok(!desktop.includes("--no-install-recommends"));
  const server = ubuntuServerJoinCommand("anx1.CODE");
  assert.ok(server.includes("--no-install-recommends") && server.endsWith("agent-nexus join anx1.CODE && agent-nexus service enable"));
});

test("the terminal page builds its socket URL and keeps phone keys", async () => {
  const { terminalSocketURL, EXTRA_KEYS, TERMINAL_THEME } = await load("ui/terminal-config.js");
  const https = { protocol: "https:", host: "hub.example.test" };
  assert.equal(terminalSocketURL("pc main/1", { shell: "git-bash", cols: 90, rows: 20 }, https),
    "wss://hub.example.test/api/devices/pc%20main%2F1/terminal/ws?shell=git-bash&cols=90&rows=20");
  assert.ok(terminalSocketURL("a", {}, { protocol: "http:", host: "127.0.0.1:8443" }).startsWith("ws://127.0.0.1:8443/api/devices/"));
  assert.equal(terminalSocketURL("b", { cols: 80, rows: 24 }, { protocol: "http:", host: "127.0.0.1:8399" }, "/api/remote/devices"),
    "ws://127.0.0.1:8399/api/remote/devices/b/terminal/ws?shell=&cols=80&rows=24");
  const keys = Object.fromEntries(EXTRA_KEYS);
  assert.equal(keys["Ctrl+C"], "\x03");
  assert.equal(keys["Esc"], "\x1b");
  assert.match(TERMINAL_THEME.background, /^#[0-9a-f]{6}$/);
});

test("the Codex button launches, signs in first, or installs, in each shell's own syntax", async () => {
  const { codexPlan, runCommand, CODEX_INSTALL } = await load("ui/terminal-config.js");
  assert.equal(runCommand("codex", "pwsh"), "codex");
  assert.equal(runCommand("C:\\Apps\\codex.exe", "powershell"), '& "C:\\Apps\\codex.exe"');
  assert.equal(runCommand("C:\\Apps\\codex.exe", "cmd"), '"C:\\Apps\\codex.exe"');
  assert.deepEqual(codexPlan({ platform: "linux", shell: "bash", cli: { id: "codex", command: "codex", signed_in: true } }),
    { kind: "launch", text: "codex" });
  // signed-in state unknown (keyring): launch, its own screen signs in if needed
  assert.equal(codexPlan({ platform: "darwin", shell: "zsh", cli: { command: "codex", signed_in: null } }).kind, "launch");
  assert.deepEqual(codexPlan({ platform: "win32", shell: "pwsh", cli: { command: "C:\\A\\codex.exe", signed_in: false } }),
    { kind: "login", text: '& "C:\\A\\codex.exe" login --device-auth; & "C:\\A\\codex.exe"' });
  assert.equal(codexPlan({ platform: "win32", shell: "cmd", cli: { command: "codex", signed_in: false } }).text,
    "codex login --device-auth & codex");

  const posix = codexPlan({ platform: "linux", shell: "bash", cli: null });
  assert.equal(posix.kind, "install");
  assert.equal(posix.text, `${CODEX_INSTALL.sh} && "$HOME/.local/bin/codex" login --device-auth; "$HOME/.local/bin/codex"`);
  const ps = codexPlan({ platform: "win32", shell: "powershell", cli: null });
  assert.ok(ps.text.startsWith(`${CODEX_INSTALL.windows}; if ($?) { & "$env:LOCALAPPDATA\\Programs\\OpenAI\\Codex\\bin\\codex.exe" login --device-auth;`));
  assert.ok(codexPlan({ platform: "win32", shell: "cmd", cli: null }).text.includes('"%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe" login'));
  assert.ok(codexPlan({ platform: "win32", shell: "git-bash", cli: null }).text.includes('"$LOCALAPPDATA/Programs/OpenAI/Codex/bin/codex.exe"'));
  assert.match(CODEX_INSTALL.sh, /^curl -fsSL https:\/\/chatgpt\.com\/codex\/install\.sh \| sh$/);
});

test("the terminal and desktop pages draw before the network answers", () => {
  const page = fs.readFileSync(path.join(STATIC, "ui/terminal.js"), "utf8");
  // no Promise.all over the device list / info before the first paint, the socket opens at once
  assert.ok(!/await Promise\.all\(\[\s*fetchJSON\(api\.devices/.test(page));
  assert.match(page, /cachedJSON\(api\.devices\)/);
  assert.match(page, /query\?\.get\?\.\("run"\) === "codex"/);
  const desktop = fs.readFileSync(path.join(STATIC, "local/desktop.js"), "utf8");
  assert.match(desktop, /desktop\/prepare`, \{ method: "POST", quiet: true, keepCache: true \}/);
  assert.ok(desktop.indexOf('cachedJSON("/api/devices")') < desktop.indexOf("desktop/prepare"));
  const ui = fs.readFileSync(path.join(STATIC, "ui/ui.js"), "utf8");
  assert.match(ui, /if \(!options\.keepCache && swr\.store\.size\)/);
});

test("the console page routes to the terminal and loads xterm from /console/ only", () => {
  const app = fs.readFileSync(path.join(STATIC, "console/app.js"), "utf8");
  assert.match(app, /"\/terminal\/:device"/);
  const page = fs.readFileSync(path.join(STATIC, "ui/terminal.js"), "utf8");
  for (const asset of ["/ui/xterm.js", "/ui/addon-fit.js", "/ui/xterm.css"]) assert.ok(page.includes(asset), asset);
  assert.ok(fs.statSync(path.join(STATIC, "ui/xterm.js")).size > 100000);
  const local = fs.readFileSync(path.join(STATIC, "local/app.js"), "utf8");
  assert.match(local, /"\/terminal\/:device"/);
  assert.match(local, /CLIENT_TERMINAL_API/);
});
