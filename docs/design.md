# agent-nexus 最终设计

> 状态：定稿 v1.1（2026-11-10，按用户决策修订，见下方“决策日志”）。基线是 `agent-nexus` 工作树 `main` 分支的 f343399。线上 legacy 也是从同一提交起步的，位于 `WorkSpace/mcp/mcp-manager`，分支 `legacy`。
> 来源：三份草稿（A-migration / B-ecosystem / C-agents）加两位评审意见。本文以 A 为骨架，嫁接了评审列出的 must_graft，并逐条处理两份 gaps_in_all（§16 有对照表）。
> 文中 `file:line` 指本工作树的源码。凡标注“已核实”的，都是写作时对照实际代码确认过的。
> **本文件将来会进入公开仓库**，所以不写云主机 IP、域名、SSH 账户、同机其他服务的布局，也不写任何凭据，一律用 `<hub-host>`、`<hub-domain>` 这类占位符。设备一律用角色名：`pc-main`（本机主力 PC）、`pc-next`（本机试运行实例）、`remote-1`、`remote-2`（两台远程设备）；真实设备 id 与角色名的对照只保存在 ops 仓库（§17 决策 6，已在 M0 执行）。
> 用户本轮的两条硬约束贯穿全文：
> 1. 凭据已经隔离到干净目录 `WorkSpace/mcp/credentials-vault`（共 38 个副本，原件未动）。仓库里不出现任何凭据；配回去的办法见 §6.9 的 `anx secrets restore`。
> 2. 企业微信插件收发消息不需要任何凭据，保持今天的行为（§6.10）。

---

## 决策日志（Decision log）

> **本节优先于正文。** 正文任何地方与本节冲突时，以本节为准；受影响的章节已经按本节改写，改写处标注“（决策 Dn）”。§17 只保留问题原文与最终结论的对照。

### 2026-11-10：用户拍板

| # | 决策 | 要点 | 改写的章节 |
|---|---|---|---|
| D1 | **许可证 Apache-2.0** | 核心仓库 `LICENSE` 为 Apache-2.0（M0 已加），`THIRD_PARTY_NOTICES.md` 保留 | §11.1 |
| D2 | **唤醒权限模式按邮箱地址配置** | 每个地址一个 `wake_permission`：`bypass`（默认）\| `full-access` \| `inherit`。`bypass` = claude `--dangerously-skip-permissions` 一类、codex `--dangerously-bypass-approvals-and-sandbox`；`full-access` = codex 的 `danger-full-access` 沙箱（审批策略 never）；`inherit` = 不加任何权限参数，沿用会话自身的模式。网关下发模式名，设备侧适配器负责翻译成命令行参数 | §8.4、§8.7、§8.10、§15 R4 |
| D3 | **私有入网版保留为只在本机构建的目标，同时新增邀请码** | 私有版永不进入公开 CI 和 Release。新增邀请码：网关控制台由 owner 生成**一次性、会过期**的邀请（码或 `agentnexus://` 链接）；公开安装包首次启动时粘贴邀请即可入网，换得**每设备一个**的凭据；设备列表里可以逐台吊销 | §9.6、§11.4（重写）、§14、§13 M16 |
| D4 | **私有插件经设备列表推送到远程设备**（2026-11-10 已由 D4r 修订：插件包改为保存在网关上） | 控制台设备列表里“安装到此设备”：网关从一台已经装有该插件的设备取包，中转给目标设备安装。远程设备不需要任何 GitHub 凭据；网关只做流式中转，不落盘保存插件代码 | §3.2、§6.1、§6.11（重写）、§12.7、§13 M9 |
| D5 | **不做代码签名** | 不买 Authenticode 证书。Tauri updater 的 minisign 更新签名不是代码签名，照常保留 | §9.4、§15 R13 |
| D6 | **设备显示名 = 自报主机名，可设备注名** | 默认显示设备自报的主机名；控制台可以设置备注名（alias）；agent 在 `device_list`、地址展示、未读提醒等所有地方看到的都是备注名（没有备注时用主机名）。`agent_id` 不变，路由仍按 `agent_id` | §5.2、§5.5、§8.1、§8.6 |
| D7 | **桌面更新体验** | 托盘图标的绿点继续表示连接状态；有可用更新时在**另一个角**出现**橙色向上箭头**角标；托盘右键菜单显示“更新到 vX.Y.Z”（没有已知更新时为“检查更新”）。**只有用户点击才安装**，取消 `auto` 模式 | §9.2、§9.4 |
| D8 | **空闲阈值 120 秒** | `presence.idle_after_s = 120` | §8.5、§8.10 |
| D9 | **邮件保留与未读升级** | 已读邮件保留 90 天。未读升级：1 小时未读 → 对收件人再唤醒一次；24 小时未读 → 给 `me`（owner）转发一封提醒；30 天未读 → 自动归档为 `expired` 并通知发件人。发件人可以查询每封邮件每个收件人的状态（delivered / woken / read / expired，另有 recalled），并撤回未读邮件。所有阈值可配置 | §8.4、§8.5、§8.10、§8.11（新增） |
| D10 | **第三方 MCP 连接器：云端单一注册表，默认在网关运行** | 只有一个注册表，存在网关。网页控制台和桌面端都有“添加”按钮，写的都是这个注册表。远程 HTTP/SSE MCP（如阿里云、Cloudflare）**默认运行在网关里**，OAuth 登录在网关域名上完成，7×24 可用，不依赖任何设备在线；必须在本地运行的（stdio、受 IP 限制的）可以固定到指定设备。取代原来“默认在设备侧”的设计，可选的 7×24 `cloud-main` 节点因此不再需要 | §0、§3.2、§7（重写 7.1–7.3、7.5）、§13 M10/M11/M16、§15 R10 |
| D11 | **发给 `me` 的邮件和 agent 的提问在 agent-nexus 内部处理** | 控制台/桌面端新增“收件箱”页（按线程显示，可以以 `me` 身份回复，回复会唤醒收件人）；在线设备弹 Windows toast，点击跳到对应线程；新增网关工具 `ask_user(question, options[], recommended, context, blocking)`，显示为问题卡片（推荐项在最前，其后是其他选项，最后是自由填写的“其他”）；简单问题可以直接在 toast 按钮上作答；答案以邮件形式回到提问的会话并唤醒它。device 插件现有的 `request_user_input` 流程并入这套机制。网关另外提供适合手机的网页收件箱，复用现有 OAuth 登录，可以安装为 PWA。**不做**企业微信或电子邮件桥接 | §2、§5.2、§8.1、§8.5、§8.12（新增）、§13 M17 |
| D12 | **GPT Companion（chatx）保持独立应用** | device 插件的 companion 唤醒适配器继续对接 chatx，chatx 不做任何改动；把 chatx 打包成可安装插件是以后的选项 | §8.7 |

补充约束：设备主机名、IP 一律不写进本文，示例只用角色名或 `<hostname>` 占位符。

### 2026-11-10（后续）：owner 修订 D4

| # | 决策 | 要点 | 改写的章节 |
|---|---|---|---|
| D4r | **内置私有插件保存在网关上（插件库）** | owner 推翻 D4 的“网关不落盘保存插件代码”：内置私有插件（ones、jubian-go、payment、wechat、unity、chrome-devtools 等）由 `anx plugin pack` 打成不含凭据和数据的包，**保存在网关**的 `<state>/plugin-repo/` 里；控制台“插件 → 插件库 → 安装到…”或本机“安装插件 → 插件库”一键安装，设备用**自己的设备凭据**从网关下载并校验 sha256，不再需要来源设备在线。包是私有的：不出现在 `/downloads`，只有 owner 和未吊销的设备能下载；包永不进入公开仓库和 CI。§6.11 的设备间中转降为未实现的备选 | §6.1、§6.11、§6.12（新增）、§17 第 4 条 |

---

## 0. 总原则

1. **线协议保持 `protocol: 1`，新能力靠双向特性协商。** legacy 云端在握手时会拒绝 `protocol != 1`（已核实 `mcpm/cloud.py:100`），所以 C 草稿提出的 protocol 2 不采用。新字段都放在 hello 和 ready 的 `anx` 子对象里。旧端只读取自己认识的键（`hello.get(...)`、agent 只检查 `method == "ready"`，已核实 `mcpm/agent.py:531`），多出来的键会被忽略。新的反向 RPC 方法只有在对端声明了相应 feature 之后才会发送。
2. **只复制，不移动；旧文件一个字节都不改。** 旧配置文件、旧根目录、旧 unit、旧 exe 都保持原样，回滚就是“停新、起旧”。A 草稿里“往 mcp-manager.json 写镜像”的做法被否决，改用 §4.5 的影子根目录。
3. **每个改名都有兼容层，至少保留到 1.0。** 旧名照样能调用，只是不在列表里展示。
4. **先云端、后设备，一次只切一个节点。** 每一步切换前，都要实际演练“新设备连旧云端”这个回滚组合（§12 的门 G2），不能只停留在纸面上。
5. **按用户要求，安全限制一律默认关闭**，但每个限制点都预留开关，默认值为 `null` 或 `false`。失控循环防护也只是一个可选开关。
6. **云端是会合点、邮局和连接器宿主；设备承载本地能力和插件凭据。**（决策 D10）远程 HTTP/SSE 第三方 MCP 默认运行在网关里，它们的 token 存放在网关状态目录（§7.3）；stdio 和受 IP 限制的连接器固定到设备，token 留在设备上。插件的 secrets 只在设备上。云端保存它本来就有的 OAuth 库、邮箱库、连接器注册表和每设备凭据摘要（§11.5）。

---

## 1. 最终命名表（R1）

