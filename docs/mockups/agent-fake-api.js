// In-memory stand-in for the AgentHub ops (docs/agent-chat.md §2–§10), for the agent-chat / agent-prompts
// mockups. Same adapter shape chat.js takes: {call(op, params, device), devices(), conversation(address), answerAsk(id, answer)}.
// ?state=first → a device with no sessions yet; ?slow=1 → slower streaming.
// Phase 2 (§16): `usage` events (context ring), `error` level notice / warn (§16.6), API models in agent_info (§16.4).

const params = new URLSearchParams(location.search);
const NOW = Date.now() / 1000;
const HOME = "/Users/lin";
const P = `${HOME}/projects`;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const SLOW = params.get("slow") ? 4 : 1;

// reachable / workdirs / files: what the 目录 menu needs to offer a device's folders through the gateway (§1).
// 研发构建机 has no agents/1 (online: false with a hint) but is on the gateway: its folders come from workdirs + files/1.
const DEVICES = [
  { id: "dev-mbp", gid: "dev-mbp", name: "林的 MacBook Pro", online: true, self: true },
  { id: "dev-win", name: "工作电脑", online: true, reachable: true, files: true, workdirs: [{ name: "billing", path: "D:\\repos\\billing" }] },
  { id: "dev-lab", name: "研发部共享 GPU 训练服务器（机房 B 区 3 号机柜）", online: true, reachable: true, files: true,
    workdirs: [{ name: "train", path: "/data/train" }] },
  { id: "dev-build", name: "研发构建机", online: false, hint: "客户端过旧或未开放 Agent", reachable: true, files: true,
    workdirs: [{ name: "ci", path: "/srv/ci" }, { name: "artifacts", path: "/srv/artifacts" }] },
  { id: "dev-nas", name: "家里的 NAS", online: false, hint: "离线", reachable: false, files: true },
];

// each device's own folders (agent_dirs without a path)
const DIRS = {
  // Windows: the drive roots are roots too (§7)
  "dev-win": { home: "C:\\Users\\user", recent: ["D:\\repos\\billing", "D:\\repos\\infra-terraform"], workspaces: [{ name: "billing", path: "D:\\repos\\billing" }],
    roots: [{ name: "主目录", path: "C:\\Users\\user", kind: "home" }, { name: "billing", path: "D:\\repos\\billing", kind: "workspace" }, { name: "C:", path: "C:\\", kind: "drive" }, { name: "D:", path: "D:\\", kind: "drive" }] },
  "dev-lab": { home: "/home/user", recent: ["/data/train/llm-finetune-2026q4-experiments", "/home/user/notebooks"], workspaces: [], roots: [{ name: "主目录", path: "/home/user" }] },
};
const FILES = {
  "/srv": ["ci", "artifacts", "www"], "/srv/ci": ["pipelines", "cache"], "/home/user": ["src", ".cache"],
};

function agentsFor(device) {
  return [
    // §16.7: models {flag, protocol, hints}: a model chip when the CLI takes one by flag or over ACP
    { kind: "claude", label: "Claude Code", installed: true, path: `${HOME}/.local/bin/claude`, version: "2.1.3 (Claude Code)", login: "ok", gateway_mcp: true, supports: { resume: true, relocate: true, approvals: true, stream: true },
      models: { flag: true, protocol: false, hints: [{ id: "sonnet", label: "Sonnet", desc: "日常编码" }, { id: "opus", label: "Opus", desc: "更强，更慢" }, { id: "haiku", label: "Haiku", desc: "最快" }, { id: "opusplan", label: "Opus Plan", desc: "规划用 Opus，执行用 Sonnet" }] } },
    { kind: "codex", label: "Codex", installed: true, path: device === "dev-win" ? "C:\\Users\\you\\AppData\\Roaming\\npm\\codex.cmd" : "/opt/homebrew/bin/codex", version: "codex-cli 0.172.0", login: "ok", gateway_mcp: false, supports: { resume: true, relocate: true, approvals: false, stream: true },
      models: { flag: true, protocol: false, hints: [] } },
    { kind: "dsh", label: "DeepSeek Harness", installed: false, path: null, version: null, login: "unknown", install: null, supports: { resume: true, approvals: true, stream: true },
      models: { flag: false, protocol: true, hints: [] } },
    { kind: "gemini", label: "Gemini CLI", installed: true, path: "/opt/homebrew/bin/gemini", version: "0.9.1", login: params.get("gemini") === "ok" ? "ok" : "missing", supports: { resume: true, approvals: true, stream: true },
      models: { flag: true, protocol: true, hints: [{ id: "auto", label: "自动" }, { id: "pro", label: "Pro" }, { id: "flash", label: "Flash" }] } },
    // §16.4: every enabled model of a provider is an Agent (adapter "api"); the picker lists them as 「API 模型」
    { kind: "api:deepseek/deepseek-chat", label: "DeepSeek V3", adapter: "api", provider_label: "DeepSeek", installed: true, login: "ok", supports: { resume: true, approvals: true, stream: true } },
    { kind: "api:openai/gpt-4.1", label: "GPT-4.1", adapter: "api", provider_label: "OpenAI", installed: true, login: "missing", supports: { resume: true, approvals: true, stream: true } },
  ];
}

