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
- **经网关操作其他设备**（模式 `gateway`）：CLI 在运行设备上跑，但要操作上下文设备 Y 的目录 D。界面上不是单独的选项：
  在“目录”标签里选了其他设备上的目录就是这个模式，选运行设备自己的目录就是 `direct`（见 §11“目录”标签）。网关为 (Y, D) 签发一个
  workspace handle（`use_device` 的 `workspace_id`），提示词片段“网关工作方式”被启用并带上 `$handle`、`$context_device`、`$context_workdir`。
  CLI 通过运行设备的**本机 MCP**（`http://127.0.0.1:8399/mcp`，design.md §19）使用网关工具，不需要手动配置：客户端事先在 CLI 的配置里
  写好 `agent-nexus` 配置项（CLI 已经通过云端网关接入时用它原有的连接器，不重复添加），写不进去的（dsh、命令里有 `{mcp_url}` 的自定义 CLI、关闭了自动接入）
  这次运行用参数临时接入；API 模型在进程内接入。提示词片段“Agent Nexus MCP 与工作区”（`$mcp_hint`）写明用哪个连接器、
  已选好的 `workspace_id`、目标设备和目录、邮箱地址。只有什么都接不上时（本机 MCP 关闭、未加入、已吊销、已锁定、网关过旧、网关关闭了设备接入、凭据不被接受，或这个 CLI 接不了）状态栏才提示原因。

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

- `agent_run_open {kind, workdir, resume_id?, prompt, permission, preset?, fragments?, context?, model?}` → `{rid, session_id?, model}`
- `agent_run_send {rid, prompt, model?}`：发下一轮（claude-stream 写入 stdin；codex-exec 启动 `exec resume`）；`model` 与当前不同时从这一轮起换模型（§16.7）
- `agent_run_read {rid, cursor, wait≤20}` → `{events, cursor, state}`（长轮询，与 `term_read` 一样有上限，不取消进行中的调用）
- `agent_run_answer {rid, approval_id, allow, remember?}`：回应审批
- `agent_run_stop {rid}`：中断当前轮；`agent_run_close {rid}`；`agent_run_list {}`：在跑的 run

进程一律以执行账户启动（`accounts.effective(root, "terminal")` 的 `popen_args` / env），工作目录必须存在且在允许范围内（§7）。

**运行环境里的 `anx`**（`agents/anx_shim.py`，`Runs._env(..., path_first)`）：安装包从不把 `anx` 放进 PATH（Windows、macOS 直接用运行时
venv 的 python 启动核心，deb 只装 `/usr/bin/agent-nexus`），而 venv 自己的 bin 目录也不能放进去（里面的 `python` 和依赖脚本会盖住用户的
工具）。所以 hub 在每个 run 的临时目录里建一个只有 `anx` 的 `.bin`，放在 PATH 最前面（PATH 的键名不分大小写，Windows 上是 `Path`）：
POSIX 是一个 `#!/bin/sh` 脚本，`exec <这个客户端的 python> -P -m agent_nexus.cli --root <root> "$@"`；Windows 复制 venv 的 `anx.exe`
（uv 的启动器，内含 python 的绝对路径，PowerShell / cmd 都能找到，也不经 cmd.exe 二次解析 `& | ^ %`），没有可用的启动器时改写
`anx.cmd`；两种情况旁边都有一个无扩展名的 sh 脚本 `anx`，给 Git Bash（Windows 上 Claude Code 的 shell）用（与 npm 的做法相同：bash 优先
运行它，PowerShell / cmd 按 PATHEXT 不会选它）。它先 `export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` 再启动 `anx.exe`：否则 MSYS
会把传给原生程序的 `/srv/app`、`/var/log` 这类参数改写成 `C:/Program Files/Git/srv/app`，在 Linux 设备上运行错误的命令。只对 anx
关掉路径转换，不改整个 run 的环境（Claude 的其他命令仍按 Git Bash 的习惯转换）。临时目录在交给执行账户之后写入，账户可读可执行；Android 不写（应用数据不能执行，也没有 CLI Agent）。
同时设置 `ANX_CONSOLE_URL=http://127.0.0.1:<控制台端口>`：`anx devices / exec / wait / kill / cp` 只连这个地址（本机 MCP 的 `/mcp`
和 `/mcp/self`），不读根目录、不需要凭据；`ANX_ROOT` 照旧被去掉（所以脚本带 `--root`）。API 模型（§16.4）不写这些。
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
- 变量：`$device $device_id $os $workdir $tmpdir $session $address $agent $date $time $owner $gateway $handle $context_device $context_workdir`
  `$target_device $target_workdir $mcp $mcp_prefix $mcp_state $mcp_hint $devices_cli`，
  未知变量原样保留并在预览里标黄；`$$` 输出 `$`（变量的值不会再被展开）。