| 对象 | 现状 | 新名 | 兼容层（保留到 1.0） |
|---|---|---|---|
| 产品名 | mcp-manager / “MCP 管理器” | **agent-nexus**，展示名 “Agent Nexus” | — |
| 公开仓库 | — | `github.com/muxigame/agent-nexus`（PUBLIC） | — |
| 分发名 | `mcp-manager`（pyproject.toml:2） | `agent-nexus` | — |
| Python 包 | `mcpm` | **`agent_nexus`**（不用 `anx`，避免与第三方包重名） | 保留薄 shim 包 `mcpm/`，其中 `cli.py`、`cloud.py`、`agent.py` 都只做 `from agent_nexus.X import *`。所以旧 bootstrap 的 `python -m mcpm.cli serve --tray`（bootstrap.rs:209）和旧 systemd 的 `python -m mcpm.cloud` 都还能用 |
| CLI | `mcpm` / `mcpm-client` / `mcpm-server`（pyproject.toml:17-20） | `anx`（manager 及全部子命令）、`anx-agent`（单独的出站连接器）、`anx-gateway`（云端） | 旧的三个 entry point 指向同一函数，调用时打印一行 deprecation 提示 |
| 云端组件叫法 | cloud / workspace-cloud | **gateway**（网关） | `/healthz` 的 `service` 字段由 `workspace-cloud`（cloud.py:124）改为 `agent-nexus-gateway`，`probe_cloud_release.py` 同步修改 |
| SDK | `mcpkit`（builtins/mcpkit） | 不改名，移到 `sdk/mcpkit`，留在核心仓库 | — |
| 唯一内置插件 | 服务名 `workspace`（builtins/codex-mcp） | **`device`**（`plugins/device`） | manifest 写 `"aliases": ["workspace"]`；云端和 agent 两侧都做别名解析（§3.4） |
| 其他插件 id | wechat、payment、jubian-go、ones、unity、chrome-devtools | **id 不变** | id 是 `mcp_id` 的组成部分，历史对话里的 `tooluse` 依赖它 |
| 插件仓库 | — | `muxigame/anx-plugin-{wecom,payment,jubian-go,ones,unity,chrome-devtools}`（PRIVATE） | — |
| Tauri productName | `MCP 管理器`（tauri.conf.json:3） | `Agent Nexus` | — |
| mainBinaryName | 未设置 | `agent-nexus`（ASCII） | `desktop.py` 查找时同时认 `ANX_DESKTOP_APP` 和 `MCPM_DESKTOP_APP` |
| Cargo 包 / bin | `mcpm-desktop` | `agent-nexus-desktop` / bin `agent-nexus` | — |
| Tauri identifier | `plus.roc.mcpm.desktop`（tauri.conf.json:5） | `io.github.muxigame.agentnexus` | 新 identifier 意味着单实例键和 Run 值名都是新的，旧便携 exe 可以并存，作为回滚件 |
| Windows 数据根 | `%LOCALAPPDATA%\Roc\MCPManager\client`（bootstrap.rs:146-149） | `%LOCALAPPDATA%\AgentNexus` | 首次启动时检测旧根，按 §4 复制迁移 |
| Linux 数据根 | `~/.local/share/roc-mcp-manager/client` | `$XDG_DATA_HOME/agent-nexus` | 同上 |
| 主配置文件 | `<root>/mcp-manager.json`（registry.py:31） | `<root>/agent-nexus.json` | 读取顺序：先新后旧；只有旧文件时只读不写，第一次需要写入时才生成新文件 |
| 状态目录 | `<root>/.mcp-manager/`（registry.py:32） | `<root>/.anx/` | 迁移时复制 |
| 凭据目录 | `<root>/secrets/` | **不变** | 保险库 `root/secrets/**` 原样放回 |
| 插件代码 / 数据 | builtins/* 与 scan 目录 | `<root>/plugins/<id>/<ver>/` 加 `current` 文件；数据在 `<root>/plugin-data/<id>/` | `scan` 继续有效；支持用 link 模式收编旧目录 |
| 插件清单 | `mcp-service.json` | 文件名**不变**，新增 `schema_version: 2` | 没有 `schema_version` 的清单按 v1 解析，即今天 `registry._build` 的逻辑 |
| 环境变量 | `MCPM_ROOT`、`MCPM_DATA_HOME`、`MCPM_AGENT_TOKEN`、`MCPM_SERVER_TOKEN`、`MCPM_DESKTOP_APP`、`MCPM_LOG_STDIO`、`MCPM_RUNTIME_ARCHIVE[_PATH]` | 对应的 `ANX_*` | `agent_nexus.env.get("ROOT")` 先读 `ANX_*`，再回退 `MCPM_*`；supervisor 给子进程两套都设置（supervisor.py:305 附近）；Rust 侧有同样的 `env_compat()` |
| HTTP 头 | `X-MCPM: 1`（web.py:68） | `X-ANX: 1` | 两个都接受 |
| `/api/ping` | `{"mcpm": ver, ...}`（web.py:99） | 同时返回 `"mcpm"` 和 `"anx"` | 旧 Tauri bootstrap（bootstrap.rs:108-110）靠 `mcpm` 键判断 manager 是否就绪 |
| 云端 unit | `mcpmanager-cloud.service`，用户 `mcpmanager` | `agent-nexus-gateway.service`，用户 `agent-nexus` | 旧 unit 只 disable，保留一个月 |
| 云端目录 | `/opt/mcpmanager`、`/etc/mcpmanager/{cloud.json,server.env}`、`/var/lib/mcpmanager` | `/opt/agent-nexus`、`/etc/agent-nexus/{gateway.json,gateway.env}`、`/var/lib/agent-nexus` | `gateway.json` 里 `token_env` 和 `agents` 引用的环境变量名（如 `MCPM_SERVER_TOKEN`）是数据，**不改名**，env 文件直接复制 |
| 默认 token_env | `MCPM_SERVER_TOKEN`（cloud.py:74） | 先找 `ANX_SERVER_TOKEN`，再找 `MCPM_SERVER_TOKEN` | — |
| Ubuntu 客户端 | 包 `roc-mcp-manager`，user unit `roc-mcp-manager.service` | 包 `agent-nexus`，user unit `agent-nexus.service`（带 `[Install]`） | 启动器检测旧根并复制迁移 |
| FastMCP 服务名 | `"Workspace"`（gateway.py:142） | `"Agent Nexus"` | 公网 MCP URL 和 OAuth issuer **不变**，已有的 ChatGPT/Claude 连接器不需要重新添加 |
| 网关工具 | `workspace_list` / `set_workspace` | `device_list` / `use_device` | 旧名永久可调用（§5.5） |
| 上下文句柄参数 | `workspace_id` | **名字永久不改**，文档里叫“上下文句柄” | 改名会让所有客户端缓存的工具 schema 失效 |
| agent_id 与 token | pc-main / remote-1 / remote-2 / portable-* | **不变** | 云端 `agents` 映射不用动，也不用重新入网 |

---

## 2. 新仓库目录布局（muxigame/agent-nexus）

```
agent-nexus/
├─ pyproject.toml              # name=agent-nexus；version 由 hatch 从 agent_nexus/__init__.py 动态读取
├─ LICENSE  THIRD_PARTY_NOTICES.md  .scrub-allow
├─ agent_nexus/                # 由 mcpm/ 经 git mv 而来
│  ├─ __init__.py              # __version__ = "0.2.0"，唯一版本源
│  ├─ env.py  paths.py         # ANX_*→MCPM_* 回退；根目录解析（Rust 侧同一套规则）
│  ├─ cli.py  supervisor.py  registry.py  lifecycle.py  observers.py  logs.py  web.py  tray.py  desktop.py
│  ├─ migrate.py  secrets_restore.py      # §4、§6.9
│  ├─ enrollment.py            # 不再写死域名，§11.4
│  ├─ link/                    # 原 agent.py
│  │  ├─ connector.py          #   run_agent / LocalGateway（_dispatch 加 session_wake/session_probe/agent_sessions）
│  │  ├─ features.py           #   hello/ready 特性协商
│  │  └─ wake/                 #   base.py claude.py codex.py companion.py notify.py push.py(占位)
│  ├─ plugins/                 # §6
│  │  ├─ manifest.py           #   v1/v2 解析；pydantic 模型与 sdk/mcpkit 共用
│  │  ├─ sources.py installer.py lock.py configpage.py compat_root.py
│  ├─ connectors/              # §7
│  │  ├─ spec.py transports.py oauth.py tokenstore.py pool.py probe.py
│  ├─ gateway/                 # 原 cloud.py + gateway.py + federation.py
│  │  ├─ app.py hub.py facade.py providers.py search.py ingress.py
│  │  ├─ store.py              #   gateway.sqlite3：handles/addresses/mail/wake/events/asks/invites/device_tokens/connectors
│  │  ├─ mail.py notice.py wake.py retention.py escalation.py   # escalation：§8.11
│  │  ├─ ask.py inbox.py       #   ask_user 与收件箱 API（§8.12）
│  │  ├─ invites.py devices.py #   邀请码入网、每设备凭据、吊销、备注名（§11.5、§5.5）
│  │  ├─ relay.py              #   插件包中转（§6.11）
│  │  ├─ connectors_host.py    #   网关托管的连接器与注册表（§7）
│  │  └─ web/                  #   网关自带的网页控制台与移动端收件箱 PWA（manifest.webmanifest、sw.js）
│  └─ static/                  # 本机控制台：插件页、连接器页、收件箱页、设备页、schema 表单渲染器
├─ mcpm/                       # 兼容 shim，1.0 删除
├─ sdk/mcpkit/                 # 共享 SDK（0.2 起独立 semver，tag 形如 mcpkit-v0.2.0）
├─ plugins/device/             # 原 builtins/codex-mcp，唯一的内置插件
│  ├─ mcp-service.json         # schema_version 2，name=device，aliases=[workspace]
│  └─ src/device/  core/ exec/ watch/ ask/ agents/ platform/{common,windows,linux,darwin}/
├─ schemas/                    # 带版本号的契约；由 mcpkit 的 pydantic 模型生成，CI 校验两者一致
│  ├─ plugin-manifest.v2.json  config-ui.v1.json  device-contract.v1.json
│  ├─ connector.v1.json        link-protocol.v1.json  mail-tools.v1.json
├─ desktop/src-tauri/          # Tauri v2：updater/autostart/process，nsis/hooks.nsh
├─ packaging/ubuntu/
├─ deploy/gateway/             # 参数化：install.sh upgrade.py agent-nexus-gateway.service.in nginx.example.conf README.md
├─ templates/plugin-python/    # mcpkit plugin new 的模板，带 .github/workflows/ci.yml
├─ scripts/  version.py build_release.py build_desktop.py build_ubuntu.py make_latest_json.py scrub_check.py
│            cutover/{preflight.ps1,stop-legacy.ps1,anx-cutover.ps1,rollback-guard.ps1}
├─ tests/   （原有 17 个测试文件）+ compat/ mail/ wake/ plugins/ connectors/ migrate/
├─ docs/    design.md（本文）device-contract.md plugin-authoring.md connectors.md mailbox.md migration.md release.md compat-clients.md
└─ .github/workflows/  ci.yml release.yml deploy-gateway.yml plugin-ci.yml（可复用）
```

**不进公开仓库的东西：**
- 6 个业务插件。当前 `.gitignore` 已经排除了 `/builtins/{chrome-devtools,jubian-go,ones,payment,unity,wechat}-mcp/`，工作树里实际只剩 `builtins/{codex-mcp,mcpkit}`（已核实）。
- 企微逆向和探测脚本，迁到 `anx-plugin-wecom/tools/`：`analyze_*wecom*`、`analyze_wxwork_sdk.py`、`analyze_helper_runtime.py`、`probe_wxwork_*`、`probe_wechat_send.py`、`probe_current_send_context.py`、`inspect_wecom_mentions.py`、`inspect_native_thread_ids.py`、`locate_send_manager_control_block.py`、`observe_current_presend.py`。
- 聚变相关脚本 `build_jubian_catalog.py`、`verify_jubian_mcp.py`，迁到 `anx-plugin-jubian-go`。
- 基础设施和私有版脚本，迁到私有仓库 `muxigame/agent-nexus-ops`：`apply_nginx_443_split.py`、`verify_shadow*`、`finalize_workspace_tls.py`、`stage_workspace_*`、`apply_workspace_auth.py`、`verify_domain_deployment.py`、`verify_ssh_deployment.py`、`verify_workspace_oauth_host.py`、`inspect_cloud_routing.py`、`inspect_local_tunnels.py`、`provision_private_ingress.py`、`encrypt_enrollment.py`、`build_private_desktop.ps1`、`smoke_private_release.py`、`activate_local_cloud.py`、`add_cloud_agent.py`、`configure_remote_client.py`、`verify_cloud_host.py`。
- 凭据。它们只在 `credentials-vault`，不进任何仓库。

---

## 3. 兼容性设计（所有里程碑都要过的回归门）

### 3.1 已核实的旧端行为（这是兼容设计的依据）

| 位置 | 行为 | 对设计的约束 |
|---|---|---|
| `cloud.py:100` | `hello.protocol != 1` 时拒绝连接 | 新设备必须继续发 `protocol: 1` |
| `cloud.py:105-111` | 接收循环里，除 `method == "catalog"` 外，其他消息都交给 `peer.resolve()`；后者遇到未知 id 时静默丢弃（federation.py:162-169） | 新设备往旧云端发 `hub_call` 不会出错，但也没有任何效果，所以只能在 ready 里看到 feature 后才发 |
| `cloud.py:148` | media 路由写死 `{"mcp": "workspace"}` | 新 agent 的 `media_get` 要把 `workspace` 当作 `device` 的别名 |
| `agent.py:402-403` 与 `agent.py:546-548` | 收到未知反向 RPC 时，抛出的 `unsupported reverse RPC method` 会被**改写成统一的** `"local MCP call failed..."` | 云端**无法**根据错误文本判断对方“不支持”（B 草稿依赖这一点，不成立），所以只能靠 hello 里的 features 判断 |
| `agent.py:531` | agent 只检查 `response.method == "ready"` | ready 里多出的 `params.anx` 对旧 agent 无害 |
| `federation.py:201-220` | `validate_catalog` 要求每个 `workspaces.*.mcp` 都是 `mcps` 的键，并且只校验这两个字段 | catalog 里新增顶层键（如 `device`）不会破坏旧云端 |
| `gateway.py:221-236` | WorkspaceProvider 按 `workspaces.*.mcp` 找到对应的 mcp 并直接暴露其工具。未选设备时，用 `setdefault` **按工具名**取所有在线设备的并集（gateway.py:233） | **结论：`workspaces.default.mcp = "device"` 在旧网关上能正常工作，不需要别名条目。** 如果再多发一个内容相同的 `mcps.workspace` 别名条目，直出工具不会重复（因为按名字去重），但 `mcplist`/`inventory`（federation.py:247-252）和 `toolsearch`（gateway.py:186-206）会列出两遍。所以**默认不发别名条目**（A 草稿的做法被否决） |
| `gateway.py:156-173` | `set_workspace` 返回 `{workspace_id, agent_id, workspace, **descriptor}`，其中带 `mcp` 字段 | 新设备连旧网关时，返回值里是 `mcp: "device"`。客户端不会拿这个字段去路由，直出工具名也不变，录制测试要锁住这一点（§3.3） |
| `mcpkit/tools.py:68-69` | `ToolFailure` 在 SchemaTool 内部就被转成 `is_error` 的 ToolResult，以**返回值**形式交出 | 未读提醒中间件要同时处理两种失败形态：返回值 `is_error=True`，以及抛出的 `ToolError`/`RoutingError`（§8.5） |
| `enrollment.py:20-21` | `cloud.enabled` 为真时直接返回，不会重新入网 | 私有版设备迁移时只要带上 `cloud` 段和 `secrets/cloud/*`，身份就不会丢（§12 阶段 5） |

### 3.2 特性协商（`schemas/link-protocol.v1.json`）

```jsonc
// device → gateway（hello，protocol 仍为 1）
{"protocol": 1, "agent_id": "pc-main", "catalog": {...},
 "anx": {"version": "0.2.0",
         "features": ["device-contract/1", "mcp-alias/1", "session_wake/1", "session_probe/1",
                      "agent_sessions/1", "hub_call/1", "connectors/1",
                      "connector_sync/1", "plugin_relay/1", "ask/1", "toast_actions/1"]}}
// gateway → device（ready）
{"method": "ready", "params": {"agent_id": "pc-main",
 "anx": {"version": "0.2.0", "features": ["hub_call/1", "mail/1", "mcp-alias/1",
                                          "connector_registry/1", "plugin_relay/1", "ask/1", "inbox/1"],
         "display_name": "<alias 或 hostname>"}}}
```

- 网关只有在 peer 的 features 里看到相应能力时，才会发出 `session_wake`、`session_probe`、`agent_sessions`、`connector_sync`、`plugin_pack`、`plugin_install`、`toast` 这些新方法。旧 agent 不声明 features，网关把它的唤醒任务标记为 `unsupported`（§8.7），也不会把它列为插件中转的来源或目标。
- 设备只有在 ready 里看到 `hub_call/1` 之后，才会发出 device→gateway 请求，格式为 `{"id":"d-<uuid>","method":"hub_call","params":{"op":...}}`。新网关在接收循环（cloud.py:105-111 的位置）里加一个分支，专门处理 `method == "hub_call"`。
- `hub_call` 的 op：`wake_report`、`presence_report`、`mail_local`（本机控制台的收件箱页和 `anx mail` CLI 用）、`ask_create` / `ask_answer`（device 插件并入的 `request_user_input`、toast 按钮或本机收件箱作答，§8.12）、`connector_registry`（本机控制台的“连接器 → 添加”写入网关注册表，§7.2）、`device_admin`（本机控制台的设备页：备注名、吊销、“安装到此设备”）、`relay_report`（插件中转的安装进度，§6.11）。网关托管连接器的 OAuth 登录在网关域名上完成，也不走这个通道（§7.3）。
- `plugin_pack` / `plugin_install` 是网关→设备的反向 RPC，包内容按 1 MiB 分块（`{transfer_id, seq, data_b64, last}`）在两条 ingress 连接之间流式转发，见 §6.11。
- 设备的主机名放在 catalog 的 `device.hostname`（§5.2），网关据此生成显示名（决策 D6）。
- M4 实现备注：`hub_call` 的 params 形如 `{"op": "<op>", "args": {...}}`，例如 `{"op":"device_admin","args":{"op":"set_alias","device_id":…,"alias":…}}`；回复为 `{"id","result"}` 或 `{"id","error"}`。M4 只实现 `device_admin` 的 `list` 与 `set_alias`，其余 op 按名字拒绝，随各自里程碑加入；features 只声明已实现的项（M4：设备 `mcp-alias/1`、`hub_call/1`，网关 `hub_call/1`、`mcp-alias/1`）。网关 API：`GET /api/devices`、`PUT /api/devices/{device_id}` `{"alias"}`，凭据与 MCP 端点相同。

### 3.3 兼容矩阵（`tests/compat/`，M0 起每个里程碑都要跑）

legacy 和 main 的基线是**同一个提交 f343399**，因此把基线的 `mcpm` 原样冻结到 `tests/compat/legacy_mcpm/`（用 `git archive f343399 mcpm` 生成，包名改成 `legacy_mcpm`）。这样就有了一个可以直接 import 的“旧网关”和“旧 agent”，不需要依赖线上环境。测试框架复用 `tests/test_cloud.py:127` 的 uvicorn 加 `run_agent` 组合。

| 组合 | 必须成立 | 实现与断言 |
|---|---|---|
| 旧 agent → 新网关 | catalog 里叫 `mcps.workspace` 的设备上，`set_workspace`、`use_device`、直出工具、`tooluse("x/workspace")`、`tooluse("x/device")`、`/media` 都可用；唤醒任务为 `unsupported` | 新 `Hub.mcp()`、`Hub.workspace()` 做别名解析 `{"workspace"↔"device"}`；media 依次尝试 `device` 和 `workspace` |
| 新 agent → 旧网关（**回滚门**） | 用 `legacy_mcpm` 的网关：`workspace_list`、`set_workspace`、直出工具都正常；直出工具名集合与 legacy codex-mcp 完全一致，且**不重复**；`mcplist` 里每台设备只有一条 device 记录；`set_workspace` 返回 `mcp:"device"`；`/media` 可用（靠别名）；新 agent 不发 `hub_call` | 录制并断言；这个测试同时就是“网关回滚后新设备可用”的门（§12 G2） |
| 旧 Tauri 壳 → 新 manager | `/api/ping` 返回值里有 `mcpm` 键 | web.py:99 返回两个键 |
| 旧客户端缓存的 schema | `set_workspace`、`workspace_list` 及所有直出工具，`required` 集合不变，原有 properties 不删也不改类型 | **规则式快照**（不做全等比较）：只允许新增可选 property。快照文件 `tests/compat/tool_schemas.json` |
| 旧插件（旧 mcpkit） | 能读到控制台写入的配置 | §4.5 影子根目录 |
| 旧 cloud.json → 新 `--check-config` | 通过；未知键只给 warning | upgrade 脚本和部署脚本都以 check 通过作为激活条件（upgrade_cloud_release.py:45） |
| 出现别名条目时 | 开关 `cloud.catalog_legacy_alias=true`（默认 false）发出 `mcps.workspace` 时，新网关按 `alias_of` 去重 | 只为万一需要在旧网关上用 `tooluse("x/workspace")` 的场景准备，默认关闭 |

### 3.4 新增可选参数对客户端缓存的影响（评审 gap）

- 给 `workspace_list`、`device_list`、`mcplist`、`mcpdetail`、`toolsearch` 加一个**可选**的 `workspace_id`，用于识别调用者。这是纯增量：旧调用不带它也能通过校验。ChatGPT 和 Claude 在连接器刷新或会话开始时会重新拉 tools/list，缓存的旧定义只是看不到新参数，调用不会因此失败。
- 有状态会话会在 `set_workspace` 后照旧发出 `tools/list_changed`（gateway.py:170-171）。
- 风险：模型如果用的是旧缓存，就不会传 `workspace_id`，这些调用拿不到提醒。缓解办法见 §8.5 的 client_id 回退和漏提醒计数。
- 规则式快照测试（§3.3）保证不会出现破坏性改动。另外 `docs/compat-clients.md` 记录 ChatGPT 和 Claude.ai 刷新后的实测情况（M6 的验收项）。

---

## 4. R1：配置根与迁移

### 4.1 根目录解析（`agent_nexus/paths.py` 与 Rust 的 `find_root` 用同一套规则）

1. 先看 `ANX_ROOT`，没有再看 `MCPM_ROOT`。指向的目录里必须有 `agent-nexus.json` 或 `mcp-manager.json`。
2. 开发模式：从 exe 所在目录和 cwd 逐级往上找，哪一级有这两个文件之一就用哪一级（取代 cli.py:29-36 和 bootstrap.rs:113-131）。本机走这一条，根目录仍然是 `WorkSpace/mcp`。
3. 安装模式：`%LOCALAPPDATA%\AgentNexus`，Linux 上是 `$XDG_DATA_HOME/agent-nexus`。
4. 第 3 步的目录不存在，而旧根 `%LOCALAPPDATA%\Roc\MCPManager\client\mcp-manager.json` 存在时，执行 `migrate_legacy_root()`。
5. autostart 的 Run 值固定带上 `--root <已解析根>`，避免 map 里提到的问题：从开发目录启动时绑到了错误的根。

### 4.2 `anx migrate`（幂等；默认 `--dry-run` 只打印计划）

```
anx migrate [--from <old_root>] [--to <new_root>] [--in-place] [--port-offset N]
            [--agent-id X] [--cloud-url U] [--apply]
```

| 源（旧根） | 目标 | 处理方式 |
|---|---|---|
| `mcp-manager.json` | `agent-nexus.json` | 键名改写：`services.workspace` → `plugins.device`（settings 原样）；`services.<x>` → `plugins.<x>`，状态记为 `pending_install` 并附上建议仓库 URL；`cloud.workspaces.*.mcp: "workspace"` → `"device"`；`cloud.mcps` → `connectors`（§7.2）；`cloud.agent_id`、`url`、`token_secret`、`token_env` 原样保留；`scan` → `plugin_paths`。写入 `"migrated_from": {"path","sha256","at"}` |
| 明文密钥（例如 `services.jubian-go.settings.password`） | `secrets/jubian-go/password` | 按插件 manifest 里的 secret 声明搬运；插件还没装时，按键名规则 `password|secret|token|apikey` 搬运，配置里改成 `{"$secret":"jubian-go/password"}`，迁移报告里逐项列出 |
| `secrets/**` | `secrets/**` | 原样复制，并调用 mcpkit `protect()` 收紧 ACL；`secrets/workspace/*` 另外复制一份到 `secrets/device/*` |
| `.mcp-manager/state*.json` | `.anx/` | 复制；`run/` 下的 pid 记录**不复制** |
| `builtins/codex-mcp/logs/watchers.sqlite3*` | `plugin-data/device/watchers.sqlite3` | **用 sqlite 在线备份 API 复制**（与 `anx-gateway migrate` 相同，包含尚在 `-wal` 里的内容，旧目录不留下 `-shm`/`-wal`），正在进行的 watch 和 companion 事件历史都能保留（嫁接自 B）；`audit.jsonl` 不复制 |
| 旧根里的 `builtins/<x>/` | 不复制 | 在 `plugins/plugins.lock.json` 里登记成 **link 插件**，指向旧目录，代码和数据都原地不动 |
| `.private/enrollment.json` | 不复制 | 已入网设备不再需要它（enrollment.py:20-21） |
| runtime 文件（`mcpm/`、`.venv`、`tools/`） | 不复制 | 新 runtime 解压到 `runtime/<ver>-<hash8>/`（§9.5） |

- `--in-place`（本机用）：新根就是旧根，在同一个目录里生成 `agent-nexus.json`，`mcp-manager.json` 一个字节都不动。
- `--port-offset N`、`--agent-id`、`--cloud-url` 供并行试运行使用（§12 阶段 2）。端口偏移后写入 `manager.port` 和各插件的 `port`。
- 迁移报告写到 `.anx/migration-report.json`，里面只有路径和 sha256，不包含任何值。

### 4.3 云端：`anx-gateway migrate --from /etc/mcpmanager --state /var/lib/mcpmanager --to /etc/agent-nexus`

- `cloud.json` 改名为 `gateway.json`，其中的绝对路径 `/var/lib/mcpmanager` 改为 `/var/lib/agent-nexus`（`auth.store`、`media_cache_dir`），并补一个 `mailbox` 段（§8.10）。
- `server.env` 改名为 `gateway.env`，里面的变量名不改。
- `oauth.sqlite3` 用 `sqlite3 .backup` 复制；media 缓存目录也复制，然后 chown 给 `agent-nexus`。
- `public_url` 保持不变，OAuth store 的 resource 绑定也就不会失效，ChatGPT 和 Claude **不需要重新授权**。
- `--check-config` 遇到未知键只给 warning（嫁接自 A/B）。

### 4.4 回滚语义

旧程序只认 `mcp-manager.json` 和旧根，所以回滚就是停新、起旧。代价是切换后在新版里做的配置改动，旧版看不到。`anx migrate --export-legacy <file>` 可以把新配置**另存**成旧格式的文件，供人工合并，不会覆盖原文件。

### 4.5 旧 mcpkit 插件的配置桥：影子根目录（替代 A 的“镜像写回”）

旧 mcpkit 按 `MCPM_ROOT/mcp-manager.json` 读取 `services.<name>`（mcpkit/config.py:38、195-205），并从 `<root>/secrets` 读密钥。对于真正加载 0.2 以下 mcpkit 的插件：`requires.mcpkit` 写明时以它为准；v1 清单没写时，看插件项目实际解析到的 mcpkit（path/editable 源、`uv.lock`、插件自己的 `.venv`），版本低于 0.2 或无法判断才算旧插件，根本不用 mcpkit 的 v1 插件不算（M0–M3 评审）：

- manager 生成 `<root>/.anx/compat-root/mcp-manager.json`，内容**只有** `services` 段，而且只投影上述旧插件自己的 `plugins.<name>` 条目（其他插件的 secrets 不会被解析进来），并带 `"_generated_by":"agent-nexus"`；再建目录联接 `compat-root/secrets` 指向 `<root>/secrets`（Windows junction 不需要管理员权限，Linux 用 symlink）。
- 启动这类插件时设置 `MCPM_ROOT=<root>/.anx/compat-root`，同时也设置 `ANX_ROOT=<root>`。
- 好处：真正的 `mcp-manager.json` 原封不动，回滚目标保持干净。等所有插件都升级到 mcpkit 0.2，这个机制就自然不再使用。

---

## 5. R2：device 插件与设备能力契约

### 5.1 节点形态（嫁接自 C，面向非 PC 设备）

| profile | 例子 | 进程模型 | 连接方式 | 唤醒 |
|---|---|---|---|---|
| `full` | Windows/Linux/macOS PC、Linux 服务器 | anx manager 加插件 | manager 出站 WSS `/ingress/{agent_id}` | manager 在本机启动进程 |
| `lite` | Android/iOS 手机、边缘盒子 | 单个原生 App 直接实现 link protocol 的子集（hello/ready/catalog/tool_call/media_get），只提供一个 MCP：`device` | 同一个入口，允许频繁断线 | 在线时用 App 通知；离线时用 `push` 适配器（先定义接口，后实现） |
| `relay` | 手表（经手机）、BLE 传感器 | 由宿主节点代理 | 宿主的 catalog 里多一个 MCP `device-<child>` | 由宿主转发 |

服务器就是 `profile=full, form=server`：没有 screen 和 uia 能力，`reachable: always`，适合托管长期运行的 codex/claude 会话。7×24 的远程连接器已经改为默认跑在网关里（决策 D10，§7.1），不再需要专门的服务器节点。

### 5.2 契约 `anx.device/1`（`schemas/device-contract.v1.json`）

**(a) catalog 新增 `device` 描述块**（旧网关会忽略它）：

```jsonc
"device": {
  "contract": "anx.device/1", "id": "pc-main",
  "hostname": "<hostname>",                      // 设备自报主机名（决策 D6），显示名的默认值
  "profile": "full", "form": "desktop",          // desktop|laptop|server|phone|tablet|watch|edge
  "platform": "windows", "os": "10.0.26200", "arch": "x86_64",
  "agent_version": "0.2.0", "mcp": "device", "reachable": "always",   // always|intermittent|push-only
  "capabilities": {
    "info": {"v": 1}, "notify": {"v": 1}, "files": {"v": 1}, "time": {"v": 1},
    "exec": {"v": 1}, "edit": {"v": 1}, "search": {"v": 1}, "plan": {"v": 1}, "media": {"v": 1}, "watch": {"v": 1}, "ask": {"v": 1},
    "screen": {"v": 1, "available": true}, "uia": {"v": 1, "available": true, "reason": null},
    "input": {"v": 1}, "agents": {"v": 1}, "companion": {"v": 1}
  },
  "agent_runtimes": {
    "claude": {"available": true, "path_known": true}, "codex": {"available": true},
    "companion": {"available": true, "routes": 4}
  }
}
```

agent 在 `refresh()`（agent.py:317）里调用 device 插件的 `device_info` 来填写这一块。插件没起来时，按 `sys.platform` 合成一个最小描述块，只声明 `info`，并标记 `degraded: true`。

**(b) 能力与标准工具名**（已有的工具名全部不变；新能力的工具一律带 `<capability>_` 前缀）：

| 层 | 能力 | 标准工具 | Windows 现状 |
|---|---|---|---|
| 核心，必须实现 | `info` | `device_info()` | 新增 |
| | `notify` | `notify(title, body, level="info"|"warn"|"urgent", actions?, dry_run?)` | 新增：Windows toast，失败时退回托盘气泡；Linux 用 notify-send |
| | `files` | `file_list`、`file_read(path, offset, limit)`、`file_write(path, content, mode)`、`file_stat` | 新增：遵守 policy.py 的 `allowed_roots`；手机上限制在 App 沙箱内 |
| | `time` | `curr_time`、`sleep` | 已有 |
| 标准，可选 | `exec` | `exec_command`、`write_stdin`、`exec_kill`、`exec_list` | 已有 |
| | `edit` | `edit_file`、`patch_file` | 取代原 `apply_patch` |
| | `search` | `file_glob`、`file_grep` | 新增。工具名沿用 `file_` 前缀而不是 `search_`，和 `file_read` 等排在一起 |
| | `plan` | `update_plan` | 已有 |
| | `media` | `view_image` + `media_get` 反向 RPC | 已有 |
| | `watch` | `watch_create/training/list/cancel/event/events` | 已有 |
| | `ask` | `request_user_input(_async/_result)` | 已有。工具名和 schema 不变，但网关声明 `ask/1` 时，实现改为经 `hub_call ask_create` 创建网关侧的 ask（§8.12），问题卡片出现在收件箱和 toast 上，`_result` 从网关取答案；网关不可用时退回今天的本机弹窗（决策 D11） |
| | `agents` | `agent_sessions(kind, workdir)`（§8.3） | 新增 |
| | `companion` | `companion_routes` | 已有（server.py:706-715） |
| 平台：windows | `screen` | `list_screens`、`screenshot`、`vision` | 已有 |
| | `uia` / `input` | `start_control`、`stop_control`、`control_status`、`read_ui`、`element` / `mouse`、`keyboard` | 已有 |
| 将来：android | `location`、`camera`、`sensors`、`clipboard`、`battery`、`apps` | `location_get`、`camera_capture`、`sensor_read`…… | — |
| 将来：watch | `health`、`haptics` | `health_read`；`notify` 加 `haptic` 字段 | — |
| 将来：server | `service` | `service_status`、`service_restart` | — |

规则：
1. 同名工具在所有平台上用同一个 inputSchema 公共子集，平台特有的参数只能以可选参数形式追加。
2. 每个工具的 `_meta` 写 `{"anx/capability": "<id>", "anx/contract": "anx.device/1"}`。网关据此给出可读的错误，例如 `device remote-2 lacks capability 'screen' (platform=linux)`，而不是 unknown tool。
3. 能力可以在运行时不可用。例如 UIA 需要交互式桌面，锁屏或断开远程桌面时报告 `available:false` 并附 `reason`。
4. 内部工具（`session_identity` 等）带 `_meta["anx/internal"]=true`，不出现在 tools/list 和 toolsearch 中。

**(c) 平台实现目录**：`plugins/device/src/device/platform/{common,windows,linux,darwin}/`，每个目录导出 `CAPABILITIES = {...}`。server 按这张表注册工具，也就是把今天 server.py:442-453 的 win32 门控泛化。现有的 TOOL_ANNOTATIONS（server.py:229-306）保持不变，由快照测试锁住。

**(d) 合规测试**：`anx device conformance <mcp-url>` 检查以下各项：核心工具是否齐全；`device_info` 能否通过 schema 校验；每个声明的能力，对应工具是否存在且带 `_meta`；`notify(dry_run=true)` 能否成功；每个工具是否都有 annotations。CI 在 windows 和 ubuntu 上对 `plugins/device` 各跑一遍。将来的 Android 实现通过 adb 端口转发跑同一套测试。

- M5 实现备注：插件目录仍是 `builtins/device`、内部包仍是 `src`（`src.*` → `device.*` 的包改名推迟，见 M5 提交说明），平台表在 `builtins/device/src/platform/{common,windows,linux,darwin}/`。契约 schema 在 `schemas/device-contract.v1.json`（打包进 wheel 的 `agent_nexus/schemas/`），其中 `x-anx-standard-tools` 是每个能力必须提供的工具，`x-anx-optional-tools` 收属于某个能力但不强制的工具（目前为空）。agent 在每次 refresh 时调用插件的 `device_info`，校验通过才发布，失败时发布 `degraded` 块；`cloud.hostname` 仍覆盖插件自报的主机名。设备新增 feature `device-contract/1`。网关：设备缺工具时，按其它设备工具的 `_meta` 或契约表找出能力名，报 `device <显示名> (<id>) lacks capability '<cap>' (platform=<p>): <reason>`；未声明能力的工具不直出（legacy 与 degraded 设备不过滤）；`_meta["anx/internal"]` 的工具不进 tools/list 与 toolsearch，但 `tooluse` 仍可调用；直出工具透传设备的 `_meta`。`/media` 优先用 `device.mcp` 指名的本地 MCP。`notify` 的 `actions` 为 `[{id, label, link?}]`（≤5 个），toast 按钮的回传（`toast_actions/1`、`ask_answer`）随 M17 实现，M5 不声明该 feature。

### 5.3 改名的机械步骤

- `git mv builtins/codex-mcp plugins/device`，内部包 `src.*` 改名为 `device.*`。清单改为 `name: device`、`aliases: ["workspace"]`。
- `registry.discover()`（registry.py:236 起）：如果 `plugins.device` 不存在而 `services.workspace` 存在，就用后者。
- mcpkit 0.2 的 `Service.load` 按 `aliases` 回退，所以 codex-mcp 的 settings.py 即使继续读 `services.workspace`，也能拿到配置。
- `configured_workspaces()` 的默认 mcp 从 `"workspace"` 改为 `"device"`（agent.py:157、167）。`LocalGateway._dispatch` 收到 `tool_call` 或 `media_get` 的 `mcp:"workspace"` 时，按别名解析到 `device`（agent.py:386、404）。
- OmniParser 权重（1.1 GB）不进仓库，也不进 NSIS 安装包。`vision` 第一次使用时下载到 `plugin-data/device/models/`。AGPL 依赖做成可选 extra，并写进 NOTICE。

### 5.4 网关暴露规则

- tools/list：选中了设备时，直出该设备 device MCP 的工具。没有选中时，取所有在线设备工具名的并集，保持 gateway.py:221-234 现有的行为。
- 新的 `DeviceProvider`（由 WorkspaceProvider 改名）按选中设备的 capabilities 过滤，不具备的能力不直出。如果调用落到了不具备该能力的设备上，返回带能力名的 RoutingError。
- 内部工具不暴露。

### 5.5 “workspace” 术语演进与向后兼容

| 新工具 | 旧工具（别名） | 说明 |
|---|---|---|
| `device_list(include_offline=false, capability=None, workspace_id=None)` | `workspace_list(workspace_id=None)` | 新工具返回 `{"devices":[{device_id, display_name, hostname, alias, profile, form, platform, online, capabilities, agent_runtimes, workdirs:[{name,path}], wakeable_kinds}]}`；旧工具返回原结构 `{"workspaces":[...]}`（gateway.py:150-154），每项只追加 `device_form`、`online`、`display_name` 三个字段 |
| `use_device(device_id, workdir="default", session_name=None, kind=None, resume_id=None, companion_route=None, wake="auto", workspace_id=None)` | `set_workspace(agent_id, workspace, + 同样的可选参数)` | 共用同一个实现。`workspace_id` 是调用方已经持有的句柄（§8.2 第 3 层用它改名）。返回旧字段的超集 `{workspace_id, agent_id, workspace, path, mcp, address, name_source, renamed_from?, mail:{unread}, hint}` |

- 配置开关 `gateway.legacy_tools`：`"listed"`（默认，新旧名都出现在 tools/list）| `"hidden"`（旧名不列出但可以调用，用 FastMCP `on_list_tools` 中间件过滤）| `"off"`。计划在切换稳定后改为 `"hidden"`，**永远不设为 `"off"`**。
- `RESERVED_TOOLS`（federation.py:13）新增：`device_list, use_device, mail_send, mail_inbox, mail_read, mail_wait, mail_who, mail_bye, mail_status, mail_recall, ask_user`。同步修改 `tests/test_cloud.py:165` 的断言。
- **显示名（决策 D6）**：`display_name = alias or hostname or agent_id`。备注名存在网关 `devices.alias`，由网页控制台或本机控制台设备页（`hub_call device_admin {op:"set_alias"}`）设置。所有面向 agent 的输出（`device_list`、`workspace_list`、`mail_who`、`use_device` 返回值里的 `device_display`、未读提醒、唤醒提示词、系统邮件）都显示 `display_name`，并在第一次出现时附 `(<agent_id>)`；输入侧 `device_id`/`agent_id` 参数同时接受 agent_id 和 display_name（display_name 有重名时报错并列出候选）。地址仍是 `<name>@<agent_id>`，展示时写成 `<name>@<display_name>`。
- 网关 instructions（gateway.py:142-147）改写：先介绍 `device_list`/`use_device` 和邮箱用法；说明旧名仍然可用；要求在 `use_device` 时带上 `session_name` 和 `kind`，并且每次调用都传 `workspace_id`；不带 `session_name` 时会得到自动名，之后带 `session_name` 和手上的 `workspace_id` 再调一次即可连同邮件改名（§8.2）。

---

## 6. R3：插件平台

### 6.1 原则

- 插件就是“一个目录 + 一份 `mcp-service.json`”。v1 清单（没有 `schema_version`）照今天 `registry._build`（registry.py:270-318）的逻辑解析；v2 只在 v1 基础上增加字段。v2 的 JSON Schema 由 `sdk/mcpkit` 的 pydantic 模型生成，放在 `schemas/plugin-manifest.v2.json`，CI 校验二者一致（嫁接自 B）。
- 插件类型：`process`（HTTP MCP 进程，即现状）、`stdio`（由 manager 用 stdio 拉起，可以直接包装 npx/uvx MCP）、`observe`（只观察，即现状）。
- 内置插件只有 `device`，随 runtime 一起发布，可以禁用，不能卸载。其他插件**一律手动安装**，来源可以是 git URL、本地路径或压缩包。
- agent-nexus 不保存任何 GitHub token，私有仓库的认证交给用户的 git 凭据，而且只在“第一台”设备（通常是 pc-main）上需要。其他设备从网关的插件库一键安装（决策 D4r，§6.12），不需要任何 GitHub 凭据。

### 6.2 manifest v2 示例：企业微信插件（零凭据）

```json
{
  "$schema": "https://raw.githubusercontent.com/muxigame/agent-nexus/v0.2.0/schemas/plugin-manifest.v2.json",
  "schema_version": 2,
  "name": "wechat",
  "display_name": "企业微信",
  "version": "0.4.0",
  "description": "读取这台电脑上企业微信的会话、消息和附件，以登录身份发送消息，查看和提交周报。发送和提交会以你的身份真实生效。",
  "publisher": "muxigame",
  "repository": "https://github.com/muxigame/anx-plugin-wecom",
  "license": "MIT",
  "icon": "favicon.png",
  "kind": "process",
  "platforms": ["win32"],
  "requires": {
    "agent_nexus": ">=0.2,<1",
    "mcpkit": ">=0.2,<0.3",
    "python": ">=3.11",
    "tools": ["uv"],
    "binaries": [{"name": "WXWork.exe", "optional": false, "hint": "需要已登录的企业微信 Windows 客户端"}]
  },
  "aliases": [],
  "legacy": {"dirs": ["wechat-mcp"]},

  "install":   {"steps": [{"run": ["uv", "sync", "--frozen", "--extra", "mcp"]}], "timeout_s": 900},
  "update":    {"migrate": null},
  "uninstall": {"steps": [], "keep_data": true},

  "command": ["uv", "run", "--extra", "mcp", "python", "-m", "wecom_reader.mcp.server",
              "--transport", "http", "--host", "{host}", "--port", "{port}"],
  "port": 8402,
  "env": {
    "WECOM_READER_DECRYPTED_DIR": "{data}/runtime/decrypted",
    "WECOM_READER_DERIVED_DIR": "{data}/runtime/derived"
  },
  "health": {"path": "/healthz", "interval": 60, "startup_timeout_s": 90},
  "autostart": true,
  "autorestart": true,
  "logs": ["{data}/logs/*.log"],
  "mcp": {"path": "/mcp", "auth": "none", "expose": "search", "media": true},

  "settings": {"media_ttl_s": 86400},
  "config": {
    "schema": {
      "type": "object",
      "properties": {
        "media_ttl_s": {"type": "integer", "title": "附件链接有效期（秒）", "default": 86400, "minimum": 60},
        "public_url":  {"type": "string", "format": "uri", "title": "公网地址（可选）", "x-anx-target": "service"}
      },
      "required": []
    },
    "ui": {"groups": [{"title": "常用", "fields": ["media_ttl_s"]},
                      {"title": "高级", "fields": ["public_url"], "collapsed": true}]},
    "restart_on_change": ["media_ttl_s", "public_url"],
    "actions": [
      {"id": "status", "title": "运行状态", "http": {"method": "GET", "path": "/anx/status"}, "display": "kv"}
    ]
  },
  "secrets": [
    {"name": "journal-cookie", "title": "周报提交 Cookie（可选）", "required": false,
     "description": "只有 submit_weekly_report / check_journal_credentials 用到；收发消息不需要任何凭据"},
    {"name": "journal-sid", "title": "周报 sid（可选）", "required": false}
  ],
  "data": ["runtime/", "logs/"],
  "permissions": {"declared": ["ui-automation", "process-memory-read", "network:outbound"]}
}
```

周报相关的两个 secret 已经核实：插件读取的是 `wechat/journal-cookie` 和 `wechat/journal-sid`（legacy `wechat-mcp/wecom_reader/mcp/server.py:816-857`），收发消息的路径不读它们。两者都是 `required: false`，所以不会让插件进入 `needs_config`。

作为对照，一个需要凭据的插件（payment）这样写：

```json
"secrets": [
  {"name": "oauth-password", "title": "OAuth 授权口令", "required": true, "generate": true},
  {"name": "bearer-token",   "title": "Bearer Token",   "required": false, "generate": true}
],
"data": ["config.json", "qrcodes/", "secrets-backups/"],
"config": {"schema": {"type": "object", "properties": {
  "config_file": {"type": "string", "default": "{data}/config.json", "title": "收款目录文件"}}, "required": []}}
```

### 6.3 字段语义（写进 schema）

| 字段 | 说明 |
|---|---|
| `name` | `^[a-z0-9][a-z0-9-]{0,39}$`，同时是 `mcp_id` 的服务段（受 federation.py:11 的 ID_PATTERN 约束） |
| `version` | semver。git 来源的插件，tag 必须是 `v<version>` |
| `requires.agent_nexus` | semver 范围。不满足时拒绝安装或启动，并提示应该升级哪一边 |
| `requires.mcpkit` | `<0.2` 或缺省时走 §4.5 的影子根目录 |
| 占位符 | 保留 `{port}`、`{host}`、`{dir}`（registry.py:113-118）；新增 `{data}`（`plugin-data/<name>`）、`{root}`、`{secrets}`（`secrets/<name>`）、`{wheels}`（runtime 自带的 wheels 目录） |
| `install.steps[]` | 在 staging 目录里依次执行。可用的环境变量有 `ANX_ROOT`、`MCPM_ROOT`、`ANX_PLUGIN_ID`、`ANX_PLUGIN_DIR`、`ANX_PLUGIN_DATA`、`ANX_WHEELS`，以及共享的 `UV_CACHE_DIR`。步骤类型：`run`（argv，可以按平台写成 `{win32: [...], linux: [...]}`）、`uv`、`npm` |
| `config.schema` | JSON Schema 2020-12 的子集：string/integer/number/boolean/enum/基础类型数组/两层以内的 object；`format` 支持 `uri`、`path`、`email`、`multiline`、`secret` |
| `x-anx-target` | `settings`（默认，写入 `plugins.<n>.settings.<k>`）或 `service`（写入 `plugins.<n>.<k>`，只允许 mcpkit 的 `SERVICE_KEYS`） |
| `x-anx-widget` / `x-anx-advanced` / `x-anx-placeholder` | UI 扩展，定义在 `schemas/config-ui.v1.json` |
| `secrets[]` | 只声明名字、标题、是否必填，可选 `generate`（随机生成）。值存放在 `secrets/<name>/<secret>`，用 mcpkit `write_secret`/`protect` 写入。配置里只出现 `{"$secret": "<name>/<secret>"}`，控制台只显示“已设置/未设置”和 `updated_at` |
| `config.actions[]` | 控制台上的按钮，manager 只代理清单里声明过的 path |
| `GET /anx/status` | 可选，返回 `{"items":[{"label","value","level"}]}`，显示在配置页顶部 |
| `data[]` | 更新时需要保留的相对路径（照顾把数据写在代码目录里的旧插件）。新插件应该用 `ANX_PLUGIN_DATA` |
| `permissions` | **仅作说明**，在安装确认页展示，不强制。预留开关 `security.enforce_plugin_permissions: false` |
| `mcp.expose` | `search`（默认）或 `direct`（加 `<name>__` 前缀后进入 tools/list）。device 是唯一的无前缀直出插件 |

缺少必填配置或必填 secret 时，插件状态为 `needs_config`，supervisor 不会自动启动它。

### 6.4 目录与锁文件（版本化目录，不用符号链接，嫁接自 B）

```
<root>/plugins/<name>/0.4.0/          代码（git checkout / 解包），含插件自己的 .venv
<root>/plugins/<name>/0.3.2/          上一版，用于回滚，只保留 1 份
<root>/plugins/<name>/current         文本文件，内容为 "0.4.0"
<root>/plugins/.staging/<name>-<rand>/
<root>/plugin-data/<name>/            持久数据
<root>/secrets/<name>/
<root>/plugins/plugins.lock.json
```

```json
{"wechat": {"version": "0.4.0", "previous": "0.3.2", "mode": "managed",
  "source": {"type": "git", "url": "https://github.com/muxigame/anx-plugin-wecom.git", "ref": "v0.4.0", "commit": "9f2c…"},
  "manifest_sha256": "…", "installed_at": "2026-11-20T08:00:00Z"},
 "unity": {"mode": "link", "path": "D:/dev/anx-plugin-unity", "version": "0.2.1"}}
```

`registry.discover()` 的扫描基目录加入 `<root>/plugins/*/current`，link 插件按锁文件里的绝对路径加入扫描。以点开头的目录照旧跳过（registry.py:247）。

### 6.5 安装 / 更新 / 卸载

**安装**：`anx plugin install <src> [--ref vX] [--link] [--sha256 H] [--yes]`，控制台对应“插件 → 添加”，有 Git、本地文件夹、压缩包三个页签。

1. **获取**到 `.staging/`：
   - git：`git clone --depth 1 --branch <ref>`，ref 默认取最高的 semver tag。
   - 本地路径：复制，过滤 `.venv`、`node_modules`、`logs`。`--link` 时只登记路径。
   - 压缩包（本地或 URL，`.zip`/`.tar.gz`）：解包时拒绝绝对路径和 `..`；带了 `--sha256` 就先校验。
2. **校验**：清单要符合 schema；检查 `requires`、平台；名字不能被别的来源占用；端口冲突沿用 `_flag_port_clashes`（registry.py:340）；`requires.binaries` 缺失只警告。
3. **确认页**（这是 UX，不是安全机制）：显示名称、版本、来源 commit、`permissions`、install steps 的完整命令行，以及需要填写的 secret。CLI 加 `--yes` 跳过。
4. **执行 install.steps**：日志流式写到 `.anx/logs/plugins/<name>-install.log`，控制台通过 SSE 实时显示（复用 web.py:289 的 `/logs/stream` 机制）。单步默认超时 600 秒。
5. **定版**：`os.replace(staging, plugins/<name>/<ver>)`，写 `current` 和锁文件，触发 `/api/rescan`。
6. 有必填项时进入 `needs_config`，控制台直接跳到配置页；否则按 `autostart` 启动，在 `health.startup_timeout_s` 内健康检查通过才算 `running`。超时只标记 `unhealthy`，不自动删除。之后 agent 刷新 catalog（agent.py:559 的 refresh_loop）。

**实现记录（M8，2026-11-10）**：`agent_nexus/plugins/installer.py`。与上文的两处差别：① install steps 在最终版本目录里执行，而不是 `.staging`——uv 的 `.venv` 和 editable `../mcpkit` 记录的是绝对路径，搬动后会失效；失败时整个版本目录删除，效果相同。② 私有插件依赖 `mcpkit = {path = "../mcpkit"}`，安装时把 runtime 自带的 mcpkit 复制到 `plugins/<name>/mcpkit`（其他相对依赖从源文件夹旁边复制），`uv sync` 原样可用。v1 清单的默认安装步骤：有 `package.json` 且没有 `node_modules` 时 `npm ci --omit=dev`，有 `pyproject.toml`（优先 `cwd`）时 `uv sync`（带上 command 里的 `--extra`/`--no-dev`）。首次启动的健康检查失败（退出、`unhealthy` 超时）回滚到安装前（旧版本、`current`、锁文件、配置条目）；mcpkit 以退出码 2 拒绝配置、或缺少必填项时不算失败，状态为 `needs_config`；端口被占用为 `conflict`，提示改端口。安装包模板里没有代码的插件条目显示为 `not_installed`（未安装），不再是“配置错误”。卸载保留 `settings`、`plugin-data/<name>`、`secrets/<name>`，`--purge` 才删除。

**更新**：`anx plugin update <name> [--ref]`。后台每 12 小时 `git ls-remote --tags` 一次，有新版本就在控制台提示。

1. 把新版本获取到新的版本目录并执行 install，旧进程照常运行。
2. 停掉旧进程（复用 supervisor stop），把 `data[]` 列出的旧式数据搬过去（同卷 rename），然后改写 `current`。
3. 如果有 `update.migrate` 就执行它，然后启动并做健康检查。
4. **失败自动回滚**：`current` 改回旧版本，数据搬回，启动旧版本，标记 `update_failed`，保留日志。
5. 新版本如果新增了必填配置，就停在 `needs_config`，等用户填写后再切换。
6. link 插件不支持更新，提示“由用户自行管理”。

**卸载**：`anx plugin uninstall <name> [--purge]`。先停止，再尽量执行 `uninstall.steps`，然后删除代码目录，移出锁文件和 `plugins.<name>` 运行参数。`plugin-data/<name>` 和 `secrets/<name>` **默认保留**，只有 `--purge` 才删除，删除前要确认。device 插件拒绝卸载。

**manager API**（加在 web.py:276-291 的路由表旁边）：

```
GET    /api/plugins                         列表、状态、可更新版本
POST   /api/plugins/install                 {source:{type,url|path,ref,sha256}, link} → job_id
GET    /api/plugins/jobs/{job_id}/stream    SSE 安装日志
POST   /api/plugins/{name}/update           {ref?}
DELETE /api/plugins/{name}?purge=0|1
GET    /api/plugins/{name}/config           {schema, ui, values, secrets:{k:{set,updated_at}}}
PUT    /api/plugins/{name}/config           {values}（422 时附字段路径）
PUT    /api/plugins/{name}/secrets/{secret} {value} 或 {generate:true}
POST   /api/plugins/{name}/actions/{id}
```

旧的 `/api/services/*` 保留，作为运行态视图。

### 6.6 配置页渲染

前端用一个小型的 schema 表单渲染器（`static/schema-form.js`，不依赖第三方库），类型对应关系：string 用 input，integer/number 用 number，boolean 用 switch，enum 用 select，`format:secret` 用 password（只写不读），`format:path` 用 folder 选择器，`multiline` 用 textarea。`ui.groups` 用来分组和折叠。前后端都做校验，后端用 `jsonschema.Draft202012Validator`（这个依赖已在 gateway.py 使用）。旧的 `EDITABLE_FIELDS`（registry.py:36-39：port、autostart 等）放到配置页的“运行”标签下。v1 清单没有 schema，退回到原始 JSON 编辑框。

### 6.7 mcpkit 0.2（`sdk/mcpkit`）

- 配置文件名按顺序查找 `("agent-nexus.json", "mcp-manager.json")`（mcpkit/config.py:38、195-205）；根目录先读 `ANX_ROOT`，再回退 `MCPM_ROOT`；`Service.load` 支持 `aliases`；新增 `plugin_data_dir()`、`service.secret("name")`（包装 `read_secret`，config.py:297），以及 `{"$secret":...}` 引用的解析。
- `mcpkit.plugin`：`@service.action("id")` 注册 `/anx/actions/<id>`，`@service.status` 注册 `/anx/status`；settings 按清单 schema 校验并填充默认值，不合法时打印字段路径并以退出码 2 退出，manager 显示为“配置错误”。
- `mcpkit.manifest`：pydantic 模型，并导出 `schemas/plugin-manifest.v2.json`。
- `mcpkit plugin new <name>`：从 `templates/plugin-python` 生成插件仓库，包括清单、server、tests、`.github/workflows/ci.yml`，以及忽略 `logs/ runtime/ secrets/ config.json *.sqlite3 *keys*.json .venv/` 的 `.gitignore`。
- `mcpkit plugin validate [dir]`：校验 schema，检查占位符是否合法，检查图标不超过 10 KB（registry.py:44）。
- `mcpkit plugin smoke [dir]`：在临时根目录里按清单安装，启动，做健康检查，调用 `tools/list`，**断言每个工具都有 `title` 以及 `readOnlyHint` 和 `destructiveHint`**（否则 ChatGPT 每次都会要求确认，教训见 gateway.py:22-25），最后停止。
- `mcpkit.testing`：pytest fixture `plugin_server`。
- 分发方式：插件依赖写成 `mcpkit @ git+https://github.com/muxigame/agent-nexus@mcpkit-v0.2.0#subdirectory=sdk/mcpkit`。核心仓库是公开的，所以私有插件的 CI 不需要额外凭据。离线安装时，runtime 自带 `wheels/mcpkit-*.whl`，通过 `--find-links {wheels}` 使用。

### 6.8 六个插件的拆仓

| 插件 id | 新私有仓库 | 历史 | 凭据和数据（从保险库配回） |
|---|---|---|---|
| wechat | `anx-plugin-wecom` | 从 `WorkSpace/mcp/nested-git-backup` 恢复 `.git`。上游是 wecom-reader 的 `feat/initial-release` 分支，本地改动（+2249/−283）先做一次提交再推送；保留上游 LICENSE | **无**。`runtime/decrypted`、`wxwork_keys.json` 是运行时派生数据，放 `{data}/runtime`，可以重新生成，不算凭据 |
| payment | `anx-plugin-payment` | 新历史 | `repo/builtins/payment-mcp/{config.json,qrcodes/*,secrets/backups/**,logs/*oauth*.json}` 恢复到 `plugin-data/payment/`；`root/secrets/payment/*` 恢复到 `secrets/payment/` |
| jubian-go | `anx-plugin-jubian-go` | 新历史；`config.example.json` 入库 | 迁移时把明文密码转存到 `secrets/jubian-go/password` |
| ones | `anx-plugin-ones` | 新历史；`logs/` 被 gitignore | `root/secrets/ones/token` |
| unity | `anx-plugin-unity` | 在 muxigame 下 fork 上游 unity-mcp 的 beta 分支（2fcc1795），本地 2 个修改文件和 3 个新增文件合为一个提交；`cwd: "Server"` | 无 |
| chrome-devtools | `anx-plugin-chrome-devtools` | 新历史；install steps 包含 `npm ci` | `root/secrets/chrome-devtools/*` |

每个仓库都要做到：`.gitignore` 由模板生成；清单升级到 v2，写上 `legacy.dirs`；`git status` 干净、gitleaks 无告警后才 push；CI 调用可复用的 `plugin-ci.yml`（§11.3）。

### 6.9 凭据配回：`anx secrets restore`（满足“后面我还得配置回去”）

```
anx secrets restore --vault <WorkSpace/mcp/credentials-vault> [--root <root>] [--include optional] [--apply]
```

默认只打印计划，不做写入。逐个文件比较 sha256：目标不存在就写入；内容相同就跳过；内容不同就**列为冲突，不覆盖**。写入后调用 `protect()`。全程**不打印任何凭据内容**。

| 保险库路径 | 恢复到 | 时机 |
|---|---|---|
| `root/secrets/<svc>/**`（chrome-devtools、ones、payment、workspace、cloud…） | `<root>/secrets/<svc>/**`，路径不变；`workspace/*` 另外复制到 `device/*` | 随时 |
| `root/secrets/cloud-agent-*.env` | `<root>/secrets/`，路径不变（agent token，`cloud.token_env` 或 `token_secret` 会引用） | 随时 |
| `root/mcp-manager.json`、`root/mcp-manager.json.bak-*`、`root/.mcp-manager/*.json` | **不直接覆盖**，只作为 `anx migrate --from-file` 的输入 | 新设备首次配置时 |
| `repo/builtins/payment-mcp/**` | 按清单 `legacy.dirs=["payment-mcp"]` 找到 payment 插件，写到它 `data[]` 声明的位置 | payment 插件安装**之后**才执行；没装就跳过并提示 |
| `repo/builtins/codex-mcp/logs/oauth-store.json.bak-domain` | 默认跳过；加 `--include optional` 时放到 `plugin-data/device/logs/` | 按需 |
| `repo/secrets/build-enrollment.json` | **永远不自动恢复**，只在本地构建私有版时用 `-Enrollment <vault 路径>` 引用 | 本地构建私有版时 |
| `root/云效apikey` | 跳过。接入云效连接器时在控制台“连接器 → 添加”里填写，值写入网关的连接器密钥存储（§7.3）；固定到设备的连接器才用 `anx secrets set connectors/yunxiao/key --from-file <path>` | 按需 |

恢复计划由一张映射表 `agent_nexus/secrets_restore.py:VAULT_MAP` 驱动。表里列不到的文件，会在计划里以“未映射”列出，不会被静默忽略。

### 6.10 企业微信：不需要凭据（R3 的硬约束）

- 现有行为：解密密钥在运行时从 WXWork 进程内存里提取，发送消息走已登录的客户端。两者都不需要用户提供任何凭据。
- 清单 `config.schema.required = []`，所有 secrets 都是 `required:false`，并且只影响周报提交。
- 验收（M9）：在干净的根目录执行 `anx plugin install <wecom 仓库>`，不填任何内容，状态直接变成 `running`/`healthy`，不会出现 `needs_config`。catalog 里有 `send_message`、`get_messages`、`list_conversations`。真机手工验收一次：读最近会话，并给“文件传输助手”发一条消息。CI 用 mock 的 sender 跑 `send_message`，不设置任何 secret。另外加一条 schema 断言：`required` 为空，所有 secrets 的 `required` 都是 false。

### 6.11 远程设备怎么安装私有插件（决策 D4，取代原来的 PAT 方案）

> **已被 D4r 修订**：内置私有插件改由网关上的插件库（§6.12）分发。下面的设备间中转方案没有实现，保留作以后“插件不想放在网关上”时的备选。

**控制台设备列表 → 选择目标设备 → “安装到此设备”**（网页控制台和本机控制台都有）：

1. 列表里为每个插件显示“已安装在哪些设备上（版本）”，数据来自各设备 catalog 里的插件版本。用户选择插件和来源设备（默认取版本最高、在线的那台），以及目标设备。
2. 网关检查：来源和目标都在线，且都声明了 `plugin_relay/1`；目标设备平台满足插件清单的 `platforms`。
3. 网关对来源设备发 `plugin_pack {name, version}`。来源设备执行与 `anx plugin pack` 相同的打包：只打包代码目录（排除 `.venv`、`node_modules`、`logs`、`plugin-data`、`secrets`、清单 `data[]` 声明的路径），生成 zip 和 sha256，并附上锁文件里的来源信息（`source.url/ref/commit`）。
4. 网关把分块**流式转发**给目标设备的 `plugin_install {transfer_id, name, version, sha256, size, source_meta}`，**只在内存里经过、不写盘**，单个包上限 `relay.max_package_mb`（默认 200）。目标设备把分块写入 `plugins/.staging/`，校验 sha256 后走 §6.5 的“校验 → 确认 → install.steps → 定版”流程，锁文件里记为 `source: {type: "relay", from: "<agent_id>", origin: <来源设备的 source>}`。
5. 目标设备用 `hub_call relay_report {transfer_id, state, log_chunk}` 回报进度，网关转给发起的控制台（SSE）。确认页（permissions、install steps、需要填写的 secret）显示在**发起操作的控制台**上，由用户点确认，确认结果随 `plugin_install` 的第二阶段下发。
6. **secrets 不随包转发**。目标设备缺必填 secret 时进入 `needs_config`，用户在控制台的目标设备配置页填写（经 `hub_call` 写到目标设备的 `secrets/`，网关不保存）。
7. 更新：目标设备上 `source.type=relay` 的插件，“检查更新”比对所有在线设备上该插件的最高版本，有更高版本时提示“从 <display_name> 更新”。
8. 回退方案：`anx plugin pack` 生成 zip，再在目标设备上 `anx plugin install x.zip --sha256 …`（离线场景、或对端是旧版 agent）。

远程设备**不需要任何 GitHub 凭据**。网关只做中转，不保存私有插件代码。

### 6.12 插件库：网关上的私有插件包（决策 D4r）

**存储。** 包放在 `plugin_repo.dir`（默认 `<state_dir>/plugin-repo`）下的 `<name>/<version>/<name>-<version>.zip`，索引在 `gateway.sqlite3` 的 `plugin_packages` 表（store 迁移 6：name、version、title、description、sha256、size、files、uploaded_at、uploaded_by、info_json）。配置 `plugin_repo: {dir, max_package_mb}`（默认 300 MB）；`plugin_repo.dir` 与 `downloads.dir` 重叠时网关拒绝启动。网关启动、`deploy/gateway/install.sh` 和 `anx-gateway migrate --apply` 都把目录建成只有 owner 可读（POSIX 0700，Windows 只给当前用户的 ACL）。包是**私有**的：`/downloads` 永不列出，所有接口都要 owner 或未吊销的设备凭据。

**打包：`anx plugin pack <folder> [-o out.zip]`**（`agent_nexus/plugins/pack.py`，纯函数）。读 `mcp-service.json`（v1/v2；版本号可来自 `pyproject.toml`）得到 name 和 version。排除：`.venv`、`.git`、`node_modules`、`__pycache__`、`.pytest_cache`、`.runtime-test`、`logs`、`secrets`、`tests`、`*.egg-info`、`config.json`（保留 `*.example.json`）、`*.sqlite3*`、`*.log`、`.env*`、`*keys*.json`、`oauth*.json`、插件自己 `.gitignore` 的条目、清单 `data[]` 声明的路径和 `env` 里指向 `{dir}/…` 的目录、v1 插件的数据目录（payment 的 `qrcodes/`、wechat 的 `runtime/` 等，`LEGACY_DATA`）、显然不是运行所需的目录（unity 的 `TestProjects/`、`website/`、`docs/` 等，`LEGACY_PRUNE`；清单可以用 `pack.exclude` 自己声明）。**文件名和目录名不分大小写**：Windows 和 macOS 上 `Config.json`、`.ENV`、`Secrets/`、`Logs/run.LOG` 就是 `config.json`、`.env`、`secrets/`、`logs/run.log`，插件会把它们当配置读，所以排除规则、数据路径和网关的检查都先转小写再比较（只有“不需要的 `tests/`”按原样比较：unity 的 `Editor/Resources/Tests` 是代码）。zip 里附 `pack.json`：`{format, name, version, content_sha256, files, created_at, excluded:{total, rules, examples}}`；文件时间固定，同样的内容得到同样的 `content_sha256`。

**凭据扫描（没有 `--force`）。** 留下的文件逐个检查：凭据类文件名（`*.pem`、`*.key`、`id_rsa`、`credentials*.json`、`*secret*.json`、`device-token` 等）、内容规则（私钥、AWS/GitHub/GitLab/Slack/Google/npm 令牌、`sk-…` 密钥、JWT）、看起来真实的 `password/token/secret/api_key/cookie` 赋值：带引号的值（`API_KEY = "…"`、`"token": "…"`）在**所有文本文件**里查，源代码（`.py`、`.js`、`.cs` 等）也查；yaml/ini/cfg/conf/properties/env/shell 文件里不带引号的 `password: …`、`password = …`、`export TOKEN=…` 也算（CRLF 也认）。占位符（`${…}`、`<…>`、`{{…}}`、`your-…`、`changeme` 等）、环境变量名、路径和带点的常量名（`MyApp.Http.LastToken`）除外。命中就拒绝打包，错误列出文件和规则，**从不回显匹配到的值**。网关收到上传时用同一套规则（`inspect_package`）再查一遍，另外拒绝包里出现 `logs/`、`secrets/`、`.git/`、`config.json`、`*.sqlite3*`、`*.log`、`.env*`、`*keys*.json`、`oauth*.json` 和清单声明的数据路径（同样不分大小写）。

**Windows 文件名与包外文件（网关检查）。** Windows 写文件时会去掉结尾的点和空格、把 `名字::$DATA` 当成 `名字`，所以 `config.json.`、`config.json `、`config.json::$DATA`、`.env ` 在设备上都会变成真正的 `config.json`/`.env`。安装器（`installer._safe_member`，网关的 `inspect_package` 也先调用它）直接拒绝这类成员名：路径的任何一段结尾是点或空格、含 `<>:"|?*` 或控制字符、或是 `CON`/`NUL`/`COM1` 等保留名，都是 `unsafe_archive`；名字规则另外按 Windows 实际写出的名字（`pack.windows_name`）比较。包用单一顶层文件夹时，顶层文件夹以外的任何成员（根目录的 `.env`、`.git/config`、`__MACOSX/…`）都算 `secrets_found`：安装器只取那个文件夹，这些文件不会被用到，却会存在网关上、发给每台下载的设备。

**隐私替换（打包时）。** 私有插件的文档和示例里带着 owner 自己账号的标识。`anx plugin pack` 在写进 zip 时替换它们（源文件夹不动），`pack.json` 的 `redacted` 列出改了哪些文件、哪条规则：任何文本文件里 `WXWork/<数字>` 的企业微信账号目录 → `WXWork/<account-id>`；`.md` 里带引号的 15 位以上数字 ID → `"<member-id>"`、UUID → 全零 UUID（代码里的 UUID 不动，可能是真常量）。规则只用通用模式，具体标识永远不写进本仓库；`content_sha256` 按替换后的内容计算。

**接口（`agent_nexus/plugin_repo.py`）。**

| 方法 | 路径 | 谁 | 说明 |
|---|---|---|---|
| GET | `/api/plugin-repo` | owner 或设备 | `{packages:[{name,version,title,description,sha256,size,files,uploaded_at,uploaded_by,platforms,latest}], max_package_mb}`，同名按版本从新到旧 |
| POST | `/api/plugin-repo[?replace=1]` | owner 或设备（见下面“谁能写”） | 请求体是 zip，或 multipart 的 `file`（multipart 必须带 `Content-Length`，否则 411 `length_required`：表单解析器会先把整个请求体落盘再交给我们计数；原始 zip 请求体可以分块，边收边计数）。网关重新校验清单、跑凭据扫描、查大小、算 sha256；同名同版本已存在时 409 `exists`，`?replace=1` 覆盖。201/200 `{package}`；422 `secrets_found`（带 `findings:[{path,rule}]`）/`invalid_manifest`/`invalid_archive`；413 `too_large`；403 `not_your_package` |
| DELETE | `/api/plugin-repo/{name}/{version}` | owner | 删除一个版本 |
| GET | `/api/plugin-repo/{name}/{version}/download` | owner 或设备 | 流式返回 zip，带 `X-ANX-SHA256`；匿名、错误或已吊销的令牌 401；name/version 只接受插件名和版本号的模式，不可能穿越目录 |
| POST | `/api/devices/{id}/plugins/install` | owner | `{name, version?, enable?}`（缺省 version = 最新版），取代原来的 501。网关对设备发反向 RPC `plugin_repo_install {name, version, sha256, size, path, enable}`，返回 202 `{job_id, device_id, display_name, name, version}` |
| GET | `/api/devices/{id}/plugins/jobs/{job_id}?since=N` | owner | 反向 RPC `plugin_repo_job`：`{state, log, log_total, result, error, code}`，控制台轮询显示进度 |

设备凭据：邀请码换来的每设备令牌（`device_tokens` 表，吊销即失效）以及 `agents`/`agent_groups` 环境变量里的旧令牌都算设备；`uploaded_by` 记 `owner`、`cli` 或 `device:<agent_id>`。

**谁能写。** owner（控制台 cookie、bearer、网关主机上的 `anx-gateway plugin-repo`）可以上传、覆盖、删除任何包。设备只能发布**自己的**插件：插件库里这个名字下已有的每个版本都必须是同一台设备上传的，否则 403 `not_your_package`“插件库里的 X 是所有者（或另一台设备）上传的：设备不能覆盖它或给它加新版本……”。这条同时挡住 `?replace=1` 覆盖和“发一个更高版本号”：包会在 owner 安装到的每台设备上跑安装步骤（`uv sync`、`npm ci`）和代码，而“安装到…”缺省装最新版本，一台被攻破的设备不能借此把代码推给其他设备。owner 覆盖了某台设备的包之后，这台设备也就不能再给它发新版本。共用一个 `agent_groups` 令牌的设备算同一个上传者。

**设备侧（新特性 `plugin_repo/1`）。** 只有运行在 manager 里的设备连接（`anx serve`、桌面端）才声明 `plugin_repo/1`，并应答 `plugin_repo_install` / `plugin_repo_job`（`HubLink.handlers`）；单独的 `anx-agent` 不声明。设备收到请求后由 `agent_nexus/plugins/hub_source.py` 用**自己的设备凭据**经 HTTPS 从网关下载到 `plugins/.uploads/`（上限就是网关索引里这个包的 `size`，没有时按 `max_package_mb` 的上界 4096 MB；所以 owner 调高 `plugin_repo.max_package_mb` 后设备照样能装），边下边算 sha256 并与网关索引里的值比对，不一致即拒绝（“插件包可能被篡改”）；然后交给 M8 安装器的 archive 流程（解包 → 安装步骤 → 健康检查 → 定版，失败回滚）。锁文件记 `source: {type: "hub", name, version, sha256}`；包里的名字或版本与请求不符也拒绝。设备离线 → 409 `device_offline`“X 不在线：等它连上网关后再安装”；没有 `plugin_repo/1` 的旧设备 → 409 `unsupported_device`“X 的 Agent Nexus 版本太旧……先把它更新到最新版本”；清单 `platforms` 与设备平台不符 → 409 `unsupported_platform`。

**本机控制台。** 加入网关后，“安装插件”弹窗的第一个来源是“插件库”（`GET /api/plugin-repo` 由本机 manager 代理，附上本机已安装的版本；没加入网关时返回 `not_joined`，不显示这个来源），选中后 `POST /api/plugin-repo/install {name, version?}` 在本机起同样的安装任务。未安装但插件库里有包的行显示“从插件库安装”；从插件库装的插件在库里有更高版本时显示 `有新版本 x.y.z` 和“更新”。

**更新与重装保留 owner 数据。** 一键“安装到…”/“有新版本”会对已装的插件做升级、降级或同版本重装。v1 插件把数据写在代码目录里（payment 的 `config.json`、`qrcodes/`、`secrets/`），所以安装器在放好新版本目录后、跑安装步骤前，把 `data[]`、`LEGACY_DATA` 和 `env` 里 `{dir}/…` 指向的路径从**当前版本**（同版本重装时是它的 `.old-` 备份）同卷搬到新目录（`Installer._carry_data`）；新包自带同名文件时 owner 的数据优先；安装失败时搬回去。之后清理 `.old-` 备份和更旧的版本目录不再会删掉数据。

**发布。** `anx plugin publish <folder|zip> [--replace]`：给文件夹时先打包，然后用本机的设备凭据 **HTTPS `POST /api/plugin-repo`** 上传（不走 `hub_call`：包可达 300 MB，远超链路消息上限）；吊销的设备得到 401。网关主机上用 `anx-gateway plugin-repo add|list|remove --config gateway.json`（add 复制而不是移动原文件，检查与上传相同）。

**边界。** 插件包永不进入公开仓库、CI 或 Release；包里没有凭据和数据，需要账号的插件装好后是 `needs_config`，在设备上填写（§6.9 的 `secrets restore` 照旧可用）。

---

## 7. R4：第三方 MCP 聚合（连接器）

### 7.1 运行位置（决策 D10：云端单一注册表，远程连接器默认跑在网关）

**注册表只有一个，存放在网关**（`gateway.sqlite3` 的 `connectors` 表，§8.4）。网页控制台和桌面端（本机控制台，经 `hub_call connector_registry`）都有“连接器 → 添加”按钮，写的都是这一份。

| `run_on` | 适用 | 行为 |
|---|---|---|
| `"gateway"`（**http/sse 的默认值**） | 远程 HTTP/SSE MCP，例如阿里云、Cloudflare、云效 | 网关进程里的 `ConnectorHost` 直接作为 MCP client 连上游。7×24 可用，**不依赖任何设备在线**。OAuth 登录在网关域名上完成（§7.3）。`mcp_id` 为 `hub/<name>`（`hub` 是保留的 agent_id，设备不能使用） |
| `"device:<agent_id>"`（**stdio 的默认值**，也可以手动选） | stdio MCP（只能在设备上起子进程）；只允许特定出口 IP 访问的服务；需要访问内网的服务 | 网关通过反向 RPC `connector_sync` 把这条定义下发给该设备，设备 manager 用今天的 `configured_targets`（agent.py:170-226）运行它，以 `<agent_id>/<name>` 出现在 catalog 里。定义里引用的 `{secret:…}` 从**该设备的** `secrets/` 读取，网关不保存 |

- 原来的“7×24 需要一个 `cloud-main` 无头节点”方案**作废**，网关本身就承担这个角色。
- 兼容：设备 `agent-nexus.json` 里本地写的 `connectors`（以及旧的 `cloud.mcps`）继续生效，在注册表里以 `source: "device-local"`、只读的形式展示，控制台提供“迁移到网关”按钮（http/sse）或“收编为固定到本设备”（stdio）。旧 agent 不支持 `connector_sync`，固定到它的连接器显示为 `pending_device_upgrade`。
- 网关托管连接器的安全边界：它们的 token 在网关上（§7.3），这是用户为 7×24 可用做的取舍，见风险 R20。

### 7.2 配置（注册表条目；旧的设备本地 `connectors`、`cloud.mcps` 按 §7.1 收编；schema 在 `schemas/connector.v1.json`）

注册表条目与原来设备侧 `connectors` 的写法一致，只多一个 `run_on`。`anx connector export/import` 与 `GET/PUT /api/connectors`（网关）读写的就是下面这个结构：

```json
"connectors": {
  "aliyun": {
    "title": "阿里云 MCP", "run_on": "gateway", "transport": "auto", "url": "https://<aliyun-mcp-endpoint>/mcp",
    "auth": {"type": "oauth", "scopes": [], "client": {"mode": "dynamic"}},
    "expose": "search", "session": "shared", "tools": {"allow": [], "deny": []},
    "timeout_s": 120, "enabled": true
  },
  "cloudflare": {
    "transport": "sse", "url": "https://<cf-mcp-endpoint>/sse",
    "auth": {"type": "oauth", "client": {"mode": "static", "client_id": "<id>", "client_secret": {"$secret": "connectors/cloudflare/client_secret"}}}
  },
  "yunxiao": {
    "transport": "http", "url": "https://<endpoint>/mcp",
    "auth": {"type": "headers", "headers": {"Authorization": "Bearer {secret:connectors/yunxiao/token}",
                                             "X-Api-Key": "{secret:connectors/yunxiao/key}", "X-Region": "cn-hangzhou"}}
  },
  "fs": {
    "run_on": "device:pc-main", "transport": "stdio", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/data"],
    "env": {"SOME_KEY": "{secret:connectors/fs/key}"}, "cwd": "."
  }
}
```

- `transport`：`http`（Streamable HTTP）、`sse`、`stdio`、`auto`。`auto` 先发 Streamable HTTP 的 `initialize`；如果返回 400、404 或 405，就按 MCP 规范的向后兼容建议，对同一个 URL 发 GET 打开 SSE，从 `endpoint` 事件里取 POST 地址。探测结果缓存在 `.anx/connectors.json`。
- `auth.type`：`none`、`bearer`（兼容旧的 `token_env`）、`headers`、`oauth`。`{secret:…}` 和 `{"$secret":…}` 在 `run_on: gateway` 时解析到网关的连接器密钥存储（`/var/lib/agent-nexus/connectors/<name>/`，§7.3），在 `run_on: device:*` 时由该设备上的 mcpkit 解析；`{env:NAME}` 读取运行位置的环境变量。控制台填写的密钥值只写不读。
- `session: "shared"`：网关侧 `ConnectorHost` 每个连接器只维持一个上游会话；设备侧 `LocalGateway.client(name, scope)`（agent.py:229）对连接器强制把 scope 设为 `"shared"`，避免每个上下文句柄各起一个 stdio 子进程或 OAuth 会话。设为 `"per_context"` 则保持今天的行为。
- `expose`：`search` 是默认值，只出现在 mcplist、toolsearch、tooluse。`direct` 时由 `ConnectorProvider` 以 `<name>__<tool>` 的名字放进 tools/list（超过 64 字符时截断并加 hash），开关 `gateway.direct_connector_tools_max` 默认为 `null`。
- catalog 里每个连接器带 `{"kind":"connector","connector_id":"<name>","fingerprint":"sha256(规范化 URL)[:12]","auth_state":"ok|needs_login|error","error":…}`。`Hub.inventory` 已经透传 `error` 字段（federation.py:251）。
- 控制台“连接器 → 添加”（网页控制台和桌面端都有，写同一个注册表）：粘贴 URL 后，由**运行位置**执行 probe（默认网关；选了设备则由该设备执行，`anx connector probe` 是同一段代码），自动探测 transport，以及是否需要 OAuth（判断依据是 401 响应和 `WWW-Authenticate: Bearer resource_metadata=…`），并预先选好鉴权方式。粘贴的是 stdio 命令时，运行位置只能选设备。

### 7.3 OAuth 2.1：fastmcp `OAuth` 的子类，只覆盖两个钩子（嫁接自 A）

> **实现说明（M10/M11，2026-11-10）**：网关托管的连接器没有继承 SDK 的 `OAuthClientProvider`，而是 `agent_nexus/connectors/oauth.py` 的小型客户端（同样是 PRM → AS metadata → DCR → PKCE S256 + resource → 刷新）：登录与传输分开，控制台的 `POST …/login` 直接返回授权地址，回调路由交付授权码，工具调用路径只拿令牌、永不交互。流程与存储位置不变，细节见 docs/connectors.md。

fastmcp 自带的 `fastmcp.client.auth.OAuth` 继承自 mcp SDK 的 `OAuthClientProvider`，已经实现了 RFC 9728 资源元数据发现、RFC 8414 授权服务器元数据、RFC 7591 动态注册、PKCE S256 和刷新。我们只覆盖两个钩子：

**两个运行位置共用同一个子类，只是回调地址和存储位置不同（决策 D10）：**

| | `run_on: gateway`（默认） | `run_on: device:*` |
|---|---|---|
| redirect_uri | `https://<hub-domain>/oauth/connectors/callback/<name>`（`public_url` 下的固定路径，DCR 注册长期有效） | `http://127.0.0.1:<manager_port>/oauth/callback/<name>` |
| 谁发起登录 | 网页控制台或桌面端点“登录”→ 网关返回授权 URL → 在用户当前的浏览器里打开 | 本机控制台 |
| 回调保护 | 回调路由要求 state 匹配，且发起登录的控制台会话已经通过网关现有的 OAuth 登录（owner 身份）；state 一次性、5 分钟过期 | 同今天 |
| token 存放 | `/var/lib/agent-nexus/connectors/<name>/oauth.json`，属主 `agent-nexus`、权限 0600，systemd `ReadWritePaths` 已覆盖 | 设备的 `secrets/connectors/<name>/oauth.json` |
| `needs_login` 提醒 | 给 `me` 发系统邮件（出现在收件箱，在线设备弹 toast，§8.12） | 同左，并调用本机 `notify` |

```python
class ConsoleOAuth(OAuth):        # agent_nexus/connectors/oauth.py（网关和设备共用）
    # redirect_uri 固定（见上表），不用随机端口，DCR 注册才能长期有效
    async def redirect_handler(self, authorization_url):
        self.flow.publish(authorization_url)            # 交给控制台，由控制台打开系统浏览器
    async def callback_handler(self):
        if not self.flow.interactive:
            raise LoginRequired(self.name)              # 非交互路径上绝不弹出登录
        return await self.flow.wait_code(timeout=300)   # 由 /oauth/callback/{name} 路由填入
```

流程：
1. 添加连接器时，先用 `check_if_auth_required` 探测，需要认证的标记为 `needs_login`。
2. 用户点“登录”（网关托管：网关的 `POST /api/connectors/{name}/login`；固定到设备：manager 的同名路由）后，运行位置以 interactive 模式打开 client。SDK 依次完成：资源元数据（PRM）→ 授权服务器元数据（AS metadata，失败时回退到 OIDC discovery）→ DCR（`client_name="Agent Nexus (<agent_id 或 hub>)"`、`redirect_uris=[固定回调地址]`、`grant_types=[authorization_code, refresh_token]`、`token_endpoint_auth_method=none`）。授权服务器不支持 DCR 时，改用 `client.mode=static`（预注册的 client_id/secret）或 CIMD URL。然后生成 PKCE 的 verifier、challenge 和 state，**带上 `resource` 参数（RFC 8707）**。M11 要核实所用 SDK 版本是否会自动带上 resource，不会的话在子类里补上。
3. 浏览器回调（网关 `GET /oauth/connectors/callback/{name}?code&state`，设备 `GET /oauth/callback/{name}?code&state`）：校验 state，由 SDK 换取 token，回调页显示“登录成功，可以关闭”。网关回调路由与网关自身作为 OAuth 授权服务器的 `/authorize`、`/token` 路径互不重叠。
4. 存储：按上表的位置写 `oauth.json`，内容包括 client_info、tokens、expires_at、as_metadata、resource 和 redirect_uri。原子写入，并调用 `protect()`。**不使用 DPAPI**，这样 export/import 才能跨设备迁移（B 草稿在这一点上自相矛盾，这里统一处理）。
5. 刷新：遇到 401 或过期时由 SDK 刷新；另有后台任务在剩余寿命不足 20% 时主动刷新。每个连接器一把 `asyncio.Lock`，并处理 refresh token 轮换。遇到 `invalid_grant` 时清除 token、保留 client_info，状态改为 `needs_login`，然后给 `me` 发一封系统邮件（“aliyun 需要重新登录”，带登录链接；收件箱和 toast 见 §8.12），固定到设备的连接器另外调用本机 `notify`。
6. **交互式登录永远不会发生在 tool_call 路径上**。agent.py:185 那条约束（无人值守的 agent 不做交互 OAuth）继续有效。调用一个状态为 `needs_login` 的连接器，会立即得到可读的错误，例如“请在控制台的连接器页登录 aliyun”（固定到设备时注明设备的 display_name）。
7. 登出：如果授权服务器有 revocation_endpoint 就先撤销，然后删除 token（`DELETE /api/connectors/{name}/token`）。
8. 测试用的授权服务器：直接用 mcpkit 的 `PasswordOAuth`（它自带 DCR 和 PKCE），见 M11。

### 7.4 真实服务的核实（评审 gap）

文中阿里云和 Cloudflare 的 URL 都是占位符。M11 的验收要求：用 `anx connector probe <url>` 对阿里云 MCP 和 Cloudflare MCP 实测，记录以下几项：使用 Streamable HTTP 还是 SSE，401 和 `WWW-Authenticate` 的情况，PRM 和 AS metadata，是否支持 DCR，是否接受网关域名上的 https redirect（默认路径）以及 loopback redirect（固定到设备时），是否限制来源 IP（限制时只能固定到设备），需要哪些 scope，是否支持 offline_access 或 refresh。结果写入 `docs/connectors-tested.md`。如果某个服务不接受 loopback 或不支持 DCR，就走 static client 或 headers 模式，并在文档里写明配置方法。在实测之前，不对这两个服务的鉴权行为下任何结论。

### 7.5 在 tools/list 与 toolsearch 中的呈现

- 默认不进入 tools/list。`mcplist` 返回 `{mcp_id:"hub/aliyun", kind:"connector", run_on:"gateway", auth_state, hosts:[…]}`；固定到设备的为 `{mcp_id:"pc-main/fs", run_on:"device:pc-main", …}`。`hub/*` 在网关进程内直接调用，不经过任何 ingress 连接，`mcplist`、`toolsearch`、`tooluse` 对它们与设备上的 MCP 一视同仁。
- **稳定别名 `@<name>`**（嫁接自 B）：`Hub.mcp("@aliyun")` 解析到一个可用、`auth_state=ok` 的宿主。同一个 fingerprint 同时出现在网关和设备上（例如收编前的设备本地连接器）时，`mcplist` 只列一次并附上 `hosts`。宿主的优先顺序由 `gateway.connector_host_preference` 决定，默认 `["hub", 当前选中设备, 其余在线设备]`。
- `toolsearch` 把连接器工具和插件工具放在一起打分，结果附带 `kind` 和 `auth_state`；`needs_login` 的连接器以 0 分条目附在末尾，提示到哪里去登录。
- 上游发来 `notifications/tools/list_changed` 时立即刷新 catalog。
- 注解原样透传；上游没有提供注解的工具，一律按 `readOnlyHint:false` 处理，不替第三方声称只读。
- 所有客户端（ChatGPT、Claude、Codex、Claude Code）都只配置同一个网关 URL `https://<hub-domain>/mcp`，不需要为每个客户端单独配置。

---

## 8. R5：云端 Agent 邮箱

### 8.1 概念

- **设备**：即 `agent_id`。在线与否由 ingress 连接决定，附带 `device` 描述块。
- **地址（address）**：`<name>@<agent_id>`，例如 `claude-unity@pc-main`。`kind` 取值为 `claude | codex | gpt | human | other`。如果 `name` 在所有设备上唯一，可以只写裸名。
- **上下文句柄（handle，即 workspace_id）**：每次 `use_device` 或 `set_workspace` 都会生成一个，指向（地址，设备，workdir）。句柄持久化存储，24 小时滑动 TTL（gateway.py:94 的语义不变），**网关重启后仍然有效**。
- **群组**：`@all`、`@device:<agent_id>`、`@kind:<k>`、`@team:<name>`（团队成员表）。
- **human**：特殊地址 `me`（kind 为 human，代表 owner）。发给它的邮件进入 agent-nexus 自己的**收件箱**（本机控制台、桌面端、网关网页收件箱/PWA），并对所有在线、且具备 `notify` 能力的设备弹 toast，点击跳到对应线程（决策 D11，§8.12）。owner 在收件箱里以 `me` 的身份回复，回复按普通邮件投递并唤醒收件人。不做企业微信或电子邮件桥接。
- **显示**：地址展示为 `<name>@<display_name>`（决策 D6，§5.5），存储和输入仍以 `<name>@<agent_id>` 为准，两种写法都接受。

### 8.2 身份绑定（在 `use_device` / `set_workspace` 时完成）

- 新增的可选参数：`session_name`、`kind`、`resume_id`（Claude session uuid / Codex thread id；也可以传 `"auto"`）、`companion_route`（chatx 书签 id；也可以传 `"auto"`，此时按 workdir 匹配 chatx 的 `GET /routes`）、`wake`（`auto` 或 `never`）。
- 同一个地址重新绑定时（kind 相同），更新 resume_id 等字段，旧句柄标记为 `superseded_by`，未读邮件全部保留。这样会话重启、压缩上下文或被唤醒之后，地址都不会变（嫁接自 C）。如果 kind 不同，返回 `name_taken` 并给出建议名。
- `kind` 没有传时，按 `get_access_token().client_id` 对应的 DCR `client_name` 推断，推断不出就记为 `other`。
- **会话名分三层自动得到**（`addresses.name_source` 记录来历：`explicit` 显式传入、`auto` 自动生成；迁移 7 之前的行保持 NULL，其中 `anon-…` 行维持旧行为直到按保留策略老化删除，其余 NULL 行按显式对待）：
  1. **自动稳定名（网关）**：没有传 `session_name` 时不再生成 `anon-<handle>`，而是 `<kind>-<workdir 目录名>`，例如 `claude-agent-nexus@pc-main`。kind 部分取 `claude|codex|gpt`；其他客户端取 DCR `client_name` 的 slug（≤20 字符），没有就用 `agent`。目录名取 workdir 最后一段（`/` 和 `\` 都算分隔符，因为设备路径可能是 Windows 路径），转小写，只留 `[a-z0-9._-]`，截断到整个名字留得下 `-NN` 后缀（≤64 字符）；没有 workdir 时就是 `<kind>`。**稳定性**：同一个（设备，OAuth client，规范化 workdir，kind）重连时拿回同一个地址，邮件不丢。候选依次是 `base`、`base-2` … `base-99`：不存在就新建；是同一个键的 `auto` 行、且持有它的句柄已空闲（该句柄最近一次使用早于 `idle_after_s`，或已过期/被取代，或地址已 `ended`）就复用；持有者还活跃（或有唤醒进程在跑）就看下一个。活跃度看的是**句柄**的使用时间而不是地址的 `last_seen_at`：hook 上报会刷新地址，但不能把正在重连的会话挤到 `-2`。调用方的上一个句柄已知（见第 3 层）且它仍属于这个自动地址的**当前持有对话**（没有被标记 `displaced`）时，优先复用那个。同一个对话自己的重新绑定（先 `use_device`、再 `set_workspace`，每次都会发一个新句柄）只把旧句柄记为 `superseded_by`，旧句柄**照样有效**：拿它收发邮件、再绑定或改名都还是这个地址，不会分叉成 `-2`（工具说明要求 agent 保留并复用 `workspace_id`，提示里也嵌着当时的句柄）。只有被别的对话接管时，旧句柄才标记 `handles.displaced=1`（迁移 8）；这样的旧句柄不算“上一个句柄”，既不能抢回也不能改名。**接管**：地址交给一个不是上一个持有者的新句柄时（另一个对话复用了空闲地址），清掉 `resume_id`、`resume_source`、`title`（除非这次绑定自己带了 resume id），免得唤醒续上前一个对话；这个对话的 hook 在下一次提问时会重新上报；该地址上原有的句柄（包括前一个对话自己早先被取代的句柄）全部标记 `displaced`。显式 `session_name` 认领一个空闲的自动地址（见下文“显式名不能抢自动地址”）同样算接管。被接管（`displaced`）的句柄**不再有邮箱身份**：`address_for_handle` 返回空，邮件工具报错“这个 workspace_id 已不再持有 X，请重新调用 set_workspace/use_device”，未读提醒也不再走 client 兜底（否则会读到、标记已读或冒用另一个对话的邮件）；显式地址上的被取代句柄照旧可用（同名就是同一个会话）。被接管的对话**永远拿不回**那个地址：它带着旧句柄重新绑定时跳过该地址（哪怕接管者此时已空闲），用 `session_name` 打那个名字认领也返回 `name_taken`——那里的邮件已经是接管者的。带着自己仍有效的早先句柄重新绑定的对话是**运行中的对话**，不是重启：它只复用没有任何有效句柄（都已过期、被取代或地址已 `ended`）的空闲自动地址；接管一个空闲持有者的地址（连同未读邮件）只留给不带上一个句柄的新绑定（重启的会话，第 1 层正是为它而设）。`mail_wait` 或阻塞的 `ask_user` 进行中的句柄算作**在用**：等待期间不会被判为空闲、不会被接管，presence 显示 `active`（邮件直接到达等待中的调用，不去唤醒）；`mail_wait` 每次返回前再按句柄核对身份，等待中途地址被接管就报错，不把接管者的邮件交出去。同一个对话带着显式（或迁移 7 之前的旧显式）地址上的句柄、不传 `session_name` 重新绑定时，只要同一设备、同一目录（或未知）、同一 kind，就**保留那个名字**，不会分叉成新的自动地址；带着旧 `anon-…` 句柄重新绑定时，那个地址的邮件并入新的自动地址（传了 `session_name` 时随改名一起过去）。**分叉收尾**：每次自动绑定时，同一个（设备，client，目录，kind）下其他没有任何有效句柄（未过期且未被取代）的自动行记为 `ended`：快速重启留下的 `-2` 不再进 `@all`、不再让 hook 匹配有歧义；邮件保留（收件箱照常列出），之后同键的绑定仍可重新启用它。显式会话、旧的 NULL 行、别的 client 或别的目录的地址永远不会被拿来用；保留名（`me`、`anon-*`、`device-ask`）跳过。自动地址是真正的会话：进 `@all`、`@device`、`@kind`，出现在 `mail_who`，hook 能匹配；`wake_policy` 默认 `auto`，但 claude/codex 唤醒**必须有 resume id**（只有 workdir 时 `--continue` 会续上这个目录里“最后一个”会话，可能是别的对话），没有就只在下次调用时提醒。这个检查在派发时（含探测之后）再做一次：接管清掉 resume id 后，接管前已排队（或等设备、冷却中）的唤醒任务记为 `skipped`（reason `no route`），不会以 `--continue` 加接管者的句柄运行。已知局限：同一台机器上所有 Claude Code 会话共用一个 DCR client，同一目录并发的两个会话分到 `base` 和 `base-2`，谁拿到哪个取决于先后，重连后邮件可能落到另一个对话；需要确定身份时请显式传 `session_name`。**显式名不能抢自动地址**：`session_name` 恰好等于一个自动地址、而调用方不是它的当前持有者时，只有“第 1 层本来就会交出去”的情况才允许（同一 OAuth client、同一目录、持有者已空闲），否则返回 `name_taken` 和建议名——自动名是可预测的，不能让别的 client 打一个名字就把别人的邮件接走。认领同样清掉 `resume_id`、`resume_source`、`title`（改名合并进去时目标拿到的是认领者的 resume id），否则唤醒会续上前一个对话。所有检查（是否可认领、kind 是否一致、改名目标的 kind）都在第一次写入之前做完，写入在一个事务里：被拒绝的绑定什么都不改，不会先把空闲持有者的句柄标记成 `displaced`。
  2. **hook 上报身份和标题（设备 → 网关）**：见 §8.3。`presence_report` 也匹配自动地址（永远不匹配旧 `anon-%`），同一目录多个候选时先去掉没有任何有效句柄的自动行（那不可能是正在上报的会话），剩下多个时照旧只认已持有该 id 的那个。已知局限：快速重启后的 `-2` 在其句柄过期（24 小时）或被收尾之前仍算候选，这段时间里这个目录的 hook 匹配可能落空（标题、resume id 暂不更新；不会匹配错）。可选 `title`（会话的可读标题）清洗后（去掉控制/格式字符，合并空白，最多 80 字符）存进 `addresses.title`，出现在 `address_view`、`mail_who` 和收件箱。`title_source=prompt`（由首个提问推出的标题）只填补空标题；客户端自己的标题总是覆盖；同一地址换了一个新的 session id 时先清掉旧标题。
  3. **agent 改名与提示**：`use_device`/`set_workspace` 新增可选参数 `workspace_id`（调用方已持有的句柄；没传时取这个 MCP 会话之前选中的句柄，且必须是同一 OAuth client 的有效句柄——不会仅凭 client 和目录去猜）。如果这个句柄绑定的是同一设备上的 `auto` 地址，而这次带了 `session_name`，就在一个事务里把它**改名**为显式地址：`addresses`（主键、name、`name_source=explicit`，kind 取新传的 kind）、`deliveries.to_addr`、`messages.from_addr` 与 `to_expr` 里的原地址、`asks.from_addr`、`handles.address`、`wake_jobs.address`、`events.address` 全部跟着改；`@team` 成员表和 `messages.to_expr` 里的每一项先按收件人规则解析（`name@设备显示名`、`name@agent_id`、唯一的裸名都算），解析到原地址的才改写：带设备部分的保留原来的设备写法（`claude-x@pc-main` → `reviewer@pc-main`），裸名改成完整的新地址（以免将来指向别的设备上的同名会话），解析到别处的不动。`handles.address` 搬走所有没被标记 `displaced` 的句柄（持有对话自己的旧句柄一起跟过去，拿最早的 `workspace_id` 也能读到改名后的邮件）；被接管的旧句柄留在原地址字符串上，没有身份。内存里的提醒缓存一并清掉，记一条 `address_renamed` 事件。**目标地址已存在时**：kind 相同则**合并**进去——所有投递（已读未读都搬，同一封信两边都有时保留目标那一行）、发出的信、提问、句柄、唤醒任务和历史都搬过去；两边都有活跃唤醒任务时，搬过去的那个结束为 `skipped`（reason `renamed`），保证每个地址只有一个活跃任务；目标保留自己的设置，只补上它缺的 resume id、标题、workdir；然后删掉自动行。kind 不同则返回 `name_taken`（附建议名），**什么都不改**，邮件留在原地址。没有可用的上一个句柄时不改名：新显式地址照常建立，旧自动地址带着邮件留着直到老化。自动命名的会话用 `set_workspace` 换了 workdir 且没带名字时，按新 workdir 重新推导（邮件不搬）。**提示**：自动命名的句柄在第一次拿到结果时（每个句柄一次，进程重启后最多再一次）多一行文本，说明当前地址，并说可以给 `set_workspace`/`use_device` 传 `session_name`（带上 `workspace_id`）换一个好记的名字；这一行放在未读提醒之前，未读提醒仍是最后一个文本块。
- 句柄在迁移前就存在、第一次被邮箱用到时（`address_for_handle` 的补绑），仍按旧规则得到 `anon-<handle>` 地址。
- 返回值新增 `address`、`mail:{unread}`、`hint`（“每次调用都请传 workspace_id；你的邮箱地址是 …”）。

### 8.3 resume id 的三种来源（优先级从高到低）

1. **Hook 上报**：`anx integrate claude` 在 `settings.json`（`$CLAUDE_CONFIG_DIR` 或 `~/.claude`）的 `hooks.SessionStart` 和 `hooks.UserPromptSubmit` 各加一个命令 hook：`<anx 的绝对路径> --root <root> hook claude --event <e>`，`timeout: 5`，`async: true`（Claude 不等它）。命令用正斜杠、不需要时不加引号，Git Bash 和 PowerShell 都能直接运行。hook 从 stdin 读 JSON（`session_id`、`transcript_path`、`cwd`、`hook_event_name`；SessionStart 另有 `source`，UserPromptSubmit 另有 `prompt`；有自定义标题时两者都带 `session_title`），POST 到本机 manager 的 `/api/agents/presence`，manager 再通过 `hub_call presence_report` 上报。`anx integrate codex` 设置 `config.toml`（`$CODEX_HOME` 或 `~/.codex`）的顶层 `notify = ["<anx>", "--root", "<root>", "hook", "codex"]`：Codex 在每个 turn 结束时把 JSON 作为最后一个参数传入（`type=agent-turn-complete`、`thread-id`、`turn-id`、`cwd`、`input-messages`、`last-assistant-message`）。Codex 只能有一个 notify 程序，**已有别的 notify 时拒绝覆盖**并报告（退出码 1）；这时可以用 `anx integrate codex --hooks`，写 Codex 新的 `hooks.json`（与 Claude 同构的 SessionStart/UserPromptSubmit，stdin 为 snake_case JSON），但 Codex 要在界面里信任一次之后才会运行它；这些条目同样带 `async: true`（Codex hooks 支持这个可选字段），Codex 不必每次提问都等它。网关按（设备，kind，cwd）匹配地址，更新 resume_id、presence 和标题（§8.2 第 2 层），并记录 `resume_source=hook`。
   - **标题**：Claude 优先用 stdin 的 `session_title`，其次是 transcript（jsonl）里最后一条 `custom-title.customTitle`、`ai-title.aiTitle`、`agent-name.agentName`；Codex 用 `session_index.jsonl`（`{id, thread_name, updated_at}`，只追加，同一 id 取最后一行）里该 thread 的 `thread_name`。都没有时退回到首个提问的第一行（`/…` 开头的斜杠命令、`<…>` 开头的命令/注入内容不算），标成 `title_source=prompt`。文件小于 4 MB 全读，否则只读头尾各 1 MB。标题会发到网关（最多 80 字符），内容来自用户的会话标题或提问。
   - **hook 绝不影响客户端**：stdout 不输出任何内容（Claude 会把 SessionStart/UserPromptSubmit 的 stdout 注入对话）；任何错误都吞掉、退出码永远是 0（UserPromptSubmit 上的退出码 2 会拦下用户的提问，所以参数解析错误也静默返回 0）；stdin 在线程里读、最多等 1.5 秒；整个 hook 最多 4 秒，超时直接 `os._exit(0)`。
   - **`anx integrate claude|codex [--dry-run] [--remove] [--hooks]`**：幂等（第二次运行不改任何东西）；保留用户文件里所有其他内容；写之前把原文件复制成 `<file>.anx-backup-<YYYYmmdd-HHMMSS>`；`--dry-run` 只打印将要做的改动；`--remove` 只删我们自己的条目（按命令里的 `hook claude|codex` 和 anx/agent_nexus 识别），删空的组和事件；旧版本留下的我们的条目会就地更新。改 `config.toml` 时把 `notify`（连同一行注释和一个空行）插在**文件最开头**：那里一定是顶层，不会被多行字符串里以 `[` 开头的行骗到；更新或删除时逐个试每一行 `notify = …`，由 TOML 解析结果决定哪一行才是真正的顶层键。写入前用 `tomllib` 校验：除 `notify` 外解析结果必须和原来完全一样，否则放弃（提示“无法在不改动其他设置的情况下放置 notify，请手动添加”）。读写都保留原有的换行符（CRLF 保持 CRLF）和开头的 UTF-8 BOM（有 BOM 的文件写回时 BOM 仍在最前面，`notify` 块插在它后面），所以 `config.toml` 的 `--remove` 能逐字节还原。`settings.json` / `hooks.json` 是**就地编辑**而不是重新序列化：只替换变动的那几段，新条目沿用文件自己的缩进、分隔符和换行（CRLF、4 空格、单行的紧凑写法都原样保留），写前确认结果解析后恰好等于计划的内容（否则才退回整体序列化），所以 add 之后 `--remove` 逐字节还原。`--remove` 后只剩空对象或空文本时**保留文件**（写成 `{}` 或空文本）：integrate 分不清文件是自己建的还是用户原有的，删掉用户的文件代价更大。因此“逐字节还原”只对原本就有内容的文件成立；原本不存在的文件 remove 后会留下一个空文件，原有的空 `"hooks": {}` 也可能被去掉。所有路径都跟随 `HOME`/`USERPROFILE`/`CLAUDE_CONFIG_DIR`/`CODEX_HOME`，测试一律用临时目录。
2. **`resume_id="auto"`**：网关调用设备上的 `agent_sessions(kind, workdir)` 反向 RPC（由 manager 直接实现）。claude 会话扫描 `~/.claude/projects/<编码后的 cwd>/*.jsonl`，codex 会话扫描 `~/.codex/sessions/**/rollout-*.jsonl` 中 cwd 匹配的文件，都取 mtime 最新的一个。返回 `{id, confidence, candidates}`，`resume_source=auto`。
3. **显式传入**：`resume_source=explicit`。

上面两种 hook 的格式已核实（Claude Code 官方 hooks 文档与本机插件的 hook 脚本；Codex 0.164 的 `notify` 与 hooks schema）。

### 8.4 数据模型（`/var/lib/agent-nexus/gateway.sqlite3`，WAL 模式，`store.py` 负责 schema 迁移）

```sql
CREATE TABLE schema_version(version INTEGER NOT NULL);
CREATE TABLE devices(
  agent_id TEXT PRIMARY KEY, features TEXT, device_json TEXT,
  hostname TEXT, alias TEXT,                -- 决策 D6：display_name = alias or hostname or agent_id
  enrolled_via TEXT,                        -- legacy_env|invite|private_profile
  online INTEGER NOT NULL DEFAULT 0, last_connected_at REAL, last_seen_at REAL);
