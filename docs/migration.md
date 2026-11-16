# 迁移与凭据恢复（M3）

设计依据：`docs/design.md` §4、§6.9。三条命令都遵守同一组规则：

- **默认只打印计划**，加 `--apply` 才写入。
- **只复制，不移动**。源文件（旧根、保险库、旧网关目录）只读，sha256 和 mtime 都不变。
- **不覆盖**。目标已存在且内容相同就跳过；内容不同就列为 `conflict`，原样保留。有冲突时退出码为 3，出错为 2。
- **不输出凭据**。计划、终端输出和 `.anx/migration-report.json` 里只有路径；sha256 只记录非凭据文件。
- 重复执行没有差异：第二次运行时每一项都是 `same`（报告文件除外，它会记录新的时间）。

## `anx migrate`

```
anx migrate [--from <旧根>] [--to <新根>] [--in-place] [--from-file <mcp-manager.json>]
            [--port-offset N] [--agent-id X] [--cloud-url U] [--apply]
anx migrate --export-legacy <新文件>
```

- 不带参数时，`--from` 是旧安装根（`%LOCALAPPDATA%\Roc\MCPManager\client`、`~/.local/share/roc-mcp-manager/client`），`--to` 是新安装根。
- `--in-place`：新根就是旧根（默认取 `--root`），只新增 `agent-nexus.json` 等文件，`mcp-manager.json` 一个字节都不动。原地迁移时相对路径保持相对；迁到别的根时，`plugin_paths`、工作区路径和 stdio 连接器的 `cwd` 会改写成指向旧根的绝对路径。
- 生成的 `agent-nexus.json`：
  - `services.workspace` → `plugins.device`，其余 `services.<x>` → `plugins.<x>`，并附 `install`：旧根 `builtins/<x>/` 里有代码的插件记为 `{"state":"linked","path":…}`，同时在 `plugins/plugins.lock.json` 登记为 link 插件（代码原地不动）；其他记为 `pending_install`。两种都带建议的仓库 URL。
  - `settings` 里键名像密码的明文值（`password|secret|token|apikey`，但不包括 `*_secret`、`*_env` 这类引用名，以及插件清单 `secrets` 声明的键）搬到 `secrets/<插件>/<键>`，配置里改为 `{"$secret": "<插件>/<键>"}`。目标凭据文件已存在且内容不同时，值留在配置里不动，并给出警告。
  - `cloud.workspaces.*.mcp: "workspace"` → `"device"`；`cloud.mcps` → 顶层 `connectors`；`agent_id`、`url`、`token_secret`、`token_env` 原样保留（私有版设备的身份因此不变，`.private/` 不复制）。
  - `scan` → `plugin_paths`；写入 `migrated_from: {path, sha256, at}`。
- 复制：`secrets/**`（`workspace/*` 另复制一份到 `device/*`）、`.mcp-manager/state*.json` → `.anx/`、device 插件的 `watchers.sqlite3`（及 `-wal`）→ `plugin-data/device/`。不复制：`run/` pid 记录、`audit.jsonl`、`.private/`、runtime（`mcpm/`、`.venv`、`tools/`）、插件代码。
- `--export-legacy <文件>`：把当前根的配置另存成旧格式（`plugins` → `services`、`device` → `workspace`、能表达的连接器 → `cloud.mcps`），供回滚后人工合并；目标文件必须不存在。
- 首次启动新安装根、而旧安装根存在时，`anx serve` 会自动执行一次同样的迁移（桌面壳先写入随包配置的问题留给 M12）。

迁移后的读取：registry、agent 和 mcpkit 0.2 都同时认 `plugins`/`services`、`plugin_paths`/`scan`、`connectors`/`cloud.mcps`，以及 `{"$secret"}` 引用。控制台写配置时沿用文件已有的段（迁移后的文件写 `plugins`）。

## 旧 mcpkit 插件：影子根 `.anx/compat-root`

根目录有 `agent-nexus.json`，而插件清单是 v1（没有 `schema_version: 2`）或声明 `requires.mcpkit` 低于 0.2 时，supervisor 启动它前会生成 `.anx/compat-root/mcp-manager.json`（只有 `services` 段，由当前配置投影而来，`$secret` 引用已解析，文件经 `protect()` 收紧权限），并建 `compat-root/secrets` 目录联接（POSIX 上是 symlink）指向 `<root>/secrets`。插件进程拿到 `MCPM_ROOT=<compat-root>` 和 `ANX_ROOT=<root>`；新版 mcpkit 优先读 `ANX_ROOT`，不受影响。旧插件自己写回的设置会落在影子文件里，下次启动时被重新生成覆盖，配置应在控制台修改。

## `anx secrets restore`

```
anx secrets restore --vault <credentials-vault> [--root <root>] [--include optional] [--apply]
```

清单来自保险库 `README.md` 的 `## 文件清单（N 个）` 一节，每行一个 `` - `path` ``；标题里的数字与列表条数不一致时给出警告并以列表为准。每一条按 `agent_nexus/secrets_restore.py:VAULT_MAP` 处理：

| 保险库路径 | 处理 |
|---|---|
| `root/secrets/<svc>/**` | 恢复到 `<root>/secrets/<svc>/**`；`workspace/*` 另外恢复到 `device/*` |
| `root/secrets/cloud-agent-*.env` | 恢复到 `<root>/secrets/` |
| `root/mcp-manager.json`、`.bak-*`、`root/.mcp-manager/*.json` | 不恢复，作为 `anx migrate --from-file` 的输入 |
| `repo/builtins/<dir>/**`（payment-mcp 等） | 找到接管该目录的已安装插件（清单 `legacy.dirs`，或目录名 / 插件 id）后写入其目录；没装就是 `plugin-missing` |
| `repo/builtins/codex-mcp/logs/oauth-store.json.bak-domain` | 默认跳过；`--include optional` 时放到 `plugin-data/device/logs/` |
| `repo/secrets/build-enrollment.json` | 永不恢复（只供本地构建私有版时引用） |
| `root/云效apikey` | 跳过，接入连接器时用 `anx secrets set connectors/yunxiao/key --from-file <path>` |
| 其他 | `unmapped`，列出但不恢复 |

清单里有、保险库里没有的文件记为 `missing`；保险库里有、清单没列的记为 `unlisted`。写入的文件都经过 `protect()`。

## `anx-gateway migrate`

```
anx-gateway migrate --from /etc/mcpmanager --state /var/lib/mcpmanager --to /etc/agent-nexus
                    [--state-to /var/lib/agent-nexus] [--owner agent-nexus] [--apply]
```

`cloud.json` → `gateway.json`（旧状态目录前缀改成新的，补一个 `mailbox` 段，其他键原样保留），`server.env` → `gateway.env`（变量名不改），状态目录里的 `*.sqlite3` 用 sqlite 在线备份复制，其他文件（media 缓存）直接复制。`--owner` 在 POSIX 上把复制出的文件 chown 给该用户。
