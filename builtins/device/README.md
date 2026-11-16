# device

agent-nexus 唯一的内置插件，插件 id 为 `device`；旧 id `workspace` 作为别名继续有效（清单里的 `aliases`）。

把这台电脑的命令行、文件编辑和桌面控制作为 MCP 工具，交给 ChatGPT、Claude 等客户端（经 agent-nexus 网关）使用。

传输、OAuth、反代发现、审计、allowed roots 和危险命令筛查来自 `mcpkit`；工具如下：

| 工具 | 用途 |
|---|---|
| `exec_command` | 执行命令（Windows 上默认是 Git Bash）；在 `yield_time_ms`（最长 150 秒）内结束就直接返回，否则返回数字 `session_id` |
| `write_stdin` | 向正在运行的命令写入字符，或用空字符串等待输出 |
| `file_read` | 按行读取文本文件；内容统一为 LF，`line_numbers=true` 带行号；GBK 文件自动识别 |
| `file_glob` | 按文件名 / 相对路径的 glob 查找文件（`*.py`、`src/**/test_*.py`），新的在前 |
| `file_grep` | 按正则搜索文件内容，返回 `文件:行号:内容`；有 ripgrep 时用 ripgrep |
| `edit_file` | 精确替换文件里的一段文本（`old_string` → `new_string`，默认要求唯一；`replace_all` 全部替换），返回改动处带行号的片段 |
| `patch_file` | 应用 unified diff（`git diff` / `diff -u` 格式），可一次改多个文件、新建、删除、重命名；全部成功或全部不改，支持 `dry_run` |
| `view_image` | 从本机文件系统读取图片给模型查看，并提供 3 天有效的临时 HTTPS 地址供需要时发给用户 |
| `update_plan` | 保存当前多步任务计划 |
| `curr_time` | 返回 UTC 时间 |
| `sleep` | 等待指定毫秒数 |
| `exec_kill` | 终止仍在运行的命令树 |
| `exec_list` | 查看仍在运行的 exec session |
| `companion_routes` | 查看 GPT Companion 的会话路由 |
| `watch_create` | 创建持久化 watcher |
| `request_user_input` | 向坐在这台机器前的人提问并等回答（单选 / 多选 / 自由文本） |
| `request_user_input_async` | 只发问不等待，返回 `question_id` |
| `request_user_input_result` | 取回之前那个问题的答案 |
| `watch_training` | 创建模型训练 watcher 组 |
| `watch_list` | 查看 watcher 状态 |
| `watch_cancel` | 取消 watcher 或 watcher 组 |
| `watch_event` | 按 event_id 读取事件及其工作目录 |
| `watch_events` | 查看 watcher 历史事件 |
| `start_control` / `stop_control` | 开始 / 结束控制这台电脑的屏幕（见下） |
| `control_status` | 查看控制会话状态，不需要会话也能调 |
| `list_screens` | 列出显示器及其在虚拟桌面里的位置 |
| `screenshot` | 截屏给模型看 |
| `vision` | 装载 / 卸载图标识别模型（OmniParser，进程内，默认不加载） |
| `read_ui` | 把屏幕读成带 ref 的命名元素（优先于截图） |
| `element` | 按 ref 操作控件（点击 / 直接触发 / 填值 / 勾选 / 展开 / 聚焦） |
| `mouse` | 移动 / 点击 / 拖拽 / 滚动，可用 `ref` 代替坐标 |
| `keyboard` | 打字或按组合键 |
| `device_info` | 按 `anx.device/1` 契约描述本机：主机名、平台、形态、声明的能力（含运行时可用性与原因）、可用的 agent 运行时 |
| `notify` | 给这台机器前的人发通知（Windows toast，失败退回托盘气泡；Linux `notify-send`），可带按钮；`dry_run` 只校验 |
| `file_list` / `file_stat` / `file_read` / `file_write` | 可移植的文件工具，遵守 `allowed_roots` |
| `agent_sessions` | 按工作目录找最新的 Claude Code / Codex 会话 id（只返回 id 和时间） |

每个工具的 `_meta` 都带 `{"anx/capability": …, "anx/contract": "anx.device/1"}`。哪些工具注册由
`src/platform/<windows|linux|darwin>/` 的 `CAPABILITIES` 决定；Linux/macOS 上 `screen`、`uia`、`input`
声明为 `available: false` 并给出原因。契约见 `docs/device-contract.md`。

