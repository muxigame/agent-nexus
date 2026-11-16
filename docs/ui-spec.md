# Agent Nexus 网页界面规范（设计系统 + 页面 + API 契约）

> 本文是所有网页界面的单一规范：云端控制台（网关站点根目录）、登录页、OAuth 授权页、本机控制台（Tauri 窗口里的 `http://127.0.0.1:<port>/`）、收件箱。后续各阶段按本文实现，**API 一节是前后端之间的契约**；与 `docs/design.md` 冲突时以 design.md 的决策日志为准，并回来改本文。
>
> 驱动这一版的 owner 反馈：“这不是一个云平台，就是一个收件箱；没看见哪里加第三方 MCP；没看见哪里可以下载安装包；安装完以后插件都被禁用了，怎么安装；UI 太丑了，重新设计”。对应的回答分别是：云端控制台（§3）、连接器页（§3.4）、下载页（§3.7）、本机插件页的“尚未启用”横幅与开关（§4.2）、本设计系统（§1）。
>
> 原则：先把功能打通，不加新的安全限制；所有限制点保持 design.md §0.5 的“默认关闭、预留开关”。

### 实现状态（2026-11-10）

| 部分 | 状态 | 代码 |
|---|---|---|
| 云端登录、会话、站点根目录（§2.1、§2.2） | 已实现 | `agent_nexus/console.py`、`static/console.html` |
| 云端 概览、设备 + 邀请（含二维码）、收件箱、下载、设置（§3.1、§3.2、§3.5–§3.7） | 已实现；设置页只读（`PUT /api/settings`、撤销客户端授权未做） | `static/console/*.js`、`static/ui/inbox.js`、`static/ui/qr.js` |
| 云端 连接器、插件（§3.3、§3.4） | 导航与占位页，后续阶段替换 | `static/console/app.js` |
| 邀请码与每设备凭据（design §11.5） | 已实现 | `agent_nexus/invites.py`、`store.py` 迁移 4、`cloud.py` ingress、`gateway_admin.py` |
| 本机 连接云端（§4.3） | 已实现，页面在本机 `/cloud`（旧控制台总览有入口，`#/enroll?invite=` 深链跳到这里） | `static/local-cloud.html`、`web.py`、`agent_nexus/join.py` |
| 本机 概览、插件、连接云端、收件箱、设置（§4.1–§4.5） | 已实现（M8 安装/卸载/配置/日志；`/cloud` 与 `#/enroll` 都落到 `#/cloud`；旧标签页控制台移到 `/classic`）；“打开数据目录”（`/api/app/reveal`）未做，设置页显示路径并可复制 | `static/index.html`、`static/local/*.js`、`static/ui/schema-form.js`、`agent_nexus/plugins/{installer,manage,schema}.py` |
| 插件库（design §6.12，决策 D4r）：云端插件页的“插件库”与“安装到…”、本机安装弹窗的“插件库”来源（§3.3、§4.2） | 已实现 | `agent_nexus/plugin_repo.py`、`plugins/{pack,hub_source}.py`、`static/console/pages.js`、`static/local/plugins.js`、`scripts/plugin_repo_screens.py` |
| PWA `/inbox/` | 换成设计系统与 `mountInbox`；登录仍走原 OAuth PKCE（`/inbox/login`），与控制台共用同一个 cookie | `static/pwa.html` |

## 0. 文件与静态资源

| 路径 | 内容 |
|---|---|
| `agent_nexus/static/ui/tokens.css` | 设计令牌（颜色、字体、间距、圆角、阴影、层级、动效），浅色 + 深色 |
| `agent_nexus/static/ui/ui.css` | 组件样式（§1.4 的全部组件） |
| `agent_nexus/static/ui/icons.js` | 内联 SVG 图标（Lucide 子集，ISC，见 `LICENSE-lucide.txt`）：`ICONS`、`iconHTML()`、`icon()`、`hydrateIcons()` |
| `agent_nexus/static/ui/ui.js` | 小工具：`el()`、`toast()`、`modal()`、`drawer()`、`confirmDialog()`、`copy()`、`fetchJSON()`、`createRouter()`、`renderShell()`、`pill()`、`tokenField()`、`emptyState()`、`skeletonRows()`、`relTime()`、`bytes()`、`logoSVG()`、`setTheme()` |
| `agent_nexus/ui_assets.py` | `ui_route()`：把上面的目录以 `/ui/<name>` 提供出去（只认目录里的直接文件和已知后缀，`Cache-Control: no-cache`、`nosniff`） |
| `docs/mockups/*.html` | 用真实 `ui.css` 做的静态样稿；`mock.js` 用真实 `ui.js` 渲染外壳 |

- **两个服务器都挂 `/ui/`**：网关 `cloud.py`（公开，无需登录，不含任何数据）和本机 `web.py`（经 `LocalOnly` 校验 Host）。页面统一写：
  ```html
  <link rel="stylesheet" href="/ui/tokens.css"><link rel="stylesheet" href="/ui/ui.css">
  <script type="module">import { renderShell, fetchJSON } from "/ui/ui.js";</script>
  ```
- 无构建步骤、无运行时 CDN。新增图标：从 `lucide-react` 的 `dist/esm/icons/<name>.mjs` 取 `__iconData.node` 生成，追加到 `ICONS`，并在 `THIRD_PARTY_NOTICES.md` 的 Lucide 一节保持版本号一致。
- 例外：**OAuth 授权页**由 mcpkit（独立 SDK）渲染，CSP 为 `default-src 'none'; style-src 'unsafe-inline'`，不能加载 `/ui/` 也不能执行脚本。它内联一份精简样式（tokens + `.auth*`、`.field`、`.input`、`.btn`、`.scope-list`），见 §2.3。
- 看样稿：在仓库根目录 `uv run python -m http.server 8765 --bind 127.0.0.1`，打开 `http://127.0.0.1:8765/docs/mockups/cloud-overview.html`。`?open=<模板 id>` 打开弹窗/抽屉，`?theme=dark` 强制主题。截图在 `agent-nexus-dist/screens/design/`（仓库外）。

## 1. 设计系统

### 1.1 方向

安静、现代的产品界面（参照 Linear / Vercel / Raycast 控制台）：左侧导航、留白充足、8px 网格、单一强调色、中性灰、用细边框而不是重阴影、10–12px 圆角卡片、清楚的空状态（一个主操作）、状态胶囊、等宽字体显示 id/令牌、toast、表单放在弹窗或抽屉里、骨架屏加载。

### 1.2 颜色