const SESSIONS = params.get("state") === "first" ? [] : [
  { kind: "claude", id: "c-7f3a", title: "登录后偶尔跳回登录页，帮我查一下原因并修复", alias: "修复登录页 OAuth 回调", cwd: `${P}/agent-nexus`, updated_at: NOW - 95, messages: 18, pinned: false, address: "claude-agent-nexus@林的 MacBook Pro" },
  { kind: "claude", id: "c-21bd", title: "给拓扑视图加地图模式，设备按地理位置分布", cwd: `${P}/agent-nexus`, updated_at: NOW - 3600 * 5, messages: 42, pinned: true },
  { kind: "codex", id: "x-77gw", title: "清理 billing 仓库里过期的迁移脚本", cwd: HOME, updated_at: NOW - 3600 * 1.1, messages: 7,
    context: { mode: "gateway", device: "dev-win", workdir: "D:\\repos\\billing" } },
  { kind: "codex", id: "x-9e01", title: "整理 CHANGELOG 并准备 0.3.0 发布说明", cwd: `${P}/agent-nexus`, updated_at: NOW - 3600 * 2.4, messages: 12 },
  { kind: "codex", id: "x-44c2", title: "排查 Windows 终端里中文乱码", cwd: `${P}/agent-nexus`, updated_at: NOW - 86400 * 1.2, messages: 27, pinned: true },
  { kind: "claude", id: "c-0a19", title: "把博客从 Hexo 迁移到 Astro", cwd: `${P}/website`, updated_at: NOW - 86400 * 2, messages: 64 },
  { kind: "codex", id: "x-1f00", title: "为收件箱写端到端测试", cwd: `${P}/agent-nexus`, updated_at: NOW - 86400 * 3.5, messages: 9 },
  { kind: "gemini", id: "g-5521", title: "总结这周的会议记录", cwd: `${HOME}/notes`, updated_at: NOW - 86400 * 4, messages: 6 },
  { kind: "claude", id: "c-77aa", title: "重构 mailbox 的存储层", cwd: `${P}/agent-nexus`, updated_at: NOW - 86400 * 12, messages: 88, archived: true },
  { kind: "codex", id: "x-3b3b", title: "试验：用 uv 管理依赖", cwd: `${P}/sandbox`, updated_at: NOW - 86400 * 20, messages: 4, archived: true },
  // §14: the folder of these two is gone: Claude can continue in a new folder, Gemini cannot (bound to its folder)
  { kind: "claude", id: "c-old1", title: "旧仓库里的部署脚本整理（目录已被删除）", cwd: `${P}/old-deploy`, cwd_missing: true, updated_at: NOW - 86400 * 6, messages: 14 },
  { kind: "gemini", id: "g-old2", title: "旧笔记目录里的周报（目录已被删除）", cwd: `${HOME}/old-notes`, cwd_missing: true, updated_at: NOW - 86400 * 8, messages: 5 },
  { kind: "claude", id: "c-md17", title: "整理 OAuth 修复的改动清单（Markdown、图片、文件链接演示）", cwd: `${P}/agent-nexus`, updated_at: NOW - 3600 * 3, messages: 4 },
  { kind: "api:deepseek/deepseek-chat", id: "a-0c11", title: "解释一下 SSE 和 WebSocket 的取舍", cwd: `${P}/agent-nexus`, updated_at: NOW - 86400 * 1.6, messages: 4 },
];

const DIFF = `@@ -41,9 +41,12 @@ async def oauth_callback(request):
     state = request.query_params.get("state")
-    if state != request.session.get("oauth_state"):
-        return RedirectResponse("/login")
+    expected = request.session.pop("oauth_state", None)
+    if not expected or not secrets.compare_digest(state or "", expected):
+        log.warning("oauth state mismatch (cookie lost?)")
+        return RedirectResponse("/login?error=state")
     token = await exchange_code(request.query_params["code"])
-    request.session["user"] = token.user
+    request.session.clear()
+    request.session["user"] = token.user
     return RedirectResponse(request.session.get("next", "/"))`;

// §17: full markdown, images and file links (agent_file_stat / agent_file_read of the fake FS below)
const MD_DEMO = [
  "## 改动清单",
  "",
  "| 文件 | 改动 | 说明 |",
  "|:---|---:|:---|",
  "| `agent_nexus/auth.py:41` | +12 −3 | 回调校验 state，失败时**记录日志** |",
  "| `tests/test_oauth_state.py` | +48 | 新增：cookie 丢失、state 不匹配 |",
  "| `docs/auth.md` | +6 | 说明 `SameSite=Lax` 的取舍（\\| 是转义的竖线） |",
  "",
  "### 下一步",
  "",
  "1. 修复回调",
  "   - [x] 用 `secrets.compare_digest` 比较 state",
  "   - [x] 失败时跳到 `/login?error=state`",
  "   - [ ] ~~改成 SameSite=None~~ *不需要*：Lax 已经够用",
  "2. 补测试，详见 tests/test_oauth_state.py:12",
  "3. 发版前看一下 ~/notes/release-todo.md 和 docs/设计/登录流程.md",
  "",
  "---",
  "",
  "> **注意**：日志在 /Users/lin/projects/agent-nexus/logs/auth.log，不在 `/api/agents`（那是路由，不是文件）。",
  "> 截图在下面。",
  "",
  "![回调失败时的登录页](docs/shots/login-error.png)",
  "",
  "外部图片默认不加载：![架构图](https://example.com/arch.png)",
  "",
  "导出的报告：`out/report.pdf`；不存在的文件不会变成链接：`agent_nexus/missing.py`。版本 v0.8.35、e.g. node.js 也不是。",
  "",
  "```python",
  "if not expected or not secrets.compare_digest(state or \"\", expected):",
  "    return RedirectResponse(\"/login?error=state\")",
  "```",
].join("\n");