## 屏幕控制

`list_screens`、`screenshot`、`mouse`、`keyboard` **只在控制会话开启时可用**，没开会话一律拒绝并提示先调 `start_control`。

### 开始控制时屏幕会发生什么

- **每一块显示器**的四边亮起蓝色辉光：最外层 3px 是亮蓝细线，向内约 96px 二次方渐隐。
- **顶部居中**出现深色胶囊标签：`● ChatGPT 正在控制电脑 │ 按 ESC 退出`。
- **鼠标指针**换成加粗的蓝色箭头（48px，带白色描边和投影），全系统生效。
- 辉光缓慢呼吸（约 3.2 秒一个周期），只改图层整体透明度，不重算位图。

结束控制时三者全部还原：

| 场景 | 兜底 |
|---|---|
| 人想立刻收回控制 | **按 ESC**，立即结束 |
| 任务正常结束 | 模型调 `stop_control` |
| 进程正常退出 | `atexit` 钩子恢复 |
| 进程被强杀 | 换光标前写了标记文件，**下次启动时自动恢复** |

### ESC 只认你的手指

标签写了「按 ESC 退出」，所以它必须真的管用——用 `WH_KEYBOARD_LL` 低级键盘钩子实现。

关键在于**只响应物理按键**：`SendInput` 发出的事件在 `KBDLLHOOKSTRUCT.flags` 里带 `LLKHF_INJECTED` 标志，
钩子看到这个标志就原样放行。所以模型通过 `keyboard` 工具给应用发 ESC（关菜单、退出对话框）不会误杀自己的会话，
而你手指按下的 ESC 一定结束控制，并且那一次按键会被吞掉，不再传给当前窗口。

钩子在 `start_control` 返回**之前**就装好，不存在"标签已经显示但 ESC 还不管用"的窗口期。
万一钩子装不上（极少见），`start_control` 会在返回值的 `notes` 里明说这句承诺不成立。

钩子**跑在自己的线程上，那个线程什么都不画**。低级键盘钩子是在装它的线程里被回调的，而且只在那个线程处理消息时才轮到它——
系统会一直把这个按键攥在手里等。一开始它和辉光共用一个线程，代价实测出来了：打 40 个字符，
没有会话 0.32 秒、有会话但关掉辉光 0.34 秒、**开着辉光 1.89 秒**——每个按键都排在一帧动画后面。
分开之后是 0.21 秒。

另外 `keyboard(action="type")` 里的换行会**真的敲回车**，而不是发 U+000A：
后者几乎没有程序会响应，实测往地址栏里打一个以换行结尾的网址，地址填好了、页面不动。

### 两个刻意的设计

**覆盖层点击穿透**（`WS_EX_TRANSPARENT`）：辉光不吃任何鼠标事件，开着控制你照样能正常用电脑。

**覆盖层不进截图**（`WDA_EXCLUDEFROMCAPTURE`）：屏幕上看得见，`screenshot` 里不存在。实测截图边缘蓝色偏差为 0。这样模型看到的是桌面本身，不会把自己的标识当成界面的一部分。旧版本 Windows 不支持这个标志时，`start_control` 会在返回值的 `notes` 里说明辉光会出现在截图里。

### 按名字点，而不是数像素

`read_ui` 把屏幕读成**带 ref 的命名元素**，`element` 按 ref 操作。能读到元素时优先用它：
点 `ref_9 '确定'` 是打在那个按钮上，从截图里量出来的坐标只是猜。

```
read_ui(scope="windows")            # 所有顶层窗口，每个带 ref
read_ui(scope="focused")            # 当前窗口的控件（默认）
read_ui(scope="ref:ref_3")          # 上一步列出的某个窗口
element(action="invoke", ref="ref_9")   # 直接触发按钮，鼠标完全不动
element(action="set_value", ref="ref_4", text="...")   # 文本直接塞进输入框
mouse(action="click", ref="ref_9")  # 真实鼠标，点在控件自己的可点击点上
```