- `prompt_preview {preset, context}` 返回 `{text, segments:[{id,title,chars}], unknown_vars}`。
- 传给 CLI：Claude `--append-system-prompt <text>`；Codex `-c developer_instructions=<text>`；dsh / 其他：作为首条消息前缀。

内置片段（中文）：身份与环境、工作目录与临时目录、与用户沟通（ask_user / mail）、团队协作（mail_who / @all）、
Agent Nexus MCP 与工作区（`$mcp_hint`）、用命令行操作其他设备（`$devices_cli`）、
网关工作方式（仅 gateway 模式，含 use_device + $handle）、项目说明（提示读取 AGENTS.md / CLAUDE.md）。

**用命令行操作其他设备**（`devices_cli`，紧跟 `nexus_mcp`，默认和精简预设都开着）：`$devices_cli` 由 hub 填（`hub.devices_cli_hint`），
一段约 350 字的说明：`anx devices`、`anx exec <设备> [--shell S] [--cwd 目录] [--timeout 秒] -- <命令>`（输出和退出码原样返回，长任务
`--detach` 后 `anx wait` / `anx kill`）、`anx cp <设备>:<路径> <设备>:<路径>`，以及引号提示：命令里有引号、`$`、反斜杠或通配符时用
`--stdin` 传原文，否则本地 shell 会先展开一遍。例子按 CLI 实际用的 shell 选：bash 的 `<<'EOF'`（也包括 Windows 上的 Claude Code，
它用 Git Bash）；Windows 上的其他 CLI（Codex、Gemini、dsh 用 PowerShell）给多行 here-string，并先设
`$OutputEncoding=[Text.UTF8Encoding]::new()`——Windows PowerShell 5.1 经管道传给原生程序时按 ASCII 编码，中文会悄悄变成 `?`。
这些命令只在这里出现，网关 MCP 的说明和工具描述里没有。以下情况为空（片段被跳过，不占 token）：API 模型；没写成 `anx`（Android）；
本机 MCP 关闭、对外监听、未加入网关、已吊销、凭据被拒、网关过旧或网关主人关闭了设备接入（这些状态下 `anx exec <其他设备>` 一直
失败，不值得每次付这段 token；锁定和离线可能在 run 中途恢复，照常提示）；只读模式；Codex 不是“完全访问”（`workspace-write` / `read-only` 沙箱不允许连回环地址）。
Claude 在“需要审批”模式下每条 `anx exec` 都照常走审批。文字里没有 `%`、`!` 和换行（批处理启动的 CLI 会拒绝这些参数）。
存过的 prompts.json 会自动补上这个内置片段（`normalize` 放回原位）。

## 7. 工作目录

