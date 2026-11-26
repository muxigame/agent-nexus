# Agent 对话（agent chat）设计

> 状态：实现中。对应 owner 需求：概览页集成聊天；选设备和 CLI；CLI 安装/登录状态与安装进度；会话发现与管理；
> ask_user / 邮件 / 会话打通；工作目录；系统提示词片段 + `$变量`；临时目录管理。

## 1. 两层模型：运行位置 与 工作上下文

一次对话（run）有两个独立的“设备”概念：

| 层 | 含义 | 决定什么 |
|---|---|---|
| **运行位置**（runner device） | CLI 进程跑在哪台设备上 | 用哪台设备上装好的 CLI、哪份登录态；会话发现和管理也在这台设备上做 |
| **工作上下文**（context） | Agent 要操作的设备和目录 | 注入提示词的 `$device` / `$workdir`；是否需要经网关工具操作 |

- 选 **本机**：运行位置 = 当前控制台所在设备（本机控制台）。发现/管理的是本机的 CLI 和会话，直接调用进程内的 AgentHub，不经网关，云端断开也能用。
- 选 **远端设备 X**：运行位置 = X。所有发现、管理、运行请求都发给 X 上的 AgentHub（云端控制台：网关直接转发；本机控制台：经网关中转）。
- 工作上下文默认 **与运行位置相同**（模式 `direct`）：CLI 在 `$workdir` 里直接用自己的工具工作，不需要网关。
- 高级选项 **经网关操作其他设备**（模式 `gateway`）：CLI 在运行设备上跑，但要操作上下文设备 Y 的目录 D。网关为 (Y, D) 签发一个
  workspace handle（`use_device` 的 `workspace_id`），提示词片段“网关工作方式”被启用并带上 `$handle`、`$context_device`、`$context_workdir`。
  这要求运行设备的 CLI 已配置 Agent Nexus 网关 MCP 连接器；状态栏显示是否检测到。

`direct` 模式下网关仍然用于 ask_user / 邮件：运行开始时网关为 (运行设备, workdir) 签发 handle 并绑定邮箱地址，
这样 Agent 调 `ask_user`、`mail_send` 时能落到同一个会话里。网关不可达时（本机 + 云端断开），对话照常进行，只是没有 ask_user / 邮件。

## 2. CLI 注册表（多 CLI）

`agent_nexus/agents/registry.py` 定义每种 CLI（`kind`）：

| kind | 名称 | 可执行文件 | 安装（Windows / macOS / Linux） | 登录检测 | 对话适配器 |
|---|---|---|---|---|---|
| `claude` | Claude Code | `claude` | `npm i -g @anthropic-ai/claude-code`（或官方 install.ps1 / install.sh） | `claude auth status`（JSON，exit 0 = 已登录，`authMethod`）；命令不可用时看 `~/.claude/.credentials.json` / `ANTHROPIC_API_KEY` | `claude-stream`：`claude -p --output-format stream-json --verbose --input-format stream-json` |
| `codex` | Codex | `codex` | `npm i -g @openai/codex`（或官方 install 脚本） | `agent_cli.codex_signed_in`；`codex login status` | `codex-exec`：每轮一个 `codex exec --json`（续接用 `exec resume <id>`） |
| `dsh` | DeepSeek Harness | `dsh` | `npm i -g @deepseek-ai/dsh`（需要 Node.js ≥ 22） | 环境里有 `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY`，或 `~/.dsh/settings.yaml` 存在 → ok；否则 unknown | `acp`：`dsh --profile acp`（ACP over stdio）；失败时退化为 `text`：`dsh --profile headless <prompt>` |
| `gemini` | Gemini CLI | `gemini` | `npm i -g @google/gemini-cli` | `GEMINI_API_KEY` / `~/.gemini/oauth_creds.json` | `acp`：`gemini --experimental-acp` |

每个定义包含：`kind, label, binaries, install{win,mac,linux}, version_args, login_probe, sessions(发现规则), adapter, resume 支持, 权限模式表`。
用户可在 `agent-nexus.json` 的 `agents.custom` 里覆盖命令模板或添加新 CLI（同样的字段，适配器选 `text` / `jsonl`）。

**状态**（`agent_info`）：`{kind,label,installed,path,version,login:"ok|missing|unknown",install:{job_id,state,progress,log_tail}|null,supports:{resume,approvals,stream}}`。

**安装**：后台任务（参照 `plugins.manage.Jobs`），以执行账户运行安装命令，逐行收集输出，按 npm/安装脚本的输出估算进度（阶段：下载 → 安装 → 校验版本）。
完成后重新探测。没有 npm 时提示先装 Node.js 或用官方安装脚本。

## 3. 会话发现与管理（按 CLI 分组）

