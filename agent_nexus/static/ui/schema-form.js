// Plugin configuration form from a manifest's JSON Schema subset (design §6.6). No dependencies besides ./ui.js.
//   import { schemaForm } from "/ui/schema-form.js";
//   const form = schemaForm({ schema, ui, values, secrets });
//   container.append(form.root);
//   const { values, secrets, errors } = form.collect();   // errors: [{path, message}] (client-side checks)
//   form.setErrors([{path: "team_id", message: "必填"}]);
// Types: string → input (format uri/path/email; multiline → textarea; secret → password), integer/number → number,
// boolean → switch, enum → select, arrays of basic types → one item per line, objects → nested group (two levels).
// Secrets are write-only: the form never receives a value, only {set, updated_at}; an empty field means "unchanged".

import { el, icon, relTime } from "./ui.js";

let counter = 0;
const uid = (key) => `sf-${++counter}-${String(key).replace(/[^A-Za-z0-9_-]/g, "")}`;

function label(key, prop, required) {
  return [prop.title || key, required ? null : el("span", { class: "optional" }, "可选")];
}

function fieldFor(key, prop, value, required, path) {
  const id = uid(path);
  const hint = prop.description ? el("span", { class: "hint" }, prop.description) : null;
  const error = el("span", { class: "error-text", role: "alert", hidden: true });
  let input, read;
  if (Array.isArray(prop.enum)) {
    input = el("select", { class: "select", id },
      prop.enum.map((option) => el("option", { value: String(option) }, option === "" ? "（不设置）" : String(option))));
    input.value = value === undefined || value === null ? String(prop.default ?? prop.enum[0] ?? "") : String(value);
    read = () => {
      const raw = input.value;
      const match = prop.enum.find((option) => String(option) === raw);
      return match === undefined ? raw : match;
    };
  } else if (prop.type === "boolean") {
    input = el("input", { type: "checkbox", role: "switch", id });
    input.checked = Boolean(value ?? prop.default);
    const field = el("div", { class: "field", dataset: { path } },
      el("label", { class: "switch" }, input, el("span"), prop.title || key), hint, error);
    return { node: field, read: () => input.checked, error, input };
  } else if (prop.type === "integer" || prop.type === "number") {
    input = el("input", { class: "input", type: "number", id, step: prop.type === "integer" ? "1" : "any",
      min: prop.minimum ?? null, max: prop.maximum ?? null, placeholder: prop.default !== undefined ? String(prop.default) : null,
      inputmode: prop.type === "integer" ? "numeric" : "decimal" });
    input.value = value === undefined || value === null ? "" : String(value);
    read = () => (input.value.trim() === "" ? null : Number(input.value));
  } else if (prop.type === "array") {
    input = el("textarea", { class: "textarea mono", id, rows: 3, spellcheck: "false", placeholder: "每行一项" });
    input.value = Array.isArray(value) ? value.join("\n") : "";
    const itemType = prop.items?.type;
    read = () => input.value.split("\n").map((line) => line.trim()).filter(Boolean)
      .map((item) => (itemType === "integer" || itemType === "number" ? Number(item) : item));
  } else if (prop.type === "object" && prop.properties) {
    const group = objectGroup(prop, value || {}, path);
    return { node: el("fieldset", { class: "form", dataset: { path } }, el("legend", { class: "strong small" }, prop.title || key), group.nodes),
      read: group.read, error, children: group.fields };
  } else if (prop.format === "multiline") {
    input = el("textarea", { class: "textarea", id, rows: 4 });
    input.value = value ?? "";
    read = () => input.value;
  } else {
    const mono = ["uri", "path", "email"].includes(prop.format) || prop["x-anx-widget"] === "mono";
    input = el("input", { class: `input ${mono ? "mono" : ""}`, id, type: prop.format === "email" ? "email" : "text",
      spellcheck: mono ? "false" : null, autocomplete: "off",
      placeholder: prop["x-anx-placeholder"] || (prop.default !== undefined && prop.default !== "" ? String(prop.default) : prop.format === "path" ? "D:/path/to/folder" : null) });
    input.value = value ?? "";
    read = () => input.value.trim();
    if (prop.format === "path") {
      input = el("div", { class: "input-icon" }, icon("folder-open", { size: 14 }), input);
      const real = input.querySelector("input");
      read = () => real.value.trim();
    }
  }
  const field = el("div", { class: "field", dataset: { path } }, el("label", { for: id }, label(key, prop, required)), input, hint, error);
  return { node: field, read, error, input };
}

function objectGroup(schema, values, prefix = "") {
  const required = new Set(schema.required || []);
  const fields = {};
  const nodes = [];
  for (const [key, prop] of Object.entries(schema.properties || {})) {
    if (!prop || typeof prop !== "object" || prop.format === "secret") continue;
    const path = prefix ? `${prefix}.${key}` : key;
    const field = fieldFor(key, prop, values[key], required.has(key), path);
    field.prop = prop;
    field.required = required.has(key);
    fields[key] = field;
    nodes.push(field.node);
  }
  return { fields, nodes, read: () => Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.read()])) };
}