const HISTORY = {
  "c-7f3a": [
    { type: "user", text: "登录后偶尔跳回登录页，帮我查一下原因并修复", t: NOW - 1400 },
    { type: "thinking", text: "“偶尔”说明和时序或 cookie 有关。先看 OAuth 回调里 state 的校验，再看 session cookie 的设置。\n回调里直接比较 state，session 丢失时会静默跳回 /login，没有日志。", t: NOW - 1395 },
    { type: "tool", id: "t1", name: "Read", title: "agent_nexus/auth.py", status: "ok", input: { file_path: "agent_nexus/auth.py" }, output: "（读取了 212 行）", t: NOW - 1390 },
    { type: "tool", id: "t2", name: "Grep", title: "SameSite|session_cookie", status: "ok", input: { pattern: "SameSite|session_cookie", path: "agent_nexus" }, output: "agent_nexus/web.py:88:    same_site=\"strict\",\nagent_nexus/web.py:89:    https_only=True,", t: NOW - 1385 },
    { type: "message", role: "assistant", text: "找到原因了：会话 cookie 用的是 `SameSite=Strict`。从 GitHub 授权页跳回来属于**跨站导航**，浏览器不会带上这个 cookie，于是回调里读不到 `oauth_state`，直接跳回了登录页。\n\n只有在你之前没有打开过控制台的情况下才会发生，所以看起来是“偶尔”。我先把回调的校验改得更严谨，并在失败时记录日志：", t: NOW - 1380 },
    { type: "file_change", path: "agent_nexus/auth.py", kind: "edit", diff: DIFF, t: NOW - 1370 },
    { type: "usage", context: { used: 612400, limit: 1000000, model: "claude-opus-4-5" }, turn: { input: 18240, output: 2130, cached: 410000, cost_usd: 0.083 }, t: NOW - 1361 },
    { type: "status", state: "done", usage: { input: 18240, output: 2130, cost_usd: 0.083 }, t: NOW - 1360 },
  ],
  "c-md17": [
    { type: "user", text: "把这次 OAuth 修复的改动整理成清单：\n\n- 用**表格**列出文件\n- 附上截图 `docs/shots/login-error.png`\n\n改动在 agent_nexus/auth.py 里。", t: NOW - 10900 },
    { type: "tool", id: "m1", name: "Read", title: "agent_nexus/auth.py", status: "ok", input: { file_path: "agent_nexus/auth.py" }, output: "（读取了 212 行）", t: NOW - 10890 },
    { type: "file_change", path: "tests/test_oauth_state.py", kind: "add", diff: "@@ -0,0 +1,3 @@\n+def test_state_mismatch():\n+    ...\n+", t: NOW - 10880 },
    { type: "message", role: "assistant", text: MD_DEMO, t: NOW - 10870 },
    { type: "status", state: "done", t: NOW - 10860 },
  ],
  // Codex: its own config hints arrive as level "notice" (grey, once per session); a warn stays yellow (§16.6)
  "x-9e01": [
    { type: "user", text: "整理 CHANGELOG 并准备 0.3.0 发布说明", t: NOW - 9000 },
    { type: "error", level: "notice", text: "Codex is ignoring `model_reasoning_summary_format` in ~/.codex/config.toml: unrecognized configuration setting\nRemove it, or upgrade Codex to a version that knows it.", t: NOW - 8999 },
    { type: "error", level: "notice", text: "Codex is ignoring `model_reasoning_summary_format` in ~/.codex/config.toml: unrecognized configuration setting\nRemove it, or upgrade Codex to a version that knows it.", t: NOW - 8998 },
    { type: "error", level: "warn", text: "经网关模式下没有检测到 Agent Nexus 连接器：ask_user / mail_send 不可用", t: NOW - 8997 },
    { type: "message", role: "assistant", text: "CHANGELOG 已按版本整理好，0.3.0 的发布说明草稿放在 `docs/release-0.3.0.md`。", t: NOW - 8900 },
    { type: "error", level: "notice", text: "Codex is ignoring `model_reasoning_summary_format` in ~/.codex/config.toml: unrecognized configuration setting\nRemove it, or upgrade Codex to a version that knows it.", t: NOW - 8899 },
    { type: "usage", context: { used: 48200, limit: null, model: "gpt-5.1-codex" }, turn: { input: 46100, output: 2100 }, t: NOW - 8890 },
    { type: "status", state: "done", t: NOW - 8889 },
  ],
};

const RUN_EVENTS = [
  { type: "status", state: "running", session_id: "c-7f3a", address: "claude-agent-nexus@林的 MacBook Pro" },
  { type: "user", text: "顺便跑一下相关测试，确认没有回归，然后推到 fix/oauth-callback 分支" },
  { type: "thinking", text: "先跑 auth 相关测试，", delta: true },
  { type: "thinking", text: "再全量跑一遍确认。", delta: true },
  { type: "tool", id: "t3", name: "Bash", title: "uv run pytest -q tests/test_auth.py", status: "running", input: { command: "uv run pytest -q tests/test_auth.py" } },
  { type: "tool", id: "t3", status: "ok", output: "........................                                [100%]\n24 passed in 1.84s" },
  { type: "tool", id: "t4", name: "Bash", title: "uv run pytest -q", status: "ok", input: { command: "uv run pytest -q" }, output: "........................................................ [ 61%]\n....................................                     [100%]\n412 passed, 3 skipped in 38.20s" },
  { type: "message", role: "assistant", text: "测试全部通过（412 个通过，3 个跳过）。", delta: false },
  { type: "message", role: "assistant", text: "改动摘要：\n\n1. 回调里用 `secrets.compare_digest` 校验 state，", delta: true },
  { type: "message", role: "assistant", text: "取出后立即作废，防止重放\n2. 校验失败时记录日志，并带上 `?error=state` 让登录页给出提示\n3. 登录成功后先 `session.clear()` 再写入用户，避免会话固定\n\n接下来推送分支：\n\n```bash\ngit push -u origin fix/oauth-callback\n```", delta: true },
  { type: "usage", context: { used: 872100, limit: 1000000, model: "claude-opus-4-5" } },
  { type: "approval", id: "ap-1", tool: "Bash", summary: "git push -u origin fix/oauth-callback", state: "pending", remember: true },
];