- `agent_dirs {path?, drives?}`：列出 `path` 下的子目录（只列目录），根是执行账户的 HOME、设备已登记的工作区，Windows 上还有各磁盘的根
  （`C:\`、`D:\`…，默认就有，旧控制台不用另外要；`drives: false` 去掉）；不允许 `..` 越界。
- **能在哪里运行**（`AgentHub._workdir_ok`，`agent_run_open` 在找网关要 handle 之前检查）：上面这些根之下；或者**正好是**这台设备的 CLI
  已经用过的目录——续接的会话自己记录的 cwd、这个 CLI 已发现会话的 cwd、「最近使用」里的目录（只有成功打开过才会进去）。
  只认这个目录本身，不含它的子目录（子目录先把上级登记为工作区）。真机：Codex 在 `D:\CPN\BMC5-workspace` 里建的会话原来连续接都被拒绝。
  POSIX 上不开放 `/`：服务常以 root 运行，`ask` 模式下 Codex 的 workspace-write 沙箱以工作目录为可写范围，选 `/` 等于整机可写。
  **永远拒绝**（`workdirs.cwd_refused`，在上面所有放行规则之前，所以从会话记录里读到的 cwd 也绕不过去——记录文件在 HOME 里，谁能写 HOME
  谁就能伪造）：文件系统或磁盘的根（`/`、`C:\`、`D:\`；磁盘根只是浏览的起点，选它下面的项目目录）、系统目录（POSIX 的 `/etc`、`/usr`、
  `/bin` 等及其子目录，`/home`、`/var`、`/tmp` 等本身；Windows 的 `%SystemRoot%`、Program Files、ProgramData 及其子目录）、
  Agent Nexus 的根目录本身和包含它的目录（如 `%LOCALAPPDATA%`；非完全信任的运行在那里能改 `agent-nexus.json` 给自己放开完全信任），
  HOME 的上级目录。例外：执行账户的 HOME 本身仍可用（它是默认工作目录，而默认的根就在 HOME 下面；这是已知的残余风险，
  要彻底关掉得把根移出 HOME 或给 CLI 加路径级的拒绝规则）。
  根目录（磁盘枚举、realpath）在线程里算（`asyncio.to_thread`），磁盘列表缓存 10 秒，网络映射盘（`DRIVE_REMOTE`）和没有根目录的盘
  （`GetDriveTypeW`）直接跳过：断开的 SMB 映射上 `isdir` 会卡几秒，不能卡住事件循环。
  工作目录是 CLI 自己的沙箱和项目边界（Codex 沙箱、Claude 的项目、API 模型的文件工具），不是授权边界（同一凭据本来就能用终端和文件）。
  Agent Nexus 自己的状态目录（`<root>/.anx`、`<root>/.mcp-manager`，含密钥和设备凭据）及其子目录永远不能当工作目录。
  runner 不再重复检查 hub 放行的目录（`Runs.open(workdir_ok=True)`，只由 hub 传，控制台参数里的同名字段无效）。
  拒绝时的提示写明可选范围：「工作目录不在允许的范围内：X（可选：执行账户的主目录、已登记的工作区[、各磁盘里的目录]，以及本机 CLI 用过的目录；其他位置请先登记为工作区）」。
- 最近使用的目录存在 `<root>/.anx/agents/recent.json`，每个 CLI 也会从发现的会话里得到常用 cwd。
- UI：目录选择器 = 最近使用 + 已登记工作区 + 浏览（起点里磁盘用硬盘图标；说明文字只在设备给出磁盘时写“和各磁盘里的目录”）。

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
  AgentHub 在找网关要 handle 之前先做本地检查（目录在允许范围内（§7）、CLI 已安装、会话 ID 和模型名合法），失败的打开不占用地址。

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
                    │      │ 💻 本机  >_ Codex  ▣ 默认模型  📁 proj  🛡 需要审批  ◔ [停止] [↑] │
                    │      └────────────────────────────────────────────────────────────┘
```

- 头部是一行轻量标题（不是工具条）：侧栏开关、当前会话标题、⋯。运行设备 / Agent / 模型 / 目录 / 权限是输入框里一排小标签（图标 + 简短值，
  完整内容在 title / aria-label），点开是原来的菜单；宽屏时标签按需省略号收缩，手机上整排横向滚动。
  按键提示只在占位文字（有键盘时）和 title 里；输入框下的小字只在续接已有会话时显示「将续接这个会话」。
- 对话区没有卡片外框：消息直接放在页面背景上，只有输入框是一块浮起的面；会话列表与对话之间只有一条细分隔线。