CREATE TABLE device_tokens(                 -- 决策 D3：邀请码入网换得的每设备凭据（§11.5）
  agent_id TEXT NOT NULL REFERENCES devices(agent_id), token_sha256 TEXT NOT NULL,
  created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL, revoked_reason TEXT,
  PRIMARY KEY(agent_id, token_sha256));
CREATE TABLE invites(
  id TEXT PRIMARY KEY, secret_sha256 TEXT NOT NULL, label TEXT, suggested_agent_id TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL,
  used_at REAL, used_by_agent_id TEXT, revoked_at REAL);
CREATE TABLE connectors(                    -- 决策 D10：云端单一注册表（§7.1）
  name TEXT PRIMARY KEY, run_on TEXT NOT NULL, spec_json TEXT NOT NULL,   -- spec 里只有 {secret:…} 引用，没有值
  source TEXT NOT NULL DEFAULT 'registry',  -- registry|device-local
  auth_state TEXT, last_error TEXT, updated_at REAL NOT NULL, updated_by TEXT);
CREATE TABLE addresses(
  address TEXT PRIMARY KEY,                 -- "<name>@<agent_id>"
  name TEXT NOT NULL, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
  workdir TEXT, resume_id TEXT, resume_source TEXT, resume_confidence REAL,
  companion_route TEXT, wake_policy TEXT NOT NULL DEFAULT 'auto',
  wake_permission TEXT,                     -- 决策 D2：bypass|full-access|inherit；NULL = 用 mailbox.wake.permission_default（bypass）
  oauth_client_id TEXT, last_seen_at REAL, last_handle TEXT,
  created_at REAL NOT NULL, ended_at REAL, meta TEXT,
  name_source TEXT,                         -- 迁移 7（§8.2）：explicit|auto；更早的行为 NULL
  title TEXT);                              -- 迁移 7：hook 上报的会话标题（≤80 字符）
