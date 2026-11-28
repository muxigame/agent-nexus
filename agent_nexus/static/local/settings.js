// 本机 · 设置 (docs/ui-spec.md §4.5): autostart, updates, this machine, appearance, quit.
import { el, mount, icon, fetchJSON, toast, confirmDialog, relTime, tokenField, appearanceControls, errorState, modal } from "/ui/ui.js";
import { lockPasswordCard } from "/local/devices.js";

function card(title, ...rows) {
  return el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, title)), el("div", { class: "card-body" }, rows));
}

function setting(title, desc, control) {
  return el("div", { class: "setting" }, el("div", { class: "setting-text" }, el("span", { class: "setting-title" }, title),
    desc ? el("span", { class: "setting-desc" }, desc) : null), control);
}

// 执行账户 (agent_nexus/accounts.py): only a root core on Linux (the Ubuntu system service) switches.
async function accountsCard() {
  let data;
  try { data = await fetchJSON("/api/accounts", { quiet: true }); } catch { return null; }
  if (!data.supported) return null;
  const purposes = [["mcp", "Agent 和 MCP 插件", "插件进程（设备插件的命令、文件读写）以这个账户运行"],
    ["terminal", "远程终端", "其他设备打开这台电脑的终端时，登录这个账户的 shell"]];
  const rows = purposes.map(([key, title, desc]) => {
    const select = el("select", { class: "input", style: { maxWidth: "220px" }, onchange: async () => {
      select.disabled = true;
      try {
        const result = await fetchJSON("/api/accounts", { method: "PUT", body: { [key]: select.value } });
        select.value = result.accounts[key];
        toast(key === "mcp" ? `插件改为以 ${select.value} 运行（正在运行的插件已重启）` : `远程终端改为登录 ${select.value}`);
      } catch { select.value = data.accounts[key]; }
      select.disabled = false;
    } }, data.users.map((user) => el("option", { value: user.name }, user.label || (user.uid === 0 ? `${user.name}（管理员，不切换）` : user.name))));
    select.value = data.accounts[key];
    return setting(title, desc, select);
  });
  return card("执行账户", ...rows,
    el("p", { class: "faint xs" }, "核心服务以 root 运行（网关连接、自动更新、远程桌面）；这里决定它替你运行的东西用哪个账户。"));
}

// 本机 MCP (agent_nexus/local_mcp.py, agents/mcp_link.py): the gateway's MCP for this computer's CLIs, no credential.
const KIND_LABEL = { claude: "Claude Code", codex: "Codex", gemini: "Gemini CLI" };
const CLI_TONE = { local: "ok", both: "ok", gateway_config: "ok", gateway_account: "ok", none: "neutral",
  unknown: "warn", name_taken: "warn", unsupported: "neutral" };

const MARK_CONNECTED_STATES = ["none", "local", "unknown", "unsupported"];

// 延长 MCP 等待: what the entry's timeout holds (agents/mcp_link.py wait record)
function waitLine(cli, on) {
  const wait = cli.wait;
  if (!wait || !wait.state) return null;
  const text = wait.state === "on" ? "等待上限 24 小时"
    : wait.state === "modified" ? (on ? "等待上限已被手动修改" : "已被手动修改，未还原")
      : wait.state === "unrestored" ? "等待上限已被手动修改，未还原" : null;
  return text ? el("span", { class: "faint xs" }, text) : null;
}

