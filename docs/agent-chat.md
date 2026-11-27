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

Codex 多代理 / 分叉（字段名和分叉点的计数方式按真实 rollout 推测，未在所有版本上核实）：
- 子代理线程（`session_meta.parent_thread_id` / `source.subagent.thread_spawn`）不单独列出，并入顶层会话的 `subagents`（个数，含嵌套）；
  按 id 仍可打开（`parent_id`、`agent_nickname`）。父会话不在列表里（已删除或超出列表窗口）的子代理作为普通会话列出（`orphan: true`）。
  `source.subagent` 是字符串（review、compact 等）的线程没有父 id，照常列出。
- 分叉（`forked_from_id`）是独立会话，带 `forked_from`；标题取分叉点之后自己的第一条提问。
- 同一线程 id 写在多个 rollout 里时合并成一条：续接用最新文件；历史按时间拼接，新文件开头复制的旧历史（只比内容，不比时间戳）跳过一次；标题取最早文件的提问。
- 界面：会话行显示「子代理 N」。

管理元数据（重命名、置顶、归档、隐藏）存在 `<root>/.anx/agents/sessions.json`，不改 CLI 自己的文件。
anx 以 root 运行（Ubuntu 系统服务）而执行账户是普通用户时，发现、读取、删除只认**执行账户自己拥有的普通文件**（`lstat`，不跟随文件符号链接）：
执行账户在 `~/.claude/projects/` 里放一个指向 root 目录的符号链接，既列不出也删不掉里面的文件。
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

`full` 在 UI 上需要明确勾选，且不作为默认值。界面的权限菜单按 `agent_info.permissions` 列出该 CLI 支持的模式（不支持的灰显并注明），
`supports.approvals` 为假时（Codex）“需要审批”显示为“沙箱内自动执行”，如实说明不会逐项询问。

**远程访问策略**（`agent-nexus.json` 的 `agents`）：本机控制台总能用进程内的 AgentHub；其他设备和云端控制台只有在 `agents.remote` 为真时才看得到
`agents/1`——Agent 能以执行账户运行命令，和终端一样，所以它默认跟随 `terminal.enabled`（关掉远程终端也就关掉了远程 Agent）。
`agents.allow_full_remote`（默认同样跟随 `terminal.enabled`）为假时，远程调用以 `full` 打开 run 会被拒绝（`code: permission_disabled`），
`agent_info` 带 `policy: {full: false}`，界面把“完全信任”灰显。

**同一会话只跑一个进程**：`agent_run_open` 带的 `resume_id` 已经有在跑的 run（另一个标签页、云端和本机同时打开）时，不再启动第二个 CLI，
而是返回那个 run（`attached: true`，有 prompt 时转给它；它正忙则 `code: busy` 并带 `rid`），界面切到那个对话。

**超时与回收**：所有审批（审批 MCP、ACP `session/request_permission`、claude `can_use_tool`）10 分钟无人回应即拒绝；
一轮在 4 个空闲周期（2 小时）里既没有任何输出、也没有人读取时视为卡住，结束进程、让出名额（会话仍可续接）。
关机（`close_all`）同步结束所有 CLI：先给每个进程组发 SIGTERM，总共等 2 秒，再 SIGKILL 剩下的（Windows：`taskkill /T /F`）。

**“总是允许”的范围**：shell 类工具（Bash / shell / execute）只记住**完全相同的命令**（空白归一化），不是命令的程序名——
记住 `git status` 不会放行 `git push --force` 或 `ls && curl … | sh`；其他工具按工具名记住。卡片上写明范围。

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
- 网关 op `agent_release {workspace_id}`：run 关闭（或打开失败）时由 AgentHub 调用，结束这个 handle，地址没有别的活跃 handle 时标记为已结束；
  下一个在同一目录开的对话因此拿回 `claude-proj`，而不是一路漂到 `-2`、`-3`……只接受 `agent_handle` 签发的 handle，且只能由申请它的设备、它指向的设备或 owner 释放。
  AgentHub 在找网关要 handle 之前先做本地检查（目录在允许范围内、CLI 已安装、会话 ID 合法），失败的打开不占用地址。