CREATE INDEX addresses_name ON addresses(name);
CREATE INDEX addresses_auto ON addresses(agent_id, name_source, oauth_client_id);
CREATE TABLE handles(                       -- 取代 gateway.py:86 的内存 dict；内存层只做缓存
  handle TEXT PRIMARY KEY, address TEXT REFERENCES addresses(address),
  agent_id TEXT NOT NULL, workspace TEXT NOT NULL, client_id TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, superseded_by TEXT,
  displaced INTEGER NOT NULL DEFAULT 0);    -- 迁移 8（§8.2）：1 = 别的对话接管了这个自动地址；升级时把 schema 7 留下的接管受害者（自动地址上、被取代且不是当前持有者的句柄）补标为 1
CREATE TABLE groups(name TEXT PRIMARY KEY, members_json TEXT NOT NULL);
CREATE TABLE messages(
  id TEXT PRIMARY KEY,                      -- "m_" + ULID（可按时间排序）
  thread_id TEXT NOT NULL, reply_to TEXT, from_addr TEXT NOT NULL, to_expr TEXT NOT NULL,
  subject TEXT, body TEXT NOT NULL,         -- body ≤ limits.max_body_bytes（默认 64 KiB）
  kind TEXT NOT NULL DEFAULT 'message',     -- message|task|result|system|question|answer
  priority TEXT NOT NULL DEFAULT 'normal',  -- normal|urgent
  hop INTEGER NOT NULL DEFAULT 0, attachments_json TEXT, created_at REAL NOT NULL);