export async function mcpCard() {
  let data;
  try { data = await fetchJSON("/api/mcp/local", { quiet: true }); } catch { return null; }
  const body = el("div", { class: "stack" });
  const wrap = card("本机 MCP", body);
  wrap.id = "local-mcp";

  // true when saved (the caller toasts only then); a change of enabled / auto_config adds or removes entries a
  // moment later (the reconcile runs after the answer), so the CLI rows are fetched again shortly after
  const refresh = async () => { try { data = await fetchJSON("/api/mcp/local", { quiet: true }); render(); } catch { /* keep */ } };
  const put = async (changes) => {
    try {
      data = await fetchJSON("/api/mcp/local", { method: "PUT", body: changes });
    } catch {
      render();
      return false;
    }
    render();
    if ("enabled" in changes || "auto_config" in changes || "extended_wait" in changes) setTimeout(refresh, 2500);
    return true;
  };
  const act = async (cli, action, force = false) => {
    try {
      const result = await fetchJSON("/api/mcp/local/clis", { method: "POST", quiet: true,
        body: { kind: cli.kind, account: cli.account, action, force } });
      const backup = result.result?.backup;
      toast(`${KIND_LABEL[cli.kind] || cli.kind}：${result.cli?.label || "已更新"}${backup ? `（原文件已备份到 ${backup}）` : ""}`);
    } catch (error) {
      if (error.data?.code === "duplicate" && !force) {
        if (await confirmDialog({ title: "仍要添加？", message: `${error.message}。`, confirmLabel: "仍要添加（会出现两套 Agent Nexus 工具）", danger: true })) {
          return act(cli, action, true);
        }
      } else {
        toast(error.message, { type: "bad" });
      }
    }
    try { data = await fetchJSON("/api/mcp/local", { quiet: true }); } catch { /* keep the old view */ }
    render();
  };
  const toggle = (key, checked, onText, offText) => el("label", { class: "switch" }, el("input", { type: "checkbox", role: "switch", checked,
    onchange: async (event) => { const on = event.currentTarget.checked; if (await put({ [key]: on })) toast(on ? onText : offText); } }), el("span"));
  // "我已连接": the account may hold the Agent Nexus cloud connector although no check saw it (the codex app-server
  // or claude mcp list answered "absent", or the entry was added before the account connector)
  const markable = (cli) => cli.kind !== "gemini" && cli.source !== "user" && MARK_CONNECTED_STATES.includes(cli.state);

  function render() {
    const ready = data.state === "ready";
    const rows = (data.clis || []).map((cli) => el("div", { class: "list-item" },
      el("div", { class: "stack grow", style: { gap: 0 } },
        el("span", { class: "title" }, `${KIND_LABEL[cli.kind] || cli.kind}`, cli.account ? el("span", { class: "faint xs" }, ` · ${cli.account}`) : null),
        el("span", { class: "sub" }, el("span", { class: `pill ${cli.stale ? "warn" : CLI_TONE[cli.state] || "neutral"}` }, cli.label || cli.state)),
        cli.backup ? el("span", { class: "faint xs mono truncate", title: cli.backup }, `备份：${cli.backup}`) : null,
        waitLine(cli, data.extended_wait !== false),
        cli.entry === "user_removed" && markable(cli)
          ? el("span", { class: "faint xs" }, "账户里已经有 Agent Nexus 云端连接器？点“我已连接”，对话里就不会再临时接入本机 MCP。") : null),
      el("div", { class: "row wrap" },
        cli.state === "local" || cli.state === "both"
          ? el("button", { class: "btn btn-sm", type: "button", onclick: () => act(cli, "remove") }, "移除")
          : el("button", { class: "btn btn-sm", type: "button", disabled: cli.state === "name_taken" || !data.usable, onclick: () => act(cli, "add") }, "接入"),
        cli.kind !== "gemini" ? el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => act(cli, "probe") }, "重新检测") : null,
        markable(cli)
          ? el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "这个 CLI 的账户里已经添加了 Agent Nexus 云端连接器", onclick: () => act(cli, "mark_connected") }, "我已连接") : null,
        cli.kind !== "gemini" && cli.source === "user"
          ? el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => act(cli, "mark_missing") }, "未连接") : null)));
    const json = JSON.stringify({ mcpServers: { "agent-nexus": { type: "http", url: data.url } } }, null, 2);
    const toml = `[mcp_servers.agent-nexus]\nurl = "${data.url}"`;
    mount(body,
      el("div", { class: "field" }, el("label", {}, "地址"), tokenField(data.url)),
      el("p", { class: ready ? "small" : "small muted" }, icon(ready ? "circle-check" : "info", { size: 14 }), " ", data.reason),
      setting("启用本机 MCP", "这台电脑上的 CLI 和你自己的 Agent 不用任何凭据就能使用网关的全部工具；只接受本机的连接", toggle("enabled", data.enabled, "已启用本机 MCP", "已关闭本机 MCP（已写入的配置项会被移除）")),
      setting("自动接入已安装的 CLI", "在 Claude Code、Codex、Gemini CLI 的配置里写入名为 agent-nexus 的配置项（不含任何凭据，写入前先备份）；已经通过云端网关接入的不会重复添加",
        toggle("auto_config", data.auto_config, "已开启自动接入", "已关闭自动接入")),
      setting("延长 MCP 等待", "Codex、Gemini CLI 的 agent-nexus 配置项：工具超时改为 24 小时，长命令可以一直等到结束；Claude Code 用后台任务等待，不改它。关闭时恢复原来的值（被手动改过的不动）；稍后自动写入，新开的 Codex / Gemini 会话生效",
        toggle("extended_wait", data.extended_wait !== false, "已开启延长 MCP 等待", "已关闭延长 MCP 等待（只还原未被手动修改的值）")),
      setting("仅限本机运行账户", "只接受 Agent Nexus 自己、执行账户和当前登录用户的进程；同一台电脑的其他用户不能使用",
        el("label", { class: "switch" }, el("input", { type: "checkbox", role: "switch", checked: data.peers === "accounts",
          onchange: (event) => put({ peers: event.currentTarget.checked ? "accounts" : "any" }) }), el("span"))),
      rows.length ? el("div", { class: "list inset" }, rows) : el("p", { class: "muted small" }, "没有检测到已安装的 Claude Code、Codex 或 Gemini CLI。"),
      el("p", { class: "faint xs" }, "Agent Nexus 发起的对话会自动带上工作区，不需要手动配置；这里的配置项供在 Agent Nexus 之外启动的 CLI 使用。"),
      el("details", {}, el("summary", { class: "small" }, "自己的 Agent 或其他 MCP 客户端"),
        el("div", { class: "stack" },
          el("p", { class: "muted small" }, "Streamable HTTP，无需 Authorization。JSON（Claude Code / Cursor 等）："),
          el("pre", { class: "code-block" }, json),
          el("p", { class: "muted small" }, "TOML（Codex ~/.codex/config.toml）："),
          el("pre", { class: "code-block" }, toml))));
  }
  render();
  return wrap;
}

