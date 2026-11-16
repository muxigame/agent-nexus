// Connectors page (docs/ui-spec.md §3.4; design §7, decision D10): third-party MCP servers hosted on the gateway.
import {
  el, mount, icon, iconHTML, fetchJSON, toast, modal, drawer, confirmDialog, copy, emptyState, errorState,
  skeletonRows, pill, relTime,
} from "/ui/ui.js";

const AUTH_LABEL = { none: "无", bearer: "Bearer", headers: "Header", oauth: "OAuth" };
const TRANSPORT_LABEL = { auto: "自动", http: "HTTP", sse: "SSE", stdio: "stdio" };
const NAME_RULE = /^[a-z0-9][a-z0-9-]{0,39}$/;

function head(title, text, actions = []) {
  return el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, title), text ? el("p", {}, text) : null),
    actions.length ? el("div", { class: "actions" }, actions) : null);
}

function openMenu(anchor, items) {
  document.querySelector(".menu.floating")?.remove();
  const rect = anchor.getBoundingClientRect();
  const menu = el("div", { class: "menu floating", role: "menu" }, items.filter(Boolean).map((item) => item.separator ? el("div", { class: "menu-sep" }) :
    el("button", { class: `menu-item ${item.danger ? "danger" : ""}`, type: "button", role: "menuitem",
      onclick: () => { menu.remove(); item.onClick(); } }, icon(item.icon, { size: 14 }), item.label)));
  document.body.append(menu);
  const width = menu.offsetWidth || 180;
  menu.style.top = `${window.scrollY + rect.bottom + 4}px`;
  menu.style.left = `${Math.max(8, Math.min(window.scrollX + rect.right - width, window.scrollX + document.documentElement.clientWidth - width - 8))}px`;
  const close = (event) => { if (!menu.contains(event.target) && event.target !== anchor) { menu.remove(); document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close));
  menu.querySelector("button")?.focus();
}

function transportText(connector) {
  if (connector.transport === "auto") return connector.transport_detected ? `自动 → ${TRANSPORT_LABEL[connector.transport_detected]}` : "自动";
  return TRANSPORT_LABEL[connector.transport] || connector.transport;
}

function initial(connector) {
  const text = (connector.title || connector.name || "?").trim();
  return text.slice(0, 1).toUpperCase();
}

/** A connector name guessed from a URL: mcp.cloudflare.com → cloudflare, openapi-mcp.cn-hangzhou.aliyuncs.com → aliyun. */
export function guessName(url) {
  let host;
  try { host = new URL(url).hostname; } catch { return ""; }
  if (/aliyuncs\.com$|aliyun\.com$/.test(host)) return "aliyun";
  const skip = new Set(["mcp", "www", "api", "com", "cn", "net", "org", "io", "dev", "app", "ai", "co", "localhost"]);
  const parts = host.split(".").filter((part) => part && !skip.has(part) && !/^\d+$/.test(part));
  if (!parts.length) return "";
  const last = parts[parts.length - 1];
  const raw = parts.length > 1 ? `${last}-${parts[0]}` : last;
  const name = raw.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return NAME_RULE.test(name) ? name : "";
}

async function startLogin(name) {
  try {
    const { authorize_url: url } = await fetchJSON(`/api/connectors/${encodeURIComponent(name)}/login`, { method: "POST" });
    window.location.href = url;
  } catch { /* toasted */ }
}

// ---------------------------------------------------------------- page