CREATE INDEX messages_thread ON messages(thread_id, created_at);
CREATE TABLE deliveries(                    -- 每个收件人一行，群组在发送时展开
  message_id TEXT NOT NULL REFERENCES messages(id), to_addr TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'unread',     -- unread|read|archived|expired|recalled
  noticed_count INTEGER NOT NULL DEFAULT 0, first_noticed_at REAL, read_at REAL,
  woken_at REAL,                            -- 第一次因这封邮件派发唤醒的时间（发件人状态 woken）
  escalation_level INTEGER NOT NULL DEFAULT 0,   -- 0 无，1 已再唤醒，2 已转告 owner，3 已过期（§8.11）
  expired_at REAL, recalled_at REAL,
  PRIMARY KEY(message_id, to_addr));
CREATE INDEX deliveries_unread ON deliveries(to_addr, state);
CREATE TABLE asks(                          -- 决策 D11：ask_user 与并入的 request_user_input（§8.12）
  id TEXT PRIMARY KEY,                      -- "q_" + ULID
  from_addr TEXT NOT NULL, message_id TEXT REFERENCES messages(id),   -- 发给 me 的 question 邮件
  question TEXT NOT NULL, options_json TEXT NOT NULL, recommended TEXT, context TEXT,
  allow_other INTEGER NOT NULL DEFAULT 1, blocking INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'ask_user',  -- ask_user|request_user_input
  state TEXT NOT NULL DEFAULT 'open',       -- open|answered|cancelled|expired
  answer_json TEXT, answered_via TEXT,      -- inbox|toast|pwa|cli
  answered_at REAL, created_at REAL NOT NULL, expires_at REAL);
CREATE TABLE wake_jobs(
  id TEXT PRIMARY KEY, address TEXT NOT NULL, agent_id TEXT NOT NULL, adapter TEXT NOT NULL,
  state TEXT NOT NULL,   -- queued|waiting_device|probing|dispatched|running|acked|done|skipped_active|unsupported|failed|expired
  message_ids TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
  dispatched_at REAL, started_at REAL, finished_at REAL, exit_code INTEGER, pid INTEGER,
  last_error TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE UNIQUE INDEX wake_one_active ON wake_jobs(address)
  WHERE state IN ('queued','waiting_device','probing','dispatched','running');
CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL, type TEXT, address TEXT, agent_id TEXT, data_json TEXT);
CREATE TABLE counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);   -- 例如 notice_missed、notice_fallback_used
```

- 附件：只存小段文本（≤64 KiB，与 body 合计计算），或者存引用（`media_url`、`{mcp_id, tool, arguments}`），**数据库里不存二进制**。
- **保留策略**（`retention.py`，每天跑一次；决策 D9）：已读邮件保留 `read_days`（默认 90 天）；未读邮件不再永久保留，由 §8.11 的升级流程在 30 天时归档为 `expired`，`expired` 和 `recalled` 的投递记录再按 `read_days` 清理；已结束的 asks 跟随其邮件清理；`events` 保留 30 天；`done/failed/expired` 状态的 wake_jobs 保留 14 天；过期句柄每天清理。每周执行一次 `VACUUM`（WAL checkpoint 之后）。估算：每天 1000 封 2 KB 的邮件，一年约 0.7 GB，所以设一个告警阈值 `retention.warn_db_mb: 1024`，在 `/healthz` 的 detail 里体现。
- systemd 加 `ReadWritePaths=/var/lib/agent-nexus`（对应 deploy_cloud_host.sh:89-95 的写法）。`upgrade.py` 在备份 `oauth.sqlite3` 的同一处（upgrade_cloud_release.py:20-23）用 `.backup` 备份这个库。
- 网关仍然是单进程，store 用一把 asyncio 写锁，读写放在 `asyncio.to_thread` 里执行。

### 8.5 工具签名（网关原生工具，加入 RESERVED_TOOLS；schema 见 `schemas/mail-tools.v1.json`）

```python
mail_send(workspace_id: str | None, to: str | list[str], body: str, subject: str = "",
          thread_id: str | None = None, reply_to: str | None = None,
          kind: Literal["message","task","result"] = "message",
          priority: Literal["normal","urgent"] = "normal",
          wake: Literal["auto","always","never"] = "auto",
          attachments: list[dict] | None = None) -> dict
# -> {"message_id","thread_id",
#     "deliveries":[{"to","presence","action":"noticed_on_next_call|wake_queued|wake_waiting_device|wake_unsupported|none"}],
#     "unknown_recipients":[...]}
# workspace_id 省略时，发件人记为 anon/system

mail_inbox(workspace_id: str, unread_only: bool = True, thread_id: str | None = None,
           limit: int = 20, before: str | None = None) -> dict
# -> {"me","unread","messages":[{"id","thread_id","from","from_kind","subject","snippet","priority","kind","created_at","state"}],"next_before"}

mail_read(workspace_id: str, message_id: str | None = None, thread_id: str | None = None,
          mark_read: bool = True) -> dict
# message_id 省略时读最早一封未读 -> {"messages":[...全文+附件...],"thread_id","participants","unread_remaining"}

mail_wait(workspace_id: str, timeout_s: int = 50, thread_id: str | None = None) -> dict
# 长轮询，最长不超过 min(timeout_s, rpc_timeout-10)；返回格式同 mail_inbox

mail_who(workspace_id: str | None = None, agent_id: str | None = None, kind: str | None = None,
         include_ended: bool = False) -> dict
# -> {"me", "devices":[{agent_id, online, form, capabilities, agent_runtimes, features}],
#     "addresses":[{address, kind, agent_id, presence, last_seen_s_ago, wakeable, wake_adapter,
#                   resume_source, unread, last_wake:{state, at}}]}

mail_bye(workspace_id: str) -> dict    # 结束会话：之后不再唤醒，邮件保留

# 决策 D9：发件人查询状态与撤回
mail_status(workspace_id: str | None, message_id: str) -> dict
# -> {"message_id","thread_id","recipients":[{"to","to_display","status":"delivered|woken|read|expired|recalled",
#     "read_at","woken_at","escalation_level","wake":{state, at}}]}
# status 的推导：recalled > expired > read > woken（woken_at 非空）> delivered
mail_recall(workspace_id: str, message_id: str, to: str | list[str] | None = None) -> dict
# 只有发件人本人能撤回；只撤回仍为 unread 的投递，已读的原样返回 not_recalled
# 撤回后取消该收件人尚未派发的 wake job 中对应的 message_id（job 变空则 skipped）
# -> {"recalled":[...],"not_recalled":[{"to","reason":"already_read|expired|not_sender"}]}

# 决策 D11：向 owner 提问（§8.12）
ask_user(workspace_id: str | None, question: str, options: list[str] | None = None,
         recommended: str | None = None, context: str = "", blocking: bool = False,
         timeout_s: int = 50) -> dict
# -> blocking=False：{"ask_id","message_id","state":"open"}，答案稍后以 kind=answer 的邮件送达并唤醒提问者
# -> blocking=True ：在 min(timeout_s, rpc_timeout-10) 内等到答案就直接返回
#                    {"ask_id","state":"answered","answer":{"choice"|"text","via"}}；
#                    等不到则返回 {"state":"open"}，答案仍会以邮件送达
```

注解：`mail_inbox`、`mail_read`、`mail_wait`、`mail_who`、`mail_status` 用 `ROUTING_READ_ONLY`（gateway.py:25）；`mail_send`、`mail_bye`、`ask_user` 设 `read_only_hint=False, destructive_hint=False, open_world_hint=True`；`mail_recall` 设 `read_only_hint=False, destructive_hint=True`（撤回不可恢复）。中文 title 加到 `tests/test_gateway_annotations.py`。

presence：设备不在 `hub.peers` 里为 `offline`；有正在运行的唤醒任务为 `busy`；`now - last_seen_at < idle_after_s`（默认 120 秒，决策 D8），或者 hook 在这段时间内上报过 prompt，为 `active`；调用过 `mail_bye` 的为 `ended`；其余为 `idle`。`last_seen_at` 由中间件在每次带身份的调用中更新，不需要额外心跳。

### 8.6 未读提醒：`MailNoticeMiddleware`（每个结果末尾都附加，包括错误）

在 `Gateway.build()` 末尾、`return server`（gateway.py:213）之前调用 `server.add_middleware(MailNoticeMiddleware(store, cfg))`。

```python
class MailNoticeMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        args = context.message.arguments or {}
        addr = await self.store.address_for_handle(args.get("workspace_id"))
        raised = None
        try:
            result = await call_next(context)
        except asyncio.CancelledError:
            raise
        except (ToolError, FastMCPError, RoutingError) as exc:   # 原生工具的错误以异常形式到达
            raised = exc
            result = ToolResult(content=[TextContent(type="text", text=str(exc))])
        if context.message.name in ("set_workspace", "use_device") and raised is None:
            addr = addr or await self.store.address_for_handle((result.structured_content or {}).get("workspace_id"))
        if addr is None:
            addr = await self.store.fallback_address(get_access_token())   # 见下文“身份回退”
        if addr is not None:
            await self.store.touch(addr)          # 更新 presence，并确认已发出的唤醒（§8.7）
            notice = await self.store.notice_for(addr)
            if notice:
                result.content = [*result.content, TextContent(type="text", text=notice.text)]
                result.meta = {**(result.meta or {}), "agent-nexus/mail": notice.meta}
                if self.cfg.structured_allows(context, result):
                    result.structured_content = {**result.structured_content, "_mail": notice.meta}
        else:
            await self.store.bump("notice_missed")
        if raised is not None:
            return ToolResult(content=result.content, meta=result.meta, is_error=True)
        return result   # 代理工具的 ToolFailure 已经是 is_error 的返回值（mcpkit/tools.py:68-69），原样保留 is_error
```

- **两种失败形态都要覆盖**：原生工具抛出的 `RoutingError`/`ToolError`，以及直出设备工具通过 `ToolFailure` 返回的 `is_error` 结果（gateway.py:266-267 → mcpkit/tools.py:68）。
- **structuredContent**：工具没有 outputSchema，或者 outputSchema 不是 `additionalProperties:false` 时，才写入 `_mail`；否则只追加文本和 `_meta`，避免 schema 校验失败。网关原生工具都返回宽松的 dict，所以总能写入。配置 `notice.structured` 可选 `auto`、`always`、`never`。
- **身份回退**（评审 gap：无状态、没有带 workspace_id 的调用）：依次尝试 (1) 有状态会话的 ctx state `mcpm_workspace`（gateway.py:97）；(2) `notice.fallback="client_id"`（默认开启）：同一个 OAuth client_id 在最近 30 分钟内**恰好只有一个**活跃地址时用它，多于一个时不猜。都失败时计数器 `notice_missed` 加 1，命中回退时 `notice_fallback_used` 加 1。两个计数器都显示在 `mail_who` 和网关 `/healthz?detail=1` 里，用来评估漏提醒的比例。
- **提醒文本**（固定前缀，单段，≤600 字符，最多列 3 封，urgent 排在最前，不用 emoji）：

```
[agent-nexus 邮箱] claude-unity@pc-main 有 3 封未读：
- m_01JC8… 来自 codex-build@remote-1「构建失败日志」(urgent) 3 分钟前
- m_01JC9… 来自 gpt-main@pc-main「请 review PR」 1 分钟前
- 另有 1 封
用 mail_read(workspace_id="…", message_id="…") 阅读，mail_inbox 查看全部。
```

- `meta` 结构：`{"unread":3,"me":"…","latest":[{"id","from","subject","priority","at"}]}`。
- 重复策略：`notice.repeat="every"`（默认，按用户要求每个结果都附加）或 `"on_change"`。`deliveries.noticed_count` 记录提示次数。
- **客户端实测**（评审 gap）：M6 验收时，在真实的 ChatGPT 和 Claude.ai 上各做一次：给自己发一封信，然后调用 `toolsearch`，让模型原样复述结果末尾的提醒。分别记录 content、structuredContent、`_meta` 三种通道哪个能被模型看到，写进 `docs/compat-clients.md`，并据此调整 `notice.structured` 的默认值。

### 8.7 唤醒路由与离线队列

**入队判定**（`mail_send` 展开收件人之后，对每个收件人 R）：

```
if R.kind == human                                                  → action = inbox（进收件箱 + 在线设备 toast，§8.12）
elif R.wake_policy == never or msg.wake == never                    → action = noticed_on_next_call
elif R.presence == active and msg.wake != always                   → noticed_on_next_call
elif R 没有路由（claude/codex 缺 resume_id 也缺 workdir；gpt 缺 companion_route）→ noticed_on_next_call
elif R 已有活跃 job → 把 message_id 追加进该 job 的 message_ids（合并）
else → 新建 job：设备在线时为 queued，否则为 waiting_device；next_at = now + debounce_s（默认 5 秒，用来合并连发的邮件）
```

**派发**（WakeDispatcher：在 app lifespan 中启动，每 2 秒扫描一次；`mail_send` 时、以及 `Hub.register`（federation.py:184）新增的 `on_device_online` 回调时立即触发）：

```
for job in due(state=queued, next_at<=now):
    if now > job.expires_at: expired; continue                       # 默认 24 小时
    peer = hub.peers.get(job.agent_id)
    if not peer: waiting_device; continue
    if "session_wake/1" not in peer.features:                        # 旧 agent
        if cfg.wake.fallback_exec: 通过 tool_call device|workspace/exec_command 执行命令模板（受设备 policy 约束）
        else: unsupported；返回给发件人的 deliveries 中说明; continue
    if adapter in (claude, codex) and "session_probe/1" in features and cfg.wake.probe_live:
        probe = await peer.call("session_probe", {kind, resume_id, workdir})   # 超时 10 秒
        if probe.live: skipped_active; continue                      # 不 fork 正在交互使用的会话；对方下次调用会看到提醒
    r = await peer.call("session_wake", {...})                       # 设备立即返回，不阻塞 Peer 的 180 秒槽位
    if r.accepted: dispatched（记录 dispatched_at、pid 或 event_id）
    else: attempts+1；next_at = now + backoff[attempts]（10s、30s、2m、10m、30m，上限 1h）；达到 max_attempts（默认 20）后 failed，并给发件人回一封 system 邮件
```

- **确认（ack）**：收件人在 `dispatched_at` 之后的下一次工具调用（中间件里的 `touch`）把 job 置为 `acked`。设备的 `hub_call wake_report{job_id, state, exit_code}` 把 job 置为 `done` 或 `failed`，并记录 exit_code。超过 `ack_timeout_s`（默认 600 秒）既没有 ack 也没有 report 时，回到 `queued` 重试，仍受 max_attempts 限制。
- **每个地址同一时刻只有一个活跃唤醒**（唯一索引保证），这是防止多个进程同时 `--resume` 同一会话最关键的一条。
- **运行期间到达的新邮件**：收到 `wake_report` 之后，如果该地址还有在 `started_at` 之后到达的未读邮件，再新建一个 job（debounce 10 秒）。
- **网关重启**：`dispatched` 和 `running` 状态的 job，如果超过 `run_timeout_s`，先调用 `session_probe` 核对状态，再决定置为 `done` 还是回到 `queued`；`probing` 回到 `queued`。重复唤醒的最坏后果只是多一次 resume。
- **离线排队**：`waiting_device` 状态的 job 在设备重连（`on_device_online`）后立即转为 `queued`。

**唤醒提示词**（网关生成，所有适配器共用；句柄在此时续期，必要时生成新句柄）：

```
[agent-nexus] 你（{address}）有 {n} 封新邮件，最新一封来自 {from}：「{subject}」。
你的 workspace_id 是 {handle}（已续期）。请调用 mail_inbox(workspace_id="{handle}") 阅读并处理，
需要回复时用 mail_send(reply_to=…)。处理完可以结束本次运行。
```

**设备侧适配器**（`agent_nexus/link/wake/`，在 manager 进程内执行，**不经过 device 插件的 exec policy**，device 插件挂了 claude/codex 的唤醒照样可用）：

| 适配器 | 默认命令模板（`agent-nexus.json` 的 `wake.<kind>`，可以覆盖） | 说明 |
|---|---|---|
| `claude` | `["{claude}", "-p", "--resume", "{resume_id}", "--output-format", "json", "{permission_flags...}", "{prompt}"]`，cwd 为 `{workdir}` | `{claude}` 的查找顺序：配置的路径 → PATH → `%APPDATA%\npm\claude.cmd` → `~/.local/bin/claude`（本机 PATH 上没有 claude）。`permission_flags` 由地址的 `wake_permission` 决定，见下表 |
| `codex` | `["{codex}", "exec", "resume", "{resume_id}", "--json", "--skip-git-repo-check", "{permission_flags...}", "{prompt}"]`；没有 id 时用 `["{codex}", "exec", "resume", "--last", ...]` | 已在本机 codex-cli 0.164.0 上核实：`exec resume` 支持 `[SESSION_ID] [PROMPT]`、`--last`、`--json`、`--skip-git-repo-check`、`-c key=value` 和 `--dangerously-bypass-approvals-and-sandbox`。`permission_flags` 见下表 |
| `companion` | 调用 device 插件的内部端点 `POST /anx/companion-event {project_id, event_type:"mail_received", summary=prompt}` | 内部执行泛化后的 `WatchManager._emit`（watchers.py:477）：插入一行 `watch_events`（`watch_id='mail'`），再调用 `_deliver_event_id`（watchers.py:495），也就是 chatx `POST /event`。投递失败由现有的 `_retry_pending`（watchers.py:524）重试。chatx 会输入固定文本“处理本地事件 evt-…”，GPT 调用 `watch_event(evt)`，拿到的 summary 就是唤醒提示词，于是它会去调用 mail_inbox。**chatx 不需要改动**（决策 D12：chatx 保持独立应用；以后可以选择把它打包成可安装插件） |
| `notify` | 调用 device 插件的 `notify`（设备声明 `toast_actions/1` 时带按钮和点击跳转，§8.12） | 用于 human 收件人（`me`）和 `ask_user` |
| `push` | 占位 | 用于 lite 节点 |

**唤醒权限模式（决策 D2，按地址配置）**：网关在 `session_wake` 参数里下发 `permission: "bypass"|"full-access"|"inherit"`，取值顺序为 地址的 `wake_permission` → `mailbox.wake.permission_default`（默认 `bypass`）。设备侧把它翻译成命令行参数，翻译表可在 `agent-nexus.json` 的 `wake.permission_flags.<kind>.<mode>` 中覆盖：

| 模式 | claude | codex | 说明 |
|---|---|---|---|
| `bypass`（默认） | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | 不弹任何确认，不加沙箱 |
| `full-access` | `--permission-mode bypassPermissions` | `-c sandbox_mode="danger-full-access" -c approval_policy="never"` | codex 用 danger-full-access 沙箱、不审批；claude 没有独立的沙箱概念，效果与 bypass 相同 |
| `inherit` | 不加参数 | 不加参数 | 沿用会话自己的权限配置（settings / config.toml / resume 时记录的模式）。如果该模式会弹确认，被唤醒的会话可能停在提示上，`anx wake doctor` 会对 inherit 地址给出警告 |

- 设置入口：收件箱和设备页里每个地址都有“唤醒权限”下拉框（网关 `PUT /api/addresses/{address}`，本机控制台经 `hub_call device_admin`）；CLI `anx mail address set <address> --wake-permission <mode>`。工具 schema 不新增这个参数，避免 agent 在调用中改写。
- codex `full-access` 的参数在 M7 用 `anx wake doctor` 在 codex-cli 0.164.0 上再核实一次，不对就改翻译表默认值。
- companion（gpt）地址没有权限模式，该字段忽略。

- 启动方式：Windows 上用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB`（与 supervisor.py:236 的做法一致），stdout 和 stderr 写到 `.anx/wake/<job>.log`，进程退出后由 manager 发送 `wake_report`。如果 manager 在进程运行期间重启，就通过 pid 文件接管：重启后轮询该 pid，退出后补发 report。
- 环境：继承 manager 的环境，并在前面补上 `%APPDATA%\npm` 和 `~/.local/bin`（autostart 启动的进程 PATH 可能不完整）。
- **会话活性探测 `session_probe`**（嫁接自 C）：(a) 有没有 claude/codex 进程的命令行里带着这个 resume_id；(b) 会话文件在 30 秒内是否被写过；(c) hook 在 `idle_after_s` 内是否上报过 prompt。三项任意一项为真就判为 live。这只是启发式判断，见风险 R1。

**锁屏、注销和 chatx 未运行**（评审 gap）：

| 情形 | 行为 |
|---|---|
| 用户已登录但锁屏 | manager 照常运行，claude/codex 唤醒不受影响。companion 依赖 WebView2 里的 JS 注入，属于尽力而为；UIA 类能力报告 `available:false` |
| 用户注销或机器未登录 | 自启动是 HKCU Run，只在登录后运行，所以 manager 不在，设备离线，job 进入 `waiting_device`，用户登录后自动投递。是否支持“不登录也运行”的计划任务模式，属于后续选项，0.2 不做 |
| chatx 未运行 | `companion_routes` 失败，`device` 描述块中 companion 为 `available:false`。gpt 地址的 job 保持 `queued` 并按退避重试，同时对该设备做一次 `notify`（“有发给 GPT 的邮件，请打开 GPT Companion”）。达到 max_attempts 后 failed |
| 远程桌面断开 | 和锁屏相同 |

**无人值守参数的验收**（评审 gap）：`anx wake doctor [--kind claude|codex|gpt]` 在设备上执行以下检查：解析可执行文件路径并打印版本；用一个临时会话（先新建一个 scratch 会话拿到 id）实际执行一次带完整无人值守参数的 resume，prompt 为“回复 OK 后结束”，断言 120 秒内退出码为 0，且过程中没有出现交互提示（stdin 设为 null，出现等待即判超时失败）；gpt 则投递一个测试事件到 chatx。每台设备在切换前都要跑一次，结果上报到 `mail_who` 的 `agent_runtimes`。

**M7 实现备注**（与正文的差异以此为准，后续里程碑可再调整）：
- 模块：网关侧 `agent_nexus/wake.py`（`WakeRouter`：入队判定、`wake_jobs`、派发、ack、`wake_report`、超时与重试）和 `agent_nexus/escalation.py`（§8.11）；设备侧 `agent_nexus/link/wake/`（`adapters.py` 命令模板与权限翻译表、`runner.py` 启动/监视/上报、`probe.py` 活性探测、`doctor.py`）。`wake_jobs` 是 `store.py` 的迁移 3，比 §8.4 多 `hop`、`permission`、`acked_at`、`reason`、`notified` 五列。
- 特性：设备声明 `session_wake/1`、`session_probe/1`；网关声明 `wake/1`，表示接受 `hub_call wake_report` 与 `presence_report`（设备只在看到 `wake/1` 时上报）。
- 入队判定：地址已有活跃 job 时**先合并**（即使收件人此刻 active）；claude/codex 的 job 在 `acked` 之后、收到 `wake_report` 之前仍占用该地址（新邮件合并进去，report 后按 `started_at` 之后的未读再建 job），这样长时间运行期间也不会出现第二个 `--resume` 进程。设备离线时为 `waiting_device`；设备在线但没有 `session_wake/1` 时当场记为 `unsupported`（`deliveries[].action = wake_unsupported`）。
- 没有进程可报告的适配器（companion、notify、`fallback_exec`）在 ack 时直接结束（`state=acked` 且 `finished_at` 非空）。ack 超时（`ack_timeout_s`）按退避回到 `queued`，同一个 job id 重派时设备若发现原进程仍在运行，只返回 `already_running`，不再启动第二个。`acked` 超过 `run_timeout_s` 仍无 report：能探测就先 `session_probe`，仍 live 则顺延，否则记 `done`（`reason` 说明）。
- 新增配置键：`mailbox.wake.rerun_debounce_s`（10，即 §8.7 “report 之后再唤醒”的 debounce）、`probe_timeout_s`（10）、`scan_interval_s`（2）；`max_wakes_per_hour`、`cooldown_s` 按“最近 1 小时 `wake_dispatched` 事件数”和“距上次唤醒的时间”实现。`retention.unread_days` 为数字时写入 `escalation.expire_after_days` 并给 warning。
- 设备侧配置：`agent-nexus.json` 的 `wake`（或 `cloud.wake`）：`executables.<kind>`（字符串或参数列表）、模板 `claude` / `claude_last` / `codex` / `codex_last`（没有 resume_id 时用 `_last`：claude `--continue`，codex `exec resume --last`）、`permission_flags.<kind>.<mode>`、`companion_endpoint`、`claude_home` / `codex_home`、`idle_after_s`、`probe_processes`。可执行文件是 `.cmd`/`.bat`（npm 的 `claude.cmd`）时，提示词压成一行并去掉 `%` 和 `"`，因为参数要经过 cmd.exe。workdir 在设备上不存在时拒绝唤醒（会话按目录存放，换目录 resume 找不到会话）。
- 进程：stdin 为 null，输出写 `.anx/wake/<job>.log`，状态写 `.anx/wake/<job>.json`；report 送达后删除 json（保留 log）。环境变量带 `ANX_WAKE_JOB`、`ANX_WAKE_ADDRESS`。
- companion：device 插件新增 `POST /anx/companion-event`（要求 `X-ANX: 1`），`project_id` 为 `auto` 或空时按 workdir 匹配 chatx 书签。chatx 没有收下事件时插件**不**用 `_retry_pending` 重试（attempts 记为 100），改由网关按退避重试，并对该设备只发一次“有发给 GPT 的邮件”提醒，避免同一封邮件被投递两次。
- hook：`anx hook claude|codex`（Claude 从 stdin 读 hook JSON，Codex `notify` 从最后一个参数读）→ 本机 `POST /api/agents/presence` → 记入 `session_probe` 的 hook 信号，并经 `hub_call presence_report` 按（设备，kind，cwd）更新地址的 resume_id（`resume_source=hook`）和 presence；同一目录有多个会话时只认已持有该 id 的那个，不猜；也匹配自动命名的地址，并带上会话标题（§8.2、§8.3）。`anx integrate claude|codex` 负责写入这些 hook（§8.3）。`--root` 指向一个不存在的目录时 hook 什么都不报（不会退回默认端口 8399，那里可能是另一套安装的 manager）。
- `anx wake doctor` 结果写到 `.anx/wake-doctor.json`，manager 把它并入 device 描述块的 `agent_runtimes.<kind>.wake_doctor`（`mail_who` 可见）；gpt 检查需要 `--endpoint` 或 `wake.companion_endpoint`（device 插件地址）。`anx mail address set <address> --wake-permission <mode>` 经本机控制台的 `/api/inbox/addresses/{address}`。
- 未读升级：同一次扫描里已逾期的多级会依次执行（先判过期，过期后不再做 1、2 级）；扫描在事件循环里同步执行（它会入队唤醒、发 toast）。
- 验收里“真机 `anx wake doctor` 对 claude、codex、gpt 各通过一次”以及 codex `full-access` 参数在 codex-cli 0.164.0 上的复核，需要 owner 在设备上实际运行，自动化测试只用假的 CLI 和假的 chatx。

### 8.8 防失控循环（可选开关，默认关闭）

`messages.hop` 等于父消息的 hop 加 1；被唤醒的会话在运行期间发出的邮件，继承所在 job 的最大 hop 加 1。开关 `limits.max_thread_hops`、`limits.max_wakes_per_hour`、`wake.cooldown_s` 默认都是 `null`。文档里说明：两个“收到就回复”的会话会互相唤醒、无限循环。`events` 表记录每次唤醒，控制台邮箱页显示“最近 1 小时唤醒次数”。

### 8.9 跨设备协作示例（验收场景）