## 10. 会话与 ask_user / 邮件打通

- run 绑定邮箱地址 `<name>@<device>`（kind = CLI kind），`$address` 注入提示词。
- 聊天区时间线 = run 事件 + 该地址发给 me 的邮件 + 该地址的 ask（open 的显示为可回答的卡片，回答走现有 `answer_ask`）。
- 前端轮询 `/api/inbox/conversation?address=` （新 op：`me` 与该地址之间的消息 + asks，按时间）。

## 11. 界面

概览页顶部是对话区（云端和本机控制台共用 `ui/chat.js`），下面保留原来的状态卡片。概览不再整页 10 秒重绘，拆成独立刷新的区块。

```
会话 [新对话] [⇤] │ [⇥] 修复登录 bug                                                    ⋯
[Claude][Codex][DS] │      (消息流居中，最宽 820px，直接放在页面背景上：文字 / 工具调用折叠 /
🔍                  │       文件改动 / 审批卡片 / ask 卡片 / 邮件)
● 修复登录 bug  2m  │
  ~/proj            │      ┌ 给 Codex 发消息 ──────────────────────────────────────────┐
                    │      │ 💻 本机  ● Codex  📁 proj  🛡 需要审批        ◔ [停止] [↑] │
                    │      └────────────────────────────────────────────────────────────┘
```

- 头部是一行轻量标题（不是工具条）：侧栏开关、当前会话标题、⋯。运行设备 / Agent / 目录 / 权限是输入框里一排小标签（图标 + 简短值，
  完整内容在 title / aria-label），点开是原来的菜单；宽屏时标签按需省略号收缩，手机上整排横向滚动。
  按键提示只在占位文字（有键盘时）和 title 里；输入框下的小字只在续接已有会话时显示「将续接这个会话」。
- 对话区没有卡片外框：消息直接放在页面背景上，只有输入框是一块浮起的面；会话列表与对话之间只有一条细分隔线。

- Agent 下拉里每个 CLI 显示状态：未安装（安装按钮 → 进度条）/ 安装中 xx% / 未登录（“在终端登录”按钮打开该设备终端并键入登录命令）/ 就绪。
- “⋯”：提示词预设、工作上下文（同设备直接 / 经网关操作其他设备 + 目录）、临时目录。
- 新页面“Agent”（侧栏）：提示词片段编辑器（左：片段列表，勾选/排序/编辑/还原，按 全部/已启用/已停用 筛选；右：变量替换后的最终提示词预览和段数）、
  临时目录管理、CLI 管理（安装/更新/登录状态）。

**接线**（`ui/agent-wire.js`）：

- 传输适配器：云端 `cloudAgentApi()`（`/api/devices/{id}/agents/{op}`，设备列表取 `/api/devices`，不在线、没有 `agents/1` 的设备灰显并注明原因）；
  本机 `localAgentApi()`（本机 → `/api/agents/local/{op}`，其他设备 → `/api/agents/remote/{id}/{op}`；云端未连接时只列本机，并在运行设备菜单里说明）。
  传输层错误（4xx/5xx、断网）也转成 `{error, code}`，和 handler 自己的错误走同一条路。POST 不清空页面的 GET 缓存（`keepCache`）。
- 概览页：页头、对话区、摘要三个区块；10 秒轮询只重绘页头和摘要，对话区只挂载一次。手机上页头只保留主按钮，让对话区更靠上。
- 在终端登录：远端设备 → 打开该设备的“终端”页，连上后键入登录命令（claude `auth login`、codex `login --device-auth`、gemini 直接运行；
  CLI 不在 PATH 上时用探测到的完整路径）。要键入的内容只存在内存里，链接本身不能让终端执行命令。
  本机控制台里的本机 → 对话框显示命令（可复制），因为用户就在这台电脑前。dsh 总是先弹对话框：`dsh web` 配置页，或设置 `DEEPSEEK_API_KEY`。
