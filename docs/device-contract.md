# 设备能力契约 `anx.device/1`

设计依据：`docs/design.md` §5.2–§5.4。机器可读的定义在 `schemas/device-contract.v1.json`。

## 能力与工具

| 层 | 能力 | 必须提供的工具 | 说明 |
|---|---|---|---|
| 核心 | `info` | `device_info` | |
| | `notify` | `notify` | `actions: [{id, label, link?}]`，最多 5 个；`dry_run` 只校验 |
| | `files` | `file_list`、`file_read`、`file_write`、`file_stat` | 遵守 `allowed_roots` |
| | `time` | `curr_time`、`sleep` | |
| 标准 | `exec` | `exec_command`、`write_stdin`、`exec_kill`、`exec_list` | |
| | `edit` | `edit_file`、`patch_file` | |
| | `search` | `file_glob`、`file_grep` | 不经 shell 按文件名 / 内容查找；有 ripgrep 时用它 |
| | `plan` | `update_plan` | |
| | `media` | `view_image`（加 `media_get` 反向 RPC） | |
| | `watch` | `watch_create/training/list/cancel/event/events` | |
| | `ask` | `request_user_input(_async/_result)` | |
| | `agents` | `agent_sessions(kind, workdir)` | 只返回会话 id 与修改时间 |
| | `companion` | `companion_routes` | |
| 平台扩展（Windows） | `screen` | `list_screens`、`screenshot`、`vision` | |
| | `uia` | `start_control`、`stop_control`、`control_status`、`read_ui`、`element` | 需要交互式桌面 |
| | `input` | `mouse`、`keyboard` | 需要交互式桌面 |

Linux 和 macOS 把 `screen`、`uia`、`input` 声明为 `{"v":1, "available": false, "reason": "..."}`，不注册对应工具。
Windows 上锁屏、注销、远程桌面断开或运行在 session 0 时，这三项在 `device_info` 里报告 `available: false` 和原因。

## 规则

1. 每个工具的 `_meta` 带 `{"anx/capability": "<能力>", "anx/contract": "anx.device/1"}`，每个工具都有 annotations。
2. 同名工具在各平台用同一个 inputSchema 公共子集；平台特有参数只能是可选参数。
3. 内部工具带 `_meta["anx/internal"] = true`：网关不把它放进 tools/list 和 toolsearch，`tooluse` 仍可调用。
4. 声明了某个能力且没有标为不可用，就要提供该能力的全部必需工具；标为不可用时要给出 `reason`。

## catalog 里的 `device` 块

agent 每次刷新 catalog 时调用 device 插件的 `device_info`，按 schema 校验后加上 `id`、`agent_version`、`mcp`（插件在本机的 MCP 名，`device` 或旧名 `workspace`）发布。`cloud.hostname` 覆盖插件自报的主机名，设为空字符串则不上报。插件不可用或返回不合规时发布最小块：只有 `info` 能力，并带 `degraded: true`。旧网关忽略整个块。

带这个块的设备在 hello 里声明 feature `device-contract/1`。

## 网关的处理

- 直出工具只包含设备声明过的能力的工具（旧 agent 和 degraded 设备不过滤）。
- 调用的工具这台设备没有时，错误里写出能力名、平台和原因，例如
  `device host-linux (remote-1) lacks capability 'screen' (platform=linux): screen control is implemented on Windows only (platform=linux)`。
- `device_list(capability="screen")` 只列出该能力可用的设备。
- `/media/<token>` 用 `device.mcp` 指名的本地 MCP；旧网关写死 `mcp: "workspace"`，新 agent 按别名解析到 `device`。

## 合规检查

```
anx device conformance http://127.0.0.1:8400/mcp [--token-env NAME | --token-file PATH] [--json]
```

检查核心工具、annotations、`_meta`、`device_info` 的 schema、声明能力与工具是否对应、`notify(dry_run=true)`（带和不带 actions）。通过返回 0，不通过返回 1，连不上返回 2。device 插件自己的测试（`builtins/device/tests/test_device_contract.py`）在进程内跑同一套检查，Windows 原生平台和模拟的 Linux 平台各一次。