1. **GPT 规划，Codex 在另一台机器上执行**：`gpt-main@pc-main` 调用 `mail_who`，看到 `codex-build@remote-1` 处于 idle 且可唤醒，于是发一封 `kind=task` 的邮件。网关在 remote-1 上执行 `codex exec resume …`。codex 读信、构建，然后用 `mail_send(reply_to=…, kind=result)` 回复。gpt-main 也处于 idle，于是走 companion 唤醒，本机 chatx 把 GPT 引导到 `mail_read`。
2. **同一台 PC 上的两个 Claude**：收件方正处于 active，不唤醒，提醒会出现在它下一次工具调用的结果末尾。
3. **发给 human**：`mail_send(to="me")` 进入收件箱，并在所有在线设备上弹出 toast；owner 在收件箱里回复，回复唤醒发件的会话。
4. **agent 提问**：`claude-unity@pc-main` 调用 `ask_user(question="用哪个分支发版？", options=["main","release/0.2"], recommended="release/0.2")`，然后结束本次运行。owner 在手机 PWA 或 toast 上点“release/0.2”，答案以 `kind=answer` 的邮件回到 `claude-unity@pc-main` 并唤醒它。

### 8.10 配置开关（`gateway.json` 的 `mailbox`；默认一律不加限制）

```json
"mailbox": {
  "enabled": true, "db": "/var/lib/agent-nexus/gateway.sqlite3",
  "notice": {"repeat": "every", "max_items": 3, "max_chars": 600, "structured": "auto", "meta": true,
             "fallback": "client_id", "fallback_window_s": 1800},
  "presence": {"idle_after_s": 120, "end_after_days": 30},
  "wake": {"enabled": true, "debounce_s": 5, "expire_s": 86400, "max_attempts": 20, "ack_timeout_s": 600,
           "run_timeout_s": 3600, "probe_live": true, "fallback_exec": false,
           "permission_default": "bypass",
           "adapters": {"claude": true, "codex": true, "companion": true, "notify": true, "push": false},
           "cooldown_s": null},
  "escalation": {"enabled": true, "rewake_after_s": 3600, "notify_owner_after_s": 86400,
                 "expire_after_days": 30, "notify_sender_on_expire": true, "scan_interval_s": 60},
  "retention": {"read_days": 90, "events_days": 30, "jobs_days": 14, "warn_db_mb": 1024},
  "inbox": {"toast": true, "toast_max_buttons": 3, "pwa": true, "ask_expire_days": 30},
  "relay": {"max_package_mb": 200, "chunk_kib": 1024},
  "invites": {"default_ttl_s": 86400, "max_ttl_s": 604800},
  "limits": {"max_body_bytes": 65536, "max_sends_per_minute": null, "max_wakes_per_hour": null,
             "max_thread_hops": null},
  "acl": {"mode": "open", "bind_to_oauth_client": false}
}
```

`--check-config` 只校验类型，遇到未知键给 warning。旧配置里的 `retention.unread_days` 视为已废弃：值为 `null` 时忽略，为数字时用作 `escalation.expire_after_days`，并给 warning。

### 8.11 未读升级、发件人状态与撤回（决策 D9）

`escalation.py` 在 WakeDispatcher 同一个 lifespan 里每 `scan_interval_s` 扫描一次 `deliveries(state='unread')`，按邮件的 `created_at` 计算未读时长（所有阈值都可配置，设为 `null` 关闭对应一级）：

| 级别 | 条件（默认） | 动作 | 记录 |
|---|---|---|---|
| 1 再唤醒 | 未读 ≥ `rewake_after_s`（1 小时），且 `escalation_level < 1` | 对收件人**再唤醒一次**：按 §8.7 入队（受唯一活跃 job 约束，已有活跃 job 时只合并），即使收件人当前是 active 也入队（相当于 `wake=always`）；收件人为 `wake_policy=never` 或 human 时跳过这一级 | `escalation_level=1`，`events` 记 `escalation_rewake` |
| 2 转告 owner | 未读 ≥ `notify_owner_after_s`（24 小时），且 `escalation_level < 2`，且收件人不是 `me` | 给 `me` 发一封 `kind=system` 的提醒：“发给 X 的邮件『subject』已 24 小时未读”，附原邮件链接，进收件箱并弹 toast。同一收件人在同一扫描周期内的多封合并成一封 | `escalation_level=2` |
| 3 过期归档 | 未读 ≥ `expire_after_days`（30 天） | 投递记为 `expired`（`expired_at`），取消相关 wake job；`notify_sender_on_expire` 时给发件人发一封 `kind=system` 的邮件“你发给 X 的『subject』已过期未读”（发件人是 anon/system 时不发） | `escalation_level=3` |

- 收件人读了（`mail_read`）或邮件被撤回，就不再升级。每一级只执行一次，网关重启后按 `escalation_level` 续上，不会重复。
- 发给 `me` 的邮件只走第 3 级（过期），不会再转告自己。
- `mail_status`（§8.5）给发件人看每个收件人的状态：`delivered`（已入库、未读）→ `woken`（因这封邮件派发过唤醒）→ `read`；终态 `expired` 或 `recalled`。收件箱的线程视图在每封自己发出的邮件下显示同样的状态。
- `mail_recall` 只撤回未读投递；被撤回的邮件从收件人的 `mail_inbox` 和未读提醒中消失，收件人那边不留正文（`mail_read` 返回 `recalled`）。

### 8.12 收件箱、`ask_user`、toast 与移动端 PWA（决策 D11）

**收件箱页**（本机控制台和桌面端是同一页；网关网页控制台也有同样的页面）：
- 左侧是线程列表（参与者、主题、未读数、最后一条的时间），右侧是线程视图。数据来自网关：本机控制台经 `hub_call mail_local`，网关网页版直接调网关 API（`/api/inbox/*`，要求 owner 已通过 OAuth 登录）。
- 默认视角是 `me`；可以切换为查看任一地址的邮箱（只读，用户已接受不设防，见 R5）。
- **以 `me` 身份回复**：等同于 `mail_send(from="me", reply_to=…)`，按 §8.7 唤醒收件人。也可以新建邮件发给任意地址或群组。
- 问题卡片（见下文）显示在线程里，并在收件箱顶部单独列出“待回答”。
- 地址设置：唤醒权限（§8.7）、`wake_policy`、手动纠正 resume_id。

**`ask_user`**：
1. 网关新建一行 `asks`，同时给 `me` 发一封 `kind=question` 的邮件（正文为 question 和 context），与提问者的地址同线程。
2. 问题卡片：**推荐项排第一**并标“推荐”，其后按原顺序列出其他选项，最后是自由填写的“其他”。没有 options 时只有自由填写框。
3. 作答（收件箱、PWA、toast、`anx ask answer <id>` 任一处）后：`asks.state=answered`；网关以 `me` 的身份给提问者回一封 `kind=answer` 的邮件（`reply_to` 为 question 邮件，正文为“已选择：…”或自由文本，`_meta` 带结构化答案），按 §8.7 唤醒提问者；`blocking=true` 且调用仍在等待时直接在返回值里交回答案（此时仍然发邮件，但不唤醒，因为提问者正处于 active）。
4. 同一问题只接受第一个答案，之后的作答返回“已由 <via> 回答”。`inbox.ask_expire_days` 后未答的 ask 记为 `expired`，并通知提问者。

**并入 device 插件的 `request_user_input`**：工具名、参数和返回格式都不变（快照锁住）。网关声明 `ask/1` 且设备 manager 已连接时，device 插件通过 manager 的 `hub_call ask_create {origin:"request_user_input", …}` 创建 ask，`request_user_input` 同步等待答案，`_async` 返回 ask id，`_result` 查询网关。答案同样出现在收件箱，同样可以从 toast 或手机作答。网关不可用时退回今天的本机弹窗，作答结果只在本地。

**Windows toast**（device 插件 `notify` 的扩展，设备声明 `toast_actions/1`）：
- 发给 `me` 的邮件和新的 ask，会对所有在线、具备 `notify` 能力的设备下发 `toast {title, body, link, actions?}`（反向 RPC）。
- 点击 toast 主体：打开本机控制台的收件箱并定位到该线程（URL `http://127.0.0.1:<port>/#/inbox/<thread_id>`，经单实例转发由桌面端打开；没有桌面端时用默认浏览器）。
- **简单问题直接在 toast 上回答**：options 不超过 `inbox.toast_max_buttons`（默认 3，含推荐项且推荐项在最前）时，每个选项是一个 toast 按钮，点击即作答（设备经 `hub_call ask_answer {ask_id, choice, via:"toast"}` 上报）。选项更多或需要自由填写时，toast 只显示“打开回答”。
- 任一处作答后，网关对其他设备下发 `toast_dismiss {tag}`，撤掉同一问题的 toast。
- 旧版设备（没有 `toast_actions/1`）退回为不带按钮的普通 `notify`。

**移动端网页收件箱（PWA）**：
- 网关在 `https://<hub-domain>/inbox/` 提供适合手机的收件箱（线程列表、线程视图、回复、问题卡片作答），静态资源在 `agent_nexus/gateway/web/`。
- 登录**复用网关现有的 OAuth 登录**（与 ChatGPT/Claude 连接器授权同一个授权服务器和 owner 口令）：网页走 authorization code + PKCE，拿到的会话保存在 HttpOnly、Secure、SameSite=Strict 的 cookie 里，后端按 owner 身份校验。不新增账户体系。
- 提供 `manifest.webmanifest` 和 service worker（只缓存静态外壳，不缓存邮件内容），可以“添加到主屏幕”。0.2 不做 Web Push，手机端靠打开 PWA 查看；以后可以加。
- **不做**企业微信或电子邮件桥接。

**M6 实现备注**（与正文的差异以此为准，后续里程碑可再调整）：
- 模块：`agent_nexus/mailbox.py`（库表逻辑，迁移 2 在 `store.py`）、`mail_tools.py`（工具）、`mail_notice.py`（`MailNoticeMiddleware`）、`inbox.py`（收件箱操作，`hub_call mail_local` 与 `/api/inbox/*` 共用）、`inbox_web.py`（网关 API 与 PWA）、`toast.py`（设备侧 toast 接口与测试替身）。网页静态资源与本机控制台共用 `agent_nexus/static/`（`inbox.js`、`pwa.html`、`manifest.webmanifest`、`sw.js`），而不是 `agent_nexus/gateway/web/`（`gateway.py` 是模块，不能同名建包）。
- 唤醒属于 M7：M6 中 `mail_send` 的 `deliveries[].action` 对 agent 收件人一律是 `noticed_on_next_call`，对 `me` 是 `inbox`；`wake` 参数已接受但不起作用；`mail_who` 的 `wakeable` 为 false。`wake_jobs` 表与未读升级（§8.11 的 1、2、3 级）随 M7 加入；撤回时取消 wake job 的步骤留有 TODO。
- 未读提醒对直出设备工具：代理结果带有原始 wire 形态（`_raw_mcp_result`），中间件追加提醒时清除它，否则修改不生效。未知工具（`NotFoundError`）同样返回 `Unknown tool: '<name>'` 的错误结果并附提醒。
- `ask_user` 必须带可识别的身份（workspace_id 或有状态会话），否则报错——答案要以邮件寄回。同一提问者的问题共用一个线程。device 插件的 `request_user_input` 经 manager 的 `/api/asks`（`hub_call ask_create/ask_get`）以地址 `device-ask@<agent_id>` 提问；`ANX_MANAGER_URL` 由 supervisor 注入；网关不可用时退回 chatx。
- toast：网关对声明 `toast_actions/1` 的设备发 `toast`/`toast_dismiss`，其余带 `notify` 工具的设备经 `tool_call notify` 发普通通知。设备侧 `DeviceNotifyToaster` 通过 device 插件的 `notify` 显示，按钮是本机控制台链接：打开线程 `/#/inbox/<thread>`，或带 HMAC 签名（密钥在 `<root>/.anx/toast-link.key`）的 `/toast/answer`，点击即经 `hub_call ask_answer {via:"toast"}` 作答。`notify` 尚不支持按 tag 撤销，`toast_dismiss` 在设备侧目前是空操作。
- PWA 登录：网关内置公共客户端 `anx-inbox`，唯一回调 `<public_url>/inbox/callback`（构建时加入 `auth.redirect_uris`），authorization code + PKCE 由网关自己完成；cookie `__Host-anx_inbox`（HttpOnly、Secure、SameSite=Strict）保存 access token，过期后重新登录（不做刷新）。只配置了静态 bearer 的网关显示令牌登录表单。cookie 认证的写请求还要求 `X-ANX: 1`。
- `mailbox.retention.interval_s`（默认 86400）是新增的配置键；`mailbox.enabled=false` 时不注册邮箱工具也不加中间件。`anx mail` / `anx ask answer` CLI 与托盘“收件箱（N）”未在 M6 实现。
- 收件人展开：组（`@all`、`@device:`、`@kind:`、`@team:`）不含发件人；显式写出的地址（裸名、`name@device`、`me`）即使是发件人自己也照常投递，§8.6 的“给自己发一封信”依赖这一点。
- `device-ask@<agent_id>` 没有读者（设备轮询 `ask_get`）：寄给它的回答邮件和过期通知在投递时即记为已读，随 `read_days` 清理；保留任务也会把该地址上遗留的未读投递改为已读。
- 每周 `VACUUM`：保留任务在 `PRAGMA wal_checkpoint(TRUNCATE)` 之后执行，距上次不足 7 天则跳过；上次时间记在 `events` 表（`type='vacuum'`），重启后仍有效。保留结果里的 `vacuumed` 为 1 表示本次执行了。

---

## 9. R6：桌面端（安装、更新、自启）

### 9.1 构建

- `scripts/build_desktop.py:66-69` 从 `cargo build` 改为 `cargo tauri build --bundles nsis`（CI 安装 `tauri-cli ^2`）。保留 `ANX_RUNTIME_ARCHIVE`（同时兼容 `MCPM_RUNTIME_ARCHIVE`）和 `CARGO_TARGET_DIR`。产物收集 `bundle/nsis/*-setup.exe`、`*.sig` 和原始 exe。原始 exe 上的内嵌检查（build_desktop.py:73-75）保持不变。`tests/test_build_pipeline.py:148-233` 和 `verify_client_artifacts.py` 的产品列表同步修改。
- `tauri.conf.json`：

```json
{
  "productName": "Agent Nexus", "mainBinaryName": "agent-nexus",
  "identifier": "io.github.muxigame.agentnexus",
  "bundle": {"active": true, "targets": ["nsis"], "createUpdaterArtifacts": true,
    "icon": ["icons/32x32.png", "icons/128x128.png", "icons/icon.ico"],
    "windows": {"nsis": {"installMode": "currentUser", "languages": ["SimpChinese", "English"],
                         "displayLanguageSelector": false, "installerHooks": "nsis/hooks.nsh",
                         "startMenuFolder": "Agent Nexus"}}},
  "plugins": {"updater": {"pubkey": "<minisign 公钥>",
    "endpoints": ["https://muxigame-prod-static-cn.oss-cn-hangzhou.aliyuncs.com/agent-nexus/latest.json"],
    "windows": {"installMode": "passive"}}}
}
```

  不写 `version`，让 v2 回退到 Cargo.toml 的版本。
- Cargo.toml 新增：`tauri-plugin-updater = "2"`、`tauri-plugin-process = "2"`、`tauri-plugin-autostart = "2"`、`tauri-plugin-deep-link = "2"`（`agentnexus://`，§11.5）。插件注册顺序：`single_instance` 必须第一个（bootstrap.rs:415），然后是 `deep_link`、`autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--autostart"]))`、`updater`、`process`。所有调用都在 Rust 侧完成，不给 `http://127.0.0.1` 这个远程源开放 capability，保持 desktop.py:5-6 的边界。

### 9.2 进程控制统一用“argv 动词 + 单实例转发”

控制台页面拿不到 Tauri API，pystray 托盘也调用不了 Tauri。所以第二个进程把参数转发给正在运行的实例的 `launch()`（bootstrap.rs:341、415）：

| 动词 | 行为 |
|---|---|
| `--autostart` | 登录自启时使用：只执行 `bootstrap()`，**跳过** `console_window::open(Target::Startup)`（bootstrap.rs:350）；如果单实例回调里再次收到该参数，什么都不做 |
| `--set-autostart=on\|off` | `app.autolaunch().enable()` 或 `disable()` |
| `--check-update` / `--apply-update` | 见 §9.4 |
| `--shutdown-manager [--timeout 20]` | 复用 `quit()` 的前半段（bootstrap.rs:363-377）。没有正在运行的实例时，就在当前进程里直接执行，供 NSIS hooks 和切换脚本使用。具体步骤见 §9.3 |
| `--root <path>` | 显式指定根目录，autostart 的 Run 值总会带上它 |

0.2 **保留 pystray 托盘**（A 的取舍）。B 提出“去掉 pystray、只用 Tauri 托盘”会改变 manager 的启动参数，而旧 bootstrap 拉起新 runtime 时会传 `--tray`（bootstrap.rs:209），所以推迟到 0.3 再评估。pystray 托盘新增两项：“开机自启”勾选项（状态只读取 Run 值；点击时执行 `agent-nexus.exe --set-autostart=on|off`），以及更新菜单项（见下）。控制台的设置页也提供同样的开关，由 manager 代为执行同一条命令。

**托盘图标与更新提示（决策 D7）**：
- 图标由 `tray.py` 合成：底图 + **绿点**（保持现有含义：已连接网关；断开时为灰点）位于右下角；有可用更新时，在**左上角**叠加一个**橙色向上箭头**角标。两个角标互不覆盖，可以同时出现。合成结果按 `(connected, update_available)` 四种组合缓存。
- 右键菜单：有已知更新时显示“更新到 vX.Y.Z”；没有时显示“检查更新”（点击执行 `agent-nexus.exe --check-update`，结果用托盘气泡提示“已是最新”或让角标出现）。
- 状态来源：manager 读取 `.anx/desktop-update.json`（§9.4），文件变化时刷新图标和菜单。
- 收件箱：托盘菜单另有“收件箱（N）”，N 为 `me` 的未读数加待回答的 ask 数，点击打开收件箱页（§8.12）。

### 9.3 干净停止（更新、卸载、切换都用同一个流程）

`--shutdown-manager` 以及 `scripts/cutover/stop-legacy.ps1` 共用以下步骤（评审 gap：停止顺序）：
1. `POST /api/manager/shutdown`（web.py:155-160），lifecycle 会写入 `requested:true`（lifecycle.py:111），bootstrap 的 watchdog 看到后不会再拉起 manager（bootstrap.rs:44-48、319）。
2. 等待 state 文件变为 `exited`，并等 manager pid 消失，最多 20 秒。supervisor 的 `shutdown()` 会停掉所有自己拥有的服务（supervisor.py:403-430）。
3. **核实残留**：服务是用 `CREATE_BREAKAWAY_FROM_JOB` 启动的（supervisor.py:236），manager 退出不会带走它们。所以要枚举 `ExecutablePath` 或 `CommandLine` 位于 `<root>` 下（`.venv`、`runtime/`、`plugins/`、`builtins/`、`tools/`）的进程，并检查 manager 端口和插件端口（默认 8399-8408，有偏移时按偏移后的端口）是否还被占用。
4. 有残留就 `taskkill /T /PID`，然后再检查一次。如果还有，以退出码 3 结束，并列出这些进程，交给调用方决定是否继续。
5. Linux 上用 `systemctl --user stop` 加 `pgrep -f <root>` 做同样的检查。

### 9.4 自动更新

- 启动 60 秒后检查一次，之后每 6 小时检查一次，只在 Rust 侧调用 `app.updater()?.check()`。
- 发现新版本时，把 `{version, notes}` 写入 `.anx/desktop-update.json`。manager 的 `/api/app/update` 读取它，控制台显示横幅，托盘出现橙色向上箭头角标，菜单显示“更新到 vX.Y.Z”（§9.2）。
- **只有用户点击才安装**（决策 D7）：点击托盘菜单或控制台横幅后执行 `agent-nexus.exe --apply-update`，转发给正在运行的实例。实例依次执行：§9.3 的干净停止 → `download_and_install`（会校验 minisign 签名）→ `app.restart()`。后台检查只下载 `latest.json`，不预先下载、不静默安装。
- 策略 `desktop.update.mode`：`notify`（默认）或 `off`。原来的 `auto` 取消；配置里写 `auto` 时按 `notify` 处理并给 warning。私有版构建固定为 `off`。
- 不做 Authenticode 代码签名（决策 D5）。minisign 签名只用于 updater 校验更新包的完整性，与代码签名无关，照常保留。
- 测试时可以用 `ANX_UPDATER_ENDPOINT` 覆盖更新地址，只有 debug 构建或带特定标记时才生效。

### 9.5 runtime 版本化解压（修复锁文件和旧文件残留）

`extract_runtime`（bootstrap.rs:152-169）原来是原地覆盖。改为：先解压到 `<root>/runtime/<version>-<hash8>.tmp/`，完成后 rename 为正式目录，再写入 `runtime/current`。manager 用 `runtime/<v>/.venv/python -m agent_nexus.cli --root <root> serve --tray` 启动。新版本第一次 ping 成功后，删除更早的版本，只保留上一个。根目录里只放 `agent-nexus.json`、`secrets/`、`plugins/`、`plugin-data/`、`.anx/`。`.venv` 跟着版本重建，有离线缓存时用 `uv sync --frozen --offline`。回滚 runtime 只需要改 `current`。

### 9.6 自启（默认开启，托盘可以切换，登录时隐藏启动）

- 首次运行时，如果 `.anx/desktop.json` 里没有 `autostart_initialized`，就执行 `enable()` 并写入这个标记。之后完全尊重用户的选择，升级也不会把用户关掉的自启重新打开。
- 写入的位置是 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Agent Nexus = "<exe>" --autostart --root <root>`，按用户写入，不需要管理员权限。
- **Linux**：`.deb` 安装 user unit `agent-nexus.service`（带 `[Install] WantedBy=default.target`）。postinst 不能代替用户执行 enable，所以首次运行 `agent-nexus` 时执行 `systemctl --user enable agent-nexus`；桌面会话另外写入 `~/.config/autostart/agent-nexus.desktop`。服务器场景提示执行 `loginctl enable-linger`。
- **macOS**：每用户的 LaunchAgent `~/Library/LaunchAgents/io.github.muxigame.agentnexus.plist`（`<app>/Contents/MacOS/agent-nexus --autostart --root <root>`，`RunAtLoad`），由 `autostart.rs` 写入和删除，只在下次登录时生效（不 `launchctl load`）；首次运行同样默认开启。数据根是 `~/Library/Application Support/AgentNexus`。见 §11.6。
- **私有版**：updater 固定 `off`，自启行为与公开版相同。私有版保留为只在本机构建的目标（决策 D3，§11.4）；分发给新设备时优先用公开安装包加邀请码（§11.5）。
- **`agentnexus://` 链接**：注册 `tauri-plugin-deep-link`（Windows 写 HKCU 的 URL 协议，不需要管理员权限），协议 `agentnexus://enroll?invite=<token>` 交给单实例的 `launch()` 处理，见 §11.5。

### 9.7 NSIS hooks（`nsis/hooks.nsh`）

- `NSIS_HOOK_PREINSTALL` 和 `NSIS_HOOK_PREUNINSTALL`：如果 `$INSTDIR\agent-nexus.exe` 存在，就执行 `ExecWait '"$INSTDIR\agent-nexus.exe" --shutdown-manager --timeout 20'`，即 §9.3 的流程。
- `NSIS_HOOK_POSTINSTALL`：执行 `nsExec::ExecToLog '"$INSTDIR\agent-nexus.exe" --prepare-runtime'`，在安装器里完成原本首次启动才做的事——把内嵌的运行包释放到 `runtime/<版本>-<hash8>/`（§9.5），再用随包的 Python 和 uv 缓存离线 `uv sync --frozen` 出管理器和每个以 `uv run` 启动的内置插件的环境，最后写 `.anx-env-ready`。进度显示在安装器的详细信息里；失败不中断安装，首次启动照旧补全。更新走同一个安装器，所以更新后打开也不用等。
- `NSIS_HOOK_POSTUNINSTALL`：删除 Run 值。
- 卸载时**不删除**数据根，提供一个“同时删除数据”的复选框，默认不勾选；命令行可以用 `anx purge`，需要确认。

---

## 10. 版本管理

- **唯一版本源**：`agent_nexus/__init__.py` 中的 `__version__`。
  - pyproject 用 `dynamic = ["version"]` 加 `[tool.hatch.version] path = "agent_nexus/__init__.py"`。
  - `scripts/version.py set X.Y.Z` 改写 `__init__.py`、`desktop/src-tauri/Cargo.toml` 的 `[package].version`（同时更新 Cargo.lock），以及 `plugins/device/mcp-service.json` 的 version。
  - tauri.conf.json 不写 version。
  - `scripts/build_ubuntu.py:9` 原来的 `VERSION` 改为从 `__init__.py` 读取。
  - `scripts/version.py check` 在 CI 里校验以上各处一致；release 时还要校验 tag `vX.Y.Z` 与源码版本一致。
- 第一个版本是 **0.2.0**，大于现有的 0.1.0（Cargo、Tauri、Python）和 0.1.4（deb），updater 和 dpkg 的版本比较都能单调递增。
- 独立的版本线：mcpkit 用 `mcpkit-vX.Y.Z`；各插件在自己的仓库打 tag；契约有 `anx.device/1`、manifest v2、link-protocol v1（只追加 features；protocol 字段固定为 1，直到某个破坏性改动所有节点都已升级）；`gateway.sqlite3` 的 `schema_version` 为整数。
- 服务端 zip 改名为 `agent-nexus-gateway-<ver>.zip`，内含 `SHA256.json` 和 `deploy/gateway/upgrade.py`，这样 upgrade 脚本就不再只存在于仓库里。云端 release 目录从 `releases/<sha16>` 改为 `releases/<ver>-<sha8>`。`/healthz` 返回 `{"status","service":"agent-nexus-gateway","version","features","db_schema"}`。

---

## 11. R7：公开仓库、CI 与发布

### 11.1 公开前的清理

1. **公开仓库用全新的孤儿历史**（M13 修订：只有第一次是孤儿提交；之后 `make_public_branch.py` 每次刷新都以现有 `public` 末端为父提交，`public` 只前进不重写，推送无需 force，旧 `v*` tag 仍在其历史里）。基线 f343399 注明了 “local only, not for publishing”，`main` 和 `legacy` 分支都**永不推送**。从清理后的工作树执行 `git checkout --orphan public && git commit`。本地 `.git/config` 设置 `remote.origin.push = refs/heads/public:refs/heads/main`，再加一个 pre-push hook，拒绝推送 `public` 以外的分支（嫁接自 B）。
2. **清理清单**（已用 grep 核实，命中以下文件）：
   - 文档和配置：`README.md`（root SSH 和 IP，第 69 行附近）、`builtins/codex-mcp/README.md`、`config/server.workspace.example.json`。
   - 代码：`mcpm/enrollment.py:28-39`（写死的域名，改为构建期注入，见 §11.4）、`scripts/build_release.py:36-38`。
   - 脚本：§2 列出的那批迁到 ops 仓库；`probe_cloud_release.py`、`probe_cloud_concurrency.py`、`probe_ubuntu_cloud.py`、`verify_public_client.py` 改为参数化（`--url`），不再写死默认值。
   - **测试夹具**：`tests/test_build_pipeline.py`、`tests/test_cloud.py`、`tests/test_enrollment.py`、`tests/test_verify_client_artifacts.py` 中的域名统一换成 `hub.example.test`，enrollment 的白名单改为从测试注入。
   - `deploy_cloud_host.sh:5-6` 写死的哈希改为参数；`upgrade_cloud_release.py:59-63` 删除 e636183 特例，`assert` 改为显式的 `if/raise`；nginx 只保留通用的反向代理示例，不提同机其他服务和 443 分流。
3. **防护措施**：
   - `scripts/scrub_check.py`：规则包括 IPv4（放行 127.0.0.1、0.0.0.0 和文档示例网段）、`root@`、私有域名和用户目录名、`agent_prefix`、私有 enrollment 文件名、单文件超过 5 MB。**规则正文放在 CI secret `SCRUB_PATTERNS` 里，仓库里只放通用规则**，这样仓库中不会出现明文域名。例外写在 `.scrub-allow`，每条必须带理由。
   - 另外运行 gitleaks，并配一个 pre-commit hook。
   - 首次推送前人工检查 `git ls-files` 和 `git status --ignored`。按 CLAUDE.md 的要求，先看 `git status` 再 `git add`，**禁止 `git add -A`**。
4. **LICENSE**：核心仓库用 **Apache-2.0**（决策 D1，M0 已加）。`THIRD_PARTY_NOTICES.md` 继承 codex-mcp 的条目，并注明 OmniParser 和 ultralytics 是按需下载的 AGPL 依赖，不随包分发。

### 11.2 工作流