// CLI 配置文件 (agents/cli_config.py): view and edit the CLIs' own config files of each run-as account.
function configDiff(diff) {
  return el("pre", { class: "chat-diff code-block" }, String(diff || "").split("\n").map((line) => {
    const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    return el("span", { class: cls }, line || " ");
  }));
}

const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n || 0} B`);

async function openConfigEditor(row, onSaved) {
  const query = `id=${encodeURIComponent(row.id)}&account=${encodeURIComponent(row.account || "")}`;
  let file;
  try { file = await fetchJSON(`/api/cli-config/file?${query}`); } catch { return; }
  if (file.too_large) { toast("文件太大（超过 4 MB），不能在这里编辑", { type: "bad" }); return; }
  let unlocked = !row.readonly_default;
  const area = el("textarea", { class: "input mono", spellcheck: "false", rows: 22, readonly: !unlocked,
    style: { width: "100%", minHeight: "360px", fontSize: "12px", whiteSpace: "pre", overflowWrap: "normal" } });
  area.value = file.text || "";
  const status = el("p", { class: "faint xs" }, file.exists ? `${file.path} · ${fmtSize(file.size)}` : `${file.path}（还不存在，保存时创建）`);
  const unlock = row.readonly_default ? el("button", { class: "btn btn-sm", type: "button", onclick: async () => {
    if (!await confirmDialog({ title: "解锁编辑 ~/.claude.json？", danger: true, confirmLabel: "解锁编辑",
      message: "这个文件保存 Claude Code 的账户和每个项目的记录，Claude Code 运行时会频繁改写它，最好先退出 Claude Code。改错可能丢失项目设置；保存前会自动备份，若文件在你编辑期间被改动则不会保存。" })) return;
    unlocked = true; area.readOnly = false; unlock.remove();
  } }, "解锁编辑") : null;
  // The file changed while editing (Claude Code rewrites ~/.claude.json all the time): take the new file as the
  // base and keep the user's text, so the next preview shows exactly what saving would change in the new file.
  const reloadBase = async () => {
    if (!await confirmDialog({ title: "文件在你编辑期间被修改", confirmLabel: "重新载入",
      message: "重新载入后以磁盘上的新文件为基准，你的文本保留在编辑框里；再次预览会显示相对于新文件的改动（包括会被你的文本覆盖掉的那些新改动）。" })) return;
    let fresh;
    try { fresh = await fetchJSON(`/api/cli-config/file?${query}`); } catch { return; }
    file = fresh;
    status.textContent = `${file.path} · ${fmtSize(file.size)} · 已重新载入`;
    toast("已重新载入，你的改动还在，请再次预览");
  };
  const backupsBox = el("div", { class: "stack" });
  const restorePreview = async (backup, dialog) => {
    let preview;
    try { preview = await fetchJSON("/api/cli-config/preview", { method: "POST", body: { id: row.id, account: row.account, backup: backup.name, base_sha256: file.sha256 } }); } catch { return; }
    modal({ title: "恢复这个备份？", size: "lg", body: el("div", { class: "stack" },
      el("p", { class: "muted small" }, preview.changed ? "恢复后文件的改动如下（当前文件会先备份）：" : "备份和当前文件内容相同。"),
      preview.changed ? configDiff(preview.diff) : null),
    actions: [{ label: "取消" }, { label: "恢复", variant: "primary", onClick: async () => {
      try {
        const result = await fetchJSON("/api/cli-config/restore", { method: "POST", body: { id: row.id, account: row.account, backup: backup.name, base_sha256: file.sha256 } });
        toast(result.backup ? `已恢复，原文件备份到 ${result.backup}` : "已恢复");
        dialog.close(); onSaved?.();
      } catch { return false; }
      return true;
    } }] });
  };
  const loadBackups = async (dialog) => {
    let listed;
    try { listed = await fetchJSON(`/api/cli-config/backups?${query}`, { quiet: true }); } catch { mount(backupsBox, el("p", { class: "muted small" }, "无法列出备份")); return; }
    const rows = (listed.backups || []).map((backup) => el("div", { class: "list-item" },
      el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title small" }, relTime(backup.mtime * 1000)),
        el("span", { class: "faint xs mono truncate", title: backup.name }, `${backup.source === "edit" ? "手动编辑" : "自动接入"} · ${fmtSize(backup.size)} · ${backup.name}`)),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => restorePreview(backup, dialog) }, "恢复")));
    mount(backupsBox, rows.length ? el("div", { class: "list inset" }, rows) : el("p", { class: "muted small" }, "还没有备份。"));
  };
  const dialog = modal({ title: `${row.label}`, description: row.account ? `账户 ${row.account}` : "", size: "lg",
    body: el("div", { class: "stack" }, status, unlock, area,
      el("details", { ontoggle: (event) => { if (event.currentTarget.open) loadBackups(dialog); } }, el("summary", { class: "small" }, "备份"), backupsBox)),
    actions: [{ label: "关闭" }, { label: "检查并预览改动", variant: "primary", onClick: async () => {
      if (!unlocked) { toast("先解锁编辑", { type: "bad" }); return false; }
      let preview;
      try { preview = await fetchJSON("/api/cli-config/preview", { method: "POST", body: { id: row.id, account: row.account, text: area.value, base_sha256: file.sha256 } }); } catch { return false; }
      if (!preview.valid) { toast(`${preview.error}${preview.line ? `（第 ${preview.line} 行）` : ""}`, { type: "bad" }); return false; }
      if (preview.stale) { await reloadBase(); return false; }
      if (!preview.changed) { toast("没有改动"); return false; }
      modal({ title: "保存这些改动？", size: "lg", body: el("div", { class: "stack" },
        preview.warning ? el("p", { class: "small" }, icon("info", { size: 14 }), " ", preview.warning) : null,
        configDiff(preview.diff)),
      actions: [{ label: "返回编辑" }, { label: "保存", variant: "primary", onClick: async () => {
        try {
          const result = await fetchJSON("/api/cli-config/file", { method: "PUT", body: { id: row.id, account: row.account, text: area.value, base_sha256: file.sha256 } });
          toast(result.backup ? `已保存，原文件备份到 ${result.backup}` : "已保存");
          dialog.close(); onSaved?.();
        } catch { return false; }
        return true;
      } }] });
      return false;
    } }] });
}

async function cliConfigCard() {
  const body = el("div", { class: "stack" });
  const wrap = card("CLI 配置文件", body);
  const load = async () => {
    let data;
    try { data = await fetchJSON("/api/cli-config", { quiet: true }); } catch (error) {
      mount(body, el("p", { class: "muted small" }, error?.data?.code === "forbidden_account" ? "只有 Agent Nexus 的执行账户和当前登录用户可以查看这些文件。"
        : error?.data?.error ? `无法读取 CLI 配置文件：${error.data.error}` : "无法读取 CLI 配置文件。"));
      return;
    }
    const rows = (data.files || []).map((row) => el("div", { class: "list-item" },
      el("div", { class: "stack grow", style: { gap: 0 } },
        el("span", { class: "title" }, row.label, row.account ? el("span", { class: "faint xs" }, ` · ${row.account}`) : null),
        el("span", { class: "faint xs mono truncate", title: row.path || "" },
          row.ok === false ? row.error || "无法读取" : row.exists ? `${fmtSize(row.size)} · ${row.mtime ? relTime(row.mtime * 1000) : ""}` : "还不存在")),
      el("button", { class: "btn btn-sm", type: "button", disabled: row.ok === false || row.too_large, onclick: () => openConfigEditor(row, load) },
        row.readonly_default ? "查看" : "查看/编辑")));
    mount(body, el("p", { class: "faint xs" }, "保存前检查语法并显示改动；每次保存都先在同一目录备份原文件，可在“备份”里一键恢复。"),
      el("div", { class: "list inset" }, rows));
  };
  await load();
  return wrap;
}

// 终端里的 anx (agent_nexus/owner_cli.py): macOS only; Windows and the deb put anx on PATH when installing.
async function cliPathCard() {
  let data;
  try { data = await fetchJSON("/api/cli-path", { quiet: true }); } catch { return null; }
  if (!data.supported) return null;
  const body = el("div", { class: "stack" });
  const act = async (action) => {
    try {
      data = await fetchJSON("/api/cli-path", { method: "POST", body: { action } });
      toast(action === "link" ? "已添加：新开的终端里可以直接运行 anx" : "已移除终端里的 anx");
    } catch { /* toasted */ }
    render();
  };
  function render() {
    mount(body,
      setting("在终端中使用 anx", data.linked ? `已链接：${data.linked} → ${data.script}。删除应用不会移除它，删除前先点“移除”`
        : "在 ~/.local/bin（或 PATH 上的 ~/bin）添加 anx，新开的终端里可以运行 anx devices、anx exec 等；~/.local/bin 不在 PATH 上时会写进 ~/.zprofile。删除应用不会移除它，删除前先点“移除”",
        data.linked ? el("button", { class: "btn btn-sm", type: "button", onclick: () => act("unlink") }, "移除")
          : el("button", { class: "btn btn-sm", type: "button", onclick: () => act("link") }, "添加")),
      data.foreign?.length ? el("p", { class: "faint xs" }, `已有其他程序的 anx：${data.foreign.join("、")}（不会覆盖）`) : null);
  }
  render();
  return card("终端里的 anx", body);
}

export async function settingsPage(ctx) {
  const { page } = ctx;
  mount(page, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "设置"), el("p", {}, "这台电脑上的 Agent Nexus。"))));
  let info, update, autostart;
  try {
    [info, update] = await Promise.all([fetchJSON("/api/ping", { quiet: true }), fetchJSON("/api/app/update", { quiet: true })]);
  } catch (error) {
    page.append(errorState(error, () => settingsPage(ctx)));
    return;
  }
  try { autostart = await fetchJSON("/api/app/autostart", { quiet: true }); } catch { autostart = { enabled: null }; }

  const system = Boolean(info.system_service || autostart.system);
  const autostartBox = el("input", { type: "checkbox", role: "switch", checked: system || Boolean(autostart.enabled),
    disabled: system || autostart.enabled === null,
    onchange: async (event) => {
      const box = event.currentTarget;
      try {
        const result = await fetchJSON("/api/app/autostart", { method: "PUT", body: { enabled: box.checked } });
        box.checked = Boolean(result.enabled);
        toast(result.enabled ? "已开启开机自启" : "已关闭开机自启");
      } catch { box.checked = !box.checked; }
    } });

  const updateArea = el("div", { class: "row wrap" });
  function renderUpdate(data) {
    const check = el("button", { class: "btn", type: "button", disabled: !(data.desktop_app || data.updater), onclick: async () => {
      check.disabled = true;
      mount(check, el("span", { class: "spinner" }), "正在检查…");
      try {
        await fetchJSON("/api/app/update/check", { method: "POST" });
        const before = data.checked_at;
        for (let i = 0; i < 30; i += 1) {
          await new Promise((r) => setTimeout(r, 1000));
          const next = await fetchJSON("/api/app/update", { quiet: true });
          if (next.checked_at !== before) { data = next; break; }
        }
        toast(data.available ? `发现新版本 v${data.version}` : "已是最新版本");
      } catch { /* toasted */ }
      renderUpdate(data);
    } }, icon("refresh-cw", { size: 16 }), "检查更新");
    mount(updateArea,
      data.available ? el("button", { class: "btn btn-primary", type: "button", onclick: async () => {
        try {
          const result = await fetchJSON("/api/app/update/apply", { method: "POST" });
          if (result.manual) {
            modal({ title: result.cancelled ? "没有授权安装" : "请在终端里安装", size: "sm",
              body: el("div", { class: "stack" },
                el("p", { class: "muted small" }, result.cancelled ? "安装需要管理员授权。也可以在终端里运行：" : "这台电脑没有图形授权窗口，请在终端里运行："),
                tokenField(result.manual)),
              actions: [{ label: "知道了", variant: "primary" }] });
          } else if (result.prompted) {
            toast("新版本已下载，请在系统的安装提示里确认（Android 要求由你确认安装）");
          } else {
            toast(data.updater === "deb" ? "更新已安装，正在重启 Agent Nexus" : "正在安装更新，完成后会自动重启");
          }
        } catch { /* toasted */ }
      } }, icon("download", { size: 16 }), `更新到 v${data.version}`) : null,
      check);
    updateText.textContent = data.available ? `v${data.version} 可用（当前 v${data.current}）` :
      `当前 v${data.current} · ${data.checked_at ? `上次检查 ${relTime(data.checked_at)}` : "还没有检查过"}${data.desktop_app || data.updater ? "" : " · 不是从桌面应用启动的，无法在这里更新"}${data.error ? ` · ${data.error}` : ""}`;
  }
  const updateText = el("span");
  const autoUpdateBox = el("input", { type: "checkbox", role: "switch", checked: update.mode === "auto",
    disabled: !(update.desktop_app || update.updater),
    onchange: async (event) => {
      const box = event.currentTarget;
      try {
        const result = await fetchJSON("/api/app/update/mode", { method: "PUT", body: { mode: box.checked ? "auto" : "notify" } });
        box.checked = result.mode === "auto";
        toast(box.checked ? "已开启自动更新" : "已关闭自动更新（有新版本时只提醒）");
      } catch { box.checked = !box.checked; }
    } });

  const looks = appearanceControls();

  const quit = el("button", { class: "btn btn-danger", type: "button", onclick: async () => {
    if (!await confirmDialog({ title: "退出 Agent Nexus？", danger: true, confirmLabel: "退出",
      message: "会停止这台电脑上的所有插件，并断开网关。Agent 将暂时无法使用这台电脑。" })) return;
    try { await fetchJSON("/api/manager/shutdown", { method: "POST" }); toast("Agent Nexus 正在退出"); } catch { /* toasted */ }
  } }, icon("power", { size: 16 }), "退出 Agent Nexus");

  const accounts = await accountsCard();
  const mcp = await mcpCard();
  const cliConfig = mcp ? await cliConfigCard() : null;
  const cliPath = await cliPathCard();
  page.append(el("div", { class: "stack lg", style: { maxWidth: "820px" } },
    card("启动", setting("开机自动启动", system ? "核心是系统服务：开机即运行，不需要任何人登录；界面在登录后自动出现"
      : autostart.enabled === null ? "只有 Windows 桌面应用支持" : "登录 Windows 后在后台启动，托盘里显示状态", el("label", { class: "switch" }, autostartBox, el("span")))),
    card("更新", setting("Agent Nexus 版本", updateText, updateArea),
      setting("自动更新", update.headless ? "这台设备没有桌面：默认自动更新（每 5 分钟检查，发现新版本就安装并重启）"
        : "每 5 分钟检查一次，发现新版本自动安装并重启；关闭后只提醒", el("label", { class: "switch" }, autoUpdateBox, el("span")))),
    card("本机",
      el("div", { class: "stack" },
        el("div", { class: "field" }, el("label", {}, "数据目录"), tokenField(info.root)),
        el("div", { class: "field" }, el("label", {}, "配置文件"), tokenField(info.config)),
        el("div", { class: "field" }, el("label", {}, "控制台地址"), tokenField(info.url)),
        el("p", { class: "faint xs" }, "插件安装在数据目录的 plugins/ 下，插件数据在 plugin-data/，凭据在 secrets/。"))),
    mcp,
    cliConfig,
    cliPath,
    accounts,
    lockPasswordCard(),
    card("外观", setting("外观风格", "自动：Windows 上用 Win11 云母，其他系统用纯净白", looks.style), setting("明暗", "只影响这个浏览器", looks.theme)),
    card("高级",
      system ? setting("重启核心服务", "停止所有插件后由系统服务立即重新启动", quit) : setting("退出", "停止所有插件并退出", quit))));
  renderUpdate(update);
}
