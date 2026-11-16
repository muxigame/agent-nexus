// The inbox on the design system (docs/ui-spec.md §3.5; decision D11, design §8.12).
//   import { mountInbox } from "/ui/inbox.js";
//   const inbox = mountInbox(target, { base: "/api/inbox", threadId, onUnread(unread, openAsks), onOpen(threadId) });
//   inbox.refresh();
// Same API as static/inbox.js (the legacy pages keep using that one). Everything user-supplied is text.

import { el, mount, icon, iconHTML, fetchJSON, toast, relTime, emptyState, skeletonRows, pill, drawer } from "./ui.js";

/** Question-card order: the recommended option first, the others in order, then the free answer. */
export function orderOptions(ask) {
  const options = Array.isArray(ask.options) ? ask.options.slice() : [];
  if (ask.recommended && options.includes(ask.recommended)) return [ask.recommended, ...options.filter((item) => item !== ask.recommended)];
  return options;
}

const KIND_TEXT = { question: "提问", answer: "回答", system: "系统", notice: "提醒", reply: "回复" };

function answerText(answer) {
  if (!answer) return "";
  if (answer.choice) return answer.choice;
  if (answer.choices) return answer.choices.join("、");
  return answer.text || "";
}

function agoText(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function folderName(path) {
  const parts = String(path || "").split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || "";
}

/** A legacy anonymous address (one per reconnect, from before automatic names; design §8.2). */
function legacyAnon(a) {
  return a.name_source === "anon" || (!a.name_source && /^anon-/.test(a.name || a.address || ""));
}

/** One viewer option: title (or workdir folder) — name · last seen · unread（只读）. */
export function viewerLabel(a) {
  const name = a.name || String(a.address || "").split("@")[0];
  const label = a.title || folderName(a.workdir);
  const parts = [label && label !== name ? `${label} — ${name}` : name];
  const ago = agoText(a.last_seen_s_ago);
  if (ago) parts.push(ago);
  if (a.unread) parts.push(`${a.unread} 未读`);
  return `${parts.join(" · ")}（只读）`;
}

/** The viewer dropdown: addresses grouped by device, most recently seen first; legacy anon rows only with mail (read or unread). */
export function viewerGroups(addresses, current = null) {
  const seen = (a) => (a.last_seen_s_ago === null || a.last_seen_s_ago === undefined ? Infinity : a.last_seen_s_ago);
  const rows = (addresses || []).filter((a) => a.address !== "me" && (!legacyAnon(a) || a.unread > 0 || a.mail > 0 || a.address === current))
    .sort((x, y) => (seen(x) - seen(y)) || String(x.address).localeCompare(String(y.address)));
  const groups = new Map();
  for (const a of rows) {
    const device = a.device_display || a.agent_id || "其他";
    if (!groups.has(device)) groups.set(device, []);
    groups.get(device).push({ value: a.address, text: viewerLabel(a), title: a.address_display || a.address });
  }
  return [...groups].map(([label, items]) => ({ label, items }));
}

function initial(name) {
  const text = String(name || "?").replace(/^[^A-Za-z0-9一-鿿]+/, "");
  return (text[0] || "?").toUpperCase();
}

export function mountInbox(target, { base = "/api/inbox", threadId = null, onUnread = null, onOpen = null, api = null } = {}) {
  const call = api || ((method, path, body) => fetchJSON(base + path, { method, body }));
  const enc = encodeURIComponent;
  const state = { threads: [], asks: [], unread: 0, viewer: "me", addresses: [], current: threadId, view: null,
    composing: false, loaded: false, error: null, query: "", generation: 0 };

  const search = el("input", { class: "input", placeholder: "搜索主题或发件人", "aria-label": "搜索", type: "search",
    oninput: (event) => { state.query = event.target.value.trim().toLowerCase(); renderList(); } });
  const listBody = el("div", { class: "inbox-threads" });
  const listPane = el("div", { class: "inbox-list" },
    el("div", { class: "inbox-toolbar" }, el("div", { class: "input-icon grow" }, icon("search", { size: 14 }), search)),
    listBody);
  const threadPane = el("div", { class: "thread-view" });
  const container = el("div", { class: "inbox" }, listPane, threadPane);
  const viewerSelect = el("select", { class: "select", "aria-label": "查看邮箱", style: { width: "auto", maxWidth: "100%" },
    onchange: (event) => { state.viewer = event.target.value || "me"; state.current = null; state.view = null; refresh(); } });
  const composeButton = el("button", { class: "btn btn-primary", type: "button", onclick: () => compose() }, icon("send", { size: 16 }), "写邮件");
  mount(target, container);

  function renderViewer() {
    const options = [el("option", { value: "me" }, "me（我）"),
      ...viewerGroups(state.addresses, state.viewer).map((group) => el("optgroup", { label: group.label },
        group.items.map((item) => el("option", { value: item.value, title: item.title }, item.text))))];
    mount(viewerSelect, options);
    viewerSelect.value = state.viewer;
  }

  function renderList() {
    if (!state.loaded) { mount(listBody, skeletonRows(4)); return; }
    if (state.error) { mount(listBody, emptyState({ icon: "circle-alert", title: "收件箱不可用", text: state.error, compact: true })); return; }
    const openAsks = state.viewer === "me" ? new Set(state.asks.filter((a) => a.state === "open").map((a) => a.thread_id)) : new Set();
    let threads = state.threads.slice().sort((a, b) => (openAsks.has(b.thread_id) - openAsks.has(a.thread_id)) || ((b.last_at || 0) - (a.last_at || 0)));
    if (state.query) {
      threads = threads.filter((t) => [t.subject, t.last_from, t.last_snippet, ...(t.participants || [])].join(" ").toLowerCase().includes(state.query));
    }
    if (!threads.length) {
      mount(listBody, emptyState({ icon: "inbox", title: state.query ? "没有匹配的邮件" : "收件箱是空的", compact: true,
        text: state.query ? "" : "Agent 用 mail_send 发给 me 或调用 ask_user 时会出现在这里" }));
      return;
    }
    mount(listBody, threads.map((thread) => {
      const asking = openAsks.has(thread.thread_id) || thread.open_asks > 0;
      return el("div", { class: `thread-item ${thread.unread ? "unread" : ""} ${thread.thread_id === state.current ? "active" : ""}`,
        role: "button", tabindex: "0", dataset: { thread: thread.thread_id },
        onclick: () => open(thread.thread_id), onkeydown: (event) => { if (event.key === "Enter") open(thread.thread_id); } },
        el("div", { class: "who" }, thread.unread ? el("span", { class: "dot info" }) : null,
          el("span", { class: "truncate" }, thread.last_from || (thread.participants || []).join("、")), el("span", { class: "when" }, relTime(thread.last_at))),
        el("div", { class: "subject" }, thread.subject || "（无主题）"),
        el("div", { class: "snippet" }, asking ? "待回答 · " : "", thread.last_snippet || ""));
    }));
  }

  function questionCard(ask) {
    const readOnly = state.viewer !== "me";
    const card = el("div", { class: "ask-card", dataset: { ask: ask.ask_id } }, el("div", { class: "strong" }, ask.question));
    if (ask.context) card.append(el("div", { class: "message-body small muted" }, ask.context));
    if (ask.state !== "open") {
      card.append(el("div", { class: "row" }, pill(ask.state === "answered" ? "ok" : "neutral", ask.state === "answered" ? "已回答" : ask.state),
        el("span", { class: "small" }, answerText(ask.answer)), ask.answered_via ? el("span", { class: "faint xs" }, `经 ${ask.answered_via}`) : null));
      return card;
    }
    const name = `ask-${ask.ask_id}`;
    const options = orderOptions(ask);
    const multi = ask.kind === "multi";
    const inputs = options.map((value, index) => {
      const input = el("input", { type: multi ? "checkbox" : "radio", name, value, disabled: readOnly, checked: !multi && index === 0 });
      return { input, node: el("label", { class: "choice" }, input, el("span", { class: "choice-title" }, value,
        value === ask.recommended ? el("span", { class: "pill accent plain", style: { marginLeft: "8px" } }, "推荐") : null)) };
    });
    const other = ask.allow_other !== false || !options.length
      ? el("input", { class: "input", placeholder: options.length ? "其他（自由填写）" : "你的回答", "aria-label": "其他", disabled: readOnly }) : null;
    const submit = el("button", { class: "btn btn-primary", type: "button", disabled: readOnly, dataset: { submit: ask.ask_id }, onclick: async () => {
      let payload;
      if (other && other.value.trim()) payload = { text: other.value.trim() };
      else if (multi) {
        const choices = inputs.filter((item) => item.input.checked).map((item) => item.input.value);
        if (choices.length) payload = { choices };
      } else {
        const chosen = inputs.find((item) => item.input.checked);
        if (chosen) payload = { choice: chosen.input.value };
      }
      if (!payload) { toast("请选择一个选项或填写回答", { type: "warn" }); return; }
      submit.disabled = true;
      try {
        const result = await call("POST", `/asks/${enc(ask.ask_id)}/answer`, payload);
        toast(result.already ? (result.message || "已经回答过了") : "已回答，提问的会话会被唤醒", { type: result.already ? "warn" : "ok" });
      } catch { /* toasted by fetchJSON */ }
      await refresh();
    } }, "提交回答");
    card.append(el("div", { class: "stack" }, inputs.map((item) => item.node), other), el("div", { class: "row" }, submit,
      readOnly ? el("span", { class: "faint xs" }, "只读：切回 me 才能作答") : null));
    return card;
  }

  function messageNode(message, ask) {
    return el("div", { class: "message", dataset: { message: message.id } },
      el("div", { class: "message-head" }, el("span", { class: `avatar sm ${message.mine ? "accent" : ""}` }, initial(message.from)),
        el("span", { class: "strong truncate" }, message.from_display || message.from),
        el("span", { class: "faint xs" }, `${message.to ? `→ ${[].concat(message.to).join("、")} · ` : ""}${relTime(message.created_at)}`),
        message.kind && message.kind !== "message" ? el("span", { class: "tag" }, KIND_TEXT[message.kind] || message.kind) : null,
        message.priority === "urgent" ? el("span", { class: "pill bad plain" }, "紧急") : null),
      message.body && !ask ? el("div", { class: "message-body" }, message.body) : null,
      message.mine && Array.isArray(message.recipients) && message.recipients.length
        ? el("div", { class: "row wrap" }, message.recipients.map((r) => el("span", { class: "tag" }, `${r.to_display || r.to}：${r.status}`))) : null,
      ask ? questionCard(ask) : null);
  }

  function renderThread() {
    container.classList.toggle("show-thread", Boolean(state.current || state.composing));
    if (state.composing) return;
    if (!state.current) {
      mount(threadPane, emptyState({ icon: "message-square", title: "选择一个线程", text: "左侧列表里的邮件和提问会显示在这里", compact: true }));
      return;
    }
    const view = state.view;
    if (!view) { mount(threadPane, skeletonRows(3)); return; }
    const asks = new Map((view.asks || []).map((ask) => [ask.message_id, ask]));
    const messages = view.messages || [];
    const last = messages[messages.length - 1];
    const openAsk = (view.asks || []).some((a) => a.state === "open");
    const subject = (messages.find((m) => m.subject) || {}).subject || "（无主题）";
    const draft = el("textarea", { class: "textarea", rows: 2, "aria-label": "回复", disabled: state.viewer !== "me",
      placeholder: state.viewer === "me" ? "以 me 身份回复…（会唤醒收件人）" : "只读：切回 me 才能回复" });
    const send = el("button", { class: "btn btn-primary btn-icon", type: "button", "aria-label": "发送", disabled: state.viewer !== "me" || !last,
      html: iconHTML("send", { size: 16 }), onclick: async () => {
        if (!draft.value.trim() || !last) return;
        send.disabled = true;
        try {
          await call("POST", "/send", { reply_to: last.id, body: draft.value });
          draft.value = "";
          toast("已回复");
        } catch { /* toasted */ }
        send.disabled = false;
        await refresh();
      } });
    mount(threadPane,
      el("div", { class: "message", style: { paddingBottom: "12px" } },
        el("div", { class: "row" },
          el("button", { class: "btn btn-ghost btn-sm only-sm", type: "button", onclick: () => { state.current = null; state.view = null; onOpen?.(null); renderThread(); renderList(); } },
            icon("arrow-left", { size: 14 }), "返回"),
          el("h2", { class: "grow truncate", style: { fontSize: "16px" } }, subject), openAsk ? pill("needs_config", "待回答") : null),
        el("div", { class: "faint xs" }, `参与者：${(view.participants || []).join("、")}`)),
      el("div", { class: "thread-messages" }, messages.map((message) => messageNode(message, asks.get(message.id)))),
      el("div", { class: "composer" }, draft, send));
  }

  function compose() {
    const to = el("input", { class: "input", id: "compose-to", placeholder: "codex@pc-main、@all 或 @device:<设备>" });
    const subject = el("input", { class: "input", id: "compose-subject" });
    const body = el("textarea", { class: "textarea", id: "compose-body", rows: 6 });
    drawer({
      title: "写邮件", description: "以 me 身份发送；收件人会在下一次调用工具时看到提醒，空闲的会话会被唤醒。",
      body: el("div", { class: "form" },
        el("div", { class: "field" }, el("label", { for: "compose-to" }, "收件人"), to, el("span", { class: "hint" }, "多个收件人用逗号或空格分隔")),
        el("div", { class: "field" }, el("label", { for: "compose-subject" }, "主题 ", el("span", { class: "optional" }, "可选")), subject),
        el("div", { class: "field" }, el("label", { for: "compose-body" }, "正文"), body)),
      actions: [{ label: "取消" }, { label: "发送", variant: "primary", icon: "send", onClick: async () => {
        if (!to.value.trim() || !body.value.trim()) { toast("请填写收件人和正文", { type: "warn" }); return false; }
        try {
          const sent = await call("POST", "/send", { to: to.value.trim().split(/[,，\s]+/).filter(Boolean), subject: subject.value, body: body.value });
          toast("已发送");
          await refresh();
          if (sent.thread_id) open(sent.thread_id);
        } catch { return false; }
        return true;
      } }],
    });
  }

  async function refresh() {
    const generation = ++state.generation;
    try {
      const viewer = state.viewer === "me" ? "" : `?viewer=${enc(state.viewer)}`;
      const data = await call("GET", `/threads${viewer}`);
      if (generation !== state.generation) return;
      state.threads = data.threads || [];
      state.asks = data.open_asks || [];
      state.unread = data.unread || 0;
      state.error = null;
      state.loaded = true;
      if (!state.addresses.length) {
        call("GET", "/addresses").then((d) => { state.addresses = d.addresses || []; renderViewer(); }).catch(() => {});
      }
      if (state.current) {
        const view = await call("GET", `/threads/${enc(state.current)}${viewer}`);
        if (generation === state.generation) state.view = view;
      }
      onUnread?.(state.viewer === "me" ? state.unread : null, state.asks.length);
    } catch (error) {
      if (generation !== state.generation) return;
      state.loaded = true;
      state.error = error.message || String(error);
    }
    renderList();
    renderThread();
  }

  function open(id) {
    state.current = id;
    state.view = null;
    state.composing = false;
    onOpen?.(id);
    renderList();
    renderThread();
    return refresh();
  }

  renderViewer();
  renderList();
  renderThread();
  return { refresh, open, state, root: container, viewerSelect, composeButton, compose };
}