| workflow | 触发条件 | 内容 |
|---|---|---|
| `ci.yml` | push / PR | ① `hygiene`（ubuntu）：`version.py check`、`scrub_check`、gitleaks、`schemas/` 与 mcpkit 模型一致性检查、所有示例清单通过 schema、`private-guard`（工作区出现 `secrets/`、`.private/`、`build-enrollment.json` 就失败）② `python`（windows-latest 与 ubuntu-latest）：`uv sync --frozen && uv run pytest`，Linux 上包一层 `xvfb-run -a`，包含 `tests/compat` ③ `sdk`：两个平台跑 mcpkit 的 pytest，再对模板执行 `plugin new` 和 `smoke` ④ `device`（windows 和 ubuntu）：`plugins/device` 的 pytest，加上 conformance，Windows 专属工具只在 windows 上跑 ⑤ `console`：`node --test tests/test_console_ui.cjs`（包含 schema 表单的用例）⑥ `desktop-check`（windows）：用 stub runtime zip 设置 `ANX_RUNTIME_ARCHIVE`，跑 `cargo check` 和 `cargo test`（bootstrap.rs:427-480 的单元测试） |
| `release.yml` | tag `v*` | 校验 tag 与版本一致 → windows：`build_release.py runtime`（带 uv 缓存）→ `cargo tauri build --bundles nsis`（使用 secrets `TAURI_SIGNING_PRIVATE_KEY` 和 `_PASSWORD`）→ macos（矩阵：`macos-15` 构建 Apple 芯片 `aarch64`，`macos-15-intel` 构建 Intel `x64`；`build_p2p_tools.py --rid osx-arm64|osx-x64` 从固定版本源码编出 MsQuic 和 OpenSSL 3.5，`cargo tauri build --bundles app,dmg`，产物 `agent-nexus_<v>_<arch>.dmg` 与带签名的更新包 `.app.tar.gz` + `.sig`；有 `APPLE_*` secrets 时做 Developer ID 签名和公证，没有时 ad-hoc 签名并给 warning）→ android（`build_p2p_tools.py --rid linux-bionic-arm64`、`build_android.py wheels`、`build_android.py apk`，用发布密钥库签名得到 `agent-nexus_<v>_arm64.apk`；没有 `ANDROID_KEYSTORE_BASE64` 时整个 job 只给 warning、什么都不构建，发布照常进行；有密钥库却没有 `ANDROID_KEYSTORE_PASSWORD` 时在第一步就以 `::error::` 失败，不等到构建完才在 Gradle 签名时失败）→ feed：等 windows 和 macos 都完成后，`make_latest_json.py` 生成唯一的 `latest.json`（`windows-x86_64`、`darwin-aarch64`、`darwin-x86_64` 三个平台）。macOS 流水线还没在 CI 上跑过，暂时是“尽力而为”：矩阵不 fail-fast，仓库变量 `ANX_RELEASE_MACOS=false` 可整体跳过；某个架构失败或被跳过时，feed 只写已有的 macOS 更新包并给 warning，`publish-oss`、`draft-release`、`publish-release` 只要求 windows、linux、android 和 feed 成功，Mac 那一项本次缺席而不拖住其他平台→ ubuntu：`build_release.py server`、`build_ubuntu.py`（只构建公开版）→ `test_no_private_payload.py`：解开所有产物，断言不含 `.private/`、`enrollment.json`、`agent_prefix` → 生成 `SHA256SUMS` → `publish-oss`：经 GitHub OIDC 换取阿里云 STS（`aliyun/configure-aliyun-credentials-action`，仓库变量 `ALIYUN_ROLE_ARN`、`ALIYUN_OIDC_PROVIDER_ARN`，不存长期 AccessKey），`publish_oss.py` 把全部产物传到 `agent-nexus/releases/<版本>/`，逐个经公网地址核对大小，全部通过后才替换 `agent-nexus/latest.json`（预发布 tag 不替换）；同时照旧建 GitHub draft release 作为存档。updater 只读 OSS 上的 `latest.json`（`make_latest_json.py --base-url` 让其中的安装包地址也指向 OSS）。0.2.0 及更早的客户端读的是 GitHub 的 `releases/latest`，所以第一个改用 OSS 的版本还要在 GitHub 上 publish 一次，老客户端才能升级过来 |
| `deploy-gateway.yml` | workflow_dispatch，或 release 发布后手动触发；使用 environment `production`，需要 reviewer 批准 | secrets：`DEPLOY_HOST`、`DEPLOY_USER`（专用用户 `anx-deploy`：authorized_keys 设 `restrict`，sudoers 只允许执行 `/opt/agent-nexus/bin/anx-upgrade`）、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`，可选 `GATEWAY_PROBE_TOKEN`。步骤：scp 服务端 zip 到 `/opt/agent-nexus/incoming/` → `ssh … sudo anx-upgrade --package … --sha256 …`（沿用现有逻辑：备份、校验、`uv sync`、`--check-config`、切换 symlink、重启、轮询 healthz，失败自动回滚）→ 在 runner 上探测 `/healthz` 中的 version 和 OAuth metadata。**不**使用 `verify_cloud_host.py` 作为门禁，因为它要求恰好 6 个 MCP 且 PC 在线 |
| `plugin-ci.yml` | `workflow_call`（可复用） | 输入：`os`（默认 `["windows-latest"]`）、`python`、`node`。步骤：checkout → setup-uv → `uvx --from "git+https://github.com/muxigame/agent-nexus@mcpkit-v0.2.0#subdirectory=sdk/mcpkit" mcpkit plugin validate .` → `uv run pytest` → `mcpkit plugin smoke .` → gitleaks。tag 触发时执行 `anx plugin pack`，把 zip 和 sha256 发布到插件仓库自己的私有 Release |

### 11.3 插件仓库的最小 CI

```yaml
# anx-plugin-<x>/.github/workflows/ci.yml
on: [push, pull_request]
jobs:
  ci:
    uses: muxigame/agent-nexus/.github/workflows/plugin-ci.yml@v0
    with: {os: '["windows-latest"]', python: "3.12"}
```

wecom、unity 等插件只在 windows 上跑；ones、jubian-go、payment 在两个平台上跑。

### 11.4 私有入网版：只在本机构建，永不进入公开 CI 和 Release（决策 D3）

- **保留**私有入网版，作为只在本机运行的构建目标（`scripts/build_private_desktop.ps1`，放在 `muxigame/agent-nexus-ops`）。公开仓库的 `ci.yml`、`release.yml` 里没有任何私有版 job，`private-guard` 和 `test_no_private_payload.py` 保证公开产物里不含私有载荷。
- `enrollment.py` 不再写死域名。入网地址来自本机的入网 profile（`.private/enrollment.json`），构建时写入 runtime 的 `.private/endpoint-allow`，运行时只接受其中列出的地址。公开代码里不含 token，也不含域名。
- `build_private_desktop.ps1 -Enrollment <vault>/repo/secrets/build-enrollment.json` 只在本机运行，输出到仓库外的目录。overlay 设置 identifier 为 `io.github.muxigame.agentnexus.private`、productName 为 `Agent Nexus Private`，updater 为 `off`。
- 公开路径 `anx enroll --file <profile.json>` 保留（公开安装包加入网文件）。给新设备分发时，**首选**下面的邀请码。

### 11.5 邀请码入网与每设备凭据（决策 D3，新增）

**生成邀请**（只有 owner）：网关网页控制台或本机控制台的“设备 → 邀请新设备”（本机控制台经 `hub_call device_admin {op:"invite_create"}`），CLI `anx-gateway invite create [--ttl 24h] [--label …] [--agent-id …]`。
- 邀请是**一次性**的，默认 24 小时过期（`invites.default_ttl_s`，上限 `max_ttl_s` 7 天）。网关只保存 secret 的 sha256（`invites` 表），明文只在创建时显示一次。
- 展示两种形式，内容等价：
  - 邀请码：`anx1.<base64url(json{"hub":"<public_url>","id":"<invite_id>","secret":"<32 字节随机>"})>`，自带网关地址，复制粘贴即可；
  - 链接：`agentnexus://enroll?invite=<同一个邀请码>`，另附二维码。
- 控制台列出未使用的邀请，可以随时作废。

**使用邀请**（公开安装包，首次启动）：
1. 根目录里没有 `cloud` 段、也没有私有 profile 时，首次启动的控制台显示“加入你的 Agent Nexus”页：粘贴邀请码（或点击 `agentnexus://` 链接自动填入），也可以选择“仅本机使用”跳过。
2. 设备向邀请里的 `hub` 发 `POST /enroll {invite_id, secret, hostname, device:{platform, os, arch, form}, agent_id?}`（HTTPS）。网关校验 secret 摘要、未过期、未使用、未作废，然后在同一个事务里把邀请标记为已使用。
3. agent_id：邀请里指定了就用它；否则用主机名转成的 slug，重名时加 `-2`、`-3`。不能用保留名（`hub`、`me`、`all` 等），也不能和 `agents` 映射里已有的 id 冲突。
4. 网关生成**每设备凭据**（32 字节随机 token），只存 sha256 到 `device_tokens`，明文只在响应里返回一次；`devices.enrolled_via='invite'`，`hostname` 记为设备自报值（决策 D6）。
5. 设备把 token 写入 `secrets/cloud/device-token`（`protect()`），在 `agent-nexus.json` 写 `cloud: {enabled:true, url, agent_id, token_secret:"cloud/device-token"}`，然后正常连接 `/ingress/{agent_id}`。
6. 失败时显示原因（过期、已使用、网关不可达），不重试同一个邀请。

**ingress 鉴权**：先按今天的 `agents` 映射（agent_id → 环境变量里的 token 摘要，cloud.py 的 ingress）校验；不在映射里的 agent_id 再查 `device_tokens`（未吊销）。两套并存，已有设备不需要重新入网。

**吊销**：设备列表里每台设备有“吊销”按钮（网关网页控制台和本机控制台都有；CLI `anx-gateway device revoke <agent_id>`）。吊销后：`device_tokens.revoked_at` 置位；网关立即关闭该设备的 ingress 连接（WebSocket close code 4401，reason `revoked`），之后的 hello 一律拒绝；设备端看到 4401 后停止重连，控制台显示“已被吊销，可用新邀请重新加入”。该设备上的地址标记为 ended，发往它们的未读邮件按 §8.11 走升级流程；固定到它的连接器显示为 `host_revoked`。按 `agents` 映射入网的旧设备，吊销按钮提示“需要从 gateway.env 删除该 token”（不在 0.2 自动改写 env 文件）。

**设备列表**（网页控制台和本机控制台共用）：显示名（可编辑备注名）、主机名、agent_id、在线状态、版本、入网方式、最后在线时间，以及“安装到此设备”（§6.11）和“吊销”两个操作。

**实现说明（M16，2026-11-10）**：
- 代码：`agent_nexus/invites.py`（编码、`POST /enroll`、ingress 校验、吊销）、`store.py` 迁移 4（`invites`、`device_tokens`，`devices` 增加 `version`、`revoked_at`）、`console.py`（控制台 API）、`gateway_admin.py`（`anx-gateway invite|device`）、`join.py`（设备侧）。网关新增特性 `invites/1`。
- ingress 顺序：`agents` 映射 → `device_tokens` → `agent_groups` 前缀。用**已吊销**的 token 连接时网关先 accept 再以 4401 `revoked` 关闭（直接拒绝握手只会得到 HTTP 403，设备分不清是吊销）；错误 token 仍是 1008/403。设备端（`agent.py`）收到 4401 后状态为 `revoked` 并停止重连。
- CLI 吊销直接改数据库；运行中的网关每 3 秒扫描一次，已无有效 token 的邀请设备会被以 4401 断开。
- 被吊销设备的 agent_id 可以被同一台机器用新邀请重新使用（不再加 `-2`）；仍有效的 agent_id、`agents` 映射里的 id、保留名都不能用。`agents` 映射现在可以为空（只用邀请码的网关）。
- `POST /enroll` 也接受整串邀请码 `{"invite": "anx1.…"}`；错误码：400 `invalid_invite`（格式或 secret 不对，未知 id 与错误 secret 不区分），410 `expired|used|revoked`，409 `agent_id_taken`，400 `reserved_agent_id`。
- 设备端写入 `cloud: {enabled, url, agent_id, token_secret: "cloud/device-token"}`，保留原 `cloud` 段的 `workspaces`、`mcps` 等键，去掉 `token_env`；`anx leave` / “退出网关”删除 token 并把 `enabled` 置为 false。

---

### 11.6 macOS 与 Android 客户端

| 平台 | 安装包 | 构建 | 自动更新 | 签名与缺省行为 |
|---|---|---|---|---|
| macOS Apple 芯片 / Intel | `agent-nexus_<v>_aarch64.dmg`、`agent-nexus_<v>_x64.dmg`（下载页按芯片分两项） | `release.yml` 的 `macos` 矩阵，每个架构一台对应的 runner（runtime 里有原生 CPython 和包）；`build_release.py` 在 zip 里保留 Unix 权限和符号链接，`bootstrap.rs` 解压时还原 | 与 Windows 同一套 Tauri updater：`latest.json` 的 `darwin-aarch64` / `darwin-x86_64` 指向 `.app.tar.gz`，`minisign` 签名（`TAURI_SIGNING_PRIVATE_KEY`）；Tauri v2 按 `<os>-<arch>` 选平台（先找 `darwin-aarch64-app`，再找 `darwin-aarch64`）。`auto_update.py` 照常调用 `agent-nexus --check-update` / `--apply-update`（`ANX_DESKTOP_APP` 指向 app 里的可执行文件），更新替换整个 `.app` 后重启 | 没有 `APPLE_*` secrets 时只有 ad-hoc 签名：首次打开要在“系统设置 → 隐私与安全性”点“仍要打开”；更新包由 updater 自己的签名校验，不受影响。`TAURI_SIGNING_PRIVATE_KEY` 缺失时与 Windows 一样让发布失败 |
| Android 10+（arm64） | `agent-nexus_<v>_arm64.apk` | `release.yml` 的 `android` job（ubuntu）：CPython 官方 Android 版、cibuildwheel 交叉编译的依赖、`linux-bionic-arm64` 的 P2P 工具（`libmuxip2p.so`、`libmsquic.so`） | 核心的 `linux_update.py`（Android 分支）从网关 `/api/downloads` 找最新 `.apk`，下载到 `<root>/.anx/updates` 并核对 SHA-256，写 `.anx/android-install.json`；应用（`Updates.java`）确认 APK 是本应用且 versionCode 更高后发通知、控制台打开时弹窗，经 FileProvider 交给系统安装程序（`REQUEST_INSTALL_PACKAGES`，首次需在“安装未知应用”里允许）。Android 一定要用户确认，所以 `auto` 模式在 Android 上是“自动下载、提示安装”。通知由 `CoreService` 在等待核心进程的循环里每 3 秒检查一次（`process.waitFor(3, SECONDS)`，不是一次阻塞到核心退出）。弹窗每个版本只问一次：点“稍后”后在应用运行期间不再主动弹出，从“安装未知应用”设置页或安装程序返回时才再问；设置页的“立即更新”写入新的 `asked_at`，应用见到后再问一次。自动轮次不重写已有的同一请求。流量计费网络（`ConnectivityManager.isActiveNetworkMetered`，应用写到 `.anx/android-network.json`）上自动轮次不下载 APK（体积超过 100 MB），等连上不计费网络再下；“立即更新”照常下载，已下载的包照常提示。装好后 `MY_PACKAGE_REPLACED` 重新启动核心 | 必须一直用同一个发布密钥库签名（secrets `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`，可选 `ANDROID_KEY_ALIAS`（默认 `agent-nexus`）、`ANDROID_KEY_PASSWORD`（默认同密钥库密码）），否则系统拒绝覆盖安装；没有密钥库时不发布 APK，有密钥库没有密码时发布失败 |

- 设备名：Android 上 `platform.node()` 总是 `localhost`，应用把“设置 → 关于手机”里的设备名（没有时用厂商 + 型号）传给核心（`ANX_ANDROID_DEVICE_NAME`），`join.local_hostname()`、`device_info` 的 `hostname` 和插件不在时的设备块都用它；`device_info.form` 为 `mobile`。
- 发布检查：`test_no_private_payload.py` 像打开 zip 一样打开 `.apk`（包括其中的 `assets/runtime.zip`），像 `.exe` 一样逐字节检查 Mach-O 可执行文件（macOS job 把 `artifacts/agent-nexus` 也传进去，`.app.tar.gz` 里的同一个文件也会被查到）；`.dmg` 只按文件名检查，它装的就是 `.app.tar.gz` 里的那个 app。
- 下载页：`downloads.json` 和网关列表都不含更新包 `.app.tar.gz`；访问者是 Android 时主推 `.apk`，是 Mac 时主推 Apple 芯片版 `.dmg`，否则主推 Windows。

## 12. R8：线上切换方案（顺序与回滚）

### 12.1 门（gate）

| 门 | 内容 | 什么时候必须通过 |
|---|---|---|
| G0 | 兼容矩阵（§3.3）全部通过，包括“新 agent → legacy_mcpm 网关”的录制测试 | 任何阶段开始之前 |
| G1 | 本地全栈演练：`smoke_local_cloud.py` 的扩展版，在本机同时跑新网关、legacy 网关、旧 agent 和新 agent，覆盖全部组合，以及邮件、唤醒（用假的 claude/codex）和插件安装 | 阶段 2 之前 |
| **G2** | **网关回滚的端到端演练**：在云主机上用**现有的 legacy release**（`/opt/mcpmanager/current`）配一份测试配置，在 `127.0.0.1:8442` 起一个临时实例。试运行的新 manager（`pc-next`）通过 SSH 隧道连上去，跑一遍 §3.3 的“新 agent → 旧网关”断言（真实环境版） | 阶段 3（网关正式切换）之前；阶段 4、5 每升级一台设备之前，都要确认 G2 的结论仍然适用于该设备的版本 |
| G3 | 每台设备都跑过 `anx wake doctor` | 该设备开启唤醒之前 |
| G4 | 每个阶段的回滚步骤都实际演练过一次（网关回滚在 8441/8442 上演练） | 阶段 3 之前 |

### 12.2 阶段 0：冻结、备份、预检

1. 保险库自检：对照 README 清单，逐个比较 38 个副本与原件的 sha256（`anx secrets restore --vault … --verify-against <root>`，只读）。
2. **离开云主机的备份**（嫁接自 C）：在云主机上打包 `/etc/mcpmanager`、`/opt/mcpmanager/current`，以及 `/var/lib/mcpmanager`（`oauth.sqlite3` 用 `sqlite3 .backup` 导出），scp 到本机 `WorkSpace/mcp/backups/<date>/`。这个目录和保险库一样，不进任何仓库。
3. 盘点现状：通过 `workspace_list` 和 `mcplist` 记录三台设备的平台（**remote-2 是 Windows 还是 Ubuntu，在这一步确认**）、根目录、启动方式、运行中的插件和 `mcp-manager.json` 的 sha256；记录云端 release 的 sha；用 ChatGPT 和 Claude 各跑一次 `set_workspace → exec_command`，作为基线。
4. **并发 agent 预检**（`scripts/cutover/preflight.ps1`，评审 gap）：
   - 列出命令行或 cwd 位于 `WorkSpace/mcp` 下的 claude、codex、python 进程（不包括 manager 自己和它管理的服务）。
   - 检查两个 worktree 的 `git status`。
   - 检查 `WorkSpace/mcp` 下 10 分钟内被修改过的文件（排除 logs 和 sqlite）。
   - 在协作信箱 `WorkSpace/home/mail` 里留言，说明切换窗口。
   - 任何一项异常就中止，并列出明细，交给用户确认。
   - 阶段 2、4 开始前各再跑一次。

### 12.3 阶段 1：本地演练（G0、G1）

全部在本机完成，不影响线上。

### 12.4 阶段 2：并行试运行（嫁接自 C，但不改 nginx）

1. **网关试运行**：在云主机上把新 release 安装到 `/opt/agent-nexus`，unit 名为 `agent-nexus-gateway-trial`，监听 `127.0.0.1:8441`。配置由 `anx-gateway migrate` 生成，状态目录是 `/var/lib/agent-nexus-trial`，**使用 oauth.sqlite3 的副本**。`agents` 里只额外加一个 `pc-next`，它的 token 只写进试运行实例的 env。**不改 nginx。**
2. **本机试运行 manager**：
   - 安装 NSIS 版 Agent Nexus。因为 identifier 不同，它和旧的便携 exe 不冲突。
   - 执行 `anx migrate --from WorkSpace/mcp --to %LOCALAPPDATA%\AgentNexus-trial --port-offset 100 --agent-id pc-next --cloud-url ws://127.0.0.1:18441 --apply`。根目录**独立**，和 legacy 共用的只有 link 插件的代码目录，manager 端口为 8499，插件端口为 8500 起。
   - 建 SSH 隧道 `ssh -N -L 18441:127.0.0.1:8441 -L 18442:127.0.0.1:8442 <hub-host>`。
   - 插件：device 用新版；ones、jubian-go、payment、chrome-devtools、unity 用 link 模式，在偏移后的端口上运行。
   - **wechat 在试运行 manager 上禁用**，因为 UI 自动化发送是单实例的，不能让两个 manager 同时操作企业微信。
3. **验证**：
   - 在本机用 bearer 客户端（例如 Claude Code 临时连到 `http://127.0.0.1:18441/mcp`）验证：旧的 `set_workspace` 调用、`use_device`、device 工具、各插件的 `tooluse`、邮件往返、未读提醒（含错误路径）、claude 和 codex 唤醒、companion 唤醒、离线排队（停掉试运行 manager 后发信，再启动）。
   - 执行 G2：同一个试运行 manager 改连隧道 18442（legacy 临时实例），跑“新 agent → 旧网关”的断言。
4. **回滚**：停掉两个临时实例，卸载试运行版（数据根保留）。线上始终没有受到影响。

### 12.5 阶段 3：网关正式切换（窗口约 5 分钟）

> 实现：`scripts/cutover/gateway-cutover.sh`（含失败自动回滚）与 `gateway-rollback.sh`，用法见 `scripts/cutover/README.md`。2026-11-10 已在云主机上用 8441（试运行 unit 与 legacy release 的配置副本轮流扮演新旧网关）演练过切换、失败自动回滚与手动回滚（门 G4）。`anx-gateway migrate` 同时复制 `<config>/secrets`（OAuth 密码与 bearer）。

1. `systemctl stop mcpmanager-cloud`。从最新的 `/var/lib/mcpmanager/oauth.sqlite3` 再用 `.backup` 复制一份到 `/var/lib/agent-nexus/`（试运行期间旧网关上可能有新的 client 注册），media 缓存也一并复制。
2. 启用正式 unit `agent-nexus-gateway`，监听 `127.0.0.1:8440`，`public_url` 与原来相同：`systemctl disable mcpmanager-cloud && systemctl enable --now agent-nexus-gateway`。**nginx 不需要改动**，因为端口和路径都没变。停掉 trial unit。
3. 三台旧版设备（protocol 1，没有 features）会自动重连，URL 和 token 都不变。
4. 验证：`/healthz` 返回新版本；ChatGPT 和 Claude 的现有连接器**不需要重新授权**就能执行 `workspace_list → set_workspace → exec_command`；`device_list` 能看到三台设备；`mcplist` 中 MCP 齐全；`/media` 可用；邮件互发和未读提醒正常（这时设备还是旧版，唤醒会显示 `unsupported`，属于预期）。
5. **回滚**：`systemctl stop agent-nexus-gateway`，把 `/var/lib/agent-nexus/oauth.sqlite3` 用 `.backup` 拷回 `/var/lib/mcpmanager/`，以保留切换期间新签发的 token（两边是同一种 mcpkit schema），然后 `systemctl enable --now mcpmanager-cloud`。设备会自动重连。代价是邮箱数据在旧版里看不到；新库保留，再次切换时可以继续使用。
6. 观察 48 小时。

### 12.6 阶段 4：本机 pc-main（就地迁移）

1. 跑一遍预检，然后执行 `scripts/cutover/stop-legacy.ps1`，即 §9.3 的完整停止流程：requested 关闭 → 核实端口 8399-8408 已释放、没有残留的 breakaway 进程 → 必要时 taskkill。同时卸载阶段 2 的试运行版。
2. `anx migrate --in-place --from WorkSpace/mcp --apply`：在同一个根目录里生成 `agent-nexus.json`，`mcp-manager.json` 不动；业务插件用 link 模式指向 `mcp-manager/builtins/<x>`；device 用新 runtime 里的版本；复制 `watchers.sqlite3`；jubian 的明文密码转存到 secrets。
3. 启动 Agent Nexus。它会以 `pc-main` 的身份连接正式网关，wechat 也启用（空配置）。
4. 验证：所有插件都是 running；`device_list` 中这台设备的 features 齐全；企业微信 `get_messages`，并给文件传输助手发一条消息；G3：claude、codex、gpt 唤醒各做一次端到端测试；注销后重新登录，只出现托盘、不弹窗口；`/api/ping` 返回两个键。
5. 之后逐个把 link 插件换成从私有仓库 `anx plugin install`，再用 `anx secrets restore` 把 payment 的配置和二维码放回去，每换一个验证一个。
6. **回滚**：执行 `agent-nexus.exe --shutdown-manager`（§9.3），再双击 `mcpm-desktop.exe.lnk`。旧根和旧配置都没有动过。注意：新版的自启 Run 值要用 `--set-autostart=off` 关掉，否则下次登录时新旧两个版本会同时启动。

### 12.7 阶段 5：remote-1、remote-2（远程，一次一台，间隔 24 小时）

1. 前提：G2 结论适用于目标版本；这台设备的 Phase 0 盘点已经完成。私有插件在切换后由控制台设备列表的“安装到此设备”从 pc-main 中转安装（决策 D4，§6.11），远程设备不需要 GitHub 凭据；中转之前，旧插件目录照旧以 link 模式运行。
2. 通过网关，用该设备上 legacy 的 `exec_command` 投放切换包：setup.exe（来自公开 Release，核对 SHA256SUMS）、`anx-cutover.ps1`、`rollback-guard.ps1`。
3. **注册两个一次性计划任务**（嫁接自 B 和 C；用计划任务是因为切换过程会杀掉执行它的 exec 会话）：
   - `anx-cutover`（T+30 秒）：
     1. 静默安装 `setup.exe /S`。
     2. 执行 `anx migrate --from "%LOCALAPPDATA%\Roc\MCPManager\client" --apply`。私有版设备会带上 `cloud` 段和 `secrets/cloud/enrolled-agent`，`enrollment.py:20-21` 保证不会重新入网，**agent_id 和 token 都会保留**。
     3. 对旧 manager 执行 `stop-legacy.ps1`。
     4. 启动 `agent-nexus.exe --autostart`。
     5. 在 180 秒内轮询本机 `/api/cloud`，直到状态为 `connected`，并且收到带 features 的 ready（成功时写入 `.anx/connected.ok`）。
     6. 超时就**自动回滚**：执行 `--shutdown-manager`、`--set-autostart=off`，然后重新启动旧的 `mcpm-desktop.exe`。
     7. 结果写入 `%TEMP%\anx-cutover.json`。
   - `anx-rollback-guard`（T+15 分钟）：如果 `connected.ok` 不存在，而且旧 manager 也没有在运行，就执行同样的回滚。这一步兜住切换脚本自己中途退出的情况。
4. 验证：网关上 `device_list` 显示该设备的 `agent_version=0.2.x` 并带有 features；通过新 device 插件读取 `anx-cutover.json`；`anx wake doctor` 结果上报（G3）；之后删除 guard 任务。
5. 如果 remote-2 是 Ubuntu：`apt install ./agent-nexus_0.2.0_amd64.deb`，然后执行 `anx migrate --from ~/.local/share/roc-mcp-manager/client`，用 `systemd-run --user --on-active=30s` 运行同一逻辑的 `anx-cutover.sh`，guard 用 `--on-active=15m`。
6. **私有版的身份迁移要在演练里测试**（评审 gap）：在 M14 中，用一份私有版根目录夹具（`cloud.enabled=true`、`token_secret=cloud/enrolled-agent`、带 `.private/enrollment.json`）跑一遍迁移，断言 agent_id 和 token 引用都没有变化、没有触发入网、`.private` 没有被复制。
7. 回滚：由看门狗自动完成，也可以通过网关或在现场手动执行。

### 12.8 阶段 6：收尾（稳定 2 周后）

- `gateway.legacy_tools = "hidden"`。
- 连接器收编（M11 之后）：把设备本地的 http/sse 连接器逐个“迁移到网关”（§7.1），在网关上重新登录 OAuth 并调用一次验证，然后删除设备本地条目；stdio 的收编为“固定到本设备”。
- 删除 `/opt/mcpmanager` 下除最后一个 release 以外的内容（先归档到离开云主机的备份），旧 unit 文件保留一个月。
- 等所有插件都升级到 mcpkit 0.2 后，影子根目录机制自然不再使用。
- 旧根 `%LOCALAPPDATA%\Roc\MCPManager` 和 legacy worktree 在用户确认后删除。
- `MCPM_*` 回退和 `mcpm` shim 保留到 1.0。

---

## 13. 有序实施里程碑（每个都附验收测试）