`element` 支持的动作取自控件自己声明的能力（结果里的 `actions` 字段）：
`click`/`double_click`/`right_click`、`invoke`、`set_value`、`toggle`、`expand`/`collapse`、
`select`、`scroll_into_view`、`focus`、`restore`。

三个实现要点：

**一次跨进程调用读完一个窗口。** 过滤条件是交给 UIA 的
`AND(IsOffscreen==False, IsControlElement==True, OR(类型 ∈ 白名单))`，
**在目标进程内部执行**，所以上万元素的窗口只回传需要的那几百个。实测：记事本 31 个元素 0.18 秒，
开了 44 个标签的 Edge 200 个（截断）0.73 秒。

**窗口列表不走 UIA。** 向 UIA 要桌面的子节点会去问每一个有窗口的进程，一个卡住的应用能拖住整次枚举
（实测 17 个窗口 5.97 秒）。`EnumWindows` 读窗口管理器自己的表，**36 个窗口 0.004 秒**。

**跨调用不保留 COM 指针。** 每个元素存的是"怎么再找到它"的配方（窗口句柄 + RuntimeId + 类型 + 名字 +
序号 + 矩形）。操作前重新查找并**校验身份**，对不上就报
`the element ... is no longer in this window (the UI changed)` 而**不是照着旧坐标点下去**。
ref 随控制会话结束一起失效。

> 微软 UFO² 在这里只用"本步序号"当身份，而且名字对不上时**只警告、照样执行**
> （`action_execution.py:100-110`）。这一点我们故意做得更严：宁可响亮地失败。

### 没有可访问性树的窗口：OCR 在 read_ui 内部兜底

**回退不能交给模型做。** 如果 `read_ui` 只回一句"这个窗口没有树，你去截图吧"，模型就要再花一次调用截图、
再从图上量坐标——**三次往返做一件事**。所以兜底发生在读取内部：UIA 读不到内容时，
同一次调用里用 Windows 自带的 OCR（`Windows.Media.Ocr`，本机、免费、中文，约 40ms）把屏幕文字变成元素，
带着一样的 ref 返回，只是 `source` 是 `ocr`。

这就是 UFO² 的 hybrid control detection，融合规则也照它：**UIA 优先，视觉候选与任一已知控件 IoU > 0.1 就丢弃，
剩下的接在后面**（`screenshot.py:1316-1344`）。多加了一条它没有的：**包含度 > 0.8 也算重复**——
按钮里的文字标签和按钮是同一个目标，IoU 抓不住这种情况。但**容器要排除**：
窗口自身也是一个元素，覆盖整个窗口，第一版实现把一个聊天窗口识别出的 78 行全删了。

OCR 元素的诚实边界，工具描述里写明了：

- 名字是从像素读的，**会错**（实测「保存」被认成「亻呆存」，有一行整个丢了）；
- **只有文字会出现**，纯图标按钮找不到；
- 背后没有提供方，所以**只能点击**，`invoke` / `set_value` 这些模式一律拒绝并说明原因。

实测各类窗口：

| 窗口 | 元素 |
|---|---|
| 计算器 / 设置 / 资源管理器 / 系统对话框 | 69 / 79 / 86 / 10 个，全部 UIA |
| Claude（Electron） | 195 个，全部 UIA |
| 企业微信（自绘 Duilib） | 1 个 UIA + **78 个 OCR** |
| Unity 编辑器 | 17 个（自绘，OCR 可补） |

> 一开始我以为 Electron 应用都读不到树，那是**误判**：QQ 和 ChatGPT 当时是最小化的，
> 窗口矩形停在 (-32000, -32000)。最小化的窗口既没有树也没有像素，`read_ui` 现在会直接说
> "先用 `element(action='restore')` 还原"。

### 纯图标按钮：OmniParser 在同一次调用里补上

OCR 只认字，所以齿轮、回形针、发送箭头这类**一个字都没有的按钮**它看不见。这部分交给 OmniParser v2
（YOLOv8 图标检测 + Florence-2 描述模型），**跑在 device 插件自己的进程里**，不占端口、不是独立 MCP。

**它默认不加载**：导入 torch 要几秒，权重要 1.4G 显存，而这台机器的 8G 是和 Unity、浏览器共用的。
由 `vision` 工具显式装卸，没装载时像素层就只有 OCR——也就是它存在之前的样子。