- 侧栏页面 `#/agent?tab=prompts|cli|tmp&device=<id>`：顶部选运行设备，三个标签页都作用于这台设备。

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

## 14. 实机核实（2026-11，Windows 工作电脑，Codex 0.164.0 / DeepSeek Harness 0.2.2-rc.5）

**Codex**
- npm 全局安装在 `%APPDATA%\npm`，有 `codex.ps1` / `codex.cmd` 两种 shim；Codex 桌面版也会写同一个 `~/.codex`（`originator: codex_work_desktop`，`source: vscode`）。
- `codex login status`：已登录时输出 `Logged in using ChatGPT`（或 API key），**写在 stderr**，判断要合并 stdout+stderr。
- `codex exec` 参数：`-s/--sandbox read-only|workspace-write|danger-full-access`、`-C/--cd <DIR>`、`-c key=value`（TOML）、`-m`、`-i`、
  `--approve-for-me`、`--dangerously-bypass-approvals-and-sandbox`；子命令 `resume <id>|--last`、`fork <id>`、`review`；prompt 为 `-` 时读 stdin。
- 会话文件 `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<id>.jsonl`，每行 `{timestamp, ordinal, type, payload}`：
  首行 `session_meta`（payload 有 `id, cwd, originator, cli_version, source, model_provider, base_instructions`，首行可能 20KB+）；
  `turn_context`（`cwd, model, approval_policy, sandbox_policy`）；`response_item`（payload.type = `message`(role user/assistant, content[].text)、
  `reasoning`、`function_call`/`function_call_output`、`custom_tool_call`/`custom_tool_call_output`、`additional_tools`）；
  `event_msg`（`task_started`、`item_completed`、`token_count`、`thread_settings_applied` …）；`token_usage_record`；`world_state`。
  role=user 的 message 里有系统注入内容（`<environment_context>`、`<user_instructions>`、AGENTS.md 等），取标题时跳过以 `<` 开头的文本。
- **标题索引** `~/.codex/session_index.jsonl`：每行 `{id, thread_name, updated_at}`，优先用它做标题。归档会话在 `~/.codex/archived_sessions/`。