- **CLI 图标**（`ui/kind-icons.js`，`kindMark`）：会话列表、CLI 标签页、Agent 标签、Agent 菜单、欢迎页、概览的对话卡片、Agent 页都用各 CLI 的图标：
  Claude Code、DeepSeek Harness、Gemini CLI 用 Simple Icons（CC0）的品牌图标（只用来标明是哪个 CLI，商标仍归各自所有），Codex 用自绘的
  「云 + >_」（不用 OpenAI 的标志），自定义 CLI 用终端图标；API 模型按服务商 id 显示（deepseek → 鲸鱼、gemini/google → Gemini、
  anthropic/claude → Anthropic，其他 → 闪电）。图标用 currentColor 画，底色是同色的淡色块（`--kind-*` 令牌，深色模式另有一组）。
  Agent 标签里图标代替原来的状态点；CLI 没就绪时图标右下角加一个小状态点（安装中蓝色、未登录等黄色、出错红色、其他灰色）。
  会话列表里的图标带 CLI 名称（`title` 和 `aria-label`）；API 模型的图标右下角有一个小“API”角标，和同一家的 CLI（DeepSeek 鲸鱼、Gemini）区分开。
- **“模型”标签**（§16.7）：在 Agent 和目录之间，CLI 能选模型时才显示（旧设备的 `agent_info` 没有 `models` 就不显示，API 模型本身就是模型）。
- Agent 下拉里每个 CLI 显示状态：未安装（安装按钮 → 进度条）/ 安装中 xx% / 未登录（“在终端登录”按钮打开该设备终端并键入登录命令）/ 就绪。
- **“目录”标签**（工作上下文就在这里选，原来 ⋯ 里的“工作上下文…”对话框已去掉）：菜单先列运行设备的目录（路径输入、最近使用、
  已登记的工作区、浏览），再按设备分组列出网络里其他在线设备的目录（“其他设备 · 经网关”）：本控制台在那台设备上用过的目录、
  那台设备自己的最近目录和工作区（它有 `agents/1` 时读它的 `agent_dirs`）、设备列表里登记的 workdirs；每组有“输入路径…”和“浏览…”
  （有 `agents/1` 用 `agent_dirs` 浏览；没有时用控制台的文件浏览 `files/1`：本机控制台 `/api/remote/devices/{id}/files`，
  网关控制台 `/api/devices/{id}/files`；都不行就只能输入完整路径）。读 `agent_dirs` 失败（旧版 AgentHub、关了 Agent）时组里写出错误，
  浏览改用 `files/1`。离线设备在末尾一行灰字，写明原因（“不可用：NAS（离线）”）；本机没加入网关 / 云端断开 / 已锁定时
  只有运行设备的目录，并说明原因。有其他设备时运行设备只列 3 个最近目录和 3 个工作区，头部有“其他设备 N”跳转。
  设备的目录列表异步到达时只重绘该组的目录列表（组头和路径输入框不动），正在输入或聚焦的行不丢；目录列表跨设备列表刷新保留
  （一分钟内不重读），只丢掉已离线的设备。本机控制台里“本机”也能是目标（运行设备是远端时），用它在网关上的 id（`gid`）。
  - 选其他设备的目录 → `context = {mode: "gateway", device, workdir}`；选运行设备的目录 → `direct`。标签显示「目录名 · 设备名」
    （网络图标、强调色，各自省略，完整内容在 title），对话区上方一行说明“CLI 在 X 上运行，经网关操作 Y 的 D”，带“更改”和改回的 ×
    （手机上省掉“CLI 在 X 上运行”、设备名截短，保证目录可见）。
  - 运行设备的 CLI 什么连接器都接不上时仍可选，目录菜单里没有提示；只有对话区上方的状态行和输入框上方写
    “Agent Nexus MCP：<原因>” / “{CLI} 无法使用 Agent Nexus MCP：<原因>”，修法在本机控制台 → 设置 → 本机 MCP（旧设备没有
    `nexus_mcp` 时仍按 `gateway_mcp: false` 提示添加网关 MCP）。目标设备离线、网关不可达时不能发送，提示里给“改用 X 上的目录”。
  - 经网关时 `agent_run_open.workdir` 是运行设备上的起点：续接会话时用会话自己的 cwd（Claude 按目录存会话），新对话留空（主目录）；
    这个起点不进“最近使用”。打开会话时 `S.workdir` 总是取会话的 cwd（不论模式），
    所以改回 direct 后续接仍在会话自己的目录里，不会带上前一个对话的目录。会话仍按运行设备列出，副标题写「⇄ Y · D」；会话元数据记下 `context {device, workdir}`
    （`agent_sessions` 返回 `context: {mode: "gateway", …}`，`agent_run_list` 的 run 也带 `context`），重新打开 / 接上 run 时恢复。
  - 状态（§16.2）：`context` 按运行设备存在 `byDevice` 里；经网关用过的目录另存 `targets: [{device, workdir, name}]`（最多 24 条），菜单优先列出；
    `deviceNames`（网关 id → 名称，最近 64 台）记下列过的设备名。目标设备不在设备列表里（网关不可达）时，标签、说明行和会话副标题
    用 `context.name`、`targets` 或 `deviceNames` 里的名称，都没有才显示 id。