export async function connectorsPage(ctx, route) {
  const { page } = ctx;
  const addButton = el("button", { class: "btn btn-primary", type: "button", onclick: () => connectorDrawer(null, reload) }, icon("plus", { size: 16 }), "添加连接器");
  const holder = el("div", {}, skeletonRows(3));
  mount(page, head("连接器", "接入第三方 MCP 服务（阿里云、Cloudflare 等）。远程 HTTP / SSE 服务运行在网关上，7×24 可用，不依赖任何设备在线。", [addButton]),
    holder, howBanner());
  const testing = new Set();
  let items = [];

  async function reload() {
    try {
      items = (await fetchJSON("/api/connectors", { quiet: true })).connectors || [];
    } catch (error) {
      mount(holder, errorState(error, reload));
      return;
    }
    render();
  }

  function render() {
    if (!items.length) {
      mount(holder, el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({
        icon: "plug", title: "还没有连接器", text: "粘贴任意 HTTP / SSE MCP 地址，网关会自动识别传输方式和登录方式。",
        action: { label: "添加连接器", icon: "plus", onClick: () => connectorDrawer(null, reload) },
        secondary: { label: "阿里云 / Cloudflare 怎么填", href: "#/connectors?help=1" } }))));
      return;
    }
    mount(holder, el("div", { class: "table-wrap" }, el("table", { class: "table stack-sm connectors-table" },
      el("thead", {}, el("tr", {}, ["连接器", "状态", "传输", "鉴权", "运行位置", "工具", ""].map((h, i) => el("th", { class: i === 5 ? "num" : null }, h)))),
      el("tbody", {}, items.map(row)))));
  }

  function row(connector) {
    const busy = testing.has(connector.name);
    const state = busy ? "testing" : connector.state;
    const needsLogin = connector.state === "needs_login";
    const primary = needsLogin
      ? el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => startLogin(connector.name) }, icon("log-in", { size: 14 }), "登录")
      : el("button", { class: "btn btn-sm", type: "button", disabled: busy || !connector.enabled, onclick: () => runTest(connector) },
        busy ? el("span", { class: "spinner" }) : icon("activity", { size: 14 }), "测试");
    return el("tr", { dataset: { connector: connector.name } },
      el("td", { class: "cell-first" }, el("div", { class: "cell-main" },
        el("span", { class: "avatar accent" }, initial(connector)),
        el("div", { class: "stack", style: { gap: 0, minWidth: 0 } },
          el("span", { class: "title" }, connector.title || connector.name, connector.title ? el("span", { class: "faint xs mono" }, ` hub/${connector.name}`) : null),
          el("span", { class: "sub mono truncate", title: connector.url }, connector.url),
          connector.error && !busy && connector.state === "error" ? el("span", { class: "error-text xs truncate", title: connector.error }, connector.error) : null))),
      el("td", { "data-label": "状态" }, pill(state)),
      el("td", { "data-label": "传输" }, el("span", { class: "tag" }, transportText(connector))),
      el("td", { "data-label": "鉴权" }, AUTH_LABEL[connector.auth?.type] || "—",
        connector.auth?.type === "oauth" && connector.oauth?.logged_in ? el("span", { class: "faint xs" }, " · 已登录") : null),
      el("td", { "data-label": "运行位置" }, el("span", { class: "row nowrap", style: { gap: "6px" } }, icon("cloud", { size: 14 }), connector.run_on_display || "网关")),
      el("td", { "data-label": "工具", class: "num" }, connector.tools_count == null ? "—" : String(connector.tools_count)),
      el("td", { class: "actions" }, el("div", { class: "btn-group" }, primary,
        el("button", { class: "btn btn-ghost btn-sm btn-icon", type: "button", "aria-label": "更多操作", html: iconHTML("ellipsis", { size: 16 }),
          onclick: (event) => openMenu(event.currentTarget, [
            { label: "编辑", icon: "pencil", onClick: () => connectorDrawer(connector, reload) },
            connector.auth?.type === "oauth" && !needsLogin ? { label: "重新登录", icon: "log-in", onClick: () => startLogin(connector.name) } : null,
            connector.auth?.type === "oauth" && connector.oauth?.logged_in ? { label: "退出登录", icon: "log-out", onClick: () => logout(connector) } : null,
            { label: connector.enabled ? "停用" : "启用", icon: "power", onClick: () => toggle(connector) },
            { label: "复制 MCP ID", icon: "copy", onClick: () => copy(connector.mcp_id) },
            { separator: true },
            { label: "删除", icon: "trash", danger: true, onClick: () => remove(connector) },
          ]) }))));
  }

  async function runTest(connector) {
    testing.add(connector.name);
    render();
    let result;
    try {
      result = await fetchJSON(`/api/connectors/${encodeURIComponent(connector.name)}/test`, { method: "POST", quiet: true });
    } catch (error) {
      result = { ok: false, error: error.message };
    }
    testing.delete(connector.name);
    await reload();
    if (result.ok) showTools(connector, result);
    else if (result.state === "needs_login") toast(result.error || "需要登录", { type: "warn", title: "需要登录", action: { label: "登录", onClick: () => startLogin(connector.name) } });
    else toast(result.error || "测试失败", { type: "bad", title: `${connector.title || connector.name} 连接失败`, timeout: 8000 });
  }

  async function logout(connector) {
    if (!await confirmDialog({ title: `退出 ${connector.title || connector.name} 的登录？`, message: "网关上的令牌会被删除（服务支持时也会撤销）。之后需要重新登录才能调用它的工具。", confirmLabel: "退出登录", danger: true })) return;
    try { await fetchJSON(`/api/connectors/${encodeURIComponent(connector.name)}/logout`, { method: "POST" }); toast("已退出登录"); } catch { /* toasted */ }
    reload();
  }

  async function toggle(connector) {
    try {
      await fetchJSON(`/api/connectors/${encodeURIComponent(connector.name)}`, { method: "PUT", body: { enabled: !connector.enabled } });
      toast(connector.enabled ? "已停用：Agent 不再看到它的工具" : "已启用");
    } catch { /* toasted */ }
    reload();
  }

  async function remove(connector) {
    if (!await confirmDialog({ title: `删除 ${connector.title || connector.name}？`, message: "定义、密钥和登录令牌都会从网关删除，Agent 不再能调用 hub/" + connector.name + "。", confirmLabel: "删除", danger: true })) return;
    try { await fetchJSON(`/api/connectors/${encodeURIComponent(connector.name)}`, { method: "DELETE" }); toast("连接器已删除"); } catch { /* toasted */ }
    reload();
  }

  await reload();
  ctx.every(15000, () => { if (!testing.size && !document.querySelector(".overlay")) reload(); });
  const login = route.query.get("login");
  if (login) {
    const ok = route.query.get("result") === "ok";
    history.replaceState(null, "", "#/connectors");
    toast(ok ? `${login} 登录成功，工具已就绪` : `${login} 登录未完成（${route.query.get("reason") || "未知原因"}）`, { type: ok ? "ok" : "bad", timeout: 6000 });
  }
  if (route.query.get("open") === "add-connector") { history.replaceState(null, "", "#/connectors"); connectorDrawer(null, reload); }
  if (route.query.get("help")) { history.replaceState(null, "", "#/connectors"); helpDialog(); }
}