const PROMPTS = {
  fragments: [
    { id: "identity", title: "身份与环境", builtin: true, scope: "all", kinds: [], enabled: true,
      text: "你是 $agent，正在 $owner 的设备 $device（$os）上工作，今天是 $date。\n你可以直接使用自己的工具读写文件、运行命令。" },
    { id: "workdir", title: "工作目录与临时目录", builtin: true, scope: "direct", kinds: [], enabled: true,
      text: "工作目录是 $workdir。只在这个目录里修改文件。\n需要草稿、下载或中间产物时放到 $tmpdir，对话结束 7 天后会自动清理。" },
    { id: "talk", title: "与用户沟通", builtin: true, scope: "all", kinds: [], enabled: true, modified: true,
      text: "你的邮箱地址是 $address。需要 $owner 做决定时调用 ask_user，给出 2–4 个选项并标出推荐项；\n完成较长的任务后用 mail_send 发一封简短的总结给 me。不要为小事打扰。" },
    { id: "team", title: "团队协作", builtin: true, scope: "all", kinds: ["claude", "codex"], enabled: false,
      text: "其他会话可能在同时工作：用 mail_who 查看在线的会话，用 mail_send 发给 @all 广播。\n收到其他会话的邮件时先回复确认。" },
    { id: "gateway", title: "网关工作方式", builtin: true, scope: "gateway", kinds: [], enabled: true,
      text: "你要操作的是另一台设备 $context_device 上的目录 $context_workdir。\n调用网关工具时总是带上 workspace_id=\"$handle\"，不要在本机直接修改文件。" },
    { id: "project", title: "项目说明", builtin: true, scope: "all", kinds: [], enabled: true,
      text: "开始前先读取工作目录里的 AGENTS.md 或 CLAUDE.md（如果有），按里面的约定工作。" },
    { id: "custom-1", title: "回复风格", builtin: false, scope: "all", kinds: [], enabled: true,
      text: "用简体中文回答，先给结论再给理由。代码改动完成后列出改了哪些文件；涉及金额时写成 $$100 的形式。\n遇到 $unknown_thing 时说明。" },
  ],
  presets: [
    { id: "default", name: "默认", order: ["identity", "workdir", "talk", "team", "gateway", "project", "custom-1"], enabled: {} },
    { id: "review", name: "只读审查", order: ["identity", "project", "talk", "custom-1"], enabled: { workdir: false, team: false } },
  ],
};

const VAR_VALUES = (device, ctx) => ({
  device: DEVICES.find((d) => d.id === device)?.name || "林的 MacBook Pro", device_id: device || "dev-mbp", os: device === "dev-win" ? "Windows 11" : "macOS 15",
  workdir: `${P}/agent-nexus`, tmpdir: `${P}/agent-nexus/.anx/agents/tmp/r-5c1e`, session: "claude-agent-nexus", address: "claude-agent-nexus@林的 MacBook Pro",
  agent: { claude: "Claude Code", codex: "Codex", dsh: "DeepSeek Harness", gemini: "Gemini CLI" }[ctx.kind] || ctx.kind, date: "2026-11-26", time: "16:40",
  owner: "林", gateway: "hub.example.com", handle: "ws_8f2c1a", context_device: "工作电脑", context_workdir: "D:\\repos\\billing",
});

const TMP = [
  { rid: "r-5c1e", path: `${P}/agent-nexus/.anx/agents/tmp/r-5c1e`, size: 18.4 * 1048576, files: 37, updated_at: NOW - 60, session: "修复登录页 OAuth 回调", kind: "claude", active: true },
  { rid: "r-4a07", path: `${P}/agent-nexus/.anx/agents/tmp/r-4a07`, size: 412 * 1048576, files: 1204, updated_at: NOW - 86400 * 1.2, session: "排查 Windows 终端里中文乱码", kind: "codex", active: false },
  { rid: "r-3310", path: `${P}/agent-nexus/.anx/agents/tmp/r-3310`, size: 2.1 * 1048576, files: 8, updated_at: NOW - 86400 * 3, session: "整理 CHANGELOG 并准备 0.3.0 发布说明", kind: "codex", active: false },
  { rid: "r-2b9d", path: `${P}/agent-nexus/.anx/agents/tmp/r-2b9d`, size: 96 * 1048576, files: 233, updated_at: NOW - 86400 * 9, session: "把博客从 Hexo 迁移到 Astro", kind: "claude", active: false },
  { rid: "r-1a44", path: `${P}/agent-nexus/.anx/agents/tmp/r-1a44`, size: 640 * 1024, files: 3, updated_at: NOW - 86400 * 16, session: "总结这周的会议记录", kind: "gemini", active: false },
];

const TREE = {
  [HOME]: ["projects", "notes", "Downloads", "Documents"],
  [P]: ["agent-nexus", "website", "sandbox", "billing-service"],
  [`${P}/agent-nexus`]: ["agent_nexus", "docs", "tests", "builtins"],
};

