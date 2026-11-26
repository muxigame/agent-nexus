// System-prompt fragment editor (docs/agent-chat.md §6).
//   import { mountPromptEditor } from "/ui/prompts.js";
//   mountPromptEditor(container, api, {kind: "codex", mode: "direct", preset: "default"});
//
// Left: the fragments of the chosen preset in order (drag or ↑/↓, enable, edit inline with $variable
// completion, 还原 for built-ins, filter 全部/已启用/已停用, 新建片段). Right: the rendered prompt for a
// preview context (CLI kind, direct/gateway) with substituted variables highlighted and unknown ones flagged.
//
// Ops (api.call(op, params)): agent_prompts → {fragments, presets, variables?};
//   agent_prompt_save {fragments, presets} | {reset: id} → the saved document;
//   agent_prompt_preview {preset, context: {kind, mode}, draft: {fragments, presets}} → {text, segments, unknown_vars, variables?}

import { el, icon, mount, toast, modal, copy, confirmDialog, skeletonRows, errorState } from "./ui.js";
import { xicon, KINDS, KIND_ORDER, kindShort, callOp } from "./agent-admin.js";

export const VARIABLES = [
  ["device", "设备名称"], ["device_id", "设备 ID"], ["os", "操作系统"], ["workdir", "工作目录"], ["tmpdir", "本次对话的临时目录"],
  ["session", "会话名"], ["address", "会话的邮箱地址"], ["agent", "CLI 名称"], ["date", "今天的日期"], ["time", "当前时间"],
  ["owner", "主人的称呼"], ["gateway", "网关地址"], ["handle", "网关工作区句柄（仅经网关）"], ["context_device", "要操作的设备（经网关）"],
  ["context_workdir", "要操作的目录（经网关）"],
];
const SCOPES = [["all", "所有模式"], ["direct", "仅同设备直接"], ["gateway", "仅经网关"]];
const VAR_RE = /\$\$|\$([A-Za-z_][A-Za-z0-9_]*)/g;