function howBanner() {
  return el("div", { class: "banner" }, icon("info", { size: 18 }),
    el("div", { class: "banner-body" },
      el("div", { class: "banner-title" }, "Agent 怎么用这些连接器？"),
      el("div", { class: "muted" }, "连接器的工具通过 ", el("code", {}, "toolsearch"), " / ", el("code", {}, "tooluse"), " 出现，MCP ID 为 ",
        el("code", {}, "hub/<名称>"), "。ChatGPT、Claude、Codex 都不需要重新连接；所有设备离线时也能调用。"),
      el("div", { style: { marginTop: "6px" } }, el("a", { href: "#/connectors?help=1" }, "阿里云、Cloudflare 的地址和鉴权怎么填"))));
}

function helpDialog() {
  const item = (title, url, text) => el("div", { class: "step" }, el("div", { class: "step-body" },
    el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, text), url ? el("pre", { class: "code-block" }, url) : null));
  modal({
    title: "常见服务怎么填", description: "粘贴地址后点“探测”，传输和鉴权会自动选好。",
    body: el("div", { class: "steps" },
      item("Cloudflare（API 全量）", "https://mcp.cloudflare.com/mcp", "传输 HTTP，鉴权 OAuth：保存后点“登录”，在 Cloudflare 页面授权。也可以改用 Bearer，填 Cloudflare API Token。"),
      item("Cloudflare 文档 / 其他产品", "https://docs.mcp.cloudflare.com/mcp", "其他官方服务器：bindings、observability、browser、containers 等，地址形如 https://<产品>.mcp.cloudflare.com/mcp。"),
      item("阿里云 OpenAPI MCP Server", "https://openapi-mcp.cn-hangzhou.aliyuncs.com/accounts/<账号>/custom/<名称>/id/<ID>/mcp",
        "在阿里云 OpenAPI 门户创建 MCP Server 后复制 Streamable HTTP 地址；鉴权 OAuth，登录用阿里云账号授权。"),
      item("只给了 API Key 的服务", null, "鉴权选 Header，例如 Authorization = Bearer <key> 或 X-Api-Key = <key>。值只写不读。")),
    actions: [{ label: "知道了", variant: "primary" }],
  });
}