```
vision(action="status" | "fetch" | "load" | "unload")
```

装载后，**同一张窗口截图**同时喂给 OCR 和图标检测，再按 树 > 文字 > 图标 融合。实测：

| 窗口 | 只有 OCR | 加上图标检测 |
|---|---|---|
| 企业微信 | 67 行文字，1.8s | 67 行 + **41 个图标**，9.8s |
| Windows 输入体验 | 120 行，2.7s | 120 行 + **41 个图标**，9.2s |
| 任务栏 / Clash Verge / WPS | 树完整，0.1–0.5s | **一模一样，像素层根本没启动** |

**检测很强，起名很弱。** 拿任务栏当标注集（UIA 知道每个图标的真名）量过一次：

| 判定 | 例子 |
|---|---|
| 对（约 6/23） | 搜索 →「A magnifying glass or search function」；QQ →「A penguin wearing a red scarf」 |
| **错** | Edge →「**the mozilla firefox logo**」；有道翻译 →「youtube」；Outlook →「mouse pad」 |
| 没用 | Typora →「A simple marker or icon」 |

也试过 CLIP 零样本分类（29 个对 10 个、24ms/个、带置信度）和悬停读提示（VS Code 不聚焦不出提示，
内容在动的窗口像素差分会框住整片正文）——都不足以替代。所以现在的做法是**先找界面自己写的字**：
图标正下方或紧邻右侧、长度不超过 12 个字的 OCR 文本，直接当它的名字。这个答案是精确的，模型的猜测不是。

名字只是**建议**，位置才是可用的部分：图标元素和 OCR 元素一样背后没有提供方，所以**只能点击**。
认不出来时宁可叫「未命名图标 3」——假名字会让模型点错，没名字只是少一点信息。

> 权重许可：OmniParser 仓库是 MIT，但图标检测器是 YOLOv8 衍生物，ultralytics 是 AGPL-3.0。

### 被遮挡的窗口

读一个窗口不能去抓"屏幕上它所在的那块区域"——前面压着别的窗口时，那些像素属于压在上面的东西。
这不是美观问题：**从那些像素识别出的文字会被当成这个窗口的控件报给模型**。
实测复现过：画图盖住计算器后，抓屏幕读到的是「文件/查看/工具/画笔/形状」，画图的工具栏。

所以窗口截图和 OCR 都走 `PrintWindow(PW_RENDERFULLCONTENT)`——**让窗口自己画自己**。
同一个遮挡场景下，现在读到的是「计算器 / DEG / MC / 三角学 / 函数」。窗口拒绝作画时才退回抓屏，
并在 `note` 里说明"可能包含前面的窗口"。

### 刚启动的程序

窗口先出现，树后建好。实测计算器启动后半秒只有 1 个元素、片刻后 69 个；设置先是 5 个（全是标题栏按钮）、
然后 79 个；资源管理器先 5 个、然后 86 个。**不处理的话模型会以为"这程序没有树"，白白退回像素。**

`read_ui` 因此做两件事：读不到内容时按固定间隔重试若干次；**树很薄（少于 15 个元素）时多看一眼取较大的结果**。

判据上踩过三个坑，都写进测试了：

- 标题栏按钮**是可操作元素**，所以"有可操作元素就算就绪"会立刻放行；
- 这些按钮的名字**带应用名**（「最小化 设置」「关闭 设置」），精确匹配认不出来，要用包含匹配；
- "有内容"和"加载完了"是两件事，**任何元素分类都区分不了**——资源管理器头两次读都有 5 个真实元素，文件列表后到。
  所以最后靠的是"薄树多看一眼"这种不讲道理但有效的办法，而不是更聪明的判据。

后来拿浏览器又撞出第四个坑，比前三个都严重：**数量根本判不出加载完没有**。
新开一个 Edge 窗口，头一两秒它稳稳地回 40 个元素——全是浏览器自己的标签栏和工具栏，
一个页面元素都没有。"树不再增长"这时是成立的，但页面还没来：

| | 0.2s | 0.8s | 1.2s | 2.1s | 2.5s | 3.6s |
|---|---|---|---|---|---|---|
| python.org | 40 | 40 | 73 | 136 | **138** | 138 |
| zh.wikipedia | 40 | 40 | 40 | 41 | — | **200** |