**品牌色 Nexus teal `#0b7285`（深色模式 `#3bc9db`）。** 理由：
1. 处在“AI 工具常见的靛蓝/紫”（Linear、Claude 系）和“在线绿”（ChatGPT、我们的“在线”状态）之间，强调色永远不会被误读成状态；
2. 青色隐含“连接/网络”的意思，契合 Nexus；
3. 白字在其上 5.6:1，作为链接色 `#0a6879` 在白底上 6.4:1，均过 WCAG AA。

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--bg` | `#f6f7f9` | `#0d1014` | 页面底色 |
| `--surface` / `-2` / `-3` | `#fff` / `#f3f4f7` / `#e9ecf0` | `#14181d` / `#1a1f26` / `#232933` | 卡片 / 悬停与次级填充 / 骨架高光 |
| `--border` / `-strong` | `#e3e6eb` / `#cdd2da` | `#262c35` / `#353d48` | 卡片分隔 / 输入框、次级按钮 |
| `--text` / `-2` / `-3` | `#14181f` / `#4f5866` / `#616a7a` | `#e7eaee` / `#a7b0bd` / `#8d97a5` | 正文 / 说明 / 弱化（均 ≥ 4.5:1） |
| `--accent` 系 | `#0b7285`，hover `#095f6f`，`--accent-subtle #e4f3f5`，`--accent-text #0a6879` | `#3bc9db`，`--text-on-accent #04252b` | 主按钮、选中导航、链接 |
| 状态 `ok/warn/bad/info/neutral` | 各有 `-bg`、`-border` | 半透明底 | 胶囊、横幅、toast；文字对其底色均 ≥ 4.5:1 |

深色模式：`prefers-color-scheme: dark` 自动生效；`<html data-theme="light|dark">` 可强制（`setTheme()` 记在 localStorage，读写都包 try/catch）。

### 1.3 排版、间距、形状

- 字体：`-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif`；等宽 `ui-monospace, "Cascadia Mono", "SF Mono", …`。正文 14px / 1.55；字号阶梯 12、13、14、16、20、24、30。
- 间距只用 `--sp-1…8` = 4、8、12、16、24、32、48、64。卡片内边距 16/24，页面左右 32（平板 24、手机 16）。
- 圆角：控件 8，卡片 12，弹窗 14，认证卡 16，胶囊全圆。控件高 36（小号 28，大号 44）。
- 阴影只给浮层（菜单、toast、弹窗）；卡片只用 1px 边框（浅色带极淡的 `--shadow-xs`）。

### 1.4 组件（`ui.css` 类名）

| 组件 | 类名 / 函数 | 规则 |
|---|---|---|
| 应用外壳 | `.app` `.sidebar` `.nav-item(.active)` `.nav-badge` `.nav-section` `.sidebar-foot` `.sidebar-status` `.main` `.topbar` `.page` `.page-head` / `renderShell()` | 侧栏 232px 固定；顶栏 56px 吸顶（滚动后出现底边）；内容最大宽 1120（`.page.wide` 1440）。顶栏左侧是面包屑，右侧放页面级图标按钮 |
| 响应式 | — | ≥1024：完整侧栏；769–1023：64px 图标栏；≤768：隐藏侧栏，底部标签栏 `.tabbar`（导航项 `primary: true` 的 4 个 + “更多”，“更多”把侧栏以抽屉形式打开）；弹窗和抽屉变成底部弹层 |
| 卡片 | `.card` `.card-head` `.card-body` `.card-foot` `.card.flush` `.card.accent` `a.card.interactive` | 卡片标题 14px 半粗；卡片内列表用 `.list.inset` 贴边 |
| 数字卡 | `.stat` `.stat-label` `.stat-value` `.stat-meta` | 概览页 4 列（平板/手机 2 列），可点击跳到对应页 |
| 列表 | `.list` `.list-item(.clickable)(.wrap-sm)` `.title` `.sub` `.meta` | 行高 ≥ 60；手机上 `.wrap-sm` 把右侧操作换到第二行 |
| 表格 | `.table-wrap` `.table(.stack-sm)` `.cell-main` `td.actions` `td.num` | 手机上 `.stack-sm` 把每行变成卡片，`td[data-label]` 显示为“标签 值” |
| 状态胶囊 | `.pill.ok/.warn/.bad/.info/.accent`（`.plain` 去掉圆点）/ `pill(state)` | 词汇见 §1.5，不要自造文案 |
| 标签 | `.tag(.mono)` | 版本、来源、传输方式等静态属性 |
| 按钮 | `.btn` `.btn-primary` `.btn-ghost` `.btn-danger(.solid)` `.btn-sm/-lg/-icon/-block` `.btn-group` | 每个视图最多一个主按钮；危险操作走 `confirmDialog({danger:true})`；纯图标按钮必须有 `aria-label` |
| 表单 | `.form` `.form-row` `.field` `label` `.hint` `.error-text` `.optional` `.input(.mono)(.input-lg)` `.select` `.textarea` `.input-group` `.input-icon` `.check` | 标签在上；错误时 `aria-invalid="true"` + `.error-text`；密钥字段一律 `type=password`，只写不读 |
| 开关 | `<label class="switch"><input type="checkbox" role="switch"><span></span>文字</label>` | 立即生效的设置用开关，需要“保存”的用复选框 |
| 分段 / 选择卡 | `.segmented(.block)` + `button[aria-pressed]` 或 `label>input:radio`；`.choices` `.choice` | 2–4 个互斥选项；带说明的选项用选择卡 |
| 设置行 | `.setting` `.setting-text` `.setting-title` `.setting-desc` | 左文字右控件 |
| 标签页 | `.tabs` `.tab[aria-selected]` `.count` | 同一数据集的过滤视图 |
| 横幅 | `.banner(.ok/.warn/.bad/.accent)` `.banner-body` `.banner-title` `.banner-actions` | 页面级提示，最多一个主操作 |
| 令牌/代码 | `.token(.multiline)(.lg)` / `tokenField(value,{masked})`；`.code-block`、`.log`、`kbd` | id、地址、哈希、邀请码一律等宽 + 复制按钮 |
| 空状态 | `.empty` / `emptyState({icon,title,text,action})` | 一句话说明 + 一个主操作 |
| 骨架 | `.skeleton(.title/.line/.block/.circle)` / `skeletonRows(n)`；`.spinner` | 首次加载显示骨架；按钮提交时显示 spinner |
| 弹窗 / 抽屉 | `modal({...})` / `drawer({...})`，`.overlay` `.modal` `.drawer` | 短表单（≤4 字段）用弹窗，长表单和详情用右侧抽屉；Esc、点遮罩关闭；焦点圈定在面板内 |
| Toast | `toast(msg,{type,title,action})` | 右下角（手机在标签栏上方），3.6s 自动消失，悬停暂停 |
| 步骤 | `.steps` `.step(.done/.current)` | 入门清单、安装说明 |
| 认证页 | `.auth` `.auth-card(.wide)` `.auth-head` `.auth-foot` `.connect-visual` `.scope-list` | 登录、授权、首次加入 |
| 收件箱 | `.inbox(.show-thread)` `.inbox-list` `.thread-item(.active/.unread)` `.thread-view` `.message` `.ask-card` `.composer` | 手机上一次只显示列表或线程 |
| Logo | `logoSVG(size)` / `brand({sub})` | 强调色圆角方块上一个中心节点连三个节点；颜色取令牌，自动适配深色 |

### 1.5 状态词汇（`STATUS` in `ui.js`，所有页面共用）