function secretField(secret) {
  const id = uid(`secret-${secret.name}`);
  const input = el("input", { class: "input mono", type: "password", id, autocomplete: "new-password", spellcheck: "false",
    placeholder: secret.set ? "已设置，留空表示不修改" : "未设置" });
  let cleared = false;
  const status = el("span", { class: "hint" });
  const error = el("span", { class: "error-text", role: "alert", hidden: true });
  const clear = secret.set ? el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
    cleared = !cleared;
    clear.textContent = cleared ? "撤销清除" : "清除";
    input.disabled = cleared;
    renderStatus();
  } }, "清除") : null;
  function renderStatus() {
    status.replaceChildren(cleared ? "保存后会删除这个凭据" :
      secret.set ? `已设置 · ${secret.updated_at ? `更新于 ${relTime(secret.updated_at)}` : ""}` : secret.required ? "必填，保存后只存在这台电脑上" : "未设置");
    if (secret.description) status.append(" · ", secret.description);
  }
  renderStatus();
  const field = el("div", { class: "field", dataset: { path: secret.name } },
    el("label", { for: id }, icon("key-round", { size: 14 }), " ", secret.title || secret.name,
      secret.required ? null : el("span", { class: "optional" }, "可选")),
    el("div", { class: "input-group" }, input, clear), status, error);
  return { node: field, error, input, secret,
    read: () => (cleared ? null : input.value.trim() === "" ? undefined : input.value.trim()) };
}

export function schemaForm({ schema = {}, ui = {}, values = {}, secrets = [] } = {}) {
  const group = objectGroup(schema, values);
  const secretFields = secrets.map(secretField);
  const placed = new Set();
  const sections = [];
  const groups = Array.isArray(ui.groups) && ui.groups.length ? ui.groups
    : [{ title: "", fields: Object.keys(group.fields).filter((key) => !group.fields[key].prop["x-anx-advanced"]) },
       { title: "高级", collapsed: true, fields: Object.keys(group.fields).filter((key) => group.fields[key].prop["x-anx-advanced"]) }];
  for (const spec of groups) {
    const nodes = (spec.fields || []).filter((key) => group.fields[key] && !placed.has(key)).map((key) => { placed.add(key); return group.fields[key].node; });
    if (!nodes.length) continue;
    if (spec.collapsed) sections.push(el("details", { class: "advanced" }, el("summary", {}, spec.title || "更多"), el("div", { class: "form", style: { marginTop: "12px" } }, nodes)));
    else sections.push(spec.title ? el("div", { class: "form" }, el("div", { class: "strong small muted" }, spec.title), nodes) : el("div", { class: "form" }, nodes));
  }
  const rest = Object.keys(group.fields).filter((key) => !placed.has(key)).map((key) => group.fields[key].node);
  if (rest.length) sections.push(el("div", { class: "form" }, rest));
  if (secretFields.length) {
    sections.push(el("div", { class: "form" }, el("div", { class: "strong small muted" }, "凭据（只写：保存后不会再显示）"), secretFields.map((f) => f.node)));
  }
  const root = el("div", { class: "form schema-form" }, sections);

  function setErrors(problems = []) {
    for (const field of [...Object.values(group.fields), ...secretFields]) { field.error.hidden = true; field.input?.removeAttribute?.("aria-invalid"); }
    for (const problem of problems) {
      const key = String(problem.path || "").replace(/^values\./, "").replace(/^secrets\./, "");
      const field = group.fields[key] || secretFields.find((f) => f.secret.name === key);
      if (!field) continue;
      field.error.textContent = problem.message;
      field.error.hidden = false;
      field.input?.setAttribute?.("aria-invalid", "true");
    }
  }

  function collect() {
    const out = {}, secretOut = {}, errors = [];
    for (const [key, field] of Object.entries(group.fields)) {
      const value = field.read();
      const prop = field.prop;
      if (typeof value === "number" && Number.isNaN(value)) { errors.push({ path: key, message: "需要数字" }); continue; }
      if (prop.type === "integer" && typeof value === "number" && !Number.isInteger(value)) errors.push({ path: key, message: "需要整数" });
      if (typeof value === "number" && prop.minimum !== undefined && value < prop.minimum) errors.push({ path: key, message: `不能小于 ${prop.minimum}` });
      if (typeof value === "number" && prop.maximum !== undefined && value > prop.maximum) errors.push({ path: key, message: `不能大于 ${prop.maximum}` });
      out[key] = value === "" && prop.default !== undefined && !field.required ? null : value;
    }
    for (const field of secretFields) {
      const value = field.read();
      if (value !== undefined) secretOut[field.secret.name] = value;
    }
    return { values: out, secrets: secretOut, errors };
  }

  return { root, collect, setErrors, fields: group.fields, secretFields };
}