function showTools(connector, result) {
  modal({
    title: `${connector.title || connector.name} 连接正常`,
    description: `${result.tools_count} 个工具 · ${TRANSPORT_LABEL[result.transport] || ""} · ${result.latency_ms} ms${result.server?.name ? ` · ${result.server.name}${result.server.version ? " " + result.server.version : ""}` : ""}`,
    body: result.tools.length ? el("div", { class: "list" }, result.tools.map((tool) => el("div", { class: "list-item" },
      el("span", { class: "avatar sm" }, icon("zap", { size: 14 })),
      el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } }, el("span", { class: "title mono truncate" }, tool.name),
        tool.description ? el("span", { class: "sub truncate", title: tool.description }, tool.description) : null)))) :
      el("p", { class: "muted" }, "服务没有提供工具（或都被 allow / deny 过滤了）。"),
    actions: [{ label: "关闭", variant: "primary" }],
  });
}

// ---------------------------------------------------------------- add / edit drawer

function segmented(options, value, onChange, label) {
  const group = el("div", { class: "segmented block", role: "group", "aria-label": label });
  for (const [key, text] of options) {
    group.append(el("button", { type: "button", "aria-pressed": String(key === value), dataset: { value: key }, onclick: () => set(key) }, text));
  }
  function set(key) {
    for (const button of group.children) button.setAttribute("aria-pressed", String(button.dataset.value === key));
    onChange(key);
  }
  return { node: group, set };
}