| state | 文案 | 色调 | 用在 |
|---|---|---|---|
| `online` / `offline` / `revoked` | 在线 / 离线 / 已吊销 | ok / neutral / bad | 设备 |
| `ok` / `needs_login` / `error` / `connecting` | 正常 / 需要登录 / 错误 / 连接中 | ok / warn / bad / info | 连接器、云端连接 |
| `device_offline` / `pending_device_upgrade` / `host_revoked` | 设备离线 / 设备需升级 / 宿主已吊销 | neutral / warn / bad | 固定到设备的连接器 |
| `running` / `starting` / `stopped` / `disabled` / `crashed` / `unhealthy` / `conflict` / `external` | 运行中 / 启动中 / 已停止 / 已禁用 / 已崩溃 / 不健康 / 端口冲突 / 外部进程 | ok / info / neutral / neutral / bad / warn / bad / info | 插件（supervisor 的 `status`） |
| `needs_config` / `installing` / `update_failed` | 需要配置 / 安装中 / 更新失败 | warn / info / bad | 插件（M8） |
| `unused` / `used` / `expired` | 未使用 / 已使用 / 已过期 | accent / neutral / neutral | 邀请 |
| `joined` / `not_joined` | 已加入 / 未加入 | ok / neutral | 本机云端连接 |

### 1.6 通用状态与交互约定

- **加载**：首屏用骨架（列表 3 行、数字卡 4 个），不要用整页 spinner；刷新数据时保留旧内容。
- **空**：`emptyState`，标题说“还没有 X”，正文一句话说明 X 是什么，主操作就是创建 X。
- **错误**：区块级用 `errorState(err, retry)`；操作级由 `fetchJSON` 自动 toast；401 触发 `anx:unauthorized`，云端页面跳 `/auth/login?next=<当前 hash>`，本机页面不会出现 401。
- **实时性**：列表页在可见时每 10s 轮询（`document.hidden` 时暂停）；插件日志走现有 SSE。
- **写请求**：都带 `X-ANX: 1`（`fetchJSON` 自动加）；成功 toast 用动词过去式（“已保存”“已吊销”）。
- **无障碍**：所有交互元素可 Tab 到达并有可见焦点环；图标按钮有 `aria-label`；颜色不是唯一的状态信号（胶囊都有文字）；`prefers-reduced-motion` 时动画降为 1ms。
- **文案**：中文，句末不加句号的短标签；技术名词（MCP、OAuth、agent_id）保留英文。

## 2. 认证相关页面

### 2.1 云端登录页 `GET /auth/login`（样稿 `cloud-login.html`）

- 目的：owner 用**与 OAuth 授权相同的 owner 密码**登录网页控制台。
- 布局：`.auth` 居中卡片：logo、标题“登录 Agent Nexus”、说明、密码框（`name=password`）、“保持登录 30 天”、主按钮“登录”；卡片下方显示网关域名和“下载客户端”链接（下载页对未登录用户也可见，§3.7）。
- 只配置了静态 bearer 的网关：字段换成“网关令牌”（`name=token`），行为同今天的 `_token_login`。
- 错误：密码错误时同页显示 `.error-text`“密码不正确”，状态码 401；失败次数按 IP 计数，超限返回 429“尝试过于频繁”（与 mcpkit 授权页同一套计数）。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/auth/login?next=/#/devices` | — | HTML（已登录则 303 到 `next`） |
| POST | `/auth/login` | 表单 `password`（或 `token`）、`next`、`remember` | 303 → `next`（只接受以 `/` 开头的同站路径），`Set-Cookie: __Host-anx_inbox=<access token>; HttpOnly; Secure; SameSite=Strict; Path=/`；`remember` 时 max-age 30 天 |
| POST | `/auth/logout` | 头 `X-ANX: 1` | 204，清 cookie |

- 实现：校验 `PasswordOAuth.password_hash`，再用 `provider.issue("anx-inbox", scopes)` 签发令牌写入 cookie——与 PWA 现有 PKCE 登录得到的是同一种令牌，`InboxWeb.owner()` 无需改动。令牌有效期：勾选“保持登录”30 天（cookie `max-age` 同），否则 12 小时的会话 cookie。登录页的 `Referrer-Policy` 是 `strict-origin`（`no-referrer` 会让浏览器的表单 POST 带 `Origin: null`，被网关的来源保护拒绝）。`/inbox/login` 保持原来的 OAuth PKCE 流程（M17 的验收测试依赖它），两条路径写的是同一个 cookie。

### 2.2 站点根目录与会话

- `GET /`：已登录 → 云端控制台外壳 `console.html`（hash 路由，§3）；未登录 → 302 `/auth/login`。替换今天“`/` 重定向到 `/inbox/`”（测试 `test_ingress_auth_and_reconnect_replaces_instance` 中对此的断言随之更新）。
- 所有 `/api/*`（`/api/downloads` 除外）接受两种 owner 凭据：`Authorization: Bearer <MCP 令牌>`（今天 `owner()` 的规则）或上面的 cookie（写请求另需 `X-ANX: 1`）。未登录返回 `401 {"error":"unauthorized"}`。

### 2.3 OAuth 授权页（mcpkit `oauth.py` 的 `PasswordOAuth.page()`，样稿 `oauth-consent.html`）

- 目的：ChatGPT / Claude 等连接器添加 Agent Nexus 时的授权确认。**功能和表单字段不变**：`POST /login`，字段 `flow`、`csrf`（hidden）、`password`，提交按钮 `name=decision value=approve|deny`；cookie 绑定、CSRF、Origin、频率限制、CSP（含对已校验回调追加的 `form-action`）全部保持。
- 变化只在呈现：
  - 标题 `授权 <客户端名> 访问 <title>`；客户端名取 DCR 的 `client_name`（缺省“此客户端”）；`title` 来自 auth 配置，**网关配置把 `auth.title` 设为 `Agent Nexus`**（今天默认是服务名 `workspace`，所以显示“连接 Workspace MCP”）。
  - 顶部 `.connect-visual`：客户端占位图标 ↔ Agent Nexus logo（内联 SVG）。
  - `.scope-list`：“授权后返回 <回调主机>”、“权限 <scopes>”、可选的 consent 文案。
  - 密码框标签“Agent Nexus 授权密码”，提示“即网关的 owner 密码”。
  - 按钮：DOM 中 **approve 在前**（回车即授权，与今天一致），用 `flex-direction: row-reverse` 显示为“拒绝 | 授权”。
  - 样式全部内联在一个 `<style>` 里（CSP 不允许外链），无脚本；深色模式用 `prefers-color-scheme`。
  - `reject_login()` 的错误页套同一张 `.auth-card`，保留诊断代码。
- mcpkit 是通用 SDK：品牌元素（logo、产品名）由配置项 `auth.brand`（默认无 logo、用 `title`）提供，网关传入 Agent Nexus；其他 mcpkit 服务外观不变但同样受益于新样式。

## 3. 云端控制台（网关站点根目录）

