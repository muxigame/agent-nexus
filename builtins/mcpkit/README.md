# mcpkit

`WorkSpace/mcp` 下**自建** MCP 服务的公共底座：统一用 FastMCP 4，一份配置文件，一套鉴权。
开源的 unity-mcp 和上游的 chrome-devtools-mcp 不改，只有我们自己的服务用它。

## 一个服务长什么样

```python
from mcpkit import main

def build(service):
    mcp = service.create_server(instructions="…")

    @mcp.tool
    def hello(name: str) -> str:
        return f"hi {name}"

    return mcp

def cli(argv=None) -> int:
    return main(__file__, build, version=__version__, argv=argv)
```

白拿的东西：命令行（`--transport http|stdio --host --port --check-config`，mcpm 传的就是这些）、
配置与密钥加载、鉴权、非本机地址无鉴权时拒绝启动、Host/Origin 校验、`/healthz`、
一行不含密钥的启动日志。

**传输默认 stdio**：客户端直接拉起进程时（Claude 桌面端不带任何参数）不该有人在监听端口；
要 HTTP 就在 `mcp-service.json` 的 command 里显式写 `--transport http`。

## 直接给 JSON schema 的工具

`@mcp.tool` 从函数签名推导 schema，这适合自己写的工具。当 **schema 本身就是契约**时用
`mcpkit.tools.SchemaTool`：ones-mcp 原样发布官方 ONES MCP 的 schema，好让同一个调用在两边都能用。

```python
from mcpkit.tools import SchemaTool, ToolFailure

server.add_tool(SchemaTool(name="get_issue", description=..., parameters=官方schema,
                           handler=handler, drop_nulls=True))
```

handler 返回任意可 JSON 化的值；抛 `ToolFailure(payload)` 则返回一个**错误结果**（模型能读、能据此改正），
而不是异常。同步 handler 自动放到线程里跑。`drop_nulls` 用于客户端给可选参数显式传 `null` 的情况。

## 配置在哪

| 位置 | 放什么 |
| --- | --- |
| `<root>/agent-nexus.json`（旧名 `mcp-manager.json`，只有旧文件时只读）的 `services.<名字>` | **唯一的编辑入口**：`public_url`、`auth`、`settings`，和 mcpm 自己的 `port`、`autostart` 放在一起 |
| `<服务>/mcp-service.json` | 服务自带的默认值（名字、端口等），被上面逐键覆盖 |
| `<root>/secrets/` | 密码和令牌。配置里只写名字，不写值 |

`auth` 和 `settings` 是**逐键合并**的（和 mcpm 合并 `env` 一样），所以在控制台改一个字段不会把其它字段清掉。
`<root>` 取 `$ANX_ROOT`（旧名 `$MCPM_ROOT` 仍然有效），否则向上找 `agent-nexus.json` 或 `mcp-manager.json`；清单里的 `aliases` 列出服务的旧名，`services.<旧名>` 仍然生效；都没有就用服务自己的目录，这样脱离管理器也能跑。

临时改一次设置不写文件：`MCPKIT_SETTINGS='{"profile":"full"}'`（ones 的端到端测试用它切工具集）。

业务数据（收款方式、watcher 数据库、二维码图片）不是配置，仍然放在各自服务里。

`auth` 段写错字段名会直接报错；顶层写错只打印警告，因为那一层还混着 mcpm 自己的键——
mcpm 以后加字段，不该让所有服务起不来。

## 鉴权三种模式

| `auth.mode` | 用途 | 关键字段 |
| --- | --- | --- |
| `none` | 仅本机 | 绑定非本机地址时**直接拒绝启动** |
| `bearer` | 本地客户端、脚本 | `bearer_secrets`（可多个，方便轮换） |
| `oauth` | ChatGPT 这类远端客户端 | `password_secret`、`public_url`，可同时保留 `bearer_secrets` |

`oauth` 是单人的授权服务器，从 payment-mcp 迁来并参数化，安全措施一条没减：

- 令牌只存 SHA-256 摘要，放在 SQLite 里；存储绑定一个 resource，换了公网地址就拒绝打开；
- 强制 PKCE S256，令牌绑定受众；回调地址必须命中白名单（默认只有 ChatGPT 的两个回调）；
- 授权表单同时绑定 CSRF 和 `__Host-` Cookie，带限速和严格 CSP；
- 请求体限长、规范化 HTTP Basic 客户端凭据、在回调上补 `iss`、日志不记 URL 和请求体。

`access_token_ttl_s` 设 `0` 表示访问令牌**永不过期**（不下发 `expires_in`）。刷新令牌本来就落盘，重启不掉线。

## 密钥

```bash
python -m mcpkit secret new workspace/bearer-token     # 随机 64 位
python -m mcpkit secret set ones/token                  # 值从标准输入读
python -m mcpkit secret check workspace/oauth-password
python -m mcpkit check ../ones-mcp                      # 校验某个服务的配置和密钥
```

值从不打印。文件和目录建好后设成仅所有者可读（Windows 用 `icacls`，其它平台用 `chmod`）。

## 代理 stdio 服务

`mcpkit.proxy.stdio_proxy` 把只支持 stdio 的上游（chrome-devtools-mcp）变成 HTTP 服务，
**所有客户端共用同一个上游会话**——浏览器的远程调试批准框因此只弹一次。
上游进程一旦消失，整个服务退出，交给 mcpm 重启，而不是留一个每次调用都报错的端点。

## 测试

```bash
uv run pytest
```

每个测试都把服务作为真实子进程启动，用真实 HTTP 打：三种鉴权模式、Host/Origin 拦截、
非本机地址拒绝、OAuth 完整流程（动态注册 → 授权 → 换令牌 → 轮换 → 重启后仍有效）、
密钥不外泄、代理共用一个上游进程且上游死亡后退出。

`test_oauth_hardening.py` 是从 payment-mcp 移植来的 51 项加固回归：客户端会自己发明的 9 种发现路径、
回调白名单、原生表单的 Origin 随 Referrer-Policy 变化、复制走的授权表单、授权码重放、
保密型客户端的两种认证方式、存储里绝不能出现明文令牌。**这些代码现在服务于所有服务，所以测试跟着代码走。**
