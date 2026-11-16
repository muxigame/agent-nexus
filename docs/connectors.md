# 第三方 MCP 连接器（网关托管）

> 对应 design.md §7（决策 D10）、里程碑 M10 / M11，界面见 ui-spec.md §3.4。

连接器把第三方的远程 MCP 服务（阿里云、Cloudflare、云效……）接到 Agent Nexus 网关上。连接器**运行在网关进程里**：

- 所有 AI 客户端（ChatGPT、Claude.ai、Claude Code、Codex）只配置网关一个地址 `https://<网关域名>/mcp`，不用为每个服务单独添加；
- 连接器的工具以 MCP ID `hub/<名称>` 出现在 `mcplist`、`mcpdetail`、`toolsearch`、`tooluse` 里；
- **不依赖任何设备在线**，7×24 可用；OAuth 登录在网关域名上完成，令牌保存在网关，不会下发到设备。

## 在控制台添加

1. 打开网关控制台 → **连接器** → **添加连接器**。
2. 粘贴 MCP 地址，点 **探测**。网关会：
   - 先发 Streamable HTTP 的 `initialize`；返回 400/404/405 时按 MCP 规范的兼容规则改用 SSE（GET 同一地址，等 `endpoint` 事件）；
   - 根据 401 和 `WWW-Authenticate: Bearer resource_metadata=…`（RFC 9728）判断是否需要 OAuth，并自动选好鉴权方式；
   - 不需要认证的服务直接列出工具数。
3. 填名称（小写字母、数字、连字符，例如 `cloudflare`；工具显示为 `hub/cloudflare`）和显示名。
4. 运行位置选 **网关**。“固定到设备”（stdio、内网或限制来源 IP 的服务）即将推出。
5. 传输一般保持 **自动**。
6. 鉴权：
   | 方式 | 适用 | 填写 |
   |---|---|---|
   | 无 | 公开服务 | — |
   | Bearer | 只给 API Token 的服务 | Token，以 `Authorization: Bearer <token>` 发送 |
   | Header | 自定义 Header（`X-Api-Key`、区域等） | 任意多个 Header 名/值 |
   | OAuth | 支持 MCP 授权规范的服务 | 默认自动注册客户端；服务不支持时填预注册的 Client ID / Secret |

   密钥只写不读：保存后控制台、API、`mcplist`/`mcpdetail` 都不会再返回它的值；编辑时留空表示保持不变。值里可以写 `{env:变量名}` 引用网关进程的环境变量。
7. 点 **保存并连接**。OAuth 连接器会直接跳到服务商的授权页，授权后回到 `/#/connectors` 并提示“登录成功”。
8. 行内的 **测试** 会重新建立会话并列出远程工具。

状态胶囊：`正常`、`需要登录`、`错误`（行内显示最后一条错误）、`测试中`、`已停用`。

## 阿里云

**阿里云 OpenAPI MCP Server**（官方托管的远程 MCP）：

- 在阿里云 OpenAPI 门户创建 API MCP Server（选择要开放的云产品 API），创建后复制它的 **Streamable HTTP** 地址，形如
  `https://openapi-mcp.cn-hangzhou.aliyuncs.com/accounts/<账号ID>/custom/<名称>/id/<ID>/mcp`
  （SSE 地址把结尾的 `/mcp` 换成 `/sse`；国际站域名不同，以控制台显示为准。没有与账号无关的通用地址。）
- 鉴权：**OAuth，必须用预注册的 Client ID**。阿里云接受动态注册请求，但登录页会拒绝它发出的客户端（`redirect_uri_mismatch`，2026-11-11 实测）。做法：
  1. 阿里云 RAM 访问控制 → OAuth 应用 → 创建应用，类型选 **Native 应用**（不需要 Secret，走 PKCE）。
  2. 回调地址填 `https://<网关域名>/oauth/connectors/callback/<连接器名>`；OAuth 范围加 `openid` 和 `/acs/mcp-server`。
  3. Agent Nexus 里编辑连接器 → OAuth → “预注册的 Client ID”，填应用 ID，Scopes 填 `openid /acs/mcp-server`，保存后点“登录”。