**DeepSeek Harness（dsh）**
- Windows 上通常是桌面版：`%LOCALAPPDATA%\Programs\DeepSeek Harness\`，CLI 启动器在 `resources\runtime\cli\bin\dsh.cmd`
  （`ELECTRON_RUN_AS_NODE=1` 运行自带的 Electron）。不在 PATH 上，检测必须查这个位置（macOS：`/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`；
  Linux：`~/.local/share/...` 或 npm 全局 `dsh`）。`dsh --version` → `0.2.2-rc.5`。
- 数据在 `$DSH_HOME`（默认 `~/.dsh`）：`sessions/`、`storages/`、`profiles/`、`dsh-runtimes/`、`.credentials.yaml`（存在即视为已配置模型凭据；**不读取内容**）。
- 档案（profile）：`dsh --profile acp`（ACP over stdio）、`dsh --profile sdk`（JSON-RPC stdio）、
  `dsh --profile headless [--json] [--session-id <id>] [--permission-mode <preset>] [--approval-policy ask|never] [--add-dir <path>] <task|->`。
  `--json` 输出逐行的 run 事件（格式未公开，解析要宽松）。首次运行某个档案会在 `~/.dsh/profiles/<name>` 生成该档案。
- 会话：`~/.dsh/sessions/<编码后的 cwd>/<session-id>/session.v4.jsonl.zstd`（zstd 压缩；旧版 v3）。编码规则：`C:\Users\you\proj` → `--C-Users-you-proj--`，空格 → `~0020`。
  **索引** `~/.dsh/storages/session_projcache/sessions/<session-id>.json`：`{version, record: {identity: {createdAt(ms), cwd}, rows: {title.val, titleInput.val.first,
  sessionListMetadata.val {blank, lastPromptAt}, sessionStats.val {turns, ...}, workingDirectory.val, sandboxMode.val, agentPreset.val, tokenUsage.val, ...}}}`。
  会话列表用这个索引（不需要解压 zstd）：标题 = `title.val` 或 `titleInput.val.first`；`blank: true` 的空会话默认隐藏。读历史需要 zstd（可选依赖，缺失时只显示元数据）。
- 适配器选择：优先 `acp`（流式 + 审批）；ACP 初始化失败时退回 `headless --json`，续接用 `--session-id`。

## 15. 实机核实（第二轮：新代码直接跑在真机上）

在 Windows 工作电脑、4090 主机（Windows）、笔记本（Windows）、Mac mini、台式机（Windows）、一台 Linux 服务器上，把本分支的
`agents/` 模块下到临时目录直接运行（只输出计数和字段名，不读会话内容；对话只在空临时目录里发测试句）。得到的事实与对应改动：

**Codex**
- 桌面版（originator `codex_work_desktop`）分叉 / 续接的会话：开头两条 `session_meta`，第三行是几 MB 的 `compacted`
  （`payload.replacement_history` 里是之前的消息），会话自己的第一句在它之后。只读头尾时 167 个会话里 103 个没标题 →
  头尾找不到时按行流式扫描（上限 `READ_BYTES`，结果随摘要缓存），再退回 compacted 历史里的第一句。修复后 165/168 有标题。
- `session_index.jsonl` 只覆盖一部分会话（121 条里只匹配 25 个），不能只靠它。
- `codex exec --json` 的非致命提示（如 config.toml 里有不认识的设置）会同时作为 `error` item 和顶层 `error` 事件出现 → 每轮去重、显示为警告；
  真正失败时先来一个顶层 `error`、再来同文的 `turn.failed`（如账户不支持配置的模型）→ 顶层 `error` 暂存一行，后面是同文失败就只显示失败。
- 官方安装脚本（Windows：`irm https://chatgpt.com/codex/install.ps1 | iex`）装到 `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe`；
  第一行就是 "==> Installing Codex CLI"（在下载之前），进度估计不能把它当作安装阶段。装完同一进程里就能找到（不依赖 PATH 刷新）。
- npm 的 `.cmd` 启动器和原生 `codex.exe` 两种都跑通两轮对话。

**DeepSeek Harness（dsh 0.2.2-rc.5）**
- `session.v4.jsonl` 记录：`session`（id、createdAt、cwd）、`user/message`（`data.content[]`，只有 `data.source.kind == "user"` 是用户说的，
  `runtime-context` / `skill-catalog` 是注入的）、`assistant/message`（`data.message.content[]`：`reasoning` / `text` / `tool-call {id,name,arguments(JSON 字符串)}`）、
  `tool/call`、`tool/result`（`data.message.toolCallId`、`content[]`、`isError`）、`turn/start`、`turn/end`（`reason.kind`）、`session/title`（dsh 自己取的标题）、
  `system/message`、`request/header` 等。读历史按这些解析。
- ACP 的 `tool_call` 对内置读文件工具报 `kind: "other"`、`title: "read"` → 名字用 title。
- dsh 不声明 `promptCapabilities.embeddedContext`，系统提示词只能拼在第一条消息前面，dsh 会用这段混合文字取标题 →
  控制台记录自己发起的会话的第一句（`first_prompt`），列表里用它当标题。声明了 embeddedContext 的 ACP CLI 会改用独立的 `resource` 块传系统提示词。
- dsh 机器上的 Python 有 `zstandard`，`.zstd` 记录能直接解压。

**Claude Code**
- 只装了 Claude 桌面版的机器上，桌面版自带完整的 Claude Code CLI：
  macOS `~/Library/Application Support/Claude/claude-code/<版本>/<id>/claude.app/Contents/MacOS/claude`（见到 2.1.331），
  Windows `%APPDATA%\Claude\claude-code\<版本>\claude.exe`（见到 2.1.275、2.1.280）。检测时取最新版本。
  这个 CLI 单独登录（`claude auth status` → `loggedIn: false`，桌面版的登录不共享）。