| # | 里程碑 | 内容 | 验收测试 |
|---|---|---|---|
| **M0** | 仓库卫生与兼容夹具 | 清理 §11.1 所列；脚本迁出；`scrub_check`、gitleaks；冻结 `tests/compat/legacy_mcpm`；录制 legacy 握手和 catalog 夹具；`ci.yml` 的 hygiene 和 python 两个 job | `git ls-files` 中不含保险库 38 个文件对应的任何路径，也不含 §11.1 列出的命中项；scrub 和 gitleaks 无告警；现有 17 个测试文件在两个平台上全部通过；兼容矩阵中“新 agent → legacy_mcpm 网关”的基线组合通过（这时“新 agent”就是当前代码） |
| **M1** | 改名与 shim | `mcpm` 改名为 `agent_nexus` 并保留 shim；CLI `anx*` 及旧别名；`env.py`、`paths.py`；`/api/ping` 返回双键；`X-ANX` 头；mcpkit 0.2 支持两种文件名和 `ANX_ROOT` | `python -m mcpm.cli status` 与 `anx status` 输出一致；`python -m mcpm.cloud --check-config` 返回 0；只有 `mcp-manager.json` 的根目录能正常加载，且不会写入它；两个文件都存在时优先使用 `agent-nexus.json`；`MCPM_ROOT` 仍然生效并打印一次警告；旧 bootstrap 能通过 ping 拉起新 manager；Rust 单元测试中 `find_root` 能识别两种文件名 |
| **M2** | 版本单一来源 | `version.py set/check`；hatch 动态版本；Tauri 不写 version；Ubuntu 构建读取统一版本 | 改一次版本，所有位置同步；手动制造不一致时 CI 失败；tag 与版本不一致时 release 失败 |
| **M3** | 迁移与凭据恢复 | `anx migrate`（含 dry-run、in-place、port-offset、agent-id）；`anx-gateway migrate`；`anx secrets restore`；影子根目录；`watchers.sqlite3` 复制；明文密钥转存 | 用旧根夹具迁移后，`plugins.device`、`connectors`、`{"$secret"}` 都正确；**旧根所有文件的 sha256 和 mtime 都不变**；重复执行没有差异；restore 计划能逐条对应保险库清单，未映射的项会列出；目标已存在且内容不同时不覆盖；输出中不含任何凭据值；旧 mcpkit 插件通过影子根读到控制台写入的配置；私有版根目录夹具迁移后身份不变（§12.7 第 6 条） |
| **M4** | 网关演进 | 特性协商；`device_list` / `use_device` 及别名；`legacy_tools` 开关；Hub 的 mcp 别名解析；句柄持久化（SQLite）；`--check-config` 对未知键宽容；media 路由兼容；`hub_call` 通道；设备显示名与备注名（决策 D6） | 兼容矩阵全部通过；网关重启后旧 `workspace_id` 仍然有效；规则式 schema 快照通过；旧版 cloud.json 能通过 check；旧 agent 不会收到任何新方法；新 agent 连旧网关时不发送 `hub_call`；**显示名**：catalog 带 `device.hostname=<hostname>` 的设备，没有备注名时 `device_list`、`workspace_list`、`mail_who` 的 `display_name` 等于主机名；设置备注名后（网关 API 和 `hub_call device_admin` 两条路径各测一次），上述输出、`use_device` 返回值、未读提醒文本、唤醒提示词都显示备注名，`agent_id` 不变；`use_device(device_id=<备注名>)` 能路由；两台设备备注名相同时按名字调用返回带候选列表的错误；旧 agent（没有 hostname）显示 agent_id |
| **M5** | device 插件与能力契约 | 改名并加 aliases；`device_info`、`notify`、`file_*`、`agent_sessions`；catalog 中的 `device` 块；平台目录拆分；`anx device conformance` | 工具名和注解快照与 legacy codex-mcp 一致（只新增）；conformance 在 windows 上通过；ubuntu 上 `screen` 显示 `available:false` 并给出原因；`tooluse("x/workspace", "exec_command")` 通过别名路由；`media_get` 使用 `mcp:"workspace"` 仍能取到文件；对不具备能力的设备调用时，错误中包含能力名；`device_info` 返回 `hostname`；`notify(actions=[…], dry_run=true)` 在声明 `toast_actions/1` 的设备上通过 schema 校验 |
| **M6** | 邮箱核心与未读提醒 | store；`mail_*` 系列工具；身份绑定与重新附着；presence；`MailNoticeMiddleware`（两种失败形态、client_id 回退、计数器）；保留策略任务 | 两个客户端分别 `use_device(session_name=…)`：A 给 B 发信；B 调用无关的 `toolsearch`，结果最后一个 TextContent 以 `[agent-nexus 邮箱]` 开头，`_meta["agent-nexus/mail"].unread == 1`，structuredContent 中有 `_mail`；B 调用一个会抛 RoutingError 的原生工具，以及一个返回 ToolFailure 的直出设备工具，两个错误结果中都带提醒；带严格 outputSchema 的工具结果仍能通过校验；`mail_read` 之后提醒消失；`@device:x` 群组正确展开；网关重启后邮件、地址、句柄都还在；不带 workspace_id 的调用使 `notice_missed` 计数加 1；保留策略任务清理过期数据（用可注入的时钟：已读 90 天后删除，未满 90 天保留）；**发件人状态与撤回（决策 D9）**：`mail_status` 依次返回 `delivered` → `read`；收件人未读时 `mail_recall` 成功，收件人的 `mail_inbox` 和提醒里不再出现该邮件、`mail_read` 返回 `recalled`；已读后撤回返回 `already_read`；非发件人撤回返回 `not_sender`；群发时只撤回仍未读的收件人；**真实 ChatGPT 和 Claude.ai 的探测结果写入 `docs/compat-clients.md`** |
| **M7** | 唤醒与离线队列 | WakeDispatcher；`session_wake`、`session_probe` 反向 RPC；claude、codex、companion、notify 适配器；hook 集成；`wake_report`；`fallback_exec`；`anx wake doctor` | 用假的 claude/codex 脚本记录 argv 和 cwd：收件人 idle 时 1 秒内被唤醒，命令行与模板一致；active 时不唤醒；`probe.live=true` 时为 `skipped_active`；设备离线时为 `waiting_device`，重连后 2 秒内派发；连发 5 封邮件只产生 1 个 job；运行期间到达的新邮件在 report 之后触发第二次唤醒；收件人下一次调用使 job 变为 `acked`；ack 超时后重试，超过上限为 failed，并有系统邮件；旧 agent 为 `unsupported`；网关重启后 job 继续执行；假 chatx 收到 `POST /event`，`watch_event(evt)` 返回唤醒提示词；`max_thread_hops=2` 时第 3 跳只投递不唤醒；**权限模式（决策 D2）**：未设置时假 claude 的 argv 含 `--dangerously-skip-permissions`、假 codex 含 `--dangerously-bypass-approvals-and-sandbox`；地址设为 `full-access` 时 codex argv 含 `sandbox_mode="danger-full-access"` 与 `approval_policy="never"`；设为 `inherit` 时两者都不含任何权限参数；`permission_default` 和设备侧翻译表覆盖都生效；**未读升级（决策 D9，可注入时钟，阈值缩短到秒级）**：未读满 `rewake_after_s` 时恰好多一次唤醒（收件人 active 时也唤醒），重复扫描不再唤醒；满 `notify_owner_after_s` 时 `me` 的收件箱里出现一封 system 提醒，同一周期多封合并；满 `expire_after_days` 时投递变为 `expired`、相关 job 取消、发件人收到一封 system 邮件，`mail_status` 显示 `woken` 之后为 `expired`；中途 `mail_read` 后不再升级；网关重启后不重复执行已完成的级别；所有阈值设为 `null` 时对应一级不触发；**真机上 `anx wake doctor` 对 claude、codex、gpt 各通过一次，无人值守运行时没有出现交互提示** |
| **M8** | 插件平台 | 清单 v2 与 schema 生成；git、path、link、archive 安装；版本化目录与 `current`；更新失败自动回滚；卸载；配置页与 schema 表单；secret API；actions 与 status | fixture 插件从本地 bare 仓库安装后为 healthy；更新到一个健康检查必定失败的版本时自动回滚，数据保留，状态为 `update_failed`；卸载后数据保留，`--purge` 才删除；含 `../` 的压缩包被拒绝；缺少必填项时为 `needs_config` 且不启动；node UI 测试覆盖分组、下拉、文件夹、secret 字段；`GET config` 响应和 `agent-nexus.json` 中都找不到 secret 明文 |
| **M9** | mcpkit 插件工具链与六仓库拆分 | `plugin new/validate/smoke`；`plugin-ci.yml`；六个私有仓库；mcpkit 改为 git 依赖；§6.11 的设备间中转安装（`plugin_pack`/`plugin_install`、`relay_report`）与压缩包回退 | 每个插件仓库的 CI 都通过；在干净的根目录从 git URL 安装 6 个插件，全部 healthy；**wecom 在零配置、零 secret 的情况下变为 running，并能收发消息（真机）**；payment 经 `secrets restore` 恢复后可用；`smoke` 能发现缺少注解的工具；**中转安装（决策 D4）**：测试里起一个网关和两个临时根目录的 agent，A 已安装 fixture 插件、B 没有且**没有任何 git 凭据**（`GIT_ASKPASS`、凭据助手都置空）：从设备列表 API 发起“安装到 B”后 B 上为 healthy，锁文件 `source.type=relay`、`from=A`，sha256 一致；包里不含 A 的 `.venv`、`plugin-data`、`secrets`；网关状态目录中没有落盘任何插件文件；篡改一个分块时 B 拒绝安装；超过 `max_package_mb` 时拒绝；目标平台不符或对端是旧 agent 时返回可读错误；A 升级到新版本后 B 显示“从 A 更新”；真机上从 pc-main 向一台远程设备中转一次私有插件 |
| **M10** | 连接器（静态） | 网关单一注册表（`connectors` 表、`/api/connectors`、`hub_call connector_registry`）；网关内的 `ConnectorHost`（`hub/<name>`）；`run_on` 与 `connector_sync` 下发到设备；设备本地 `connectors` 与 `cloud.mcps` 的收编；headers、bearer、`{secret:}` 引用；shared 会话；`auto` 传输探测与 SSE 回退；`@name` 别名与 fingerprint 去重；`anx connector probe` | **云端托管（决策 D10）**：从网关 API 和本机控制台（`hub_call`）各添加一个本地 stub HTTP 连接器，两者写入同一个注册表、彼此可见；`run_on` 默认是 `gateway`；**所有设备都断开时** `hub/<name>` 仍出现在 mcplist 和 toolsearch 中，并能通过 tooluse 调用；只支持 SSE 的 stub 在 `auto` 模式下能连上；headers 被正确注入，密钥值不出现在 `GET /api/connectors` 响应和 catalog 里；**固定到设备**：stdio stub 只能选设备，经 `connector_sync` 下发后以 `<agent_id>/<name>` 出现，10 个句柄并发调用时只启动 1 个子进程，设备离线时显示不可用；旧 agent 上显示 `pending_device_upgrade`；设备本地已有的连接器显示为 `device-local`，“迁移到网关”后 fingerprint 相同、mcplist 只出现一次，`@name` 优先路由到 `hub` |
| **M11** | 连接器 OAuth | `ConsoleOAuth`（网关和设备两种回调）；网关域名上的固定回调 `/oauth/connectors/callback/<name>`；网关状态目录与设备 secrets 两种 token store；主动刷新；`needs_login` 状态；给 `me` 的系统邮件；static client 和 CIMD；resource 参数核实；真实服务探测 | 以 mcpkit PasswordOAuth 作为授权服务器：**网关托管的连接器**在网页控制台点登录后，DCR 注册的 redirect_uri 是网关 `public_url` 下的固定路径，回调完成后 `auth_state=ok`，token 文件在网关状态目录且权限为 0600，此后所有设备离线时仍能调用；state 错误或过期、以及未登录 owner 会话的回调都被拒绝；固定到设备的连接器走 loopback 回调，同样能登录（DCR + PKCE）后调用；把 access token 设为 5 秒有效期后能自动刷新；吊销 refresh token 后状态变为 `needs_login`，tool_call 立即返回可读错误，不会挂起，也不会弹出浏览器，`me` 的收件箱里出现一封带登录链接的系统邮件；**`docs/connectors-tested.md` 记录阿里云和 Cloudflare 以网关托管方式的实测结果，并各成功调用一次** |
| **M12** | 桌面 | NSIS；hooks；updater；autostart；argv 动词；`--shutdown-manager`（§9.3）；runtime 版本化；Linux user unit 与 XDG autostart；托盘角标与更新菜单（决策 D7）；`agentnexus://` deep link | **托盘图标单元测试**（`tray.py` 的合成函数，不需要真实托盘）：四种 `(connected, update_available)` 组合各生成一张图，已连接时右下角是绿点，有更新时左上角出现橙色向上箭头，两者不重叠，无更新时左上角与底图一致；菜单在 `desktop-update.json` 有版本时为“更新到 vX.Y.Z”，没有时为“检查更新”，文件变化后菜单刷新；**只在点击后安装**：发现新版本后不调用 `download_and_install`，直到执行 `--apply-update`；配置 `mode=auto` 时按 `notify` 处理并有 warning；在干净的 Windows 用户下：安装 → 注销再登录，只出现托盘，manager 在运行；托盘切换自启后 Run 值相应变化，升级后用户的选择保持不变；本地起一个静态服务提供 0.2.1 的 latest.json，`notify` 模式下点击更新：旧 manager 和插件全部退出、没有残留的 python.exe、没有 access denied、0.2.1 使用新的 runtime 目录启动；签名被篡改时拒绝安装；卸载后没有残留进程、Run 值被删除、数据保留；旧便携 exe 与新安装版可以同时存在；打开 `agentnexus://enroll?invite=…` 会把邀请码填进正在运行实例的入网页（没有运行时先启动）；Ubuntu VM 上重新登录后 user unit 在运行 |
| **M13** | CI、发布与部署 | `release.yml`、`deploy-gateway.yml`；`anx-deploy` 用户和 sudoers；`upgrade.py` 参数化；私有版放入 ops 仓库，只在本机构建（决策 D3） | 在测试 tag `v0.2.0-rc.1` 上：draft release 中有 setup.exe、.sig、latest.json、gateway zip、deb、SHA256SUMS，且**没有任何私有版产物**；公开仓库的所有 workflow 文件中不出现 `build_private_desktop`、`enrollment` 构建参数（静态检查）；minisign 验签通过；no-private-payload 测试通过；本机运行一次私有版构建，产物在仓库外，updater 为 off；deploy job 部署到阶段 2 的 8441 试运行实例上，probe 通过；人为制造 check-config 失败时自动回滚 |
| **M14** | 切换演练 | 阶段 0–2；门 G0–G4 | 每一个门都留有可复查的记录（日志和命令输出）；回滚演练都在 5 分钟内恢复 |
| **M15** | 正式切换 | 阶段 3–6 | 每个阶段的验证清单都打勾；切换后 48 小时内三台设备都带 features 在线；`events` 表中有统计出的唤醒成功率 |
| **M16** | 邀请码入网与每设备凭据（决策 D3，取代原来的可选 `cloud-main` 节点） | `invites`、`device_tokens` 表；`POST /enroll`；网页控制台、本机控制台与 `anx-gateway invite create` 三处生成邀请；首次启动的“加入”页；ingress 双重鉴权（`agents` 映射 + `device_tokens`）；吊销与 4401；设备列表 | 生成的邀请码解码后含 `hub`、`id`、`secret`，网关库里只有 secret 的 sha256；公开安装包（测试中为临时根目录的 manager）粘贴邀请码后完成入网：`secrets/cloud/device-token` 已写入、`agent-nexus.json` 有 `cloud` 段、连上 `/ingress`、`device_list` 中显示名为主机名；**同一邀请第二次使用被拒绝**；过期的、已作废的、secret 错误的邀请都被拒绝且不产生设备；两台主机名相同的设备得到 `-2` 后缀的 agent_id；保留名不能使用；按 `agents` 映射入网的旧设备照常连接（兼容）；**吊销**：在线设备的连接在 1 秒内以 4401 关闭，之后用同一 token 的 hello 被拒绝，设备停止重连，其他设备不受影响；吊销一台不影响同一邀请批次的其他设备；输出和日志中不出现 token 明文 |
| **M17** | 收件箱、`ask_user`、toast 与 PWA（决策 D11） | 收件箱页（本机控制台/桌面端/网关网页）；以 `me` 回复；`ask_user` 与 `asks` 表；`request_user_input` 并入；`toast`、`toast_dismiss` 反向 RPC 与 `ask_answer`；托盘“收件箱（N）”；网关 `/inbox/` PWA 与 OAuth 登录 | 发给 `me` 的邮件出现在收件箱线程中，所有声明 `toast_actions/1` 的在线设备（假的 device 插件）收到带 `link` 的 toast；在收件箱以 `me` 回复后，原发件人的地址被唤醒（假 claude 记录到 argv）；`ask_user(options=["a","b"], recommended="b")` 的问题卡片顺序为 b、a、其他；从 toast 按钮作答（`hub_call ask_answer`）后提问者收到 `kind=answer` 的邮件并被唤醒，其他设备收到 `toast_dismiss`；第二次作答返回“已回答”；`blocking=true` 在超时内作答时直接返回答案且不唤醒，超时则返回 `open`、之后仍以邮件送达；选项超过 3 个时 toast 只有“打开回答”；旧版设备收到普通 notify；`request_user_input`、`_async`、`_result` 的 schema 快照不变，网关可用时问题出现在收件箱，网关不可用时退回本机弹窗；**PWA**：未登录访问 `/inbox/` 被重定向到网关现有的 OAuth 登录，登录后 cookie 为 HttpOnly/Secure/SameSite=Strict；`manifest.webmanifest` 合法，service worker 不缓存 `/api/inbox/*` 响应；手机视口（375 px）下 node UI 测试覆盖线程列表、回复和问题卡片；没有企业微信或邮件桥接的配置项 |
| M18（可选） | 扩展 | 限制开关产品化；lite 节点注册（§14，复用 M16 的邀请码）；PWA Web Push | lite 节点用邀请码入网并在线 |

依赖关系：M0 → M1 → M2 → M3 → M4 → (M5 ‖ M6) → M7；M8 依赖 M3，可以与 M4–M7 并行；M9 依赖 M8（中转安装还依赖 M4 的 `hub_call`）；M10 依赖 M4 和 M8；M11 依赖 M10 和 M6（`needs_login` 系统邮件）；M12 依赖 M1 和 M2，可以与 M3 之后的工作并行；M13 依赖 M12；M16 依赖 M4，deep link 部分依赖 M12；M17 依赖 M5、M6、M7；M14 依赖 M4–M7、M12、M13；M15 的阶段 3（网关）只依赖 M4 和 M6（M7 不是前置条件，唤醒随设备升级生效），阶段 4 依赖 M5、M7、M8、M12（插件用 link 模式，**不依赖 M9 的拆仓**），阶段 5 还要求 M13，远程设备的私有插件在阶段 5 之后用 M9 的中转安装替换 link 模式。M16、M17 不是切换的前置条件，可以在 M15 之后上线。

---

## 14. 将来的 lite 节点注册与 token 轮换（只做设计，M18）

- 现状：`agents` 映射是 agent_id 到环境变量名，变量里存 token，比较的是 sha256 摘要（cloud.py 的 ingress）；`groups` 按前缀分组，供 portable 入网使用。M16 起另有 `device_tokens` 表（§11.5）。
- 注册：**直接复用 §11.5 的邀请码**（决策 D3）。控制台生成邀请时可以选“手机/边缘设备”，以二维码显示 `agentnexus://enroll?invite=…`；手机 App 扫码后调用同一个 `POST /enroll`，换取每设备 token，写入 `device_tokens`，不再改写 `gateway.env`，也不需要重启网关。
- 轮换：`device_tokens` 允许同一设备有两条未吊销的 token（新旧并存），`anx-gateway device rotate <id>` 生成新 token 并通过反向 RPC 下发给设备，设备确认后吊销旧 token。
- 推送唤醒：lite 节点在 hello 中声明 `push: {provider: "fcm"|"apns", token}`，网关的 `push` 适配器在节点离线时发送静默推送。

---

## 15. 主要风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | `session_probe` 只是启发式判断，如果误判为“不活跃”，`--resume` 会让正在交互使用的会话多出一个分支 | hook 上报提高准确度；每个地址只有一个活跃唤醒；`idle_after_s` 可以调；`skipped_active` 会记录 |
| R2 | resume id 选错（同一目录有多个会话） | 优先用 hook；`mail_who` 显示 `resume_source` 和置信度，可以人工纠正；自动命名的地址只有 resume id 才能唤醒，不按 workdir 续上“最后一个”会话 |
| R2a | 自动名按（设备，OAuth client，workdir）分配：同一目录并发的两个同类会话共用一个 client，重连后 `base` / `base-2` 可能互换，邮件落到另一个对话 | 复用前先认调用方自己的上一个句柄（属于当前持有对话，同一对话的重新绑定不算接管）；活跃的持有者不被抢；被接管的旧句柄失去邮箱身份，不能读、抢回或改名；接管时清掉前一个对话的 resume id 和标题；显式名不能抢别人的自动地址；需要确定身份时显式传 `session_name`（或带 `workspace_id` 改名）；文档写明 |
| R3 | companion 唤醒不保证送达（依赖 ChatGPT 的 DOM；有草稿时会放弃；30 秒超时；chatx 接受请求就算 delivered） | 以收件人的下一次调用作为 ack，超时后重试；提醒文本是兜底 |
| R4 | 无人值守的权限参数（跳过确认）意味着被唤醒的会话可以不经确认执行命令 | 这是用户要求的“先不设限”，默认 `bypass`（决策 D2）；每个地址可以改为 `full-access` 或 `inherit`；翻译表可以配置；设置入口只在控制台和 CLI，agent 不能通过工具参数修改 |
| R5 | 身份不设防：持有句柄，或者用同名同 kind 重新绑定，就能读取别人的邮箱 | 用户已接受；`acl.mode` 和 `bind_to_oauth_client` 已预留 |
| R6 | 失控循环消耗额度 | `max_thread_hops` 等开关默认关闭；`events` 统计；控制台显示每小时唤醒次数 |
| R7 | 无状态客户端不带 workspace_id，收不到提醒 | instructions 引导；client_id 回退；`notice_missed` 计数 |
| R8 | ChatGPT 对 structuredContent 和 `_meta` 的处理可能变化 | M6 实测并记录；`notice.structured` 可以切换 |
| R9 | OAuth 服务之间的差异（不支持 DCR、不接受 loopback、要求 CIMD） | static client、CIMD、headers 模式；M11 实测 |
| R10 | 连接器所在设备离线时不可用 | 远程连接器默认跑在网关（决策 D10），不依赖设备；只有固定到设备的 stdio/受 IP 限制的连接器受影响，`@name` 自动选择其他承载设备 |
| R11 | 远程设备没有 GitHub 凭据 | 不再需要：设备列表的“安装到此设备”经网关中转（决策 D4，§6.11）；回退为压缩包 |
| R12 | 每次桌面更新都要下载完整 runtime（约 34 MB 起） | 可以接受；插件不打包进去，体积会下降 |
| R13 | SmartScreen 警告（安装包没有 Authenticode 签名） | 用户决定不做代码签名（决策 D5）；首次安装点一次“仍要运行”；静默安装和 updater 更新不受影响 |
| R14 | 上游许可证（unity-mcp、wecom-reader）以及 AGPL 的 vision 依赖 | 插件仓库私有；AGPL 部分按需下载，不随包分发 |
| R15 | 公开仓库残留敏感内容 | scrub 是硬门；万一推送出去，**立即轮换** enrollment 和 agent token，不能只靠改写历史 |
| R16 | 网关是单进程 | Hub、调度器、中间件都假设单 worker；已持久化，重启是安全的；多 worker 不在本设计范围内 |
| R17 | 切换窗口内正在进行的调用会丢失 | 现状如此；邮件和 job 都已持久化 |
| R18 | 本机就地迁移期间，有其他 agent 在写 `WorkSpace/mcp` | 阶段 0、2、4 都跑 preflight；协作信箱留言；不使用 `git add -A` |
| R19 | 新旧两个版本的自启项同时存在 | 回滚步骤中明确执行 `--set-autostart=off`；新旧 identifier 不同，Run 值名也不同，可以分别检查 |
| R20 | 网关托管连接器后，第三方 MCP 的 token 集中在云主机上，网关被攻破会一并泄露 | 用户为 7×24 可用接受的取舍（决策 D10）；token 文件 0600、属主为专用用户；控制台只写不读；可以把敏感连接器固定到设备；离开云主机的备份（§12.2）同样包含这些文件，按凭据对待 |
| R21 | 邀请码在有效期内泄露，被别人拿去入网 | 一次性、默认 24 小时过期、只存摘要；设备列表能看到每台设备的入网时间和主机名，可以立即吊销（§11.5） |
| R22 | 网页收件箱/PWA 暴露在公网 | 复用网关现有的 OAuth 登录；cookie 为 HttpOnly/Secure/SameSite=Strict；service worker 不缓存邮件内容 |
| R23 | 插件中转把一台设备上的代码装到另一台设备上，来源设备被篡改时会扩散 | 安装前在发起的控制台显示来源设备、版本、commit 和 install steps，由用户确认；sha256 端到端校验 |

---

## 16. 评审 gaps_in_all 对照表

| 评审 gap | 本文的处理 |
|---|---|
| 旧 WorkspaceProvider 遇到 `mcp="device"` 或别名条目时的行为 | §3.1 已对照代码核实：`mcp="device"` 可以正常工作；别名条目会导致 mcplist 和 toolsearch 重复，所以默认不发；§3.3 用 `legacy_mcpm` 做录制测试 |
| 网关回滚的端到端测试 | §12.1 的 G2：用真实的 legacy release 在 8442 上演练，同时作为本地 compat 测试 |
| 可选 `workspace_id` 对客户端缓存 schema 的影响 | §3.4 的规则式快照；只允许追加可选参数 |
| 本机新旧版本共用根目录和端口、停止顺序 | 试运行使用独立根目录并偏移端口（§12.4）；§9.3 的五步停止流程，确认 breakaway 进程已退出 |
| 私有版设备迁移后不丢 enrollment/token | §3.1 已核实 enrollment.py:20-21；§12.7 第 6 条，以及 M3、M14 中的夹具测试 |
| 并发 agent 编辑共享根目录 | §12.2 第 4 条 preflight，阶段 2、4 开始前各跑一次 |
| 无人值守唤醒的参数 | §8.7 的模板（codex 参数已在本机核实）和按地址配置的权限模式（决策 D2，默认 bypass），`anx wake doctor` 作为真机验收 |
| 注销或锁屏时的唤醒 | §8.7 的状态表 |
| ChatGPT 和 Claude.ai 对提醒的实际展示 | M6 的实测项和 `docs/compat-clients.md` |
| 不带 workspace_id 时的身份回退和指标 | §8.6：client_id 回退加 `notice_missed`、`notice_fallback_used` 计数 |
| 阿里云和 Cloudflare 的真实鉴权行为 | §7.4 的 `connector probe` 和 M11 实测，实测前不下结论 |
| 远程设备安装私有插件的凭据 | §6.11 的设备间中转安装（决策 D4），远程设备不需要凭据 |
| 邮箱数据保留、附件、库大小 | §8.4 的保留策略、附件规则和容量估算；§8.11 的未读升级与 30 天过期（决策 D9） |
| lite 节点注册和 token 轮换 | §14，复用 §11.5 的邀请码 |
| Linux 和 macOS 的自启、私有版的更新 | §9.6 及 M12 的 Ubuntu 用例；私有版 updater 固定为 off |
| LICENSE 和测试夹具的泄露审计 | §11.1（夹具清单已经过 grep 核实），Apache-2.0（决策 D1） |

---

## 17. 用户决策（原 Open decisions；2026-11-10 已全部拍板）

> 本节只保留每个问题和最终结论的对照，**详细内容以文首“决策日志”为准**。原先列出的推荐默认值和备选方案已经失效，不再保留。

| # | 问题 | 结论 | 见 |
|---|---|---|---|
| 1 | 公开仓库的许可证 | **Apache-2.0**（M0 已加 `LICENSE`、`THIRD_PARTY_NOTICES.md`） | D1、§11.1 |
| 2 | 被唤醒会话的权限模式 | **按邮箱地址配置**：`bypass`（默认）\| `full-access` \| `inherit` | D2、§8.7 |
| 3 | 私有入网版以后怎么办 | **保留**，只在本机构建，永不进公开 CI 和 Release；**另加邀请码**入网、每设备凭据、可逐台吊销 | D3、§11.4、§11.5 |
| 4 | 远程设备怎么获取私有插件 | **修订（D4r）**：插件包保存在网关的插件库，控制台“安装到…”一键安装（原结论“经网关从已有该插件的设备中转”作废）；远程设备不需要 GitHub 凭据（PAT 方案作废） | D4r、§6.12 |
| 5 | 是否购买 Authenticode 代码签名证书 | **不做代码签名** | D5、§9.4 |
| 6 | 本文件以及设备 id 是否进入公开仓库 | 保留 `docs/design.md`，只用角色名（pc-main、pc-next、remote-1、remote-2）；主机名、IP 不写进本文。**M0 已执行** | 文首说明 |
| 6b | 设备显示名 | 默认取设备自报的主机名，控制台可设备注名，agent 处处看到备注名，`agent_id` 不变 | D6、§5.5 |
| 7 | 桌面更新策略 | 托盘绿点表示连接；有更新时另一角出现**橙色向上箭头**角标；右键菜单“更新到 vX.Y.Z”或“检查更新”；**只有用户点击才安装**（取消 `auto`） | D7、§9.2、§9.4 |
| 8 | 唤醒判定为空闲的阈值 `idle_after_s` | **120 秒** | D8 |
| 9 | 邮箱保留期 | 已读保留 **90 天**；未读 1 小时再唤醒一次、24 小时转告 owner、30 天归档为过期并通知发件人；发件人可查询状态、撤回未读；阈值都可配置 | D9、§8.11 |
| 10 | 是否做 7×24 连接器节点（cloud-main） | **不需要**：连接器注册表放在网关，远程 HTTP/SSE 连接器默认在网关里运行，OAuth 在网关域名上完成；stdio、受 IP 限制的可以固定到设备 | D10、§7 |
| 11 | 发给 `me` 的邮件提醒到哪里 | agent-nexus 内部处理：收件箱页、在线设备的 Windows toast（可点击跳转、简单问题直接按钮作答）、新工具 `ask_user`、手机网页收件箱（PWA，复用 OAuth 登录）；`request_user_input` 并入；不做企业微信或邮件桥接 | D11、§8.12 |
| 12 | GPT Companion（chatx） | 保持独立应用，wake 适配器对接它，chatx 不改；打包成插件是以后的选项 | D12、§8.7 |
