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
  // head line: session toggle · current title · ⋯ (the ⋯ menu keeps presets / temp dirs / 新对话; the work context
  // moved into the 目录 chip)
  assert.match(chat, /const head = el\("div", \{ class: "chat-head" \}, sideBtn, headTitle, moreBtn\)/);
  assert.match(chat, /const main = el\("section", \{ class: "chat-main", "aria-label": "对话" \}, head, ctxLine,/);
  for (const item of ["编辑提示词片段…", "临时目录…"]) assert.ok(chat.includes(item), item);
  assert.ok(!chat.includes("工作上下文…") && !chat.includes("contextDialog"), "no separate work-context dialog");
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

// ---------------------------------------------------------------- folders on any device of the network (§1 gateway)

const DEVICES = [
  { id: null, gid: "pc-self", name: "我的笔记本", online: true, self: true, workdirs: [{ name: "site", path: "/Users/me/site" }] },
  { id: "pc-win", name: "工作电脑", online: true, reachable: true, files: true, workdirs: [{ name: "billing", path: "D:\\repos\\billing" }] },
  { id: "pc-old", name: "构建机", online: false, hint: "客户端过旧或未开放 Agent", reachable: true, files: true, workdirs: [] },
  { id: "pc-nas", name: "NAS", online: false, hint: "离线", reachable: false },
];

test("the 目录 menu's other devices: everyone but the runner, with its gateway id; 本机 by its gid", async () => {
  const { contextTargets } = await load("ui/chat.js");
  // runner = 本机 (the local console's null): the others, 本机 left out
  const fromSelf = contextTargets(DEVICES, null);
  assert.deepEqual(fromSelf.map((t) => [t.id, t.call, t.online, t.agents, t.files]),
    [["pc-win", "pc-win", true, true, true], ["pc-old", "pc-old", true, false, true], ["pc-nas", "pc-nas", false, false, false]]);
  // runner = 工作电脑: 本机 is a target under its gateway id; its own ops still go to the console (call null)
  const fromWin = contextTargets(DEVICES, "pc-win");
  assert.deepEqual(fromWin[0], { id: "pc-self", call: null, self: true, name: "本机（我的笔记本）", online: true, agents: true, files: false,
    workdirs: [{ name: "site", path: "/Users/me/site" }], hint: "" });
  assert.ok(!fromWin.some((t) => t.id === "pc-win"));
  // 本机 without a gateway id (not joined): never a target; old device lists without `reachable` fall back to online
  assert.deepEqual(contextTargets([{ id: null, name: "本机", online: true, self: true }, { id: "a", name: "A", online: true }], "a"), []);
  assert.equal(contextTargets([{ id: "b", name: "B", online: true }], "a")[0].online, true);
  assert.deepEqual(contextTargets(null, null), []);
});

test("a device's folders: used here first, then its own recent folders, workspaces and registered workdirs, each once", async () => {
  const { targetFolders } = await load("ui/chat.js");
  const target = { id: "pc-win", workdirs: [{ name: "billing", path: "D:\\repos\\billing" }, { name: "ops", path: "D:\\ops" }] };
  const used = [{ device: "pc-other", workdir: "/x" }, { device: "pc-win", workdir: "D:\\repos\\infra" }];
  const dirs = { recent: ["D:\\repos\\billing", { path: "D:\\tmp" }], workspaces: [{ name: "Billing", path: "D:\\repos\\billing" }] };
  const folders = targetFolders(target, { used, dirs });
  assert.deepEqual(folders.map((f) => [f.path, f.name, f.from]), [
    ["D:\\repos\\infra", "infra", "used"], ["D:\\repos\\billing", "billing", "recent"], ["D:\\tmp", "tmp", "recent"], ["D:\\ops", "ops", "workspace"]]);
  assert.equal(targetFolders(target, { used, dirs, limit: 2 }).length, 2);
  assert.deepEqual(targetFolders({ id: "z" }).map((f) => f.path), []);
});

test("the 目录 chip reads <folder> · <device> in gateway mode, with the whole story in its title", async () => {
  const { folderChip, rememberTarget } = await load("ui/chat.js");
  const gw = folderChip({ workdir: "/Users/me/site", context: { mode: "gateway", device: "pc-win", workdir: "D:\\repos\\billing\\" }, runnerName: "本机", targetName: "工作电脑" });
  assert.deepEqual(gw, { value: "billing", device: "工作电脑", gateway: true, title: "目录：工作电脑 上的 D:\\repos\\billing\\（经网关；CLI 在 本机 上运行）" });
  assert.deepEqual(folderChip({ workdir: "/srv/app", context: { mode: "direct" }, runnerName: "X" }), { value: "app", device: "", gateway: false, title: "目录：/srv/app" });
  assert.equal(folderChip({ workdir: "", context: null, runnerName: "X" }).value, "选择目录");
  // the folders used through the gateway: newest first, each device+folder once
  let list = rememberTarget([], "a", "/1");
  list = rememberTarget(list, "b", "/2");
  list = rememberTarget(list, "a", "/1");
  assert.deepEqual(list, [{ device: "a", workdir: "/1" }, { device: "b", workdir: "/2" }]);
  assert.equal(rememberTarget(Array.from({ length: 30 }, (_, i) => ({ device: "d", workdir: `/${i}` })), "e", "/e").length, 24);
  // with the device's name, so it reads as that name while the device is not in the list
  assert.deepEqual(rememberTarget(list, "a", "/1", { name: "工作电脑" })[0], { device: "a", workdir: "/1", name: "工作电脑" });
  assert.equal(rememberTarget([], "a", "/1", { max: 0 }).length, 0);
});

test("picking a folder on another device is gateway mode; the run, the session and the stored state carry it", () => {
  const chat = read("ui/chat.js");
  const pick = chat.slice(chat.indexOf("function useTarget("), chat.indexOf("function loadTargetDirs("));
  assert.ok(pick.includes('S.context = { mode: "gateway", device: t.id, workdir: path, name: t.name }') && pick.includes("rememberTarget(usedTargets(), t.id, path, { name: t.name })"));
  assert.ok(chat.includes("function useDirect(path) {") && chat.slice(chat.indexOf("function useDirect(path) {"), chat.indexOf("function useTarget(")).includes("S.context = directContext()"));
  // send: the context goes to agent_run_open; the runner's cwd is the session's when resuming, else the hub's default
  assert.ok(chat.includes('context: gatewayCtx() ? { mode: "gateway", device: S.context.device, workdir: S.context.workdir } : { mode: "direct" }'));
  assert.ok(chat.includes('workdir: gatewayCtx() ? S.session?.cwd || "" : S.workdir'));
  // a session / run brings its context back; per-device prefs (§16.2) keep it
  const open = chat.slice(chat.indexOf("async function openSession("), chat.indexOf("function attachRun("));
  assert.ok(open.includes("adoptContext(s.context)"));
  assert.ok(chat.includes("if (run.context) adoptContext({ ...run.context, name: run.context.name || run.context_device });"));
  // the run device's folder is the session's in either mode: back in direct mode it resumes where Claude keeps it
  assert.ok(open.includes("if (s.cwd && !s.cwd_missing) S.workdir = s.cwd;") && !open.includes("!gatewayCtx()) S.workdir"));
  const attach = chat.slice(chat.indexOf("function attachRun("), chat.indexOf("async function poll("));
  assert.ok(attach.includes("if (run.workdir) S.workdir = run.workdir;") && attach.includes('run.mode === "gateway"'));
  assert.ok(chat.includes("adoptContext(prefs.context);") && /const prefs = \{ kind: S\.kind, workdir: S\.workdir, [^}]*context: S\.context/.test(chat));
  // offline target / gateway out of reach: blocked with a way back to the run device's folders
  const blocked = chat.slice(chat.indexOf("function blockedReason("), chat.indexOf("function renderComposer("));
  assert.ok(blocked.includes("contextProblem()") && blocked.includes("directContext()"));
  // browsing another device: its AgentHub, or files/1 for a device without agents/1
  assert.ok(chat.includes('callOp(api, "agent_dirs", params, t.call)') && chat.includes("api.files(t.id"));
  // the session row says whose folder a gateway session works on
  assert.ok(chat.includes("whereText(s.context, s.cwd)") && chat.includes("whereText(run.context, run.workdir)"));
});

test("the 目录 menu redraws only a device's folder list, so typing and focus survive its folders arriving", () => {
  const chat = read("ui/chat.js");
  const menu = chat.slice(chat.indexOf("function dirMenu("), chat.indexOf("function browseDirs("));
  const section = menu.slice(menu.indexOf("const section = (t) => {"), menu.indexOf("const info = agentInfo();"));
  const paint = section.slice(section.indexOf("const paint = () => {"));
  assert.ok(paint.includes("mount(list,") && !paint.includes("mount(box"), "only the list is remounted");
  assert.ok(!paint.includes("pathField") && !paint.includes('"chat-dir-dev-head"'), "head and field are built once");
  assert.ok(paint.includes("document.activeElement") && paint.includes("dataset.path"), "a focused folder row keeps its place");
  assert.ok(section.includes('"aria-controls": fieldId') && section.includes("id: fieldId"), "the 输入路径 toggle names its field");
  // offline devices with their reasons as text; a jump to the other devices; agent_dirs errors are shown
  assert.ok(menu.includes("不可用：") && menu.includes("（${t.hint || \"离线\"}）") && !menu.includes('title: offline.map'));
  assert.ok(menu.includes("chat-dir-jump") && menu.includes("读不到这台设备的目录"));
  // the device list refresh keeps the folder lists (60 s throttle), dropping devices that went offline
  assert.ok(!chat.includes("targetDirs.clear()"));
  // the gateway out of reach is said directly, not through the devices note's pitch
  const problem = chat.slice(chat.indexOf("function contextProblem("), chat.indexOf("function adoptContext("));
  assert.ok(problem.includes("连不上网关") && !problem.includes("S.devicesNote}"));
  const css = read("ui/ui.css");
  assert.ok(/\.chat-ctx-run \{ display: none; \}/.test(css) && css.includes(".chat-ctx-target {"), "phones: the folder always shows");
});

test("both consoles give the chat what the gateway folders need", () => {
  const wire = read("ui/agent-wire.js");
  assert.ok(wire.includes('self.gid = me.device_id'), "本机's gateway id (local console)");
  assert.equal((wire.match(/\.\.\.targetFacts\(d, /g) || []).length, 2, "reachable / workdirs / files in both device lists");
  assert.ok(wire.includes('files: listFiles("/api/devices")') && wire.includes('files: listFiles("/api/remote/devices")'));
  const css = read("ui/ui.css");
  for (const rule of [".chat-chip-dir.via {", ".chat-dir-dev-head {", ".chat-dir-warn {", ".chat-ctx.warn {"]) assert.ok(css.includes(rule), rule);
  assert.ok(!css.includes(".chat-ctx-choices") && !css.includes(".chat-ctx-browse"), "the old dialog's rules are gone");
});