导航（`renderShell`）：概览 `#/`、设备 `#/devices`、连接器 `#/connectors`、插件 `#/plugins`、收件箱 `#/inbox`、—其他— 下载 `#/downloads`、设置 `#/settings`；底部状态“网关在线 · v<version>”，“退出登录”。手机标签栏：概览、设备、连接器、收件箱、更多。收件箱导航项显示未读数徽标（`/api/inbox/summary` 的 `unread + open_asks`）。

### 3.1 概览 `#/`（样稿 `cloud-overview.html`）

- 目的：一眼看到网关是否健康、下一步该做什么。回应“这不是一个云平台”：首页就是整个平台的入口。
- 布局：页面头（问候 + 一句状态总结；按钮“下载客户端”“添加连接器”）→ 需要处理的横幅（连接器需要登录、设备被吊销、数据库超警戒，按严重度只显示最重要的 1 条，其余合并成“另有 N 项”）→ 4 个数字卡（在线设备 x/y、连接器（需要登录数）、未读邮件、待回答）→ 左列：“开始使用”清单（4 步，全部完成后整卡隐藏）+ 设备简表；右列：MCP 地址卡（可复制 `<public_url>/mcp`、已授权客户端、版本、工具数）+ 最近收件箱 3 条。
- 状态：加载时数字卡与列表为骨架；`summary` 失败 → 整页 `errorState`。
- API：`GET /api/console/summary`

```json
{
  "gateway": {"version": "0.2.0", "public_url": "https://hub.example.com", "mcp_url": "https://hub.example.com/mcp",
              "features": ["hub_call/1", "..."], "db_warning": false},
  "devices": {"online": 2, "total": 3},
  "connectors": {"total": 4, "needs_login": 1, "error": 0},
  "inbox": {"unread": 3, "open_asks": 1},
  "tools": {"total": 148, "connectors": 37},
  "clients": [{"client_id": "…", "name": "ChatGPT", "kind": "chatgpt", "last_seen_at": 1762760000}],
  "setup": {"installer_available": true, "device_joined": true, "client_authorized": true, "connector_added": false},
  "alerts": [{"kind": "connector_needs_login", "target": "aliyun", "title": "阿里云 MCP 需要重新登录", "action": "login"}]
}
```

### 3.2 设备 `#/devices`（样稿 `cloud-devices.html`，`?open=invite`）

- 目的：查看、命名、吊销设备；生成邀请码（决策 D3、D6）。
- 布局：页面头 + 主按钮“邀请新设备”→ 设备表（设备〔图标按 form/platform、显示名、主机名、系统〕、状态、agent_id、版本〔落后于最新版时 `可更新`〕、入网方式〔令牌映射/邀请码〕、最后在线、操作：备注名、更多菜单〔查看插件、复制 agent_id、吊销〕）→ “未使用的邀请”列表（备注、id、创建时间、过期时间、作废）。
- 邀请弹窗两步：①表单（备注 可选、有效期 select：1 小时/24 小时/7 天、指定 agent_id 可选）→ ②结果（成功横幅“只显示这一次”、邀请码 `tokenField(multiline)`、`agentnexus://` 链接、过期时间、“还没装客户端？下载”链接）。
- 吊销：`confirmDialog({danger})`“吊销后该设备立即断开，需要新的邀请才能重新加入”；按 `agents` 映射入网的设备返回 409，弹窗显示“需要从 gateway.env 删除该 token”。
- 空状态：“还没有设备” + 主操作“邀请新设备”，次操作“下载客户端”。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/devices` | — | `{"devices":[Device]}`（已存在，补字段） |
| PUT | `/api/devices/{device_id}` | `{"alias": "工作站" \| null}` | `{"device": Device}`（已存在） |
| POST | `/api/devices/{device_id}/revoke` | — | `{"device": Device, "closed": true}`；409 `{"error":"…","code":"agents_map"}` |
| GET | `/api/invites` | `?state=unused` | `{"invites":[Invite]}` |
| POST | `/api/invites` | `{"label":"家里的台式机","ttl_s":86400,"agent_id":null}` | 201 `{"invite": Invite, "code":"anx1.<base64url(json{hub,id,secret})>", "link":"agentnexus://enroll?invite=anx1.…"}`（明文只此一次） |
| DELETE | `/api/invites/{invite_id}` | — | `{"invite": Invite}`（state 变为 `revoked`） |

```json
// Device（在今天 devices.describe() 的基础上补 version、enrolled_via、last_seen_at、revoked、plugins_count）
{"device_id": "pc-main", "display_name": "工作站", "alias": "工作站", "hostname": "DESKTOP-7Q2K",
 "online": true, "platform": "windows", "form": "pc", "os": "Windows 11", "version": "0.2.0",
 "enrolled_via": "agents_map|invite", "last_seen_at": 1762760000, "revoked": false, "plugins_count": 6,
 "features": ["…"], "capabilities": {}, "agent_runtimes": [], "workdirs": [{"name": "projects", "path": "…"}]}
// Invite
{"id": "inv_4fK9q2", "label": "家里的台式机", "created_at": 1762760000, "expires_at": 1762846400,
 "state": "unused|used|expired|revoked", "used_by": null, "agent_id": null}
```

本机控制台经 `hub_call device_admin` 调同一组操作：`op` = `list`、`set_alias`、`revoke`、`invite_create`、`invite_list`、`invite_revoke`，参数与上表请求体相同，返回体相同。

### 3.3 插件（按设备）`#/plugins`、`#/plugins/<device_id>`