`agent_nexus/agents/sessions.py`，在运行设备上执行，以执行账户的 HOME 为准：

- Claude：`~/.claude/projects/<mangled cwd>/<id>.jsonl`
- Codex：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（`$CODEX_HOME` 优先）
- dsh / gemini：注册表里的 glob；未知格式时只列文件和时间。

每条会话：`{kind,id,title,cwd,updated_at,created_at,messages,size,source:"disk",managed:bool,pinned,archived,alias}`。
标题取首条用户消息（截断）或 CLI 自带的 summary。读取有大小上限并缓存（mtime + size）。

管理元数据（重命名、置顶、归档、隐藏）存在 `<root>/.anx/agents/sessions.json`，不改 CLI 自己的文件。
“删除”= 从列表隐藏；可选“删除会话文件”，需二次确认，只删除注册表认识的会话文件。

`agent_session_read`：把会话文件规范化成统一事件（见 §4），用于在聊天区展示历史。

UI：侧栏顶部是 CLI 标签（Claude Code / Codex / DeepSeek / 全部），下面按最近时间列出会话，显示工作目录和时间，可搜索；
置顶在前、归档折叠。点会话 = 加载历史并可以续接。

## 4. 统一事件

所有适配器把 CLI 输出转成同一种事件，前端只认这一种：

```json
{"seq": 12, "t": 1764150000.1, "type": "message", "role": "assistant", "text": "…"}
{"type": "thinking", "text": "…"}
{"type": "tool", "id": "call_1", "name": "Bash", "title": "npm test", "input": {…}, "status": "running|ok|error", "output": "…(截断)"}
{"type": "file_change", "path": "src/a.ts", "kind": "edit|add|delete", "diff": "…"}
{"type": "approval", "id": "ap_1", "tool": "Bash", "summary": "rm -rf build", "state": "pending|allowed|denied"}
{"type": "ask", "ask_id": "q_…", "title": "…", "options": […], "state": "open|answered"}
{"type": "mail", "message_id": "m_…", "from": "codex@pc", "subject": "…", "body": "…"}
{"type": "status", "state": "starting|running|idle|done|failed|stopped", "session_id": "…", "usage": {"input": 0, "output": 0, "cost_usd": 0}}
{"type": "error", "text": "…"}
{"type": "user", "text": "…"}
```

## 5. 运行（run）

`agent_nexus/agents/runner.py`。一个 run = 一个会话窗口，在运行设备上：

- `agent_run_open {kind, workdir, resume_id?, prompt, permission, preset?, fragments?, context?}` → `{rid, session_id?}`
- `agent_run_send {rid, prompt}`：发下一轮（claude-stream 写入 stdin；codex-exec 启动 `exec resume`）
- `agent_run_read {rid, cursor, wait≤20}` → `{events, cursor, state}`（长轮询，与 `term_read` 一样有上限，不取消进行中的调用）
- `agent_run_answer {rid, approval_id, allow, remember?}`：回应审批
- `agent_run_stop {rid}`：中断当前轮；`agent_run_close {rid}`；`agent_run_list {}`：在跑的 run

进程一律以执行账户启动（`accounts.effective(root, "terminal")` 的 `popen_args` / env），工作目录必须存在且在允许范围内。
事件缓冲每个 run 最多 2000 条 / 4 MB，超出丢最旧的并记录 `dropped`。run 空闲 30 分钟自动关闭进程（会话文件仍在，可续接）。

**权限模式**（每个 CLI 映射到自己的参数）：

| 模式 | Claude | Codex | dsh |
|---|---|---|---|
| `ask`（默认）需要审批 | `--permission-prompt-tool` 指向 anx 的审批 MCP 工具 | `--sandbox workspace-write --ask-for-approval never`（exec 模式不能交互审批，越界操作会被沙箱拒绝） | ACP `session/request_permission` → 审批卡片 |
| `edits` 自动改文件 | `--permission-mode acceptEdits` | `--sandbox workspace-write` | — |
| `readonly` 只读 | `--permission-mode plan` | `--sandbox read-only` | — |
| `full` 完全信任 | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | — |

`full` 在 UI 上需要明确勾选，且不作为默认值。

**审批**（Claude `ask` 模式）：run 启动时生成一个 `--mcp-config`，其中有一个 stdio MCP 服务器 `anx agent-approve --run <rid> --port <p>`，
它把审批请求 POST 到本机 AgentHub 的本地回环端点（带一次性令牌），AgentHub 产生 `approval` 事件并等待回应（默认 10 分钟超时 = 拒绝）。

## 6. 系统提示词：片段 + `$变量`

`agent_nexus/agents/prompts.py`，数据存在 `<root>/.anx/agents/prompts.json`：