- 阿里云文档另有“静态凭据”方式（服务端用 AccessKey 换令牌）和 `aliyun mcp-proxy` 本地代理，供不支持 OAuth 的客户端使用；网关支持 OAuth，不需要它们。

核实情况：上面的地址格式与鉴权方式来自阿里云帮助中心与 OpenAPI 文档（“在 Agent 中集成 OpenAPI MCP Server”“使用 aliyun mcp-proxy 代理 OpenAPI MCP Server”、`GetApiMcpServer` 接口返回的 `urls.mcp`）。**以网关托管方式对真实阿里云服务的端到端实测（是否支持 DCR、是否接受网关域名上的 https 回调、refresh token）尚未完成**，按 design §7.4 的要求，结果应写入 `docs/connectors-tested.md`。如果阿里云拒绝动态注册，按下文“服务不支持动态注册”改用预注册客户端。

**阿里云百炼 MCP 广场**等以 API Key 鉴权的服务：选 **Bearer**，填 DashScope API Key（未实测）。

## Cloudflare

Cloudflare 官方托管的远程 MCP 服务器（来自 Cloudflare 开发者文档“Cloudflare's own MCP servers”，均为 Streamable HTTP，路径 `/mcp`）：

| 服务 | 地址 |
|---|---|
| Cloudflare API（整套 API，Code Mode：搜索 + 执行） | `https://mcp.cloudflare.com/mcp` |
| 文档 | `https://docs.mcp.cloudflare.com/mcp` |
| Containers | `https://containers.mcp.cloudflare.com/mcp` |
| Browser Run | `https://browser.mcp.cloudflare.com/mcp` |
| Digital Experience Monitoring | `https://dex.mcp.cloudflare.com/mcp` |
| Cloudflare One CASB | `https://casb.mcp.cloudflare.com/mcp` |
| Agents SDK 文档 | `https://agents.cloudflare.com/mcp` |

- 鉴权：**OAuth**（连接时用 Cloudflare 账号授权）。Cloudflare API 服务器也接受 **Bearer**：填一个 Cloudflare API Token（适合不想走浏览器登录、或要限定权限的场景）。
- 旧文章里的 `https://<产品>.mcp.cloudflare.com/sse` 是早期的 SSE 地址；新建连接器请用 `/mcp`，传输保持“自动”即可兼容两者。

核实情况：地址列表核对了 Cloudflare 官方文档；**网关托管方式下的真实登录与调用尚未实测**（同上，记录到 `docs/connectors-tested.md`）。

## 服务不支持动态注册（DCR）

登录时如果提示“服务拒绝了动态注册”或“不支持动态注册客户端”：

1. 在服务商的开发者后台创建一个 OAuth 应用，回调地址（redirect URI）填
   `https://<网关域名>/oauth/connectors/callback/<连接器名称>`（控制台编辑抽屉里会显示这个完整地址）；
2. 编辑连接器 → 鉴权 OAuth → **预注册的 Client ID**，填 Client ID，机密客户端再填 Client Secret；
3. 保存后点 **登录**。

## 运行细节