- “⋯”：提示词预设、临时目录、新对话。
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
2. 新会话 `session/new {cwd, mcpServers: []}` → `{sessionId}`；续接 `session/load {sessionId, cwd, mcpServers: []}`（agentCapabilities.loadSession 为真时），
   否则声明了 `agentCapabilities.sessionCapabilities.resume` 时 `session/resume {sessionId, cwd, mcpServers}`（dsh 0.2.2：不重放历史；cwd 要和会话自己的目录一致）。
   原来只认 `loadSession`，dsh 的续接都变成“不支持续接旧会话，已开始一个新会话”。
   响应里的 `models`（Gemini：`{availableModels: [{modelId, name, description}], currentModelId}`）和 `configOptions` 里 id 为 `model` 的选项
   （dsh：值是 JSON `["provider","model"]`）记下来，发 `model` 事件；要换模型时 `session/set_model {sessionId, modelId}` 或
   `session/set_config_option {sessionId, configId: "model", value}`，第一轮消息等它的回答再发（§16.7）。
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
经网关模式下非只读的 Codex 运行，对检测到的 Agent Nexus 连接器自动加这条覆盖。

0.8.31 起由本机 MCP 接管（design.md §19）：每个对话最多一个 Agent Nexus 连接器——`agent-nexus` 配置项、用户原有的网关连接器
（配置项或 Claude.ai / ChatGPT 账户连接器），或者按运行临时接入（claude `--mcp-config` 里的 `agent-nexus`、codex `-c mcp_servers.agent-nexus.url=…`、
ACP `mcpServers` 的 http 项、自定义 CLI 的 `{mcp_url}`）；检测结果是“无法确定”（unknown）时什么都不接，提示词只写“如果你有 Agent Nexus
的工具，只用其中一个连接器”。免审批的工具按权限模式给出明确清单（只读：只读工具；逐项审批：再加 mail_send + ask_user；自动改文件：
经网关时再加 file_write / edit_file / patch_file；完全：全部），exec_command 等在完全之外从不免审批：Codex 用 `enabled_tools` 只留下清单里的
工具再加审批覆盖，Claude 用 `--allowedTools` 列出清单。只读清单里有 `exec_wait`（只等待、读取已经批准过的命令）。0.8.35 起网关默认不直接列出少用的工具（design.md §20.1）：`curr_time`、`device_info` 等按名字调用仍免审批，经 `tooluse` 调用时和其它 `tooluse` 一样要审批。`agent_info` 的 `nexus_mcp` 报告每个 CLI
用哪个连接器（`gateway_mcp` 保留一版，只有什么都接不上时为 false）；“没有配置连接器”的提示已删除。