- 目的：在云端看到每台设备装了哪些插件、是否在运行，并远程启停。回答“怎么安装插件”：页首说明“插件库里的私有插件保存在网关上，一键安装到任意在线设备”。
- 插件库（design §6.12，决策 D4r；当前实现）：页首主按钮“上传插件包”（选 zip，`POST /api/plugin-repo`；同版本已存在时 confirm“覆盖已有的版本？”后带 `?replace=1`；被拒绝时弹窗列出 `findings` 的文件和规则，从不显示匹配到的值）。“插件库”卡片每个插件一行：首字头像、title、name `tag.mono`、最新版本 `tag.mono`、“N 个版本”、描述、“大小 · 文件数 · 上传于 …”，操作为删除（菜单里按版本删除，confirm 说明“已经装到设备上的插件不受影响”）和主按钮“安装到…”。空库：`emptyState({icon:"package", title:"插件库还是空的"})` 加“上传插件包”。右侧卡片“怎么把插件放进插件库”：pack → 上传/publish → 安装到设备，加“插件包只保存在网关上”的 `banner`。
- “安装到…”弹窗：版本 select（默认最新）+ 设备单选（`.choices.device-choices`；在线且声明 `plugin_repo/1` 的可选，离线/已吊销/旧版灰显并写原因“离线”“客户端版本太旧，先更新”）。点“安装”后 `POST /api/devices/{id}/plugins/install`，弹窗切到进度：四步 `.steps`（下载插件包 → 校验 sha256 → 安装依赖 → 启动并检查健康，按设备日志推进，失败的一步标 `.step.failed`）+ `.log.install-log` 实时日志（每 0.7 秒 `GET …/jobs/{job_id}?since=N`）。结果 `banner`：running → ok“已安装并在运行”；needs_config → warn“已安装，需要在设备上填写配置”；失败 → bad“安装失败，已回滚”+ 第一行错误。弹窗打开时页面不自动刷新。`#/plugins?install=<name>` 直接打开该插件的“安装到…”，`#/plugins/<device_id>` 时默认选中该设备。
- 布局：左侧设备选择（手机为顶部 select），右侧该设备插件列表（同 §4.2 的行样式，只读字段 + 开关 + 启动/停止/重启）。每行“安装到其他设备”按钮在 relay（M9）完成前禁用并显示 `tag`“即将推出”。
- 状态：设备离线 → `emptyState({icon:"wifi-off", title:"设备离线", text:"设备上线后才能查看插件"})`；旧版设备（未声明 `plugins/1`）→ “此设备的客户端版本过旧，升级到 0.2.0 以上后可在这里管理插件”。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/devices/{device_id}/plugins` | — | `{"device_id","online":true,"plugins":[PluginSummary]}`；离线 409 `{"error":"device offline","code":"device_offline"}`；旧设备 409 `code:"unsupported"` |
| POST | `/api/devices/{device_id}/plugins/{name}/{action}` | action = `enable`/`disable`/`start`/`stop`/`restart` | `{"plugin": PluginSummary, "message": "…"}` |
| POST | `/api/devices/{device_id}/plugins/install` | `{"name":"ones","version":"0.2.0"?,"enable":true?}`（缺省 version = 插件库里的最新版） | 202 `{"job_id","device_id","display_name","name","version"}`；网关不认识的设备 404 `unknown_device`“网关上没有这台设备：X”；离线 409 `device_offline`“X 不在线：等它连上网关后再安装”；旧设备（没有 `plugin_repo/1`）409 `unsupported_device`“X 的 Agent Nexus 版本太旧……先把它更新到最新版本”；平台不符 409 `unsupported_platform`；库里没有 404 `unknown_package` |
| GET | `/api/devices/{device_id}/plugins/jobs/{job_id}` | `?since=N` | `{"id","state":"running|done|failed","log":[…],"log_total","result":{"name","version","state","message"}?,"error","code"}` |
| GET | `/api/plugin-repo` | — | `{"packages":[{"name","version","title","description","sha256","size","files","uploaded_at","uploaded_by","platforms","latest"}],"max_package_mb"}` |
| POST | `/api/plugin-repo` | zip 请求体或 multipart `file`；`?replace=1` | 201/200 `{"package":{…,"replaced"}}`；409 `exists`；413 `too_large`；422 `secrets_found`（`findings:[{path,rule}]`）/`invalid_manifest`/`invalid_archive` |
| DELETE | `/api/plugin-repo/{name}/{version}` | — | `{"removed":true,"name","version"}` |
| GET | `/api/plugin-repo/{name}/{version}/download` | owner 或设备令牌（`Authorization: Bearer`） | zip 流，`X-ANX-SHA256`；其余 401 |

实现：网关向设备发反向 RPC `plugin_admin {op, name?}`（新特性 `plugins/1`，旧 agent 不会收到），设备 manager 调本机 §4.2 的同名逻辑。`PluginSummary` 是 §4.2 `Plugin` 去掉路径、命令、日志位置后的子集。

### 3.4 连接器 `#/connectors`（样稿 `cloud-connectors.html`，`?open=add-connector`）

- 目的：添加、编辑、测试、登录第三方 MCP（决策 D10，design §7）。回答“没看见哪里加第三方 MCP”。
- 布局：页面头 + 主按钮“添加连接器”→ 表（连接器〔名称、URL 或命令，等宽截断〕、状态、传输〔`自动 → HTTP` 显示探测结果〕、鉴权、运行位置〔网关/设备名〕、工具数、操作：`needs_login` 时主按钮“登录”，否则“测试”；更多菜单：编辑、退出登录、复制名称、迁移到网关〔device-local 条目〕、删除）→ 说明横幅（工具如何出现在 `toolsearch`/`tooluse`）。
- 添加/编辑抽屉：URL 或命令 + “探测”→ 探测结果横幅（自动选好传输与鉴权）→ 名称（`[a-z0-9-]`，提示“工具显示为 hub/<名称>”）、显示名 → 运行位置选择卡（网关〔推荐〕/固定到设备 + 设备 select；粘贴的是 stdio 命令时只能选设备）→ 传输分段（自动/HTTP/SSE；命令时固定 stdio）→ 鉴权分段（无/Bearer/Header/OAuth，各自字段；密钥只写不读，编辑时显示“已设置，留空保持不变”）→ 高级（暴露方式 search/direct、超时、工具 allow/deny）。底部：左“测试连接”，右“取消”“保存并连接”。保存 OAuth 连接器后自动发起登录。
- OAuth 登录：`POST …/login` 拿到 `authorize_url`，`window.location` 跳过去；回调 `/oauth/connectors/callback/<name>` 完成后 303 到 `/#/connectors?login=<name>&result=ok|error`，页面据此 toast。
- 空状态：“还没有连接器”，正文“粘贴任意 HTTP / SSE MCP 地址，网关会自动识别传输方式和登录方式”，主操作“添加连接器”。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/connectors` | — | `{"connectors":[Connector]}` |
| POST | `/api/connectors` | `ConnectorInput` | 201 `{"connector": Connector}`；名称冲突 409 |
| PUT | `/api/connectors/{name}` | `ConnectorInput` 的部分字段（省略的密钥 = 保持） | `{"connector": Connector}` |
| DELETE | `/api/connectors/{name}` | — | 204 |
| POST | `/api/connectors/probe` | `{"url":"…"}` 或 `{"command":["npx","-y","…"]}`，`"run_on":"gateway"` | `{"transport":"http|sse|stdio","needs_oauth":true,"auth_hint":"oauth|bearer|none","server":{"name":"…","version":"…"},"tools_count":23,"error":null}` |
| POST | `/api/connectors/{name}/test` | — | `{"ok":true,"tools_count":23,"latency_ms":182,"error":null}` |
| POST | `/api/connectors/{name}/login` | — | `{"authorize_url":"https://…"}` |
| POST | `/api/connectors/{name}/logout` | — | `{"connector": Connector}`（state → `needs_login`） |
| GET / PUT | `/api/connectors/export`、`/api/connectors/import` | design §7.2 的整表结构 | 批量导入导出（`anx connector export/import` 用同一结构） |

```json
// ConnectorInput
{"name": "cloudflare", "title": "Cloudflare", "url": "https://…/sse",            // 或 "command": ["npx", "-y", "…"], "env": {}, "cwd": "."
 "transport": "auto|http|sse|stdio", "run_on": "gateway|device:<agent_id>",
 "auth": {"type": "none"}
       | {"type": "bearer", "token": "<只写>"}
       | {"type": "headers", "headers": {"Authorization": "<只写>", "X-Region": "cn-hangzhou"}}
       | {"type": "oauth", "scopes": [], "client": {"mode": "dynamic"} | {"mode": "static", "client_id": "…", "client_secret": "<只写>"}},
 "expose": "search|direct", "timeout_s": 120, "enabled": true, "tools": {"allow": [], "deny": []}}