```json
{"fragments": [{"id": "identity", "title": "身份与环境", "text": "…$device…", "enabled": true, "builtin": true,
                "scope": "all|direct|gateway", "kinds": ["claude","codex"]}],
 "presets": [{"id": "default", "name": "默认", "order": ["identity", "workdir", …], "enabled": {…}}]}
```

- 内置片段可编辑，可“还原”；可新增、排序、启用/停用、按 CLI 和模式限定。
- 变量：`$device $device_id $os $workdir $tmpdir $session $address $agent $date $time $owner $gateway $handle $context_device $context_workdir`，
  未知变量原样保留并在预览里标黄；`$$` 输出 `$`。
- `prompt_preview {preset, context}` 返回 `{text, segments:[{id,title,chars}], unknown_vars}`。
- 传给 CLI：Claude `--append-system-prompt <text>`；Codex `-c developer_instructions=<text>`；dsh / 其他：作为首条消息前缀。

内置片段（中文）：身份与环境、工作目录与临时目录、与用户沟通（ask_user / mail）、团队协作（mail_who / @all）、
网关工作方式（仅 gateway 模式，含 use_device + $handle）、项目说明（提示读取 AGENTS.md / CLAUDE.md）。

## 7. 工作目录

- `agent_dirs {path?}`：列出 `path` 下的子目录（只列目录），根是执行账户的 HOME 和设备已登记的工作区；不允许 `..` 越界。
- 最近使用的目录存在 `<root>/.anx/agents/recent.json`，每个 CLI 也会从发现的会话里得到常用 cwd。
- UI：目录选择器 = 最近使用 + 已登记工作区 + 浏览。

## 8. 临时目录

- 每个 run 一个 `$tmpdir` = `<root>/.anx/agents/tmp/<rid>`，以执行账户创建（Unix 下 chown）。
- `agent_tmp_list` → `[{rid,path,size,files,updated_at,session,kind,active}]`；`agent_tmp_clean {rid?|older_than_days?}`。
- 自动清理：run 关闭 7 天后删除；总量超过 2 GB 时从最旧删起；同时清理 `.anx/wake/*.log` 超过 14 天的。

## 9. 传输

- 设备端：`AgentHub.handlers()` 注册上述反向 RPC，特性 `agents/1`。
- 网关（云端控制台）：`/api/devices/{id}/agents/{op}`，owner 鉴权（cookie + 同源，或 bearer）或设备凭据（与终端一致，设备锁定时拒绝），
  只允许白名单里的 op，原样 `peer.call`。长轮询 `run_read` 的 wait ≤ 20 s。
- 本机控制台：`/api/agents/local/{op}` 直接调进程内 AgentHub；`/api/agents/remote/{device_id}/{op}` 经网关中转（设备凭据，`lock.require_unlocked()`）。
- 网关 op `agent_handle {device_id, workdir, kind, name}`：签发 workspace handle 并绑定邮箱地址（复用 `gateway.select` 的逻辑），供 run 注入。

## 10. 会话与 ask_user / 邮件打通

- run 绑定邮箱地址 `<name>@<device>`（kind = CLI kind），`$address` 注入提示词。
- 聊天区时间线 = run 事件 + 该地址发给 me 的邮件 + 该地址的 ask（open 的显示为可回答的卡片，回答走现有 `answer_ask`）。
- 前端轮询 `/api/inbox/conversation?address=` （新 op：`me` 与该地址之间的消息 + asks，按时间）。

## 11. 界面

概览页顶部是对话区（云端和本机控制台共用 `ui/chat.js`），下面保留原来的状态卡片。概览不再整页 10 秒重绘，拆成独立刷新的区块。

```
┌ 会话（按 CLI 分组） ┐┌ 运行设备 [本机 ▾]  Agent [Codex ● 已就绪 v0.172 ▾]  目录 [~/proj ▾]  权限 [需要审批 ▾]  ⋯ ┐
│ [Claude][Codex][DS] ││  (消息流：文字 / 工具调用折叠 / 文件改动 / 审批卡片 / ask 卡片 / 邮件)              │
│ 🔍                  ││                                                                                    │
│ ● 修复登录 bug  2m  ││                                                                                    │
│   ~/proj            ││ ┌──────────────────────────────────────────────────────────────┐ [停止] [发送]       │
└─────────────────────┘└ └ 输入框（Enter 发送，Shift+Enter 换行）                          ┘                    ┘
```

- Agent 下拉里每个 CLI 显示状态：未安装（安装按钮 → 进度条）/ 安装中 xx% / 未登录（“在终端登录”按钮打开该设备终端并键入登录命令）/ 就绪。
- “⋯”：提示词预设、工作上下文（同设备直接 / 经网关操作其他设备 + 目录）、临时目录。
- 新页面“Agent”（侧栏）：提示词片段编辑器（左：片段列表，勾选/排序/编辑/还原，按 全部/已启用/已停用 筛选；右：变量替换后的最终提示词预览和段数）、
  临时目录管理、CLI 管理（安装/更新/登录状态）。