**Codex 的 ChatGPT 账户连接器**（真机，codex-cli 0.164.0）：账户里的连接器是 `codex_apps` 下的 app（工具名 `<app>.<tool>`），`codex exec`
固定以审批策略 never 运行，app 的工具调用一律被 Codex 自己拒绝（“MCP tool call requires approval, but approval policy is never”，完全信任的
`--dangerously-bypass-approvals-and-sandbox` 也不例外）；`mcp_servers.<名字>.default_tools_approval_mode` 管不到 app，按运行批准 app 要
`apps.<connector id>.…`，而 id 不是显示名（名字写错会被静默忽略）。所以 `for_run` 对 Codex 的 `gateway_account` 改为按运行临时接入本机 MCP
（`attach: "flags"`、`use: agent-nexus`、`shadowed: <账户连接器名>`、`run_only: true`，`state` 不变，config.toml 仍不写条目），
运行带 `-c mcp_servers.agent-nexus.url=…`、`default_tools_approval_mode="approve"` 和按权限模式的 `enabled_tools`；提示词写“临时接入了本机
Agent Nexus MCP……不要调用 <名字>.*”（没有 handle 时也有这一句）。本机 MCP 提供不了网关工具（未加入、已吊销、关闭等）时保持原样并在对话里
警告“codex exec 无法审批……本次对话调用不了 Agent Nexus 工具”。Agent 页的标签写成“ChatGPT 账户连接器 X（codex exec 无法审批它的工具）”，
本机 MCP 能提供时接“；对话中临时接入本机 MCP（不写入配置）”，不能时接“；本机 MCP 不可用，对话里调用不了 Agent Nexus 工具，请先开启本机 MCP”；
工具列表过时只作为交互式 Codex 的提醒放在最后（刷新它对对话没有用）。
被动检测只看得到 `codex_apps`，不再用它覆盖检测到的连接器名和“过时”标记。

**0.8.34 起（主人的决定）**：既然账户连接器在 `codex exec` 里批准不了、工具列表又是快照，Codex 只有账户连接器（`gateway_account`，或
`both` 且另一个不是配置项）时，客户端照样把专用配置项永久写进 `~/.codex/config.toml`，并且无需审批：
`[mcp_servers.agent-nexus]` 带 `url`、`tool_timeout_sec`、`default_tools_approval_mode = "approve"`（Gemini 的条目同理带 `"trust": true`）。
之后状态是 `both`，对话里用 `agent-nexus`；主人手动删除的条目不再加回。早先写入的、缺少审批设置的条目会自动补上。上面“按运行临时接入”
只在本机 MCP 当时提供不了网关工具、条目还没写入时才用得上。读取各 CLI 的配置文件时忽略 BOM（Windows 编辑器常带）。

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

### 16.7 选择模型
- **每个 CLI 怎么换**（registry `model_args` / `model_hints`，`agent_info` 和 `AgentDef.view()` 里是 `models: {flag, protocol, hints}`）：
  - Claude Code：`--model <别名或全名>`（sonnet / opus / haiku / opusplan …），放在 `{options...}`，续接时也带；进程活着时换模型发
    stream-json 控制请求 `{"type":"control_request","request":{"subtype":"set_model","model":…}}`，Claude 回答成功后才发 `model` 事件，
    被拒绝时显示黄色警告并保持原来的模型。
  - Codex：`codex exec -m <id>`，放在 `{options...}`，也就在 `resume` 之前；每轮一个进程，所以下一轮就用新模型；用量行和上下文上限按所选模型算。
    `-m` 在 `exec resume` 上是否生效要在真机确认（不行就改 `-c model="<id>"`）。
  - Gemini CLI：`-m <id>`（auto / pro / flash / flash-lite …），ACP 会话里再用 `session/set_model`。gemini-cli 0.71.1 的
    `availableModels` 只列完整 id（`gemini-2.5-pro` 等），别名 pro / flash / flash-lite 对到该档最新的 `gemini-<版本>-<档>`；列表里没有的 id
    也照发 `session/set_model`，由 Gemini 的回答决定（拒绝时警告并保持当前模型）。
  - DeepSeek Harness：没有命令行参数，只能经 ACP 的 `model` 配置项换；请求的名字要和它列出的选项对上（`provider/model`、模型名或显示名），
    对不上就提示“没有提供模型 X，沿用当前模型”（不改运行的模型）；ACP 起不来改用 headless 时不能选模型，给一行灰色提示。
  - ACP 会话里切回“默认模型”：会话以默认模型开始时记下它报告的模型，切回时再请求它；以指定模型开始的会话不知道默认是哪个，
    提示“这个会话没有报告默认模型，保持当前模型 X”，运行的模型不变。
  - 自定义 CLI：`agents.custom.<kind>.model_args`（如 `["--model", "{model}"]`）和 `model_hints`；没有 `model_args` 又不是 ACP 时不能选。
  - API 模型：kind 本身就是模型，不显示模型标签，`model` 被忽略。