区别不在数量而在**位置**：加载中时窗口高度 15% 以下只有 1 个元素（标签栏那个"新建标签页"按钮），
加载完是 86（python.org）和 149（维基百科）。所以判据换成**正文区里有没有东西**。

等多久则由**窗口还在不在重绘**决定，不是定时器：加载中的浏览器每隔 0.3 秒抓一帧，5 帧全变；
画完的自绘窗口 5 帧全不变。**画完了还没树，就是不会有树**——这条把无树窗口的首次读取从 9.4 秒降到 5.4 秒。
而且每个窗口只给**一次**宽限期，之后照它本来的样子读（重复读取 6.3 秒 → 0.58 秒）。

### 有的程序根本不回答

不是所有窗口都会及时应答 UI Automation。这台机器上一个 Unity 编辑器实例，**每次都要 23 秒**才肯返回它那 17 个元素
（另一个 Unity 实例是 1.4 秒）。更糟的是所有 UIA 调用共用一个 COM 线程，**它一卡，读别的窗口也跟着一起等**。

所以读取有 4 秒上限（动作没有——`invoke` 打开一个菜单本来就可能慢），超时后：

- 这次调用直接去读像素，Unity 那个窗口于是给出 **56 个 OCR 元素**，比它那 17 个树元素有用；
- **把卡住的线程退役、另起一个**，代价是一个线程等它自己结束，不这么做的代价是期间每一次调用都陪等；
- 记住这个窗口 5 分钟内不再问树。

实测：22.4 秒 → 首次 5.0 秒、之后 0.94 秒，而且紧接着读资源管理器只要 0.40 秒。

### 动作自带最新状态

`mouse`、`keyboard`、`element` 的结果里直接带着**这次动作产生的新状态**，不用再单独看一眼：

| `observe` | 行为 |
|---|---|
| `auto`（默认） | 有可操作元素就回元素 diff；**没有就自动附一张焦点窗口的截图** |
| `ui` | 完整元素列表 |
| `image` | 强制截图 |
| `none` | 不观察 |

diff 只说变化：`invoke · window: 企业微信 · focus changed · 1 new element(s) · 10 gone`。
比重发整棵树或整张截图便宜一个数量级。

动作后会**等屏幕稳定**（连续两帧缩略图相同即稳，上限 1.5 秒）再观察，所以拿到的是动作的结果而不是动画中间态。
等不稳会如实写进 `settle.note`——聊天类应用一直在动，这种情况很常见。

> UFO² 在这一步**完全没有稳定检测**（`AFTER_CLICK_WAIT: 0`），它把"上一步生效了吗"交给模型看红框截图判断。

### 坐标

所有坐标都是**虚拟桌面像素**，和 `list_screens` 报的一致；主屏左边的显示器 x 是负数。进程启用了 per-monitor v2 DPI 感知，所以缩放显示器上坐标也是真实像素。

截图会被缩小，**图片坐标不等于屏幕坐标**。每次截图都返回来源区域和缩放比，换算写在返回值里：

```
screen_x = region.x + image_x / scale
screen_y = region.y + image_y / scale
```

`mouse` 收到桌面以外的坐标会直接报错并告诉你桌面范围，而不是悄悄夹到边界。

### 配置

`mcp-manager.json` 的 `services.workspace.settings.control`：

| 键 | 默认 | 说明 |
|---|---|---|
| `banner_text` | `ChatGPT 正在控制电脑` | 顶部标签文字（右侧的「按 ESC 退出」固定显示） |
| `pulse` | `true` | 辉光呼吸 |
| `show_indicator` | `true` | 辉光和标签 |
| `change_cursor` | `true` | 替换系统指针 |
| `use_ocr` | `true` | 无树窗口用 Windows OCR 兜底 |
| `vision_model_dir` | `runtime/models/omniparser` | OmniParser 权重目录（相对工程根） |
| `vision_device` | `auto` | `auto` / `cuda` / `cpu` |
| `vision_caption` | `true` | 关掉就只出图标框、不起名字 |
| `max_screenshot_edge` | `1568` | 截图最长边 |

图标模型**不会因为这些配置自动加载**，始终要显式调 `vision(action="load")`。