// Connector（GET 不返回任何密钥值）
{"name": "cloudflare", "title": "Cloudflare", "url": "https://…/sse", "command": null,
 "transport": "sse", "transport_detected": "sse", "run_on": "gateway", "run_on_display": "网关",
 "auth": {"type": "oauth", "secret_set": true, "header_names": []},
 "source": "registry|device-local", "state": "ok|needs_login|error|device_offline|pending_device_upgrade|host_revoked|disabled",
 "error": null, "tools_count": 23, "mcp_id": "hub/cloudflare", "fingerprint": "3f9c0a1b2c4d",
 "expose": "search", "timeout_s": 120, "enabled": true, "updated_at": 1762760000}
```

本机控制台经 `hub_call connector_registry {op: list|create|update|delete|probe|test|login|logout, …}` 读写同一个注册表，参数与返回同上（`login` 返回的 `authorize_url` 由本机在默认浏览器打开）。

### 3.5 收件箱 `#/inbox`、`#/inbox/<thread_id>`（样稿 `cloud-inbox.html`）

- 目的：查看 agent 发给 `me` 的邮件与提问，以 `me` 回复（决策 D11）。今天的 `/inbox/` PWA 改用同一套组件。
- 布局：页面头（视角 select：`me` / 任一地址〔只读〕，地址按设备分 `<optgroup>`（标签为设备显示名），组内与组间都按最近活动排在前面；每项写成“标题（没有标题时用 workdir 目录名）— 名字 · 最近活动 · N 未读（只读）”，鼠标悬停显示完整地址；旧的 `anon-…` 地址只有在有邮件（已读或未读，`address_view.mail` > 0）或正被查看时才列出（设计 §8.2）；select 最宽 100%，手机上截断显示；主按钮“写邮件”→ 抽屉）→ `.inbox` 双栏：左线程列表（搜索框；“待回答”置顶；未读加粗 + 蓝点；发件人、相对时间、主题、摘要）；右线程视图（标题 + 状态胶囊、参与者、消息、`.ask-card`〔推荐项第一并标“推荐”，其后其他选项，最后“其他（自由填写）”〕、底部 `.composer`）。手机：列表与线程二选一，线程顶部“返回”。
- 空状态：“收件箱是空的”，正文“Agent 用 mail_send 发给 me 或调用 ask_user 时会出现在这里”。
- 地址设置（线程头更多菜单 → 抽屉）：唤醒权限 `bypass|full-access|inherit`、`wake_policy`、resume_id 纠正。
- API（已存在，`inbox.py` 的操作，cookie 或 bearer）：

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/inbox/summary` | — | `{"unread":3,"open_asks":1}` |
| GET | `/api/inbox/threads` | `?viewer=me` | `{"viewer","viewer_display","threads":[{"thread_id","subject","participants","unread","last_at","snippet","has_open_ask"}]}` |
| GET | `/api/inbox/threads/{thread_id}` | `?viewer=me&mark_read=1` | `{"thread_id","participants","messages":[{"id","from","from_display","to","subject","body","kind","created_at","state","ask"?}]}` |
| POST | `/api/inbox/send` | `{"to":["codex@pc-main"],"subject","body","reply_to"?}` | `mail_send` 的返回 |
| GET | `/api/inbox/asks` | `?state=open` | `{"asks":[…]}` |
| GET | `/api/inbox/asks/{ask_id}` | — | `{"ask":…}` |
| POST | `/api/inbox/asks/{ask_id}/answer` | `{"choice":"…"}` 或 `{"text":"…"}` | `{"ask":…,"state":"answered"}`；已答 409 |
| GET | `/api/inbox/addresses` | `?include_ended=1` | `{"addresses":[{"address","address_display","name","name_source":"explicit\|auto\|anon","title","kind","agent_id","device_display","workdir","presence","last_seen_s_ago","unread",…}]}` |
| PUT | `/api/inbox/addresses/{address}` | `{"wake_permission":"bypass","wake_policy":…,"resume_id":…}` | `{"address":…}` |

前端共用一个 ES 模块 `/ui/inbox.js`（由今天的 `static/inbox.js` 改写为基于 `ui.js` 的 `mountInbox(target, {api, base})`），云端控制台、PWA、本机控制台都挂它；PWA 仍在 `/inbox/`（manifest、service worker 作用域不变）。

### 3.6 设置 `#/settings`

- 卡片“网关”：public_url、MCP 地址（复制）、版本、状态目录、数据库大小（超警戒显示 warn 胶囊）。
- 卡片“已授权的客户端”：DCR 注册的客户端列表（名称、类型、注册时间、最后使用），每行“撤销授权”（confirm）。
- 卡片“邮箱”：保留天数、未读升级阈值（1h 再唤醒 / 24h 提醒 me / 30 天过期），开关 + 数字输入，“保存”。
- 卡片“邀请”：默认有效期、最长有效期。
- 卡片“外观”：主题 分段（跟随系统/浅色/深色，仅本浏览器）。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/settings` | — | `{"gateway":{"public_url","mcp_url","version","state_dir","db_mb","db_warning"},"auth":{"mode":"oauth|bearer"},"mailbox":{"retention":{"read_days":90},"escalation":{"rewake_after_s":3600,"notify_owner_after_s":86400,"expire_after_days":30}},"invites":{"default_ttl_s":86400,"max_ttl_s":604800}}` |
| PUT | `/api/settings` | 同结构的 `mailbox`、`invites` 子集 | 同 GET；写入网关状态库的覆盖层（不改 `gateway.json`），重启后仍生效 |
| GET | `/api/oauth/clients` | — | `{"clients":[{"client_id","client_name","kind":"chatgpt|claude|inbox|other","created_at","last_used_at"}]}` |
| DELETE | `/api/oauth/clients/{client_id}` | — | 204（吊销该客户端所有令牌；内置 `anx-inbox` 不可删，409） |

### 3.7 下载 `#/downloads`（样稿 `cloud-downloads.html`）

- 目的：回答“没看见哪里可以下载安装包”。**未登录也能访问**：`/downloads` 是一个公开的独立页面（同样式，无侧栏，顶部 logo + “登录控制台”），登录后在控制台内以 `#/downloads` 显示同样内容。
- 布局：主卡（Windows 安装包：版本胶囊、平台、大小、发布日期、主按钮“下载安装包”、SHA-256 `tokenField`）→ 左“安装之后”3 步（运行安装包〔未签名，SmartScreen 提示怎么办〕→ 用邀请码加入〔链接到设备页邀请〕→ 在插件页打开开关启用插件）、右“其他平台”（Linux .deb、网关 zip）→ 历史版本表。
- 空状态（网关没有可提供的安装包）：“还没有可下载的安装包”，正文说明配置 `downloads.dir` 或 `downloads.release_url`。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/downloads` | —（公开） | 见下 |
| GET | `/downloads/{filename}` | —（公开） | 文件（`Content-Disposition: attachment`），或 302 到 `release_url` 下的同名文件 |

```json
{"latest": "0.2.0",
 "items": [{"id": "windows-x64", "platform": "windows", "arch": "x64", "kind": "desktop", "label": "Agent Nexus for Windows",
            "version": "0.2.0", "filename": "agent-nexus_0.2.0_x64-setup.exe", "size": 44669337,
            "sha256": "<64 hex>", "url": "/downloads/agent-nexus_0.2.0_x64-setup.exe", "published_at": "2026-11-08", "latest": true},
           {"id": "linux-deb", "kind": "desktop", "...": "..."},
           {"id": "gateway-zip", "kind": "gateway", "...": "..."}]}
