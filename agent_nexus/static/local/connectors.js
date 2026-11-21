// The client's 连接器: local MCP servers that agent-nexus does not run (an IDE's MCP, a tool you
// started yourself, a stdio command), offered to the gateway next to this device's plugins.
import { el, mount, icon, fetchJSON, toast, modal, confirmDialog, emptyState, errorState, pill } from "/ui/ui.js";

const TRANSPORT_LABEL = { http: "HTTP", sse: "SSE", stdio: "命令（stdio）" };

function field(label, control, hint) {
  return el("div", { class: "field" }, el("label", {}, label), control, hint ? el("span", { class: "faint xs" }, hint) : null);
}

function editor(existing, onSaved) {
  const name = el("input", { class: "input mono", placeholder: "例如 vscode-mcp", value: existing?.name || "", disabled: !!existing });
  const title = el("input", { class: "input", placeholder: "显示名称（可选）", value: existing?.title && existing.title !== existing.name ? existing.title : "" });
  const description = el("input", { class: "input", placeholder: "给 AI 看的说明（可选）", value: existing?.description || "" });
  const transport = el("select", { class: "select" }, Object.entries(TRANSPORT_LABEL).map(([value, label]) =>
    el("option", { value, selected: (existing?.transport || "http") === value }, label)));
  const url = el("input", { class: "input mono", placeholder: "http://127.0.0.1:3000/mcp", value: existing?.url || "" });
  const command = el("input", { class: "input mono", placeholder: "npx -y @modelcontextprotocol/server-filesystem C:\\data",
    value: (existing?.command || []).join(" ") });
  const cwd = el("input", { class: "input mono", placeholder: "工作目录（可选）", value: existing?.cwd || "" });
  const authType = el("select", { class: "select" }, [["none", "无"], ["bearer", "Bearer 令牌"], ["headers", "自定义请求头"]].map(([value, label]) =>
    el("option", { value, selected: (existing?.auth?.type || "none") === value }, label)));
  const token = el("input", { class: "input mono", type: "password", autocomplete: "off",
    placeholder: existing?.auth?.type === "bearer" ? "已保存；留空则不变" : "令牌" });
  const headers = el("textarea", { class: "textarea mono", rows: 3, placeholder: existing?.auth?.type === "headers"
    ? `已保存（${(existing.auth.headers || []).join("、")}）；留空则不变` : "X-Api-Key: ...\nX-Region: cn-hangzhou" });
  const httpPart = el("div", { class: "stack" }, field("地址", url), field("认证", authType),
    field("令牌", token, "保存在本机 secrets/ 目录，不写进配置文件，也不会显示出来"), field("请求头", headers, "每行一个：名称: 值"));
  const stdioPart = el("div", { class: "stack" }, field("启动命令", command, "agent-nexus 会在需要时启动它并通过标准输入输出通信"), field("工作目录", cwd));
  const sync = () => {
    const isStdio = transport.value === "stdio";
    httpPart.hidden = isStdio; stdioPart.hidden = !isStdio;
    token.parentElement.hidden = authType.value !== "bearer";
    headers.parentElement.hidden = authType.value !== "headers";
  };
  transport.addEventListener("change", sync); authType.addEventListener("change", sync); sync();

  modal({ title: existing ? `编辑 ${existing.title}` : "添加连接器",
    description: "把本机已有的 MCP 服务（不是 agent-nexus 插件）接进来，网关和 AI 客户端就能通过这台设备使用它。",
    body: el("div", { class: "stack" }, field("名称", name, "设备目录里显示为 <设备>/<名称>"), field("显示名称", title), field("说明", description),
      field("连接方式", transport), httpPart, stdioPart),
    actions: [{ label: "取消" }, { label: "保存", variant: "primary", submit: true, onClick: async () => {
      const body = { title: title.value.trim(), description: description.value.trim(), transport: transport.value };
      if (transport.value === "stdio") {
        body.command = command.value.trim().split(/\s+/).filter(Boolean);
        if (cwd.value.trim()) body.cwd = cwd.value.trim();
      } else {
        body.url = url.value.trim();
        body.auth = { type: authType.value };
        if (authType.value === "bearer" && token.value.trim()) body.auth.token = token.value.trim();
        if (authType.value === "headers" && headers.value.trim()) {
          body.auth.headers = Object.fromEntries(headers.value.split("\n").map((line) => line.split(/:(.*)/s))
            .filter((parts) => parts[0]?.trim()).map(([key, value]) => [key.trim(), (value || "").trim()]));
        }
      }
      try {
        await fetchJSON(`/api/local-connectors/${encodeURIComponent(name.value.trim())}`, { method: "PUT", body, quiet: true });
      } catch (error) { toast(error.message, { type: "bad" }); return false; }
      toast("已保存；网关在半分钟内看到它");
      onSaved();
    } }] });
}

