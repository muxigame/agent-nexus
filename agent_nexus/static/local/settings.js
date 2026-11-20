// 本机 · 设置 (docs/ui-spec.md §4.5): autostart, updates, this machine, appearance, quit.
import { el, mount, icon, fetchJSON, toast, confirmDialog, relTime, tokenField, setTheme, errorState, modal } from "/ui/ui.js";
import { lockPasswordCard } from "/local/devices.js";

function card(title, ...rows) {
  return el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, title)), el("div", { class: "card-body" }, rows));
}

function setting(title, desc, control) {
  return el("div", { class: "setting" }, el("div", { class: "setting-text" }, el("span", { class: "setting-title" }, title),
    desc ? el("span", { class: "setting-desc" }, desc) : null), control);
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

  const autostartBox = el("input", { type: "checkbox", role: "switch", checked: Boolean(autostart.enabled), disabled: autostart.enabled === null,
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

  const themeMode = (() => { try { return localStorage.getItem("anx-theme") || "system"; } catch { return "system"; } })();
  const themes = el("div", { class: "segmented", role: "group", "aria-label": "主题" },
    [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]].map(([value, label]) => el("button", { type: "button", "aria-pressed": String(themeMode === value),
      onclick: (event) => { setTheme(value); for (const b of themes.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === event.currentTarget)); } }, label)));

  const quit = el("button", { class: "btn btn-danger", type: "button", onclick: async () => {
    if (!await confirmDialog({ title: "退出 Agent Nexus？", danger: true, confirmLabel: "退出",
      message: "会停止这台电脑上的所有插件，并断开网关。Agent 将暂时无法使用这台电脑。" })) return;
    try { await fetchJSON("/api/manager/shutdown", { method: "POST" }); toast("Agent Nexus 正在退出"); } catch { /* toasted */ }
  } }, icon("power", { size: 16 }), "退出 Agent Nexus");

  page.append(el("div", { class: "stack lg", style: { maxWidth: "820px" } },
    card("启动", setting("开机自动启动", autostart.enabled === null ? "只有 Windows 桌面应用支持" : "登录 Windows 后在后台启动，托盘里显示状态", el("label", { class: "switch" }, autostartBox, el("span")))),
    card("更新", setting("Agent Nexus 版本", updateText, updateArea)),
    card("本机",
      el("div", { class: "stack" },
        el("div", { class: "field" }, el("label", {}, "数据目录"), tokenField(info.root)),
        el("div", { class: "field" }, el("label", {}, "配置文件"), tokenField(info.config)),
        el("div", { class: "field" }, el("label", {}, "控制台地址"), tokenField(info.url)),
        el("p", { class: "faint xs" }, "插件安装在数据目录的 plugins/ 下，插件数据在 plugin-data/，凭据在 secrets/。"))),
    lockPasswordCard(),
    card("外观", setting("主题", "", themes)),
    card("高级",
      setting("旧版控制台", "原来的标签页式控制台，日志查看更细", el("a", { class: "btn", href: "/classic" }, "打开", icon("external-link", { size: 14 }))),
      setting("退出", "停止所有插件并退出", quit))));
  renderUpdate(update);
}