- **会话**：每个连接器在网关里只保持一个上游会话，所有客户端、所有 Agent 共享；会话断开后下一次调用重新建立。工具调用**从不自动重试**，失败时返回“执行结果未知”。
- **超时**：默认 120 秒（高级选项里 5–600 秒）。
- **OAuth**（design §7.3）：RFC 9728 资源元数据 → RFC 8414 授权服务器元数据（回退到 OIDC discovery）→ RFC 7591 动态注册（固定回调 `<public_url>/oauth/connectors/callback/<name>`，`token_endpoint_auth_method=none`）→ 授权码 + PKCE S256 + `state` + RFC 8707 `resource`。没有指定 scope 时使用服务要求的 scope，服务支持 `offline_access` 时自动带上，以拿到 refresh token。
  - 令牌在 `<state_dir>/connectors/<name>/oauth.json`，其他密钥在同目录的 `secrets.json`；原子写入，仅属主可读（Linux 0600）。数据库 `gateway.sqlite3` 的 `connectors` 表只存定义和工具缓存，不含任何密钥。
  - 到期前自动刷新（后台每 30 秒检查，剩余寿命不足 20% 时刷新；调用前也会检查），refresh token 轮换会被保存；401 时强制刷新一次。
  - refresh token 失效（`invalid_grant`）时清除令牌、保留客户端注册，状态变为“需要登录”，并给 `me` 发一封系统邮件（收件箱可见）。此后调用会**立即**返回“请在控制台的连接器页登录”，不会挂起，也不会弹出任何登录。
  - 回调要求一次性、5 分钟内有效的 `state`，并要求浏览器带着控制台的 owner 会话（控制台 Cookie 是 SameSite=Strict，从服务商跳回时浏览器不带它，回调页会先做一次同站跳转）。
  - **实现说明**：design §7.3 原计划继承 fastmcp 的 `OAuth`（SDK 的 `OAuthClientProvider`）。实际实现是 `agent_nexus/connectors/oauth.py` 里的小型 OAuth 客户端：控制台需要 `POST …/login` 直接**返回**授权地址、由回调路由交付授权码，并且工具调用路径绝不能触发交互登录；把“登录”和“传输”分开后，传输层只看到一个只会加令牌、必要时刷新的 `httpx2.Auth`。
- **在 Agent 里的样子**（design §7.5）：`mcplist` 中连接器条目带 `kind: "connector"`、`run_on: "gateway"`、`auth_state`；`toolsearch` 结果带 `kind`，需要登录的连接器列在 `connectors_needing_login`；`tooluse("hub/<名称>", 工具, 参数)` 在网关进程内直接调用。`mcplist(agent_id="hub")` 只列连接器。
- **工具过滤**：高级选项里的“只允许 / 屏蔽这些工具”。

## API（owner：Bearer 或控制台 Cookie，写请求带 `X-ANX: 1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/connectors` | 列表 / 新建（`ConnectorInput`，名称冲突 409） |
| GET / PUT / DELETE | `/api/connectors/{name}` | 读取 / 部分更新（省略的密钥 = 保持）/ 删除（204，连同密钥与令牌） |
| POST | `/api/connectors/probe` | `{"url"}` → `{transport, needs_oauth, auth_hint, server, tools_count, error}` |
| POST | `/api/connectors/{name}/test` | 重新连接并列出工具：`{ok, tools_count, latency_ms, error, tools}` |
| POST | `/api/connectors/{name}/login` | `{authorize_url, redirect_uri}` |
| POST | `/api/connectors/{name}/logout`、DELETE `…/token` | 撤销（服务支持时）并删除令牌 |
| GET / PUT | `/api/connectors/export`、`/api/connectors/import` | design §7.2 的整表结构（导出不含密钥值） |
| GET | `/oauth/connectors/callback/{name}` | OAuth 回调，完成后 303 到 `/#/connectors?login=<name>&result=ok\|error` |

本机控制台经 `hub_call connector_registry {op: list|create|update|delete|probe|test|login|logout}` 读写同一个注册表。

## 还没有做的

- 固定到设备（`run_on: "device:<agent_id>"`、stdio、`connector_sync` 下发）、设备本地 `connectors` 的收编与“迁移到网关”、`@name` 别名与 fingerprint 去重；
- `expose: "direct"`（目前按“仅搜索”处理）；
- 上游 `notifications/tools/list_changed` 的即时刷新（目前在测试、重连时以及每 10 分钟重新列出工具）；
- 阿里云、Cloudflare 的真实端到端实测记录（`docs/connectors-tested.md`）。