export function connectorDrawer(existing, reload) {
  const editing = Boolean(existing);
  const secretHint = editing ? "已设置，留空保持不变" : "只写不读，保存后不再显示";
  const state = {
    transport: existing?.transport || "auto",
    auth: existing?.auth?.type || "none",
    clientMode: existing?.auth?.client?.mode || "dynamic",
  };
  const url = el("input", { class: "input mono", id: "c-url", value: existing?.url || "", autocomplete: "off", spellcheck: "false",
    placeholder: "https://mcp.example.com/mcp", inputmode: "url" });
  const probeButton = el("button", { class: "btn", type: "button", onclick: () => probe() }, icon("search", { size: 14 }), "探测");
  const probeBox = el("div");
  const name = el("input", { class: "input mono", id: "c-name", value: existing?.name || "", maxlength: "40", disabled: editing,
    autocomplete: "off", spellcheck: "false", placeholder: "例如 cloudflare" });
  const nameHint = el("span", { class: "hint" }, `工具显示为 hub/${existing?.name || "<名称>"}`);
  const title = el("input", { class: "input", id: "c-title", value: existing?.title || "", maxlength: "64", placeholder: "例如 Cloudflare" });
  name.addEventListener("input", () => { nameHint.textContent = `工具显示为 hub/${name.value || "<名称>"}`; });
  url.addEventListener("change", () => { if (!editing && !name.value) { name.value = guessName(url.value); name.dispatchEvent(new Event("input")); } });

  const transport = segmented([["auto", "自动"], ["http", "HTTP"], ["sse", "SSE"]], state.transport, (v) => { state.transport = v; }, "传输");

  // auth panes
  const token = el("input", { class: "input mono", id: "c-token", type: "password", autocomplete: "new-password", placeholder: existing?.auth?.type === "bearer" ? secretHint : "API Token" });
  const headerRows = el("div", { class: "stack" });
  const addHeader = (key = "", placeholder = "") => {
    const keyInput = el("input", { class: "input mono", value: key, "aria-label": "Header 名", placeholder: "Authorization", autocomplete: "off", spellcheck: "false" });
    const valueInput = el("input", { class: "input mono", type: "password", "aria-label": "Header 值", placeholder: placeholder || "值", autocomplete: "new-password" });
    const line = el("div", { class: "header-row" }, keyInput, valueInput,
      el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "删除这个 Header", html: iconHTML("x", { size: 14 }), onclick: () => line.remove() }));
    headerRows.append(line);
  };
  if (existing?.auth?.type === "headers") for (const key of existing.auth.header_names || []) addHeader(key, secretHint);
  else addHeader("Authorization", "Bearer <key>");
  const clientId = el("input", { class: "input mono", id: "c-client", value: existing?.auth?.client?.client_id || "", placeholder: "预先在服务商注册的 Client ID", autocomplete: "off" });
  const clientSecret = el("input", { class: "input mono", id: "c-client-secret", type: "password", autocomplete: "new-password",
    placeholder: existing?.auth?.client?.secret_set ? secretHint : "公共客户端可留空" });
  const scopes = el("input", { class: "input mono", id: "c-scopes", value: (existing?.auth?.scopes || []).join(" "), placeholder: "留空 = 按服务的要求" });
  const staticFields = el("div", { class: "stack" },
    el("div", { class: "form-row" },
      el("div", { class: "field" }, el("label", { for: "c-client" }, "Client ID"), clientId),
      el("div", { class: "field" }, el("label", { for: "c-client-secret" }, "Client Secret ", el("span", { class: "optional" }, "可选")), clientSecret)),
    el("span", { class: "hint" }, "在服务商的应用设置里登记回调地址：", el("code", {}, `${location.origin}/oauth/connectors/callback/${existing?.name || "<名称>"}`)));
  const clientSeg = segmented([["dynamic", "自动注册（推荐）"], ["static", "预注册的 Client ID"]], state.clientMode, (v) => {
    state.clientMode = v;
    staticFields.hidden = v !== "static";
  }, "OAuth 客户端");
  staticFields.hidden = state.clientMode !== "static";
  const panes = {
    none: el("div", { class: "hint" }, "服务不需要认证（或只在内网可用）。"),
    bearer: el("div", { class: "field" }, el("label", { for: "c-token" }, "Token"), token, el("span", { class: "hint" }, "以 Authorization: Bearer <token> 发送。可写 {env:变量名} 引用网关的环境变量。")),
    headers: el("div", { class: "stack" }, headerRows,
      el("button", { class: "btn btn-ghost btn-sm", type: "button", style: { alignSelf: "flex-start" }, onclick: () => addHeader() }, icon("plus", { size: 14 }), "添加 Header"),
      el("span", { class: "hint" }, `Header 值只写不读${editing ? "；留空的值保持不变" : ""}。`)),
    oauth: el("div", { class: "stack" },
      el("span", { class: "hint" }, "保存后会跳到服务商的登录页；在网关域名上完成授权，令牌保存在网关，不会下发到设备，到期自动刷新。"),
      clientSeg.node, staticFields,
      el("div", { class: "field" }, el("label", { for: "c-scopes" }, "Scope ", el("span", { class: "optional" }, "可选，空格分隔")), scopes)),
  };
  const paneBox = el("div", { style: { marginTop: "8px" } });
  const showPane = (key) => { state.auth = key; mount(paneBox, panes[key]); };
  const authSeg = segmented([["none", "无"], ["bearer", "Bearer"], ["headers", "Header"], ["oauth", "OAuth"]], state.auth, showPane, "鉴权");
  showPane(state.auth);

  const expose = el("select", { class: "select", id: "c-expose" },
    el("option", { value: "search", selected: (existing?.expose || "search") === "search" }, "仅搜索（toolsearch / tooluse）"),
    el("option", { value: "direct", selected: existing?.expose === "direct" }, "直接列出（即将推出，现按仅搜索处理）"));
  const timeout = el("input", { class: "input", id: "c-timeout", type: "number", min: "5", max: "600", value: String(existing?.timeout_s || 120) });
  const allow = el("input", { class: "input mono", id: "c-allow", value: (existing?.tools?.allow || []).join(", "), placeholder: "留空 = 全部" });
  const deny = el("input", { class: "input mono", id: "c-deny", value: (existing?.tools?.deny || []).join(", "), placeholder: "例如 delete_zone" });
  const list = (input) => input.value.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean);

  const runOn = el("div", { class: "choices" },
    el("label", { class: "choice" }, el("input", { type: "radio", name: "run_on", value: "gateway", checked: true }), icon("cloud", { size: 18 }),
      el("span", { class: "stack", style: { gap: 0 } }, el("span", { class: "choice-title" }, "网关（推荐）"), el("span", { class: "choice-sub" }, "7×24 可用，不依赖设备"))),
    el("label", { class: "choice disabled", title: "即将推出" }, el("input", { type: "radio", name: "run_on", value: "device", disabled: true }), icon("monitor", { size: 18 }),
      el("span", { class: "stack", style: { gap: 0 } }, el("span", { class: "choice-title" }, "固定到设备 ", el("span", { class: "tag" }, "即将推出")),
        el("span", { class: "choice-sub" }, "stdio、内网或限制 IP 的服务"))));

  function body() {
    const auth = { type: state.auth };
    if (state.auth === "bearer") auth.token = token.value.trim() || undefined;
    if (state.auth === "headers") {
      auth.headers = {};
      for (const line of headerRows.children) {
        const [key, value] = line.querySelectorAll("input");
        if (key.value.trim()) auth.headers[key.value.trim()] = value.value.trim() || null;
      }
    }
    if (state.auth === "oauth") {
      auth.scopes = scopes.value.split(/\s+/).filter(Boolean);
      auth.client = state.clientMode === "static"
        ? { mode: "static", client_id: clientId.value.trim(), client_secret: clientSecret.value.trim() || undefined }
        : { mode: "dynamic" };
    }
    const data = { url: url.value.trim(), title: title.value.trim() || null, transport: state.transport, run_on: "gateway",
      auth, expose: expose.value, timeout_s: Number(timeout.value) || 120, tools: { allow: list(allow), deny: list(deny) } };
    if (!editing) data.name = name.value.trim();
    // An unchanged auth section in edit mode keeps every stored secret.
    if (editing && state.auth === existing.auth.type && state.auth !== "oauth" && state.auth !== "headers" && !auth.token) delete data.auth;
    return data;
  }

  async function probe() {
    if (!url.value.trim()) { url.focus(); return null; }
    mount(probeBox, el("div", { class: "banner info" }, el("span", { class: "spinner" }), el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, "正在探测…"))));
    let result;
    try {
      const request = { url: url.value.trim(), transport: state.transport };
      if (state.auth === "bearer" && token.value.trim()) request.auth = { type: "bearer", token: token.value.trim() };
      if (state.auth === "headers") request.auth = body().auth;
      result = await fetchJSON("/api/connectors/probe", { method: "POST", body: request, quiet: true });
    } catch (error) {
      mount(probeBox, banner("bad", "探测失败", error.message));
      return null;
    }
    if (!editing && !name.value) { name.value = guessName(url.value); name.dispatchEvent(new Event("input")); }
    if (!title.value && result.server?.name) title.placeholder = result.server.name;
    if (result.error && !result.needs_oauth && result.auth_hint !== "bearer") {
      mount(probeBox, banner("bad", "探测失败", result.error));
      return result;
    }
    if (result.transport && state.transport === "auto") { /* keep 自动; the detected value shows after saving */ }
    if (result.needs_oauth) authSeg.set("oauth");
    else if (result.auth_hint === "bearer" && state.auth === "none") authSeg.set("bearer");
    const parts = [result.transport ? `服务使用 ${TRANSPORT_LABEL[result.transport]} 传输` : "服务需要认证",
      result.needs_oauth ? "需要 OAuth 登录（已为你选好）" : result.auth_hint === "bearer" ? "需要 Token 或 Header（已切到 Bearer，也可以选 Header）" : "不需要登录",
      result.tools_count != null ? `发现 ${result.tools_count} 个工具` : null].filter(Boolean);
    mount(probeBox, banner(result.error ? "warn" : "ok", result.error ? "探测完成，有提示" : "探测成功", parts.join("，") + "。" + (result.error ? ` ${result.error}` : "")));
    return result;
  }

  const panel = drawer({
    title: editing ? `编辑 ${existing.title || existing.name}` : "添加连接器",
    description: editing ? `MCP ID：hub/${existing.name}` : "粘贴 MCP 地址，网关会探测传输方式以及是否需要登录。",
    body: el("form", { class: "form", onsubmit: (event) => event.preventDefault() },
      el("div", { class: "field" }, el("label", { for: "c-url" }, "MCP 地址"),
        el("div", { class: "input-group" }, url, probeButton),
        el("span", { class: "hint" }, "Streamable HTTP 或 SSE 地址。stdio 命令（npx …）需要固定到设备，即将推出。")),
      probeBox,
      el("div", { class: "form-row" },
        el("div", { class: "field" }, el("label", { for: "c-name" }, "名称"), name, nameHint),
        el("div", { class: "field" }, el("label", { for: "c-title" }, "显示名 ", el("span", { class: "optional" }, "可选")), title)),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "运行位置"), runOn),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "传输"), transport.node,
        el("span", { class: "hint" }, "自动：先试 Streamable HTTP，不支持时退回 SSE。")),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "鉴权"), authSeg.node, paneBox),
      el("details", { class: "advanced" }, el("summary", {}, "高级选项"),
        el("div", { class: "form", style: { marginTop: "12px" } },
          el("div", { class: "form-row" },
            el("div", { class: "field" }, el("label", { for: "c-expose" }, "工具暴露方式"), expose),
            el("div", { class: "field" }, el("label", { for: "c-timeout" }, "超时（秒）"), timeout)),
          el("div", { class: "form-row" },
            el("div", { class: "field" }, el("label", { for: "c-allow" }, "只允许这些工具 ", el("span", { class: "optional" }, "逗号分隔")), allow),
            el("div", { class: "field" }, el("label", { for: "c-deny" }, "屏蔽这些工具 ", el("span", { class: "optional" }, "逗号分隔")), deny))))),
    footLeft: el("button", { class: "btn btn-ghost", type: "button", onclick: () => probe() }, icon("activity", { size: 16 }), "测试连接"),
    actions: [{ label: "取消" }, { label: editing ? "保存" : "保存并连接", variant: "primary", onClick: save }],
  });
  if (!editing) requestAnimationFrame(() => url.focus());

  async function save() {
    const data = body();
    if (!data.url) { toast("请填写 MCP 地址", { type: "bad" }); url.focus(); return false; }
    if (!editing && !NAME_RULE.test(data.name || "")) { toast("名称只能使用小写字母、数字和连字符", { type: "bad" }); name.focus(); return false; }
    let saved;
    try {
      saved = editing
        ? await fetchJSON(`/api/connectors/${encodeURIComponent(existing.name)}`, { method: "PUT", body: data })
        : await fetchJSON("/api/connectors", { method: "POST", body: data });
    } catch { return false; }
    const connector = saved.connector;
    reload?.();
    if (connector.auth?.type === "oauth" && !connector.oauth?.logged_in) {
      toast("已保存，正在打开登录页…", { type: "info" });
      startLogin(connector.name);
      return true;
    }
    toast(editing ? "已保存" : "连接器已添加，正在连接…");
    setTimeout(() => reload?.(), 1500);
    return true;
  }
  return panel;
}

function banner(tone, title, text) {
  const glyph = { ok: "circle-check", bad: "circle-alert", warn: "triangle-alert", info: "info" }[tone];
  return el("div", { class: `banner ${tone}` }, icon(glyph, { size: 18 }),
    el("div", { class: "banner-body" }, el("div", { class: "banner-title" }, title), text ? el("div", { class: "muted" }, text) : null));
}