export function createFakeApi({ activeRun = true } = {}) {
  const runs = new Map();
  const asks = [
    { ask_id: "q-1", question: "顺便把会话 cookie 改成 SameSite=Lax 吗？", context: "改成 Lax 后从 GitHub 跳回时会带上 cookie，问题就从根上解决了；Strict 更严格，但每次外部跳转进来都要重新登录。",
      options: ["改成 Lax", "保持 Strict", "先不动，开个 issue"], recommended: "改成 Lax", state: "open", from: "claude-agent-nexus@林的 MacBook Pro", t: NOW + 3.4, visible_at: NOW + 3.4 },
  ];
  const mails = [
    { id: "m-1", from: "codex-agent-nexus@工作电脑", to: "me", subject: "Windows 上也复现了跳回登录页", body: "用 Edge 从 GitHub 授权页返回时同样丢 cookie，日志里是 `oauth state mismatch`。\n复现步骤和 HAR 放在 `D:\\tmp\\oauth-har\\` 了，修好后告诉我，我在这边再验证一次。", t: NOW - 600 },
  ];
  const installJobs = new Map();
  const agents = new Map(DEVICES.map((d) => [d.id, agentsFor(d.id)]));
  const meta = new Map();
  let prompts = JSON.parse(JSON.stringify(PROMPTS));
  for (const f of prompts.fragments) if (f.builtin) f.default_text = f.text;
  PROMPTS.fragments.find((f) => f.id === "talk").default_text = "你的邮箱地址是 $address。需要 $owner 做决定时调用 ask_user；完成较长的任务后用 mail_send 发一封简短的总结给 me。";
  prompts.fragments.find((f) => f.id === "talk").default_text = PROMPTS.fragments.find((f) => f.id === "talk").default_text;

  function newRun(kind, sessionId, workdir, events, state = "running") {
    const rid = `r-${Math.random().toString(36).slice(2, 6)}`;
    const run = { rid, kind, session_id: sessionId, workdir, events: [], queue: events.slice(), state, started_at: NOW - 60, waiters: [], address: "claude-agent-nexus@林的 MacBook Pro" };
    runs.set(rid, run);
    drip(run);
    return run;
  }
  function push(run, ev) {
    run.events.push({ seq: run.events.length + 1, t: Date.now() / 1000, ...ev });
    if (ev.type === "status") run.state = ev.state;
    for (const resolve of run.waiters.splice(0)) resolve();
  }
  async function drip(run) {
    while (run.queue.length) {
      await wait((run.queue[0].type === "message" ? 220 : 160) * SLOW);
      push(run, run.queue.shift());
    }
  }
  function script(run, events) { run.queue.push(...events); if (run.queue.length === events.length) drip(run); }

  if (activeRun && SESSIONS.length) newRun("claude", "c-7f3a", `${P}/agent-nexus`, RUN_EVENTS);

  function preview({ preset, context = {}, draft }, device) {
    const doc = draft || prompts;
    const p = doc.presets.find((x) => x.id === preset) || doc.presets[0];
    const ids = [...(p.order || [])];
    for (const f of doc.fragments) if (!ids.includes(f.id)) ids.push(f.id);
    const values = VAR_VALUES(device, context);
    const segments = [], unknown = new Set(), texts = [];
    for (const id of ids) {
      const f = doc.fragments.find((x) => x.id === id);
      if (!f) continue;
      const on = p.enabled && id in p.enabled ? p.enabled[id] : f.enabled !== false;
      if (!on) continue;
      if (f.scope && f.scope !== "all" && f.scope !== (context.mode || "direct")) continue;
      if (f.kinds?.length && !f.kinds.includes(context.kind)) continue;
      const text = String(f.text || "").trim().replace(/\$\$|\$([A-Za-z_][A-Za-z0-9_]*)/g, (m, name) => {
        if (m === "$$") return "$";
        if (name in values) return values[name];
        unknown.add(`$${name}`);
        return m;
      });
      segments.push({ id, title: f.title, chars: text.length });
      texts.push(text);
    }
    return { text: texts.join("\n\n"), segments, unknown_vars: [...unknown], variables: values };
  }

  // §17: a tiny file system on 本机 for agent_file_stat / agent_file_read (relative paths join the folder)
  const AUTH_PY = Array.from({ length: 212 }, (_, i) => i === 40 ? '    expected = request.session.pop("oauth_state", None)' : i === 41 ? "    if not expected or not secrets.compare_digest(state or \"\", expected):" : `# auth.py line ${i + 1}`).join("\n");
  const FS = {
    [`${P}/agent-nexus/agent_nexus/auth.py`]: { kind: "text", text: AUTH_PY },
    [`${P}/agent-nexus/tests/test_oauth_state.py`]: { kind: "text", text: Array.from({ length: 48 }, (_, i) => i === 11 ? "def test_state_mismatch(client):" : `# test line ${i + 1}`).join("\n") },
    [`${P}/agent-nexus/docs/auth.md`]: { kind: "text", text: "# 登录\n\nSameSite=Lax：跨站导航会带上 cookie。\n" },
    [`${P}/agent-nexus/docs/设计/登录流程.md`]: { kind: "text", text: "# 登录流程\n\n1. 跳到 GitHub\n2. 回调\n" },
    [`${HOME}/notes/release-todo.md`]: { kind: "text", text: "- [ ] 更新 CHANGELOG\n- [ ] 打 tag\n" },
    [`${P}/agent-nexus/logs/auth.log`]: { kind: "text", text: Array.from({ length: 30 }, (_, i) => `2026-11-28 10:${String(i).padStart(2, "0")} WARNING oauth state mismatch (cookie lost?)`).join("\n") },
    [`${P}/agent-nexus/docs/shots/login-error.png`]: { kind: "image", mime: "image/png", png: true },
    [`${P}/agent-nexus/out/report.pdf`]: { kind: "binary", mime: "application/pdf", text: "%PDF-1.4 demo report\n" },
  };
  let pngB64 = null;
  function demoPng() {   // drawn once: a mock screenshot of the login page with its error banner
    if (pngB64) return pngB64;
    const c = document.createElement("canvas"); c.width = 480; c.height = 280;
    const g = c.getContext("2d");
    g.fillStyle = "#f4f6fa"; g.fillRect(0, 0, 480, 280);
    g.fillStyle = "#fff"; g.fillRect(120, 40, 240, 200); g.strokeStyle = "#d6dbe3"; g.strokeRect(120.5, 40.5, 240, 200);
    g.fillStyle = "#fdecea"; g.fillRect(136, 60, 208, 34); g.fillStyle = "#b42318"; g.font = "13px sans-serif"; g.fillText("登录已过期（state 不匹配），请重试", 146, 82);
    g.fillStyle = "#e7eaf0"; g.fillRect(136, 112, 208, 28); g.fillRect(136, 150, 208, 28);
    g.fillStyle = "#0a6879"; g.fillRect(136, 194, 208, 30); g.fillStyle = "#fff"; g.fillText("用 GitHub 登录", 196, 214);
    pngB64 = c.toDataURL("image/png").split(",")[1];
    return pngB64;
  }
  const b64utf8 = (text) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
  function fsResolve(raw, base) {
    let path = String(raw || "").trim();
    if (path.startsWith("~/")) path = `${HOME}/${path.slice(2)}`;
    else if (!path.startsWith("/")) { if (!base) return null; path = `${base}/${path.replace(/^\.\//, "")}`; }
    const out = [];
    for (const part of path.split("/")) { if (part === "..") out.pop(); else if (part && part !== ".") out.push(part); }
    return `/${out.join("/")}`;
  }
  function fsBytes(f) { return f.b64 !== undefined ? f.b64 : f.png ? demoPng() : b64utf8(f.text || ""); }
  const fsSize = (f) => Math.floor(fsBytes(f).length * 3 / 4);

  // §18: attachments land in a run's (or a stage's) tmp folder on the device; the fake keeps their bytes in FS
  const TMP_BASE = `${HOME}/.anx/agents/tmp`;
  const uploads = new Map();
  const attKind = (name) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(name) ? "image" : /\.(md|txt|log|json|csv|py|js|ts)$/i.test(name) ? "text" : "binary";
  const attMime = (name) => ({ png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", pdf: "application/pdf", md: "text/markdown", txt: "text/plain" })[(name.split(".").pop() || "").toLowerCase()] || "application/octet-stream";
  function userEvent(prompt, attachments) {
    const files = (attachments || []).map((path) => { const f = FS[path]; return f ? { path, name: path.split("/").pop(), size: fsSize(f), mime: f.mime } : null; }).filter(Boolean);
    return files.length ? { type: "user", text: prompt || "", attachments: files } : { type: "user", text: prompt || "" };
  }
  function attachOk(paths) {
    for (const path of paths || []) if (!FS[path] || !path.startsWith(`${TMP_BASE}/`)) return { error: `附件 ${String(path).split("/").pop()} 已经不在了，请重新添加`, code: "not_found" };
    return null;
  }

  const ops = {
    agent_attach_begin: () => { const stage = `s${Math.random().toString(16).slice(2, 10)}`; return { stage, dir: `${TMP_BASE}/${stage}/attachments` }; },
    agent_attach_put: async ({ rid, stage, name, size, offset, data, id }) => {
      await wait(params.get("slowup") ? 400 : 30);
      const folder = `${TMP_BASE}/${rid || stage}/attachments`;
      let up = id ? uploads.get(id) : null;
      if (!up) {
        if (offset) return { error: "上传要从头开始", code: "bad_request" };
        const safe = String(name || "file").replace(/[\\/:*?"<>|,%!^&]/g, "_");
        up = { id: `u${Math.random().toString(16).slice(2, 8)}`, folder, name: safe, size, parts: [] };
        uploads.set(up.id, up);
      }
      up.parts.push(atob(data || ""));
      const have = up.parts.reduce((n, x) => n + x.length, 0);
      if (have < size) return { id: up.id, have, size, done: false };
      let final = up.name, n = 1;
      while (FS[`${folder}/${final}`]) { n += 1; final = up.name.replace(/(\.[^.]*)?$/, (ext) => ` (${n})${ext || ""}`); }
      const path = `${folder}/${final}`;
      FS[path] = { kind: attKind(final), mime: attMime(final), b64: btoa(up.parts.join("")), text: attKind(final) === "text" ? new TextDecoder().decode(Uint8Array.from(up.parts.join(""), (c) => c.charCodeAt(0))) : undefined };
      uploads.delete(up.id);
      return { id: up.id, have, size, done: true, path, name: final, display: name, mime: attMime(final) };
    },
    agent_attach_drop: ({ path, id }) => { if (id) uploads.delete(id); if (path) delete FS[path]; return { ok: true }; },
    agent_file_stat: ({ base, paths = [] }, device) => ({ files: paths.map((raw) => {
      const path = fsResolve(raw, base);
      const f = device && device !== "dev-mbp" ? null : FS[path];
      if (!f) return { raw, path, exists: false };
      return { raw, path, exists: true, dir: false, size: fsSize(f), mtime_ns: 1, kind: f.kind, mime: f.mime || "text/plain", name: path.split("/").pop() };
    }) }),
    agent_file_read: ({ path, mode = "bytes", line = 1, offset = 0, length = 1 << 20 }) => {
      const f = FS[path];
      if (!f) return { error: `文件不存在：${path}`, code: "missing" };
      const name = path.split("/").pop();
      if (mode === "text") {
        const lines = String(f.text || "").split("\n");
        const first = Math.max(1, Math.min(line, lines.length) - 250);
        return { path, name, size: fsSize(f), text: lines.slice(first - 1, first - 1 + 1000).join("\n"), first_line: first, total_lines: lines.length, truncated: first > 1, scanned_all: true, encoding: "utf-8" };
      }
      const all = atob(fsBytes(f));
      const part = all.slice(offset, offset + length);
      return { path, name, size: all.length, offset, data: btoa(part), mime: f.mime || "application/octet-stream", eof: offset + part.length >= all.length };
    },
    agent_info: (p, device) => ({ agents: agents.get(device || "dev-mbp").map((a) => ({ ...a, install: installJobs.get(`${device}:${a.kind}`)?.public() || null })),
      // ?caps=old: a device whose AgentHub predates attachments and moving sessions (no paperclip, no relocation)
      ...(params.get("caps") === "old" ? {} : { caps: { attachments: { chunk: 512 * 1024, file_max: 50 * 1024 * 1024, files: 10, message_max: 100 * 1024 * 1024 }, relocate: 1 } }) }),
    agent_sessions: () => ({ sessions: SESSIONS.map((s) => ({ ...s, ...(meta.get(s.id) || {}) })) }),
    agent_session_read: ({ id }) => { if (!HISTORY[id]) return { events: [{ type: "user", text: SESSIONS.find((s) => s.id === id)?.title || "" }, { type: "message", role: "assistant", text: "（演示数据：这个会话只有标题。）" }, { type: "status", state: "done" }] }; return { events: HISTORY[id], address: "claude-agent-nexus@林的 MacBook Pro" }; },
    agent_session_meta: (p) => { const { kind, id, delete_file, ...rest } = p; if (delete_file) { const i = SESSIONS.findIndex((s) => s.id === id); if (i >= 0) SESSIONS.splice(i, 1); } else meta.set(id, { ...(meta.get(id) || {}), ...rest }); return { ok: true }; },
    agent_run_list: () => ({ runs: [...runs.values()].filter((r) => r.state !== "closed").map(({ rid, kind, session_id, workdir, state, started_at, address }) => ({ rid, kind, session_id, workdir, state, started_at, address })) }),
    agent_run_open: ({ kind, workdir, resume_id, prompt, model, attachments, stage }) => {
      const bad = attachOk(attachments);
      if (bad) return bad;
      const session = resume_id ? SESSIONS.find((x) => x.id === resume_id) : null;
      let moved = null;
      if (session?.cwd_missing && workdir && workdir !== session.cwd) {
        // §14: Codex / Claude continue in the new folder (Claude from a copy of the transcript); others refuse
        if (!["claude", "codex"].includes(session.kind)) return { error: "这个 CLI 的会话和它原来的目录绑定，不能换到别的目录续接；可以在其他目录新开对话", code: "relocate_unsupported" };
        moved = session.cwd;
        Object.assign(session, { cwd: workdir, cwd_missing: false });
      }
      const run = newRun(kind, resume_id || `n-${Date.now()}`, workdir, [], "starting");
      if (stage) { runs.delete(run.rid); run.rid = stage; runs.set(stage, run); }   // the stage folder is the run's
      run.model = model || "";
      push(run, { type: "status", state: "starting" });
      if (model) push(run, { type: "model", model });
      push(run, userEvent(prompt, attachments));
      script(run, demoReply(prompt, run.model, attachments));
      return { rid: run.rid, session_id: run.session_id, address: run.address, model: model || null, ...(moved ? { moved_from: moved } : {}) };
    },
    agent_run_send: ({ rid, prompt, model, attachments }) => {
      const run = runs.get(rid);
      if (!run) return { error: "对话已结束", code: "not_found" };
      const bad = attachOk(attachments);
      if (bad) return bad;
      if (model !== undefined && model !== run.model) { run.model = model; push(run, { type: "model", model: model || null }); }
      push(run, userEvent(prompt, attachments));
      script(run, demoReply(prompt, run.model, attachments));
      return { ok: true, model: run.model || null };
    },
    // §16.7: what the model menu offers (Codex asks its app-server: model/list)
    agent_models: ({ kind }) => kind === "codex"
      ? { kind, models: [{ id: "gpt-6.1-sol", label: "GPT-6.1 Sol", desc: "最新", default: true }, { id: "gpt-6-astra", label: "GPT-6 Astra" }, { id: "gpt-5.6-terra", label: "GPT-5.6 Terra" }], default: "gpt-6.1-sol", source: "cli" }
      : { kind, models: agentsFor().find((a) => a.kind === kind)?.models?.hints || [], default: kind === "claude" ? "sonnet" : "", source: "hints" },
    agent_run_read: async ({ rid, cursor = 0, wait: w = 20 }) => {
      const run = runs.get(rid);
      if (!run) return { error: "对话已结束", code: "not_found" };
      if (run.events.length <= cursor) await Promise.race([new Promise((resolve) => run.waiters.push(resolve)), wait(Math.min(w, 20) * 1000)]);
      return { events: run.events.slice(cursor), cursor: run.events.length, state: run.state };
    },
    agent_run_answer: ({ rid, approval_id, allow, remember }) => {
      const run = runs.get(rid);
      push(run, { type: "approval", id: approval_id, state: allow ? "allowed" : "denied", remembered: remember });
      script(run, allow ? [
        { type: "tool", id: "t9", name: "Bash", title: "git push -u origin fix/oauth-callback", status: "ok", input: { command: "git push -u origin fix/oauth-callback" }, output: "To github.com:lin/agent-nexus.git\n * [new branch]      fix/oauth-callback -> fix/oauth-callback" },
        { type: "message", role: "assistant", text: "已推送到 `fix/oauth-callback`。要开 PR 的话告诉我。" },
        { type: "usage", context: { used: 961300, limit: 1000000, model: "claude-opus-4-5" }, turn: { input: 9620, output: 840, cached: 880000, cost_usd: 0.031 } },
        { type: "status", state: "done" },
      ] : [{ type: "message", role: "assistant", text: "好的，没有推送。改动还在工作区里。" }, { type: "status", state: "done", usage: { input: 2100, output: 60, cost_usd: 0.006 } }]);
      return { ok: true };
    },
    agent_run_stop: ({ rid }) => { const run = runs.get(rid); run.queue.length = 0; push(run, { type: "status", state: "stopped" }); return { ok: true }; },
    agent_install: ({ kind }, device) => {
      const key = `${device}:${kind}`;
      const job = { id: `job-${kind}`, progress: 0, lines: [], state: "running", public() { return { job_id: this.id, state: this.state, progress: this.progress }; } };
      installJobs.set(key, job);
      const steps = [["$ npm i -g @deepseek-ai/dsh", 8], ["npm http fetch GET 200 https://registry.npmjs.org/@deepseek-ai%2fdsh 412ms", 22], ["npm http fetch GET 200 https://registry.npmjs.org/zstd-napi 380ms", 38],
        ["added 214 packages in 9s", 70], ["$ dsh --version", 88], ["0.2.2-rc.5", 100]];
      (async () => {
        for (const [line, pct] of steps) {
          await wait(900 * SLOW);
          job.lines.push(line); job.progress = pct;
        }
        job.state = "done";
        const a = agents.get(device || "dev-mbp").find((x) => x.kind === kind);
        Object.assign(a, { installed: true, version: "0.2.2-rc.5", path: `${HOME}/.npm-global/bin/dsh`, login: "unknown" });
        installJobs.delete(key);
      })();
      return { job_id: job.id };
    },
    agent_install_job: ({ job_id, since = 0 }, device) => {
      const job = [...installJobs.values()].find((j) => j.id === job_id);
      if (!job) return { state: "done", progress: 100, lines: [], next: since };
      return { state: job.state, progress: job.progress, lines: job.lines.slice(since), next: job.lines.length, stage: job.progress < 40 ? "下载中" : job.progress < 80 ? "安装中" : "校验版本" };
    },
    agent_dirs: ({ path } = {}, device) => {
      const own = device && device !== "dev-mbp" ? DIRS[device] : null;
      if (own && !path) return own;
      if (own) return { path, parent: null, dirs: [] };
      if (!path) return { recent: [`${P}/agent-nexus`, `${P}/website`, `${HOME}/notes`], workspaces: [{ name: "agent-nexus", path: `${P}/agent-nexus` }, { name: "billing-service", path: `${P}/billing-service` }], roots: [{ name: "主目录", path: HOME }], home: HOME };
      const kids = TREE[path] || [];
      return { path, parent: path === HOME ? null : path.split("/").slice(0, -1).join("/") || null, dirs: kids.map((name) => ({ name, path: `${path}/${name}` })) };
    },
    agent_prompts: () => JSON.parse(JSON.stringify({ ...prompts, variables: undefined })),
    agent_prompt_save: (p) => { if (p.reset) { const f = prompts.fragments.find((x) => x.id === p.reset); if (f) { f.text = f.default_text; f.modified = false; } } else prompts = JSON.parse(JSON.stringify({ fragments: p.fragments, presets: p.presets })); return JSON.parse(JSON.stringify(prompts)); },
    agent_prompt_preview: (p, device) => preview(p, device),
    agent_tmp_list: () => ({ items: TMP, total: TMP.reduce((s, x) => s + x.size, 0) }),
    agent_tmp_clean: ({ rid, older_than_days }) => {
      const gone = TMP.filter((x) => !x.active && (rid ? x.rid === rid : NOW - x.updated_at > older_than_days * 86400));
      for (const g of gone) TMP.splice(TMP.indexOf(g), 1);
      return { removed: gone.length, freed: gone.reduce((s, x) => s + x.size, 0) };
    },
  };

  let demoUsed = 21000;
  function demoReply(prompt, model = "", attachments = []) {
    const short = String(prompt || "").slice(0, 40) || "看看附件";
    demoUsed += 3420;
    const seen = (attachments || []).length ? [{ type: "message", role: "assistant", text: `（演示）收到 ${attachments.length} 个附件：\n\n${attachments.map((p) => `- ${p}`).join("\n")}\n\n`, delta: false }] : [];
    return [
      { type: "status", state: "running" },
      { type: "thinking", text: `理解任务：${short}` },
      ...seen,
      { type: "tool", id: `t${Math.random()}`, name: "Bash", title: "git status --short", status: "ok", input: { command: "git status --short" }, output: " M agent_nexus/auth.py\n?? tests/test_oauth_state.py" },
      { type: "message", role: "assistant", text: "（演示）我看了一下工作区：", delta: false },
      { type: "message", role: "assistant", text: "有 1 个修改、1 个新文件，", delta: true },
      { type: "message", role: "assistant", text: "都和这次修复有关。", delta: true },
      { type: "usage", context: { used: demoUsed, limit: 200000, model: model || "demo-model" }, turn: { input: 3200, output: 220, cost_usd: 0.012 } },
      { type: "status", state: "done" },
    ];
  }

  return {
    async call(op, p = {}, device) {
      await wait(op === "agent_run_read" ? 0 : 60 + Math.random() * 80);
      const fn = ops[op];
      if (!fn) return { error: `未知操作 ${op}`, code: "unknown_op" };
      if (device === "dev-nas") return { error: "设备离线", code: "offline" };
      try { return await fn(p, device); } catch (error) { return { error: String(error.message || error), code: "internal" }; }
    },
    // ?gw=off: 本机 not joined (the local console then lists only 本机 and says why)
    // §17: the local console loads an external image on a click (?extimg=off: like the cloud console, a new tab)
    externalImages: params.get("extimg") !== "off",
    async devices() { await wait(40); return params.get("gw") === "off" ? [{ ...DEVICES[0], gid: undefined }] : DEVICES; },
    devicesNote: () => (params.get("gw") === "off" ? "加入网关后，也能在其他设备上运行 Agent" : ""),
    // files/1 listings of another device (the local console's /api/remote/devices/{id}/files)
    async files(device, path) {
      await wait(120);
      if (device === "dev-nas") throw new Error("设备离线");
      const at = path || "/home/user";
      if (!FILES[at] && path) throw new Error(`不是文件夹：${path}`);
      return { path: at, parent: at === "/" ? "" : at.split("/").slice(0, -1).join("/") || "/", home: "/home/user", sep: "/",
        entries: (FILES[at] || []).map((name) => ({ name, dir: true })) };
    },
    // the ask arrives a moment after the run's events (as ask_user would, mid-run)
    async conversation(address) { await wait(80); return { messages: mails, asks: asks.filter((a) => Date.now() / 1000 >= a.visible_at) }; },
    async answerAsk(id, answer) {
      const ask = asks.find((a) => a.ask_id === id);
      if (ask) Object.assign(ask, { state: "answered", answer });
      return { ok: true };
    },
  };
}