`show_indicator` 和 `change_cursor` 存在是为了测试和无头环境，**正常使用不要关**：它们是屏幕前的人唯一能察觉「机器正被别人操作」的线索。

### 测试

单元测试不碰真实桌面。会真的开窗口、换光标、移指针的那几个用 `CONTROL_LIVE_TESTS=1` 显式打开：

```bash
CONTROL_LIVE_TESTS=1 uv run pytest tests/test_control.py -k test_live
```

图标那条链路有一个端到端的 live 测试：抓一张本机任务栏的图，画进一个自绘的 Tk 窗口（**没有树**，
而且是我们自己的进程），读出图标、按名字点一个，再核对**这一下是不是落在那个图标自己的矩形里**。
探针窗口跑在独立进程里（Tk 的解释器活到退出会让整个进程崩在 `Tcl_AsyncDelete`，而那是在断言全过之后），
并且 120 秒后自己关闭——测试被中断时不会在桌面上留下一个窗口。

装 OmniParser：

```bash
uv sync --extra vision
```

`transformers` 被钉在 5 以下不是保守：OmniParser 的描述权重是 Florence-2 的原始布局，在 transformers 5 上
两条路都加载不了（原生实现要转换过的配置，模型自带的远程代码撞上已删除的属性）。4.5x 上也要两个绕法，
都写在 `vision.py` 的注释里：加载时必须 `attn_implementation="eager"`（远程代码把 `_supports_sdpa` 写成了
转发给 `self.language_model` 的属性，而 4.5x 在 `__init__` 里就要选注意力实现，那时它还没被赋值），
生成时必须 `use_cache=False`（远程代码按旧的元组格式读缓存，一开缓存就崩在第一个 token 之前）。

## Watcher / GPT Companion

Watcher 会把定义和事件历史保存在 SQLite（默认 `logs/watchers.sqlite3`）。发生事件时只向本机 GPT Companion 发送一个很小的 event_id；真实日志、checkpoint 和训练目录仍留在原位置。

常用入口是 `watch_training`：可以同时盯 `exec_command` 的 session、训练日志和 checkpoint glob。收到 Companion 的最小提醒后，模型先调用 `watch_event(event_id)` 拿到原 watcher 的 `workdir / target / group`，再自己去本机检查。

Companion 默认地址是 `http://127.0.0.1:17891`。如果临时不可用，事件会保存在 SQLite 并重试投递。文件、日志和 glob watcher 在 MCP 重启后可恢复；进程 session 属于进程内状态，服务重启后会标记为 stale。

## 编辑文件

`edit_file` 适合一处改动：

```
edit_file(path="src/app.py", old_string="    return 1\n", new_string="    return 2\n")
→ {"status": "ok", "replacements": 1, "first_line": 42}
```

`old_string` 必须和文件内容逐字一致（从 `file_read` 复制），出现多次时报错，除非 `replace_all=true`。

`patch_file` 适合多处、多文件的改动，输入就是 `git diff` 的输出：

```
patch_file(patch="--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2\n")
→ {"status": "ok", "files": [{"action": "modified", "path": "...", "hunks": [{"hunk": 1, "line": 1}]}]}
```

- `/dev/null` 新建或删除文件，git 的 `rename from` / `rename to` 重命名；
- hunk 的行号对不上时取最近的匹配位置，并在结果里报告 `offset`；只差行尾空白时也能匹配，报告 `whitespace_fuzz`；
- 先在内存里算完所有文件，任何一处不匹配就一个文件都不改；`dry_run=true` 只检查不写入。

两个工具都保留文件原有的 UTF-8 BOM 和换行风格（LF / CRLF），写入是原子替换。

## 向用户提问

```
request_user_input(question="这次部署到哪里？", title="部署方式",
                   kind="single", options=["本地反代", "海外服务器"],
                   allow_custom=True)
→ {"answered": true, "selected": ["本地反代"], "text": "先跑一周看看"}
```

问题出现在 chatx 的「GPT 的提问」面板里（弹通知并提到前台），调用一直阻塞到有人回答。
`kind` 取 `single` / `multi` / `text`；`allow_custom` 在选项旁边再加一个自由输入框。