- 未登录时 `claude -p` 回一条 "Not logged in · Please run /login" 的回复，再给一个同文的失败 result → 不重复显示，改成指向「在终端登录」的提示。

**运行环境**
- Linux 服务器上 agent-nexus 以 root 运行且没有 `HOME` 环境变量：检测和会话发现正确落到 `/root`。
- 没有 npm 时：Claude Code / Codex 自动改用官方安装脚本；dsh / Gemini 只能用 npm，给出需要的 Node.js 版本（dsh 另外建议装自带 dsh 的桌面版）。
- 有的国内服务器访问 raw.githubusercontent.com 很慢或不通。

**经网关（真机）**：在 Windows 工作电脑上用本分支起一个临时网关（loopback、独立数据目录）并让一个临时设备客户端加入，
以网关主人身份通过 `/api/devices/{id}/agents/*` 驱动：设备宣告 `agents/1`；`agent_info` 看到 Codex 0.164.0、dsh 0.2.2-rc.5、Gemini 0.45.0；
`agent_sessions` 按 CLI 分组（claude 168、codex 174、dsh 27、gemini 43）；Codex（codex-exec，只读）和 dsh（acp）各两轮对话都正常，
之后会话出现在列表里（标题是第一句、managed），历史可读，改名 / 置顶 / 隐藏生效；`agent_dirs`、提示词预览正常。
注意：网关主人的写请求需要 `X-ANX: 1` 头（浏览器控制台会带）；设备客户端被强杀时 muxi-p2p-agent 子进程会留下。

**经网关模式（真机）**：同一台 Windows 电脑上起临时网关 + 设备 A、B。A 上的 Codex（只为这次测试用 `-c mcp_servers.anx.url=… bearer_token_env_var=…`
给它网关 MCP，用户的 config.toml 不动）以经网关模式操作 B 上的目录：拿到网关签发的 handle 和地址 `codex-target-b@anx-e2e-b`，
用系统提示里的 workspace_id 调网关的 `mail_send`，邮件进了这个地址的会话（`/api/inbox/conversation`，聊天里合并显示）。
发现：`codex exec` 从不询问，需要审批的 MCP 工具一律被拒（"该工具需要审批，但当前审批策略为 never"）。Codex 的
`mcp_servers.<name>.default_tools_approval_mode`（取值 auto / prompt / writes / approve，从 0.164 的二进制里查到）设为 `approve` 后正常 →
经网关模式下非只读的 Codex 运行，对检测到的 Agent Nexus 连接器自动加这条覆盖；没有连接器时对话里给出提示，`agent_info.gateway_mcp` 报告检测结果。

**Claude Code（真机，Mac mini）**：桌面版自带的 Claude Code 2.1.331 需要单独 `auth login`（桌面版的登录不给命令行用；登录信息进钥匙串
`Claude Code-credentials`）。登录后：`claude-stream` 两轮对话正常（第一轮用 `Read` 读测试文件并回复内容，第二轮续接）；会话出现在列表里，
目录和标题正确，历史含工具调用；"需要审批"模式下 `echo` 这类只读命令 Claude Code 自己放行，`touch` 这类写操作经 anx 的审批 MCP
发出审批请求，允许后执行。

## 16. 第二期：独立对话页、状态保持、上下文用量、API 接入

### 16.1 独立「对话」页
- 两个控制台的侧边栏新增「对话」（路由 `#/chat`，可带 `?device=&kind=&session=`），聊天组件铺满内容区（侧栏 + 消息流 + 输入框，高度 = 视口 − 顶栏，手机端减去底部标签栏）。
- 概览页不再嵌入聊天，改为一张「对话」卡片：最近 3 个会话（点开直达 `#/chat?…`）+「新对话」按钮。
- 「Agent 设置」页（提示词 / CLI / 临时目录 / API）不变，另加 API 标签页（16.5）。