```

数据来源：网关配置 `downloads: {"dir": "<state_dir>/downloads", "release_url": null, "links": [{"label","url","description"}]}`；目录里放发布产物与 `SHA256SUMS`（M13 的 release 已产出），版本从文件名解析，响应另带 `links` 与 `release_url`。实现差异：网关不主动访问 `release_url`（不读远端 `latest.json`），本地没有的文件 302 到 `<release_url>/<文件名>`，`release_url` 本身作为一条链接显示；不带 `setup` 的 `.exe` 归为“Windows 便携版”（`kind: portable`）。部署时把新安装包复制进 `dir`（见 `deploy/gateway/README.md`）。

## 4. 本机控制台（`agent_nexus/static/index.html` + `web.py`，Tauri 窗口）

导航：概览 `#/`、插件 `#/plugins`、连接云端 `#/cloud`、收件箱 `#/inbox`、—其他— 设置 `#/settings`；底部状态显示云端连接（“已连接 hub.example.com” / “仅本机 · 未加入网关” / “连接错误”）。没有登录（`LocalOnly` 只允许本机）。所有写请求带 `X-ANX: 1`。

### 4.1 概览 `#/`

- 卡片：云端连接（状态胶囊、网关、agent_id、显示名；未加入时主操作“加入网关”）；插件统计（运行中/已禁用/需要处理，点击跳插件页对应标签）；应用（版本、有更新时“更新到 vX.Y.Z”主按钮、开机自启开关）；收件箱最近 3 条。
- 首次启动且 `cloud` 未配置、也没有私有 profile：直接跳 `#/cloud`（design §11.5）。
- API：组合已有接口 `GET /api/services`（含 `manager.cloud`、`manager.app_update`）、`GET /api/inbox/summary`、`GET /api/app/autostart`；不新增。

### 4.2 插件 `#/plugins`、`#/plugins/<name>`（样稿 `local-plugins.html`，`?open=install`）

- 目的：安装、启用、配置、查看日志。回答“安装完以后插件都被禁用了”：安装包的默认配置把业务插件写成 `enabled:false`（`config/client.example.json` 的 `services`），页面必须让“启用”一目了然。
- 布局：页面头（“重新扫描”、主按钮“安装插件”）→ 有禁用插件时 `.banner.accent`“N 个插件尚未启用 … 打开右侧开关即可启动”+“全部启用”→ 标签页 全部/运行中/需要处理 → 列表行：图标（插件 icon 或首字）、名称、版本 `tag`、来源 `tag`（内置/git/本地文件夹/压缩包/外部）、描述、状态胶囊、操作（日志、配置图标按钮；`needs_config` 时显示“配置”按钮）、启用开关（开 = `enabled:true` + 启动；关 = 停止 + `enabled:false`）。
- 详情抽屉 `#/plugins/<name>`：标签页 概览（kv：状态、端口、PID、运行时长、健康检查、来源、目录、命令〔等宽〕、环境变量名）/ 配置（manifest v2 schema 渲染的表单：分组、下拉、文件夹、secret〔只写〕；`autostart`、`autorestart`、端口）/ 日志（stdout/stderr 分段，`.log` 区块，SSE 实时追加，“复制”“打开日志文件夹”）。底部：启动/停止/重启、卸载（danger，confirm 中可勾选“同时删除数据”）。
- 安装弹窗：来源分段 文件夹 / Git / 压缩包；文件夹：路径 + “浏览…”（Tauri 下调用对话框，浏览器下隐藏）+ “链接而不是复制”；Git：仓库地址 + 分支/标签；压缩包：文件选择。输入后自动 `inspect`，显示“找到插件 X 版本 Y · 传输 · 端口 · 依赖”或错误；“安装后立即启用”开关（默认开）。提交后弹窗内显示进度日志，完成后 toast 并关闭。
- 空状态：“还没有插件”，主操作“安装插件”。
- 错误：端口冲突（`conflict`）在行内显示 message 与“改端口”按钮；`crashed` 显示最后退出码与“查看日志”。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/plugins` | — | `{"plugins":[Plugin]}`（`/api/services` 的用户视图；旧接口保留） |
| GET | `/api/plugins/{name}` | — | `{"plugin": Plugin}` |
| POST | `/api/plugins/inspect` | `{"source":"path","path":"D:/work/x"}` / `{"source":"git","url","ref"}` / multipart `file` | `{"name","version","title","transport","port","requirements":["python>=3.11"],"warnings":[],"exists":false}` |
| POST | `/api/plugins/install` | `{"source":"path","path","link":false,"enable":true}` / `{"source":"git","url","ref","enable":true}` / multipart `source=zip,file,enable` | 202 `{"job_id":"j_…"}` |
| GET | `/api/plugins/jobs/{job_id}` | — | `{"state":"running|done|failed","log":["…"],"plugin":Plugin?,"error":null}` |
| POST | `/api/plugins/{name}/enable` · `/disable` | — | `{"plugin": Plugin, "message":"…"}` |
| POST | `/api/plugins/enable` | `{"names":["ones","unity"]}`（“全部启用”） | `{"plugins":[Plugin],"errors":[{"name","error"}]}` |
| POST | `/api/plugins/{name}/start` · `/stop` · `/restart` | `?takeover=1` 可选 | `{"plugin": Plugin, "message":"…"}`（同今天的 `/api/services/{name}/{action}`） |
| GET | `/api/plugins/{name}/config` | — | `{"schema":{…manifest v2 config…},"values":{"token":{"set":true},"region":"cn"},"runtime":{"port":8418,"autostart":true,"autorestart":true}}` |
| PUT | `/api/plugins/{name}/config` | `{"values":{…},"runtime":{…}}`（secret 省略 = 不变，`null` = 清除） | `{"plugin": Plugin, "restart_needed": true}` |
| DELETE | `/api/plugins/{name}` | `?purge=1` 可选 | `{"removed":true,"data_kept":true}` |
| GET | `/api/plugins/{name}/logs` | `?source=stdout|stderr&lines=400` | `{"path","text","offset"}`（同今天） |
| GET | `/api/plugins/{name}/logs/stream` | 同上 | SSE（同今天） |
| POST | `/api/rescan` | — | `{"services":[…]}`（已存在） |

```json
// Plugin（supervisor.snapshot() 的超集；status 取 §1.5 词汇）
{"name": "ones", "title": "ONES", "description": "ONES 项目管理：工时、任务、需求", "version": "0.2.4",
 "status": "needs_config", "enabled": true, "autostart": true, "autorestart": true, "pending_restart": false,
 "source": {"type": "builtin|path|link|git|archive|external", "url": "…", "ref": "main", "path": "…"},
 "needs_config": true, "missing": ["token"], "has_config_schema": true,
 "icon_url": "/api/services/ones/icon?v=…", "port": 8412, "pid": null, "uptime_s": null,
 "health": {}, "message": "", "exit_code": null, "crashes": 0, "dir": "…", "command": ["…"], "env_keys": ["…"],
 "logs": ["stdout", "stderr"]}