- **模型名**：`[A-Za-z0-9][A-Za-z0-9._:/@+=\[\]-]{0,99}`（不能以 `-` 开头，没有空格、引号、`%`、`!`），总是单独一个 argv，不拼进 shell 字符串；
  不合法时 `agent_run_open` / `agent_run_send` 返回 `bad_model`（在找网关要 handle 之前）；空值或不传 = CLI 自己的默认模型。
- **`agent_models {kind, fresh?}`** → `{models: [{id, label, desc?, default?}], default, source: cli|cache|session|hints, flag, protocol}`：
  Codex 以执行账户启动 `codex app-server` 调 `model/list`（结果缓存 10 分钟；失败或空的结果缓存 60 秒，`fresh` 也要距上次询问 60 秒以上
  才重问；同一账户同时只起一个 app-server，其他调用等它的结果——这个 op 远程也能调，而 app-server 会顺带启动用户配置的 stdio MCP；
  账户能用哪些模型以它为准，真机上 ChatGPT 账户拒绝过 gpt-5.5），失败时读 `~/.codex/models_cache.json`，再不行用提示列表；ACP 代理用正在运行的会话报告的列表；其他用定义里的提示。
  `default` 是 CLI 自己设置里的模型（Codex `config.toml` 的 `model`、Claude `~/.claude/settings.json` 的 `model`，都不是凭据文件）。
  旧设备没有这个 op 时界面只用 `agent_info.models.hints`。
- **事件**：`{"type":"model","model":X|null,"options"?:[{id,label,desc}]}`：换模型**确定之后**才发（每轮一个进程的 Codex、还没启动的进程：
  立即；活着的 Claude / ACP 进程：CLI 回答成功后），ACP 会话开始时也发；界面在消息流里画一行“模型：X”，有 `options` 时更新菜单。
  `Run.view()` 和 `agent_run_open` 的结果带 `model`；会话元数据记下控制台最后要的模型（续接时每次打开都记，默认模型记为空；运行中
  `agent_run_send` 换的模型在运行关闭时记下），`agent_session_read` 返回 `model`。
- **界面**：模型标签只在选了模型时显示模型名；没选时只有一个芯片图标（提示文字仍是“模型：默认模型”），手机上不把目录和权限标签挤出屏幕。
  菜单：默认模型（“使用 {CLI} 自己的设置（X）”）、列出的模型（标签和 id 只差大小写时不重复写 id）、手动填过的、“这个会话上次用的”
  （已在列表里就在那一项的说明后加“· 这个会话上次用的”；只取会话元数据，不取 ACP 会话开始时代理自己报告的模型）、
  分隔线、“其他模型…”（输入框，同一个正则先在浏览器里检查，示例“opus、gpt-5.6-sol、gemini-2.5-pro”）。选择按 CLI 记在控制台存储的
  `modelByKind` 里（别名到处一样，所以不按设备）；模型列表（`agent_models` 的结果、ACP 报告的选项）按“设备:CLI”缓存，换设备不串。打开对话时带上 `model`；已有对话里改了模型，下一次 `agent_run_send` 带上新的
  `model`，正在运行时提示“下一轮起使用 X”。