**超时不算失败**：等过 `ask_timeout_s`（默认 240 秒）还没人答，返回的是
`answered=false` 加一个 `question_id`，问题仍然挂在屏幕上，之后用
`request_user_input_result(question_id, wait_s=…)` 接着取。这样调用方的 MCP 客户端
自己的请求超时就不会把一个还有效的问题变成错误。用户点「跳过」则返回 `cancelled=true`，
和「答了个空」区分得开。

答案内容不进审计日志，只记 `question_id` 和最终状态。

## 终端

Windows 这台机器上暂时没有可靠 PTY。`exec_command(tty=true)` 会接受参数，但当前仍使用 pipes。普通构建、git、脚本、交互式 `stdin` 提示都可以工作；依赖完整 TTY 控制序列的程序不保证行为一致。

## 启动

平时由 mcpm 托管（Windows 直接启动 `WorkSpace/mcp/mcpm-desktop.exe.lnk`，桌面程序会自动启动 manager 和 autostart 服务），端口 8400，经 cloudflared 隧道暴露为
`https://<hub-domain>/mcp`。手动：

```bash
uv sync
uv run python -m src.server --transport http --port 8400     # HTTP
uv run python -m src.server --transport stdio                # 本地客户端
uv run python -m src.server --transport http --check-config   # 只校验配置和密钥
uv run pytest
```

## 配置

服务基于 [`mcpkit`](../mcpkit/README.md)：命令行、鉴权、Host 校验、`/healthz` 都来自它。
配置写在根目录的 `agent-nexus.json` → `services.device`（旧的 `mcp-manager.json` → `services.workspace` 仍然会被读取）：

| 位置 | 内容 |
| --- | --- |
| `public_url` | `https://<hub-domain>`。**换域名只改这一处**：OAuth 的 issuer、`/.well-known` 与 resource 全部由它推导，Host 白名单也跟着它走 |
| `auth` | `mode: oauth`（ChatGPT）+ `bearer_secrets`（本地客户端的静态令牌）；`access_token_ttl_s` 设 `0` 表示永不过期 |
| `settings` | 这个服务**做什么**：`bash_path`、`default_workdir`、`allowed_roots`、`env`、`command_deny`、`allow_dangerous`、`limits`、companion 与 watcher 的地址和数据库 |
| `mcp/secrets/workspace/`（迁移后为 `secrets/device/`） | `oauth-password`（授权页密码）、`bearer-token`（静态令牌）、`oauth.sqlite3`（授权状态） |

密钥用 `python -m mcpkit secret new workspace/<名字>` 生成或轮换，值从不打印。

## 鉴权

ChatGPT 走 OAuth：动态注册 → 授权页输入密码 → PKCE 换码 → 令牌。令牌在库里只存 SHA-256 摘要，
重启不掉线；刷新令牌每次使用后轮换。本地客户端可以直接用静态 Bearer 令牌。

2026-09-18 从自写实现迁到 mcpkit 时，7 个已注册客户端和当时生效的访问 / 刷新令牌一并平移，
**ChatGPT 没有重新授权**（迁移前令牌是明文存 JSON，迁移后只存摘要）。

## 安全

这是一个最终可以执行本机命令和修改文件的服务。OAuth、HTTPS 和 `allowed_roots` 都应该保留；
`allow_dangerous=false` 默认开启明显破坏性命令的拦截，但它只是 guard rail，不是 OS 级 sandbox。

工具注解全部声明 `destructive_hint=false`，这是有意为之，用来免掉客户端的逐次确认。
**它不代表这些工具是安全的**——`exec_command`、`edit_file`、`patch_file`、`exec_kill` 照样能删文件、杀进程。
真正的边界是 `allowed_roots`、`command_deny` 和 `allow_dangerous`。`read_only_hint` 则如实标注。

屏幕控制把能力扩大到了 GUI：开启会话后可以看屏幕、点任何窗口、按任何键。它**没有额外的权限门**（按需求如此），
边界仍然是这台服务器的鉴权本身——能拿到 OAuth 令牌的人本来就有 `exec_command`，也就已经拥有这台机器。
控制会话不改变"谁能进来"，只改变"进来以后能做什么"，并且用屏幕上的辉光和标签让机器前的人始终知道正在发生。