```


实现补充（插件库，design §6.12）：加入网关后，安装弹窗的来源分段第一个是“插件库”（没加入时不显示），默认选中；列表 `.choices.repo-choices` 每个插件只显示最新版本：title、版本、状态 `tag`（未安装 / 已安装 / 已装 x / `tag.accent`“有新版本（已装 x）”）、描述、“大小 · 上传于 …”；选中后预览 `banner.ok`“将从插件库安装 X 版本 · sha256 前 12 位”，已装时注明“将更新到 / 重新安装（失败会自动回滚）”。连不上网关时显示 `banner.warn`“暂时看不到插件库”+ 原因。列表行：未安装且插件库里有包时副标题为“网关插件库里有 x.y.z，可以直接安装”，按钮变为主按钮“从插件库安装”；`source.type=hub` 的来源 `tag` 为“插件库”，库里有更高版本时加 `tag.accent`“有新版本 x.y.z”和“更新”按钮。`?open=install&source=repo` 直接打开插件库来源。手机宽度（≤480px）下四个来源分段排成两行两列（`.segmented.source-tabs`），不截断最后一个。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/plugin-repo` | — | `{"available":true,"packages":[{…网关字段…,"installed_version","installed","from_hub","update_available"}]}`；没加入网关 409 `not_joined`；没连上 409 `not_connected`；旧网关 409 `unsupported_gateway` |
| POST | `/api/plugin-repo/install` | `{"name","version"?,"enable"?}` | 202 `{"job_id","name","version"}`，进度同 `/api/plugins/jobs/{id}` |

实现补充（M8）：上传压缩包走 `POST /api/plugins/uploads?filename=x.zip`（请求体即文件，返回 `upload_id`），再以 `{"source":"archive","upload_id"}` 调 inspect/install；`GET /api/plugins/suggestions` 返回在旧版管理器目录里找到的插件文件夹（安装弹窗的一键路径）；`GET /api/plugins/jobs/{id}?since=N` 增量返回日志。v1 清单没有 `config`/`secrets` 的六个私有插件，由 `agent_nexus/plugins/schema.py` 的 `LEGACY_HINTS` 提供表单与必填项，清单自己声明后以清单为准。

### 4.3 连接云端 `#/cloud`（样稿 `local-join.html`）

- 未加入：标题“加入你的 Agent Nexus”→ 左卡：邀请码 textarea（等宽，接受 `anx1.…` 或 `agentnexus://enroll?invite=…`；粘贴后本地解码显示“将加入 <hub> · 本机显示为 <hostname>”横幅）、设备 ID（可选，占位为自动生成的 slug）、主按钮“加入网关”、“暂时仅本机使用”；右卡：“邀请码从哪里来？”3 步。`#/cloud?invite=…`（deep link）自动填入。
- 加入中：按钮 spinner；失败按 `code` 显示 `.error-text`：已过期、已使用、已作废、网关不可达（不自动重试同一邀请）。
- 已加入：卡片显示状态胶囊（已连接/连接中/错误/已被吊销）、网关、agent_id、显示名（“在云端控制台修改备注名”链接）、已协商特性、连接时长；操作“打开云端控制台”“退出网关”（danger confirm；删除本机 device-token 与 `cloud` 段）。被吊销（4401）：`.banner.bad`“此设备已被吊销，可用新邀请重新加入”并回到未加入表单。
- 私有入网版（已有 profile）：只读显示，不提供退出。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/cloud` | — | `{"state":"not_joined|connecting|connected|error|revoked|disabled","hub":"https://hub.example.com","agent_id":"pc-main","display_name":"工作站","features":[…],"since":1762760000,"error":null,"managed":false}`（今天的 `cloud_status` 补字段） |
| POST | `/api/cloud/join` | `{"invite":"anx1.…","agent_id":null}` | `{"state":"connected","hub","agent_id","display_name"}`；400 `invalid_invite`，409 `already_joined`，410 `expired|used|revoked`，502 `hub_unreachable` |
| POST | `/api/cloud/leave` | — | `{"state":"not_joined"}`；`managed` 时 409 |

### 4.4 收件箱 `#/inbox`

与 §3.5 同一组件；数据经已存在的本机 `/api/inbox/*`（`hub_call mail_local`）。网关未连接时显示 `emptyState({icon:"cloud-off", title:"收件箱需要连接网关", action:{label:"连接云端", href:"#/cloud"}})`。toast 链接 `/#/inbox/<thread_id>` 直接定位线程。

### 4.5 设置 `#/settings`

- “启动”：开机自启开关（`GET/PUT /api/app/autostart`，已存在）。
- “更新”：当前版本、最新版本、上次检查时间；“检查更新”（`POST /api/app/update/check`），有更新时主按钮“更新到 vX.Y.Z”（`POST /api/app/update/apply`；决策 D7：只有点击才安装）。数据 `GET /api/app/update`（已存在）。
- “本机”：根目录、配置文件、控制台端口（只读，等宽 + 复制）；“打开数据目录”“打开日志目录”（`POST /api/app/reveal {"what":"root|logs"}`，新增，经桌面壳 `--reveal` 动词；无桌面壳时 409 并显示路径）。
- “外观”：主题分段（跟随系统/浅色/深色）。
- “高级”：退出 Agent Nexus（`POST /api/manager/shutdown`，已存在，danger confirm）。

## 5. 页面与样稿对照

| 页面 | 样稿 | 截图（`agent-nexus-dist/screens/design/`） |
|---|---|---|
| 云端 概览 | `cloud-overview.html` | `cloud-overview-{light,dark,phone,phone-full}.png` |
| 云端 设备 + 邀请弹窗 | `cloud-devices.html`、`?open=invite` | `cloud-devices*.png`、`cloud-devices-invite-*.png` |
| 云端 连接器 + 添加抽屉 | `cloud-connectors.html`、`?open=add-connector` | `cloud-connectors*.png`、`cloud-connectors-add-*.png` |
| 云端 下载 | `cloud-downloads.html` | `cloud-downloads-*.png` |
| 云端 收件箱 | `cloud-inbox.html` | `cloud-inbox-*.png` |
| 登录 | `cloud-login.html` | `cloud-login-*.png` |
| OAuth 授权 | `oauth-consent.html`（无脚本） | `oauth-consent-*.png` |
| 本机 插件 + 安装弹窗 | `local-plugins.html`、`?open=install` | `local-plugins*.png`、`local-plugins-install-*.png` |
| 本机 连接云端 | `local-join.html` | `local-join-*.png` |

未出样稿的页面（云端插件、设置，本机概览、设置）由已有组件组合，布局按本文对应小节。