## 12. ACP 适配器（dsh、gemini）

ACP（Agent Client Protocol）：JSON-RPC 2.0，stdio 上每行一条。客户端（anx）流程：

1. `initialize {protocolVersion: 1, clientCapabilities: {fs: {readTextFile: false, writeTextFile: false}, terminal: false}}`
2. 新会话 `session/new {cwd, mcpServers: []}` → `{sessionId}`；续接 `session/load {sessionId, cwd, mcpServers: []}`（agentCapabilities.loadSession 为真时）
3. 每轮 `session/prompt {sessionId, prompt: [{type: "text", text}]}` → `{stopReason}`；期间收到通知 `session/update {sessionId, update}`：
   `sessionUpdate` = `agent_message_chunk` / `agent_thought_chunk`（`content.text`）、`tool_call` / `tool_call_update`
   （`toolCallId, title, kind, status: pending|in_progress|completed|failed, content, rawInput`）、`plan`（`entries`）、`user_message_chunk`（加载历史时）。
4. 代理发来的请求 `session/request_permission {sessionId, toolCall, options: [{optionId, name, kind: allow_once|allow_always|reject_once|reject_always}]}`
   → 产生 `approval` 事件；回应 `{outcome: {outcome: "selected", optionId}}`，停止时 `{outcome: {outcome: "cancelled"}}`。
5. 中断：通知 `session/cancel {sessionId}`。未支持的代理请求（fs/*、terminal/*）回 JSON-RPC 错误 -32601。

适配器对字段宽松：未知的 `sessionUpdate` 忽略，缺字段不报错。

## 13. 模块接口（实现约定）

包 `agent_nexus/agents/`：

```python
# registry.py
@dataclass
class AgentDef:
    kind: str; label: str; binaries: tuple[str, ...]
    install: dict[str, list[str]]      # {"windows": argv, "darwin": argv, "linux": argv}
    version_args: tuple[str, ...]
    adapter: str                        # "claude-stream" | "codex-exec" | "acp" | "text"
    command: list[str]                  # 适配器启动模板，占位符 {bin} {workdir} {resume_id} {prompt} {system_prompt}
    resume: bool; approvals: bool
    permission_flags: dict[str, list[str]]   # mode -> extra argv
    session_globs: list[str]            # 相对执行账户 HOME，如 ".claude/projects/*/*.jsonl"
def definitions(cfg: dict | None = None) -> dict[str, AgentDef]       # 内置 + agents.custom 覆盖
def locate(d: AgentDef, env: dict) -> str | None                      # 复用 agent_cli.find 的路径知识
def probe(d: AgentDef, env: dict, *, timeout=6) -> dict                # {installed,path,version,login}
class Installs:                                                         # start(kind, env, popen_args) -> job_id; get(job_id, since)

# sessions.py
def discover(defs, home: Path, *, kind=None, limit=200) -> list[dict]
def read(defs, home: Path, kind: str, session_id: str, *, max_events=2000) -> list[dict]   # 统一事件
class SessionMeta:  # <root>/.anx/agents/sessions.json：rename/pin/archive/hide

# adapters.py + runner.py
class Adapter:  # build(def, opts) -> argv; feed(line) -> list[event]; send(text) -> bytes|None 或需要新进程; answer(approval)
class Runs:     # open/send/read/answer/stop/close/list；spawn 用 env + popen_args；事件缓冲 + 条件变量长轮询

# prompts.py
class Prompts:  # load/save/fragments CRUD/reset/presets；render(preset, vars) -> {text, segments, unknown_vars}
def variables(context: dict) -> dict

# workdirs.py / tmpdirs.py
def list_dirs(path, roots) -> dict; class Recent
class TmpDirs:  # create(rid, account) -> Path; list(); clean(rid=None, older_than_days=None); sweep()

# hub.py
class AgentHub:  # 组合以上；handlers() -> {"agent_info": …, "agent_install": …, "agent_install_job": …,
                 #  "agent_sessions": …, "agent_session_read": …, "agent_session_meta": …,
                 #  "agent_run_open"/"send"/"read"/"answer"/"stop"/"close"/"list",
                 #  "agent_dirs", "agent_prompts", "agent_prompt_save", "agent_prompt_preview",
                 #  "agent_tmp_list", "agent_tmp_clean"}
FEATURE = "agents/1"
```

所有 handler：入参 dict，出参 dict；出错返回 `{"error": "中文说明", "code": "…"}`，不抛异常。