### 16.2 状态保持
- 聊天状态（运行设备、Agent、目录、权限、工作上下文、当前会话 kind+id、当前 rid、侧栏是否收起 `sideClosed`、未发出的草稿）存 `localStorage`，键 `anx-chat:<console>`（`<console>` = `local` 或 `cloud:<origin>`），每次变化写入。
- 打开对话页时按这个状态恢复：设备离线或不再支持时退回默认设备并提示一次；rid 还活着就接上（`agent_run_read`），否则打开该会话的历史。
- 切换页面不销毁正在进行的读取：聊天组件在模块级保留一个实例（同一个 console 只建一次），页面切走时只把 DOM 摘下，回来时挂回，长轮询继续（页面隐藏超过 10 分钟才暂停）。
- URL 参数优先于存储的状态（从概览卡片 / 收件箱跳转用）。
- 会话列表在宽布局下默认打开；只有新键 `sideClosed: true`（本版本里点了收起）才保持收起，旧版本的 `sideCollapsed` 读到后删除一次、不再生效。
  断点按聊天组件自身宽度（container query）：≥ 720px 列表常驻（720–999px 时窄一些，248px），< 720px 变成组件内的抽屉。

### 16.3 上下文用量圆环
- 事件：`{"type": "usage", "context": {"used": 123456, "limit": 1000000|null, "model": "…"}, "turn": {"input":…, "output":…, "cached":…, "cost_usd":…}}`。每轮结束发一次（在 `status done` 之前）；拿得到时在轮内也可以更新。`limit` 未知时为 null。
- 来源：
  - Claude（stream-json）：`result.modelUsage[<model>].contextWindow` 为上限；`used` = 最后一条 assistant 消息 `usage` 的 input + cache_read + cache_creation + output。
  - Codex（exec --json）：`turn.completed.usage` 给本轮 token；上限优先取会话 rollout 里最近的 `event_msg/token_count.info.model_context_window`（runner 知道 session id，按 sessions 的 codex 路径找文件尾部），没有就按模型表；`used` = `info.last_token_usage.input_tokens + output_tokens`（rollout）或本轮 input+output。
  - ACP / text：没有可靠数据时只发 `turn`（若有），`context.limit=null`。
  - API（16.4）：自己计：`used` = 上一次请求的 prompt+completion token（以服务商返回为准），上限来自模型配置（`context_window`），没配就查内置表，查不到为 null。
- 历史：`agent_session_read` 返回的事件里，最后一次能算出的用量也以一条 `usage` 事件给出（Claude 从 jsonl 的 usage，Codex 从 token_count）。
- 界面：输入框右下角一个圆环（直径 18px，按 used/limit 填充；>80% 变黄，>95% 变红）；`limit` 为 null 时圆环换成 token 数字。悬停显示「已用 872.1K / 1M（87%）· 模型 …」。

### 16.4 API 接入（客户端直连）
- **原则**：请求从运行设备直接发给服务商（不经网关）；密钥只存在设备本机；网关只同步「服务商 + 模型」的非机密配置。
- **协议**（三种，每种一个适配器，全部流式）：
  - `openai`：Chat Completions（`POST {base_url}/chat/completions`，`stream: true`，`stream_options.include_usage`），tools = function calling。覆盖 OpenAI、DeepSeek、通义、Kimi、智谱、OpenRouter、Ollama、vLLM、NewAPI/OneAPI 等中转。
  - `anthropic`：Messages（`POST {base_url}/v1/messages`，SSE），tool_use / tool_result。
  - `gemini`：`POST {base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse`，functionDeclarations / functionResponse。