export async function localConnectorsPage(ctx) {
  const { page } = ctx;
  const holder = el("div", { class: "stack lg" });
  mount(page, el("div", { class: "page-head" },
    el("div", { class: "titles" }, el("h1", {}, "连接器"),
      el("p", {}, "本机上不归 agent-nexus 管的 MCP 服务——编辑器自带的、自己启动的、或一条命令——接进来后和插件一样出现在网关里。")),
    el("div", { class: "actions" }, el("button", { class: "btn btn-primary", type: "button", onclick: () => editor(null, load) },
      icon("plus", { size: 16 }), "添加连接器"))), holder);

  async function test(connector, button) {
    button.disabled = true;
    try {
      const result = await fetchJSON(`/api/local-connectors/${encodeURIComponent(connector.name)}/test`, { method: "POST", quiet: true });
      if (result.ok) toast(`连接正常：${result.tools.length} 个工具${result.tools.length ? `（${result.tools.slice(0, 5).join("、")}${result.tools.length > 5 ? "…" : ""}）` : ""}`);
      else toast(`连接失败：${result.error}`, { type: "bad", timeout: 8000 });
    } catch (error) { toast(error.message, { type: "bad" }); }
    button.disabled = false;
  }

  async function remove(connector) {
    if (!await confirmDialog({ title: `删除 ${connector.title}？`, danger: true, confirmLabel: "删除",
      message: "从这台设备移除这个连接器，保存的令牌也一并删除。MCP 服务本身不受影响。" })) return;
    try { await fetchJSON(`/api/local-connectors/${encodeURIComponent(connector.name)}`, { method: "DELETE" }); toast("已删除"); }
    catch { /* toasted */ }
    load();
  }

  async function load() {
    let data;
    try { data = await fetchJSON("/api/local-connectors", { quiet: true }); }
    catch (error) { mount(holder, errorState(error, load)); return; }
    const connectors = data.connectors || [];
    if (!connectors.length) {
      mount(holder, el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({ icon: "plug", title: "还没有连接器",
        text: "比如 VS Code / Cursor 打开的 MCP 端口、本机跑着的数据库 MCP，或者一个 npx 启动的 MCP 服务器。",
        action: { label: "添加连接器", icon: "plus", onClick: () => editor(null, load) } }))));
      return;
    }
    mount(holder, el("div", { class: "list" }, connectors.map((connector) => {
      const testButton = el("button", { class: "btn btn-sm", type: "button", onclick: () => test(connector, testButton) }, icon("activity", { size: 14 }), "测试");
      return el("div", { class: "list-item", dataset: { connector: connector.name } },
        el("span", { class: "avatar" }, icon("plug", { size: 16 })),
        el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } },
          el("span", { class: "title truncate" }, connector.title, el("span", { class: "tag", style: { marginLeft: "8px" } }, TRANSPORT_LABEL[connector.transport] || connector.transport)),
          el("span", { class: "sub mono truncate" }, connector.url || (connector.command || []).join(" "))),
        el("div", { class: "meta row", style: { gap: "6px" } },
          pill(connector.enabled ? "ok" : "disabled", connector.enabled ? "已启用" : "已停用"),
          testButton,
          el("button", { class: "btn btn-sm", type: "button", onclick: () => editor(connector, load) }, icon("pencil", { size: 14 }), "编辑"),
          el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "删除", title: "删除", onclick: () => remove(connector) },
            icon("trash", { size: 14 }))));
    })));
  }
  await load();
}