export function mountPromptEditor(container, api, init = {}) {
  const S = {
    doc: null, preset: init.preset || "default", kind: init.kind || "claude", mode: init.mode || "direct",
    filter: "all", editing: null, draft: null, preview: null, previewError: "", saveState: "idle",
    variables: VARIABLES.map(([name, desc]) => ({ name, desc, value: "" })),
  };
  let alive = true;
  const call = (op, params) => callOp(api, op, params);
  const root = el("div", { class: "pe" });
  mount(container, root);

  async function load() {
    mount(root, skeletonRows(4));
    try {
      const doc = await call("agent_prompts", {});
      if (!alive) return;
      adopt(doc);
    } catch (error) {
      if (alive) mount(root, errorState(error, load));
      return;
    }
    build();
    renderAll();
    preview();
  }

  function adopt(doc) {
    S.doc = { fragments: (doc.fragments || []).map((f) => ({ ...f })), presets: (doc.presets || []).map((p) => ({ ...p, order: (p.order || []).slice(), enabled: { ...(p.enabled || {}) } })) };
    if (!S.doc.presets.length) S.doc.presets.push({ id: "default", name: "默认", order: S.doc.fragments.map((f) => f.id), enabled: {} });
    if (!S.doc.presets.some((p) => p.id === S.preset)) S.preset = S.doc.presets[0].id;
    if (Array.isArray(doc.variables) && doc.variables.length) {
      S.variables = doc.variables.map((v) => (typeof v === "string" ? { name: v, desc: "" } : { name: v.name, desc: v.desc || v.description || "", value: v.value ?? v.example ?? "" }));
    }
  }

  const preset = () => S.doc.presets.find((p) => p.id === S.preset) || S.doc.presets[0];
  const fragment = (id) => S.doc.fragments.find((f) => f.id === id);
  function ordered() {
    const p = preset();
    const ids = (p.order || []).filter((id) => fragment(id));
    for (const f of S.doc.fragments) if (!ids.includes(f.id)) ids.push(f.id);
    return ids.map(fragment);
  }
  const isOn = (f) => { const p = preset(); return p.enabled && f.id in p.enabled ? !!p.enabled[f.id] : f.enabled !== false; };

  // ---------------------------------------------------------------- layout

  const presetSelect = el("select", { class: "select pe-preset", "aria-label": "预设", onchange: () => { S.preset = presetSelect.value; S.editing = null; renderAll(); preview(); } });
  const saveNote = el("span", { class: "pe-save", "aria-live": "polite" });
  const tabs = el("div", { class: "pe-tabs", role: "tablist", "aria-label": "筛选片段" });
  const list = el("div", { class: "pe-list", role: "list", "aria-label": "提示词片段" });
  const kindSelect = el("select", { class: "select chat-sm pe-ctx-kind", "aria-label": "预览用的 CLI", onchange: () => { S.kind = kindSelect.value; preview(); } });
  const modeSeg = el("div", { class: "segmented pe-ctx-mode", role: "group", "aria-label": "预览用的工作方式" });
  const previewBody = el("div", { class: "pe-preview-body", tabindex: "0", "aria-label": "最终提示词预览" });
  const previewStats = el("div", { class: "pe-stats" });
  const unknownNote = el("div", { class: "pe-unknown", hidden: true });

  function build() {
    mount(root,
      el("div", { class: "pe-toolbar" },
        el("label", { class: "pe-toolbar-label" }, "预设"), presetSelect,
        el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: newPreset }, icon("plus", { size: 14 }), "新建预设"),
        el("span", { class: "spacer" }), saveNote),
      el("div", { class: "pe-grid" },
        el("section", { class: "pe-left card", "aria-label": "片段" },
          el("div", { class: "pe-left-head" }, tabs,
            el("button", { class: "btn btn-sm", type: "button", onclick: newFragment }, icon("plus", { size: 14 }), "新建片段")),
          list),
        el("section", { class: "pe-right card", "aria-label": "预览" },
          el("div", { class: "pe-right-head" },
            el("div", { class: "pe-right-title" }, el("h3", {}, "最终提示词"), previewStats),
            el("div", { class: "pe-ctx" }, kindSelect, modeSeg,
              el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "复制预览", title: "复制预览",
                onclick: () => S.preview?.text && copy(S.preview.text) }, icon("copy", { size: 15 })))),
          unknownNote,
          previewBody)));
  }

  function renderAll() { renderToolbar(); renderTabs(); renderList(); renderCtx(); }

  function renderToolbar() {
    mount(presetSelect, S.doc.presets.map((p) => el("option", { value: p.id, selected: p.id === S.preset }, p.name || p.id)));
    renderSave();
  }
  function renderSave() {
    const map = {
      idle: null, saving: [el("span", { class: "spinner" }), "保存中…"], saved: [icon("check", { size: 14 }), "已保存"],
      failed: [icon("circle-alert", { size: 14 }), "没有保存", el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => saveNow() }, "重试")],
    };
    saveNote.className = `pe-save ${S.saveState}`;
    mount(saveNote, map[S.saveState]);
  }

  function renderTabs() {
    const all = ordered();
    const on = all.filter(isOn).length;
    const tab = (key, label, n) => el("button", { class: "pe-tab", type: "button", role: "tab", "aria-selected": String(S.filter === key),
      onclick: () => { S.filter = key; renderTabs(); renderList(); } }, label, el("span", { class: "count" }, String(n)));
    mount(tabs, tab("all", "全部", all.length), tab("on", "已启用", on), tab("off", "已停用", all.length - on));
  }

  function renderCtx() {
    const kinds = KIND_ORDER.slice();
    if (!kinds.includes(S.kind)) kinds.push(S.kind);
    mount(kindSelect, kinds.map((k) => el("option", { value: k, selected: k === S.kind }, kindShort(k))));
    mount(modeSeg, [["direct", "同设备"], ["gateway", "经网关"]].map(([key, label]) => el("button", { type: "button", "aria-pressed": String(S.mode === key),
      onclick: () => { S.mode = key; renderCtx(); preview(); } }, label)));
  }

  // ---------------------------------------------------------------- the fragment list

  function isModified(f) {
    if (f.modified !== undefined) return !!f.modified;
    return f.default_text !== undefined && f.default_text !== f.text;
  }

  function tagsOf(f) {
    const tags = [el("span", { class: `tag ${f.builtin ? "" : "accent"}` }, f.builtin ? "内置" : "自定义")];
    if (f.scope && f.scope !== "all") tags.push(el("span", { class: "tag" }, f.scope === "gateway" ? "仅经网关" : "仅同设备"));
    if (Array.isArray(f.kinds) && f.kinds.length && f.kinds.length < KIND_ORDER.length) tags.push(el("span", { class: "tag" }, f.kinds.map((k) => kindShort(k)).join(" / ")));
    if (f.builtin && isModified(f)) tags.push(el("span", { class: "tag pe-tag-mod" }, "已修改"));
    return tags;
  }

  function included(f) {
    if (!S.preview?.segments) return null;
    return S.preview.segments.some((s) => s.id === f.id);
  }

  function renderList() {
    const all = ordered();
    const shown = all.filter((f) => S.filter === "all" || (S.filter === "on" ? isOn(f) : !isOn(f)));
    if (!shown.length) {
      mount(list, el("div", { class: "pe-empty" }, S.filter === "off" ? "没有停用的片段" : S.filter === "on" ? "还没有启用任何片段，最终提示词为空" : "还没有片段"));
      return;
    }
    mount(list, shown.map((f) => (S.editing === f.id ? editorRow(f) : fragmentRow(f, all))));
  }

  function fragmentRow(f, all) {
    const index = all.indexOf(f);
    const on = isOn(f);
    const inPreview = included(f);
    const check = el("input", { type: "checkbox", checked: on, "aria-label": `启用“${f.title}”`, onchange: () => setEnabled(f, check.checked) });
    const firstLine = String(f.text || "").split("\n").find((line) => line.trim()) || "（空）";
    const row = el("div", { class: `pe-item ${on ? "" : "off"}`, role: "listitem", dataset: { id: f.id }, draggable: "false" },
      el("span", { class: "pe-grip", title: "拖动排序", "aria-hidden": "true" }, xicon("grip-vertical", { size: 16 })),
      el("label", { class: "pe-check" }, check),
      el("div", { class: "pe-item-main" },
        el("div", { class: "pe-item-title" }, el("span", { class: "truncate" }, f.title || f.id), tagsOf(f)),
        el("div", { class: "pe-item-peek truncate" }, firstLine),
        on && inPreview === false ? el("div", { class: "pe-item-skip" }, icon("info", { size: 12 }), "当前预览的 CLI / 模式不包含这一段") : null),
      el("div", { class: "pe-item-actions" },
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => startEdit(f) }, "编辑"),
        f.builtin && isModified(f) ? el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "恢复为内置内容", onclick: () => resetFragment(f) }, "还原") : null,
        el("span", { class: "pe-move" },
          el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "上移", disabled: index === 0, onclick: () => move(f, -1) }, xicon("arrow-up", { size: 14 })),
          el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "下移", disabled: index === all.length - 1, onclick: () => move(f, 1) }, xicon("arrow-down", { size: 14 })))));
    enableDrag(row, row.querySelector(".pe-grip"));
    return row;
  }

  // drag to reorder: only from the grip
  let dragId = null;
  function enableDrag(row, grip) {
    grip.addEventListener("pointerdown", () => { row.draggable = true; });
    row.addEventListener("dragstart", (event) => { dragId = row.dataset.id; row.classList.add("dragging"); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", dragId); });
    row.addEventListener("dragend", () => { row.draggable = false; row.classList.remove("dragging"); dragId = null; list.querySelectorAll(".drop-before, .drop-after").forEach((n) => n.classList.remove("drop-before", "drop-after")); });
    row.addEventListener("dragover", (event) => {
      if (!dragId || dragId === row.dataset.id) return;
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      row.classList.toggle("drop-after", after);
      row.classList.toggle("drop-before", !after);
    });
    row.addEventListener("dragleave", () => row.classList.remove("drop-before", "drop-after"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!dragId || dragId === row.dataset.id) return;
      const after = row.classList.contains("drop-after");
      const ids = ordered().map((f) => f.id).filter((id) => id !== dragId);
      ids.splice(ids.indexOf(row.dataset.id) + (after ? 1 : 0), 0, dragId);
      preset().order = ids;
      changed();
    });
  }

  function move(f, delta) {
    const ids = ordered().map((x) => x.id);
    const i = ids.indexOf(f.id), j = i + delta;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    preset().order = ids;
    changed();
    requestAnimationFrame(() => list.querySelector(`[data-id="${CSS.escape(f.id)}"] [aria-label="${delta < 0 ? "上移" : "下移"}"]`)?.focus());
  }

  function setEnabled(f, on) {
    preset().enabled = { ...(preset().enabled || {}), [f.id]: on };
    changed();
  }

  // ---------------------------------------------------------------- inline editor

  function startEdit(f) {
    S.editing = f.id;
    S.draft = { title: f.title || "", text: f.text || "", scope: f.scope || "all", kinds: Array.isArray(f.kinds) ? f.kinds.slice() : [] };
    renderList();
    requestAnimationFrame(() => list.querySelector(".pe-editor textarea")?.focus());
  }

  function editorRow(f) {
    const d = S.draft;
    const title = el("input", { class: "input chat-sm", value: d.title, "aria-label": "标题", maxlength: "60", oninput: () => { d.title = title.value; } });
    const area = el("textarea", { class: "textarea mono pe-text", rows: 8, "aria-label": "片段内容", spellcheck: "false" });
    area.value = d.text;
    area.addEventListener("input", () => { d.text = area.value; preview(); autocomplete(area); });
    area.addEventListener("click", () => closeComplete());
    area.addEventListener("keydown", (event) => completeKey(event, area));
    area.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== area) closeComplete(); }, 150));
    const scope = el("select", { class: "select chat-sm", "aria-label": "适用模式", onchange: () => { d.scope = scope.value; preview(); } },
      SCOPES.map(([key, label]) => el("option", { value: key, selected: d.scope === key }, label)));
    const kinds = el("div", { class: "pe-kinds", role: "group", "aria-label": "适用的 CLI" },
      KIND_ORDER.map((k) => {
        const box = el("input", { type: "checkbox", checked: !d.kinds.length || d.kinds.includes(k), onchange: () => {
          const all = Array.from(kinds.querySelectorAll("input")).map((input, i) => (input.checked ? KIND_ORDER[i] : null)).filter(Boolean);
          d.kinds = all.length === KIND_ORDER.length ? [] : all;
          preview();
        } });
        return el("label", { class: "check pe-kind" }, box, kindShort(k));
      }));
    const varsBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-haspopup": "listbox", onclick: () => { area.focus(); insertAtCaret(area, "$"); autocomplete(area, true); } }, "$ 插入变量");
    const save = () => {
      if (!d.title.trim()) { toast("给片段起个标题", { type: "warn" }); title.focus(); return; }
      Object.assign(f, { title: d.title.trim(), text: d.text, scope: d.scope, kinds: d.kinds });
      delete f._new;  // Ctrl+Enter too: a new fragment is kept only once saved
      if (f.builtin) f.modified = true;
      S.editing = null; S.draft = null;
      changed();
    };
    area.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); save(); } });
    return el("div", { class: "pe-item pe-editor", role: "listitem", dataset: { id: f.id } },
      el("div", { class: "pe-editor-grid" },
        el("div", { class: "field" }, el("label", {}, "标题"), title),
        el("div", { class: "field" }, el("label", {}, "适用模式"), scope)),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "适用的 CLI"), kinds),
      el("div", { class: "field pe-text-field" }, el("div", { class: "row between" }, el("label", { class: "field-label" }, "内容"), varsBtn), area,
        el("div", { class: "hint" }, "输入 $ 会提示变量；$$ 输出一个 $。Ctrl+Enter 保存。")),
      el("div", { class: "pe-editor-foot" },
        !f.builtin ? el("button", { class: "btn btn-sm btn-danger", type: "button", onclick: () => removeFragment(f) }, icon("trash", { size: 14 }), "删除") : null,
        el("span", { class: "spacer" }),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => { S.editing = null; S.draft = null; if (f._new) dropNew(f); renderList(); preview(); } }, "取消"),
        el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => save() }, "保存")));
  }

  function newFragment() {
    let n = 1;
    while (fragment(`custom-${n}`)) n += 1;
    const f = { id: `custom-${n}`, title: "新片段", text: "", builtin: false, scope: "all", kinds: [], enabled: true, _new: true };
    S.doc.fragments.push(f);
    const p = preset();
    p.order = [...ordered().map((x) => x.id).filter((id) => id !== f.id), f.id];
    p.enabled = { ...(p.enabled || {}), [f.id]: true };
    if (S.filter === "off") S.filter = "all";
    renderTabs();
    startEdit(f);
    requestAnimationFrame(() => list.querySelector(".pe-editor")?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }
  function dropNew(f) {
    S.doc.fragments = S.doc.fragments.filter((x) => x !== f);
    for (const p of S.doc.presets) { p.order = (p.order || []).filter((id) => id !== f.id); if (p.enabled) delete p.enabled[f.id]; }
    renderTabs();
  }

  async function removeFragment(f) {
    const ok = await confirmDialog({ title: `删除“${f.title}”？`, message: "所有预设里都会去掉这一段。", confirmLabel: "删除", danger: true });
    if (!ok) return;
    S.editing = null; S.draft = null;
    dropNew(f);
    changed();
  }

  async function resetFragment(f) {
    if (f.default_text !== undefined) {
      Object.assign(f, { text: f.default_text, title: f.default_title || f.title, modified: false });
      changed();
      toast(`已还原“${f.title}”`, { timeout: 1800 });
      return;
    }
    try {
      adopt(await call("agent_prompt_save", { reset: f.id }));
      renderAll();
      preview();
      toast(`已还原“${f.title}”`, { timeout: 1800 });
    } catch (error) {
      toast(error.message, { type: "bad", title: "还原失败" });
    }
  }

  function newPreset() {
    const name = el("input", { class: "input", placeholder: "例如：只读审查", "aria-label": "预设名称", maxlength: "30" });
    modal({
      title: "新建预设", description: `从“${preset().name || preset().id}”复制片段顺序和启用状态。`, size: "sm",
      body: el("div", { class: "field" }, el("label", {}, "名称"), name),
      actions: [{ label: "取消" }, { label: "创建", variant: "primary", onClick: () => {
        const value = name.value.trim();
        if (!value) { toast("填写预设名称", { type: "warn" }); return false; }
        let id = value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "preset";
        while (S.doc.presets.some((p) => p.id === id)) id = `${id}-${Math.random().toString(36).slice(2, 5)}`;
        S.doc.presets.push({ id, name: value, order: ordered().map((f) => f.id), enabled: { ...(preset().enabled || {}) } });
        S.preset = id;
        changed();
        return true;
      } }],
    });
  }

  // ---------------------------------------------------------------- save + preview

  let saveTimer = null;
  function changed() {
    renderAll();
    preview();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 500);
  }
  async function saveNow() {
    clearTimeout(saveTimer);
    S.saveState = "saving"; renderSave();
    try {
      const clean = { fragments: S.doc.fragments.filter((f) => !f._new).map(({ _new, ...f }) => f), presets: S.doc.presets };
      await call("agent_prompt_save", clean);
      if (!alive) return;
      S.saveState = "saved";
    } catch (error) {
      if (!alive) return;
      S.saveState = "failed";
      toast(error.message, { type: "bad", title: "提示词没有保存" });
    }
    renderSave();
  }

  let previewTimer = null, previewSeq = 0;
  function preview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 220);
  }
  function draftDoc() {
    const fragments = S.doc.fragments.map(({ _new, ...f }) => (S.editing === f.id && S.draft ? { ...f, ...S.draft } : f));
    return { fragments, presets: S.doc.presets };
  }
  async function runPreview() {
    const seq = ++previewSeq;
    try {
      const result = await call("agent_prompt_preview", { preset: S.preset, context: { kind: S.kind, mode: S.mode }, draft: draftDoc() });
      if (!alive || seq !== previewSeq) return;
      S.preview = result;
      S.previewError = "";
      if (result.variables && typeof result.variables === "object" && !Array.isArray(result.variables)) {
        for (const v of S.variables) if (v.name in result.variables) v.value = result.variables[v.name];
      }
    } catch (error) {
      if (!alive || seq !== previewSeq) return;
      S.previewError = error.message;
    }
    renderPreview();
    if (!S.editing) renderList();
  }

  function highlight(text, values) {
    const out = [];
    let last = 0;
    for (const match of String(text || "").matchAll(VAR_RE)) {
      if (match.index > last) out.push(text.slice(last, match.index));
      if (match[0] === "$$") out.push("$");
      else if (values && match[1] in values) out.push(el("mark", { class: "pe-var", title: `$${match[1]}` }, String(values[match[1]] ?? "")));
      else out.push(el("mark", { class: "pe-var unknown", title: "未知变量，原样保留" }, match[0]));
      last = match.index + match[0].length;
    }
    if (last < String(text || "").length) out.push(text.slice(last));
    return out;
  }

  // Without variable values only the unknown names (still literal in the text) can be marked.
  function markUnknown(text, unknown) {
    const names = unknown.map((v) => String(v).replace(/^\$/, "")).filter(Boolean);
    if (!names.length) return [text];
    const re = new RegExp(`\\$(${names.map((n) => n.replace(/[^A-Za-z0-9_]/g, "")).join("|")})(?![A-Za-z0-9_])`, "g");
    const out = [];
    let last = 0;
    for (const match of text.matchAll(re)) {
      if (match.index > last) out.push(text.slice(last, match.index));
      out.push(el("mark", { class: "pe-var unknown", title: "未知变量，原样保留" }, match[0]));
      last = match.index + match[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function renderPreview() {
    const p = S.preview;
    if (S.previewError) {
      mount(previewBody, el("div", { class: "pe-preview-error" }, icon("circle-alert", { size: 16 }), `预览失败：${S.previewError}`));
      mount(previewStats);
      unknownNote.hidden = true;
      return;
    }
    if (!p) return;
    const segments = Array.isArray(p.segments) ? p.segments : [];
    const values = p.variables && typeof p.variables === "object" && !Array.isArray(p.variables) ? p.variables : null;
    const chars = String(p.text || "").length;
    mount(previewStats, el("span", {}, `${segments.length} 段`), el("span", {}, `${chars.toLocaleString("zh-CN")} 字`));
    const unknown = Array.isArray(p.unknown_vars) ? p.unknown_vars : [];
    unknownNote.hidden = !unknown.length;
    mount(unknownNote, icon("triangle-alert", { size: 14 }),
      el("span", {}, "未知变量会原样保留：", unknown.map((v, i) => [i ? "、" : "", el("code", {}, v.startsWith("$") ? v : `$${v}`)])));
    if (!segments.length && !p.text) {
      mount(previewBody, el("div", { class: "pe-preview-empty" }, "没有启用的片段，CLI 会使用它自己的默认提示词。"));
      return;
    }
    const doc = draftDoc();
    // With the variable values we render each segment from its template, so substitutions can be highlighted.
    if (values && segments.length && segments.every((s) => doc.fragments.some((f) => f.id === s.id))) {
      mount(previewBody, segments.map((s) => {
        const f = doc.fragments.find((x) => x.id === s.id);
        return el("div", { class: "pe-seg" },
          el("div", { class: "pe-seg-head" }, el("span", {}, f.title || s.title || s.id), el("span", { class: "faint" }, `${(s.chars ?? f.text.length).toLocaleString("zh-CN")} 字`)),
          el("div", { class: "pe-seg-text" }, highlight(String(f.text || "").trim(), values)));
      }));
      return;
    }
    mount(previewBody, el("div", { class: "pe-seg" }, el("div", { class: "pe-seg-text" }, markUnknown(String(p.text || ""), unknown))));
  }

  // ---------------------------------------------------------------- $ autocomplete

  let complete = null;   // {box, items, index, area, start}
  function closeComplete() { complete?.box.remove(); complete = null; }
  function insertAtCaret(area, text) {
    const start = area.selectionStart, end = area.selectionEnd;
    area.setRangeText(text, start, end, "end");
    area.dispatchEvent(new Event("input"));
  }
  function autocomplete(area, force = false) {
    const caret = area.selectionStart;
    const before = area.value.slice(0, caret);
    const match = before.match(/(^|[^$])\$([A-Za-z_][A-Za-z0-9_]*)?$/);
    if (!match) { closeComplete(); return; }
    const word = (match[2] || "").toLowerCase();
    const items = S.variables.filter((v) => v.name.startsWith(word) || (word.length > 1 && v.name.includes(word)));
    if (!items.length || (!force && items.length === 1 && items[0].name === word)) { closeComplete(); return; }
    const start = caret - word.length - 1;
    closeComplete();
    const box = el("div", { class: "menu pe-complete", role: "listbox", "aria-label": "变量" });
    complete = { box, items, index: 0, area, start };
    paintComplete();
    document.body.append(box);
    const { x, y, h } = caretCoords(area, caret);
    const rect = area.getBoundingClientRect();
    const left = Math.min(rect.left + x, window.innerWidth - box.offsetWidth - 8);
    let top = rect.top + y + h + 4;
    if (top + box.offsetHeight > window.innerHeight - 8) top = rect.top + y - box.offsetHeight - 4;
    Object.assign(box.style, { position: "fixed", left: `${Math.max(8, left)}px`, top: `${Math.max(8, top)}px` });
  }
  function paintComplete() {
    const { box, items, index } = complete;
    mount(box, items.slice(0, 8).map((v, i) => el("button", {
      class: `pe-complete-item ${i === index ? "on" : ""}`, type: "button", role: "option", "aria-selected": String(i === index), tabindex: "-1",
      onmousedown: (event) => { event.preventDefault(); choose(i); },
    }, el("code", {}, `$${v.name}`), el("span", { class: "pe-complete-desc" }, v.desc), v.value ? el("span", { class: "pe-complete-val mono truncate" }, String(v.value)) : null)));
  }
  function choose(i) {
    const { area, start, items } = complete;
    const v = items[i];
    const caret = area.selectionStart;
    area.setRangeText(`$${v.name}`, start, caret, "end");
    closeComplete();
    area.dispatchEvent(new Event("input"));
    closeComplete();
  }
  function completeKey(event, area) {
    if (!complete || complete.area !== area) return;
    const n = Math.min(8, complete.items.length);
    if (event.key === "ArrowDown") { complete.index = (complete.index + 1) % n; paintComplete(); event.preventDefault(); }
    else if (event.key === "ArrowUp") { complete.index = (complete.index - 1 + n) % n; paintComplete(); event.preventDefault(); }
    else if ((event.key === "Enter" || event.key === "Tab") && !event.isComposing && !event.ctrlKey && !event.metaKey) { event.preventDefault(); choose(complete.index); }
    else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeComplete(); }
  }
  // Caret position inside a textarea via a mirror div with the same box and font.
  function caretCoords(area, position) {
    const style = getComputedStyle(area);
    const mirror = el("div");
    for (const prop of ["boxSizing", "width", "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "borderTopWidth", "borderLeftWidth", "borderRightWidth", "tabSize", "wordBreak"]) mirror.style[prop] = style[prop];
    Object.assign(mirror.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", overflowWrap: "break-word", top: "0", left: "-9999px" });
    mirror.textContent = area.value.slice(0, position);
    const marker = el("span", {}, "​");
    mirror.append(marker);
    document.body.append(mirror);
    const result = { x: marker.offsetLeft - area.scrollLeft, y: marker.offsetTop - area.scrollTop, h: parseFloat(style.lineHeight) || 20 };
    mirror.remove();
    return result;
  }

  load();
  return {
    refresh: load,
    setContext({ kind, mode } = {}) { if (kind) S.kind = kind; if (mode) S.mode = mode; if (S.doc) { renderCtx(); preview(); } },
    destroy() { alive = false; closeComplete(); clearTimeout(saveTimer); clearTimeout(previewTimer); },
  };
}

export { KINDS };