- **配置**：`agent-nexus.json` → `agents.api.providers[]`：`{id, label, protocol, base_url, headers?: {…非机密}, models: [{id, label?, context_window?, max_output?, tools?: true}], enabled}`；网关同步的同名结构存在网关，设备拉取后合并（本机配置优先）。
- **密钥**：本机 `<root>/.anx/agents/api-keys.json`（0600 / 仅当前账户 ACL），`{provider_id: {key, updated}}`；接口只能写和删，读时只返回 `{set: true, last4}`。也支持 `env:VARNAME` 引用环境变量。密钥设置时绑定服务商地址的 origin（scheme://host:port）；地址改了（本机编辑或网关同步）密钥不会发往新地址，状态为 `origin_changed`，`agent_run_open` / `agent_api_test` 返回 `code: key_origin_changed`（「服务商地址变了，请重新输入密钥」）。`base_url` 不能带账号密码。
- **作为 Agent**：每个启用的模型是一个 Agent，kind = `api:<provider>/<model>`，label = 模型 label，在 Agent 选择器里单列「API 模型」一组；`supports`: resume ✓、approvals ✓、tools 按模型配置。
- **对话循环**（`agents/api/loop.py`）：消息 → 服务商流式 → 文本/思考增量转统一事件；遇到工具调用 → 按权限模式审批（复用 approval 事件与 `agent_run_answer`）→ 在本机执行 → 结果回填 → 继续，直到无工具调用（上限 50 步）。
- **工具**（首期）：`read_file`、`write_file`、`edit_file`（精确替换）、`list_dir`、`glob`、`grep`、`run_command`（工作目录内，超时 120s，输出截断）、以及经网关的 `ask_user` / `mail_send`（有 handle 时）。权限：readonly 只给读类工具；ask 时写/执行需审批；edits 时写自动、执行需审批；full 全自动。路径限制在工作目录 + 临时目录内（符号链接解析后校验）。anx 自己的状态目录（`<root>/.anx`、`.mcp-manager`，含密钥和设备凭据）即使在工作目录内也拒绝访问；服务以 root / SYSTEM 运行、执行账户是别的用户时，文件工具在以执行账户启动的辅助进程里执行（`agents/api/fileops.py`）。`ask_user` / `mail_send` 走网关 `hub_call agent_ask` / `agent_mail_send`（带 run 的 workspace handle，只接受申请它的设备），`ask_user` 轮询 `ask_get` 等主人回答（最多 10 分钟）；readonly 只给 `ask_user`，`mail_send` 按写操作审批。
- **会话存储**：`<root>/.anx/agents/api-sessions/<id>.jsonl`（每行一条统一事件 + 用于续接的原始消息），会话发现里作为 kind `api:*` 列出，按 provider 分组显示为「API」标签页。
- **系统提示词**：直接作为各协议的 system 字段（openai: system 消息；anthropic: `system`；gemini: `systemInstruction`）。
- **错误**：401/403 → 「密钥无效或无权限」；429 → 显示并按 `retry-after` 自动重试一次；网络错误 → 显示服务商 host。
- **RPC**（设备端，经 `agents/1` 同样可远程调用）：`agent_api_providers`（列出，含 key 状态）、`agent_api_provider_save`、`agent_api_provider_delete`、`agent_api_key_set {provider_id, key|null}`、`agent_api_test {provider_id, model}`（发一条极短请求，返回延迟/错误）、`agent_api_models {provider_id}`（openai 协议调 `/models` 列出可选模型）。
- **网关**：`hub_call` op `api_config_get` / `api_config_put`（只接受非机密字段，带 key 字段直接拒绝），控制台 API 页可选「同步到所有设备」。

### 16.5 界面：Agent 设置 → API
- 服务商列表（名称、协议、地址、密钥状态 ●已设置 ****1a2b / ○未设置、模型数）；新增/编辑弹窗：预设模板（OpenAI、DeepSeek、通义、Kimi、OpenRouter、Ollama、Anthropic、Gemini、自定义 OpenAI 兼容/NewAPI），选模板自动填 protocol + base_url；密钥输入框只写不读；「测试」按钮；「从服务商拉取模型」（openai 协议）。
- 每个模型一行：启用开关、显示名、上下文窗口、是否允许工具。

### 16.6 Codex 配置警告
- `Codex is ignoring … unrecognized configuration setting` 这类来自 CLI 自身配置的提示：适配器发 `{"type":"error","level":"notice",…}`；界面把 `notice` 显示成一行灰色小字（可展开），同一会话同一文本只显示一次。`warn` 仍为黄色，无 level 为红色。
