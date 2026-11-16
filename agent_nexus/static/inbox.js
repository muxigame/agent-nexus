/* agent-nexus inbox (decision D11, design §8.12).
 *
 * One implementation for the local console / desktop (served at /inbox.js,
 * API /api/inbox/* proxied to the gateway through hub_call mail_local) and
 * the gateway's mobile PWA (served at /inbox/inbox.js, API /api/inbox/*).
 * The host page supplies api(method, path, body) and calls refresh().
 * Everything user-supplied is inserted as text, never as HTML.
 */
(function (global) {
  "use strict";

  const STYLE = `
.anx-inbox{display:grid;grid-template-columns:minmax(220px,320px) 1fr;gap:12px;min-height:60vh}
.anx-list,.anx-thread{border:1px solid var(--line,#d5dce5);border-radius:10px;background:var(--panel,#fff);overflow:auto}
.anx-list header,.anx-thread header{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid var(--line,#d5dce5)}
.anx-list h2,.anx-thread h2{font-size:15px;margin:0}
.anx-threads{list-style:none;margin:0;padding:0}
.anx-threads li{padding:10px 12px;border-bottom:1px solid var(--line,#eef1f5);cursor:pointer}
.anx-threads li.active{background:var(--hover,#eef4ff)}
.anx-threads li .anx-sub{font-weight:600;display:flex;justify-content:space-between;gap:6px}
.anx-threads li .anx-snip{color:var(--muted,#5b6b7f);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.anx-badge{background:#d23c3c;color:#fff;border-radius:9px;padding:0 7px;font-size:12px;font-weight:600}
.anx-pending{padding:8px 12px;border-bottom:1px solid var(--line,#d5dce5);background:var(--warn-bg,#fff8e6)}
.anx-pending h3{font-size:13px;margin:0 0 6px}
.anx-msg{padding:10px 12px;border-bottom:1px solid var(--line,#eef1f5)}
.anx-msg.mine{background:var(--mine,#f6f9ff)}
.anx-meta{color:var(--muted,#5b6b7f);font-size:12px;display:flex;flex-wrap:wrap;gap:8px}
.anx-body{white-space:pre-wrap;word-break:break-word;margin:6px 0 0}
.anx-card{margin:8px 0;padding:10px;border:1px solid #e3b341;border-radius:8px;background:#fffaf0}
.anx-card .anx-q{font-weight:600;margin:0 0 6px}
.anx-card button,.anx-reply button,.anx-compose button{margin:4px 6px 4px 0;padding:8px 12px;border-radius:6px;border:1px solid #9fb3c8;background:#fff;cursor:pointer;font:inherit}
.anx-card button.anx-rec{border-color:#2f6fd6;background:#2f6fd6;color:#fff}
.anx-card input[type=text],.anx-reply textarea,.anx-compose input,.anx-compose textarea{box-sizing:border-box;width:100%;padding:8px;font:inherit;border:1px solid #c5d0dc;border-radius:6px}
.anx-reply,.anx-compose{padding:10px 12px}
.anx-empty{color:var(--muted,#5b6b7f);padding:16px}
.anx-back{display:none}
@media (max-width:700px){
 .anx-inbox{grid-template-columns:1fr}
 .anx-inbox.has-thread .anx-list{display:none}
 .anx-inbox:not(.has-thread) .anx-thread{display:none}
 .anx-back{display:inline-block}
}`;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "class") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child != null && child !== false) node.append(child);
    }
    return node;
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
  function viewerLabel(a) {
    const name = a.name || String(a.address || "").split("@")[0];
    const label = a.title || folderName(a.workdir);
    const parts = [label && label !== name ? `${label} — ${name}` : name];
    const ago = agoText(a.last_seen_s_ago);
    if (ago) parts.push(ago);
    if (a.unread) parts.push(`${a.unread} 未读`);
    return `${parts.join(" · ")}（只读）`;
  }

  /** The viewer dropdown: grouped by device, most recently seen first; legacy anon rows only with mail. */
  function viewerGroups(addresses, current) {
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

  /** Question-card order: the recommended option first, the rest in order, then "other". */
  function orderOptions(ask) {
    const options = Array.isArray(ask.options) ? ask.options.slice() : [];
    if (ask.recommended && options.includes(ask.recommended)) {
      return [ask.recommended, ...options.filter((item) => item !== ask.recommended)];
    }
    return options;
  }

  function cardEntries(ask) {
    const entries = orderOptions(ask).map((value) => ({ value, label: value, recommended: value === ask.recommended }));
    if (ask.allow_other !== false || !entries.length) entries.push({ value: null, label: "其他", other: true });
    return entries;
  }

  function when(seconds) {
    if (!seconds) return "";
    const date = new Date(seconds * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function answerText(answer) {
    if (!answer) return "";
    if (answer.choice) return answer.choice;
    if (answer.choices) return answer.choices.join("、");
    return answer.text || "";
  }

  function injectStyle() {
    if (typeof document === "undefined" || !document.head || document.getElementById?.("anx-inbox-style")) return;
    const style = document.createElement("style");
    style.id = "anx-inbox-style";
    style.textContent = STYLE;
    document.head.append(style);
  }

  function mount(root, opts) {
    injectStyle();
    const base = opts.base || "/api/inbox";
    const enc = encodeURIComponent;
    const state = { threads: [], asks: [], current: opts.threadId || null, view: null, viewer: "me",
      addresses: [], composing: false, error: "", generation: 0, loading: false };
    const list = el("ul", { class: "anx-threads" });
    const pending = el("div", { class: "anx-pending", hidden: true });
    const counter = el("span", { class: "anx-badge", hidden: true });
    const viewerSelect = el("select", { title: "查看哪个邮箱", onchange: (e) => { state.viewer = e.currentTarget.value || "me"; state.current = null; state.view = null; refresh(); } });
    const listPane = el("aside", { class: "anx-list" },
      el("header", {}, el("h2", {}, "收件箱 ", counter),
        el("button", { class: "btn small", onclick: () => { state.composing = true; state.current = null; renderThread(); } }, "新邮件")),
      el("div", { class: "anx-viewer" }, viewerSelect), pending, list);
    const threadPane = el("section", { class: "anx-thread" });
    const container = el("div", { class: "anx-inbox" }, listPane, threadPane);
    root.replaceChildren(container);

    function call(method, path, body) { return opts.api(method, base + path, body); }

    function notify(text, tone) { if (opts.toast) opts.toast(text, tone); }

    function renderViewer() {
      const options = [el("option", { value: "me" }, "me（我）"),
        ...viewerGroups(state.addresses, state.viewer).map((group) => el("optgroup", { label: group.label },
          group.items.map((item) => el("option", { value: item.value, title: item.title }, item.text))))];
      viewerSelect.replaceChildren(...options);
      viewerSelect.value = state.viewer;
    }

    function renderList() {
      const unread = state.viewer === "me" ? state.unread || 0 : 0;
      counter.textContent = String(unread);
      counter.hidden = !unread;
      const open = state.viewer === "me" ? state.asks.filter((ask) => ask.state === "open") : [];
      pending.hidden = !open.length;
      pending.replaceChildren(el("h3", {}, `待回答（${open.length}）`),
        ...open.map((ask) => el("div", { class: "anx-snip" },
          el("a", { href: "#", onclick: (e) => { e.preventDefault(); openThread(ask.thread_id); } }, `${ask.from}：${ask.question}`))));
      if (!state.threads.length) {
        list.replaceChildren(el("li", { class: "anx-empty" }, state.error || "没有邮件"));
        return;
      }
      list.replaceChildren(...state.threads.map((thread) => el("li", {
        class: thread.thread_id === state.current ? "active" : "", "data-thread": thread.thread_id,
        onclick: () => openThread(thread.thread_id) },
        el("div", { class: "anx-sub" }, el("span", {}, thread.subject || "(无主题)"),
          thread.unread ? el("span", { class: "anx-badge" }, String(thread.unread)) : null),
        el("div", { class: "anx-snip" }, `${thread.last_from}：${thread.last_snippet}`),
        el("div", { class: "anx-meta" }, el("span", {}, thread.participants.join("、")), el("span", {}, when(thread.last_at)),
          thread.open_asks ? el("span", {}, "有待回答的问题") : null))));
    }

    function questionCard(ask) {
      const card = el("div", { class: "anx-card", "data-ask": ask.ask_id });
      card.append(el("p", { class: "anx-q" }, ask.question));
      if (ask.context) card.append(el("p", { class: "anx-body" }, ask.context));
      if (ask.state !== "open") {
        card.append(el("p", { class: "anx-meta" }, ask.state === "answered"
          ? `已回答：${answerText(ask.answer)}（${ask.answered_via || ""}）` : `状态：${ask.state}`));
        return card;
      }
      const readOnly = state.viewer !== "me";
      const entries = cardEntries(ask);
      if (ask.kind === "multi") {
        const boxes = entries.filter((e) => !e.other).map((entry) => {
          const box = el("input", { type: "checkbox", value: entry.value, disabled: readOnly });
          return { box, node: el("label", { class: "anx-opt" }, box, entry.label + (entry.recommended ? "（推荐）" : "")) };
        });
        card.append(...boxes.map((b) => b.node), el("button", { disabled: readOnly, onclick: () => {
          const choices = boxes.filter((b) => b.box.checked).map((b) => b.box.value);
          if (choices.length) answer(ask, { choices });
        } }, "提交所选"));
      } else {
        for (const entry of entries.filter((e) => !e.other)) {
          card.append(el("button", { class: entry.recommended ? "anx-rec" : "", "data-choice": entry.value, disabled: readOnly,
            onclick: () => answer(ask, { choice: entry.value }) }, entry.recommended ? `${entry.label}（推荐）` : entry.label));
        }
      }
      if (entries.some((e) => e.other)) {
        const input = el("input", { type: "text", placeholder: ask.options?.length ? "其他（自由填写）" : "你的回答", disabled: readOnly });
        card.append(el("div", { class: "anx-other" }, input,
          el("button", { "data-choice": "__other__", disabled: readOnly, onclick: () => {
            if (input.value.trim()) answer(ask, { text: input.value.trim() });
          } }, "其他：提交")));
      }
      return card;
    }

    async function answer(ask, payload) {
      try {
        const result = await call("POST", `/asks/${enc(ask.ask_id)}/answer`, payload);
        notify(result.already ? (result.message || "已经回答过了") : "已回答", result.already ? "warn" : "ok");
      } catch (err) {
        notify(err.message, "error");
      }
      await refresh();
    }

    function messageNode(message) {
      const node = el("div", { class: `anx-msg${message.mine ? " mine" : ""}`, "data-message": message.id },
        el("div", { class: "anx-meta" }, el("strong", {}, message.from), el("span", {}, when(message.created_at)),
          message.kind !== "message" ? el("span", {}, message.kind) : null,
          message.priority === "urgent" ? el("span", { class: "anx-badge" }, "urgent") : null),
        message.subject ? el("div", { class: "anx-sub" }, message.subject) : null,
        el("p", { class: "anx-body" }, message.body));
      if (message.mine && Array.isArray(message.recipients)) {
        node.append(el("div", { class: "anx-meta" }, ...message.recipients.map((r) => el("span", {}, `${r.to_display}：${r.status}`))));
      }
      return node;
    }

    function composeForm() {
      const to = el("input", { placeholder: "收件人：地址、名字或 @all / @device:<设备>" });
      const subject = el("input", { placeholder: "主题" });
      const body = el("textarea", { rows: 5, placeholder: "正文" });
      return el("div", { class: "anx-compose" },
        el("header", {}, el("button", { class: "anx-back", onclick: () => { state.composing = false; renderThread(); } }, "‹ 返回"), el("h2", {}, "新邮件（以 me 身份）")),
        to, subject, body,
        el("button", { onclick: async () => {
          if (!to.value.trim() || !body.value.trim()) return;
          try {
            const sent = await call("POST", "/send", { to: to.value.trim().split(/[,，\s]+/).filter(Boolean), subject: subject.value, body: body.value });
            state.composing = false;
            notify("已发送", "ok");
            await refresh();
            openThread(sent.thread_id);
          } catch (err) { notify(err.message, "error"); }
        } }, "发送"));
    }

    function renderThread() {
      container.className = `anx-inbox${state.current || state.composing ? " has-thread" : ""}`;
      if (state.composing) { threadPane.replaceChildren(composeForm()); return; }
      const view = state.view;
      if (!state.current || !view) {
        threadPane.replaceChildren(el("p", { class: "anx-empty" }, state.current ? "正在加载…" : "选择左侧的一个线程"));
        return;
      }
      const asks = new Map((view.asks || []).map((ask) => [ask.message_id, ask]));
      const nodes = [];
      for (const message of view.messages) {
        nodes.push(messageNode(message));
        const ask = asks.get(message.id);
        if (ask) nodes.push(questionCard(ask));
      }
      const last = view.messages[view.messages.length - 1];
      const draft = el("textarea", { rows: 3, placeholder: state.viewer === "me" ? "以 me 身份回复（会唤醒收件人）" : "只读：切回 me 才能回复", disabled: state.viewer !== "me" });
      const reply = el("div", { class: "anx-reply" }, draft,
        el("button", { disabled: state.viewer !== "me" || !last, onclick: async () => {
          if (!draft.value.trim() || !last) return;
          try {
            await call("POST", "/send", { reply_to: last.id, body: draft.value });
            draft.value = "";
            notify("已回复", "ok");
          } catch (err) { notify(err.message, "error"); }
          await refresh();
        } }, "回复"));
      threadPane.replaceChildren(
        el("header", {}, el("button", { class: "anx-back", onclick: () => { state.current = null; state.view = null; renderThread(); renderList(); } }, "‹ 返回"),
          el("h2", {}, (view.participants || []).join("、"))),
        ...nodes, reply);
    }

    async function loadThread(threadId, generation) {
      const viewer = state.viewer === "me" ? "" : `?viewer=${enc(state.viewer)}`;
      const view = await call("GET", `/threads/${enc(threadId)}${viewer}`);
      if (generation !== state.generation || threadId !== state.current) return;
      state.view = view;
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
        state.error = "";
        if (!state.addresses.length) {
          call("GET", "/addresses").then((d) => { state.addresses = d.addresses || []; renderViewer(); }).catch(() => {});
        }
        if (state.current) await loadThread(state.current, generation);
        if (opts.onUnread) opts.onUnread(state.viewer === "me" ? state.unread : null, state.asks.length);
      } catch (err) {
        if (generation !== state.generation) return;
        state.error = `收件箱不可用：${err.message}`;
      }
      renderList();
      if (!state.composing) renderThread();
    }

    function openThread(threadId) {
      state.current = threadId;
      state.composing = false;
      state.view = null;
      renderList();
      renderThread();
      return refresh();
    }

    renderViewer();
    renderList();
    renderThread();
    return { refresh, open: openThread, state, root: container };
  }

  global.AnxInbox = { mount, orderOptions, cardEntries, viewerGroups, viewerLabel };
})(typeof window !== "undefined" ? window : globalThis);
