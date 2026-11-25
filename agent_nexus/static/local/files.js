// 文件: browse another device's folders, fetch its files and send files to it (files/1).
// The manager picks the route: the P2P tunnel first, the gateway relay when no hole punches.
import { el, icon, fetchJSON, toast, drawer, emptyState } from "/ui/ui.js";

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

function when(seconds) {
  if (!seconds) return "";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function join(dir, name, sep) {
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function routeTag(route) {
  if (!route) return null;
  if (route.startsWith("网关中转")) return el("span", { class: "pill warn", title: route }, "网关中转");
  // 经某台设备中转（中转组网，端到端加密）："经 X 中转（…）" 只显示前半句
  const relayed = route.match(/^经 (.+?) 中转/);
  if (relayed) return el("span", { class: "pill ok", title: route }, `经 ${relayed[1]} 中转`);
  return el("span", { class: "pill ok", title: route }, "P2P 直连");
}

export function openFiles(device) {
  const base = `/api/remote/devices/${encodeURIComponent(device.device_id)}/files`;
  let current = null; // the last listing
  const pathInput = el("input", { class: "input mono", type: "text", placeholder: "文件夹路径", style: { flex: 1, minWidth: 0 },
    onkeydown: (event) => { if (event.key === "Enter") load(pathInput.value.trim()); } });
  const route = el("span", {});
  const drives = el("select", { class: "select", style: { width: "auto", display: "none" }, "aria-label": "磁盘",
    onchange: () => load(drives.value) });
  const listBox = el("div", { class: "list", style: { minHeight: "200px" } });
  const transfers = el("div", { class: "stack", style: { gap: "6px" } });
  const picker = el("input", { type: "file", multiple: true, style: { display: "none" },
    onchange: () => { upload([...picker.files]); picker.value = ""; } });

  const toolbar = el("div", { class: "stack", style: { gap: "8px" } },
    el("div", { class: "row", style: { gap: "6px" } },
      el("button", { class: "btn btn-sm btn-icon", type: "button", title: "上一级", "aria-label": "上一级",
        onclick: () => current?.parent && load(current.parent) }, icon("chevron-left", { size: 16 })),
      pathInput,
      el("button", { class: "btn btn-sm", type: "button", onclick: () => load(pathInput.value.trim()) }, "前往"),
      el("button", { class: "btn btn-sm btn-icon", type: "button", title: "刷新", "aria-label": "刷新",
        onclick: () => load(current?.path || "") }, icon("refresh-cw", { size: 14 }))),
    el("div", { class: "row", style: { gap: "6px", flexWrap: "wrap", alignItems: "center" } },
      el("button", { class: "btn btn-sm", type: "button", onclick: () => current && load(current.downloads) },
        icon("download", { size: 14 }), "下载文件夹"),
      el("button", { class: "btn btn-sm", type: "button", onclick: () => current && load(current.home) },
        icon("folder-open", { size: 14 }), "主目录"),
      drives,
      el("span", { style: { marginLeft: "auto" } }, route),
      el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => picker.click() },
        icon("upload", { size: 14 }), "发送文件到这里")),
    picker);

  const body = el("div", { class: "stack", style: { gap: "12px" } }, toolbar, transfers, listBox);
  const panel = drawer({ title: `文件 · ${device.display_name}`, size: "lg",
    description: "浏览对方的文件夹、下载文件，或把文件发送过去。优先打洞直连，打不通自动走网关中转。", body });

  // Drop files anywhere on the list to send them into the open folder.
  listBox.addEventListener("dragover", (event) => { event.preventDefault(); listBox.style.outline = "2px dashed var(--accent)"; });
  listBox.addEventListener("dragleave", () => { listBox.style.outline = ""; });
  listBox.addEventListener("drop", (event) => {
    event.preventDefault();
    listBox.style.outline = "";
    upload([...(event.dataTransfer?.files || [])]);
  });

  async function load(path) {
    listBox.replaceChildren(el("div", { class: "list-item" }, el("span", { class: "spinner" }),
      el("span", { class: "muted" }, current ? "读取中…" : "正在连接（首次直连可能需要十几秒）…")));
    try {
      const listing = await fetchJSON(`${base}?path=${encodeURIComponent(path || "")}`);
      current = listing;
      pathInput.value = listing.path;
      route.replaceChildren(routeTag(listing.route) || "");
      if ((listing.roots || []).length > 1) {
        drives.style.display = "";
        drives.replaceChildren(...listing.roots.map((root) => el("option", { value: root,
          selected: listing.path.toUpperCase().startsWith(root.toUpperCase()) || null }, root)));
      }
      render(listing);
    } catch (error) {
      listBox.replaceChildren(el("div", { class: "list-item" }, el("span", { class: "muted" }, error.message || "读取失败")));
    }
  }

  function render(listing) {
    if (!listing.entries.length) {
      listBox.replaceChildren(emptyState({ icon: "folder-open", title: "空文件夹", text: "把文件拖到这里，或点“发送文件到这里”。", compact: true }));
      return;
    }
    listBox.replaceChildren(...listing.entries.map((entry) => {
      const full = join(listing.path, entry.name, listing.sep || "/");
      const meta = el("div", { class: "meta" },
        entry.dir ? null : el("span", { class: "sub" }, size(entry.size)),
        entry.dir ? null : el("a", { class: "btn btn-sm btn-ghost", href: `${base}/download?path=${encodeURIComponent(full)}`,
          download: entry.name, title: "下载到本机" }, icon("download", { size: 14 }), "下载"));
      return el("div", { class: `list-item ${entry.dir ? "clickable" : ""}`, style: { minHeight: "44px", padding: "6px 12px" },
        tabindex: entry.dir ? 0 : null,
        onclick: entry.dir ? () => load(full) : null,
        onkeydown: entry.dir ? (event) => { if (event.key === "Enter") load(full); } : null },
        icon(entry.dir ? "folder-open" : "file-text", { size: 16 }),
        el("div", { class: "stack grow", style: { gap: 0, minWidth: 0 } },
          el("span", { class: "title truncate" }, entry.name),
          el("span", { class: "sub" }, when(entry.mtime))),
        meta);
    }), listing.truncated ? el("div", { class: "list-item muted" }, "条目太多，只显示前 5000 个") : null);
  }

  function upload(files) {
    if (!current || !files.length) return;
    const dir = current.path;
    for (const file of files) send(file, dir);
  }

  function send(file, dir) {
    const bar = el("progress", { max: 100, value: 0, style: { width: "100%" } });
    const label = el("span", { class: "sub truncate" }, `${file.name} · ${size(file.size)}`);
    const item = el("div", { class: "card", style: { padding: "8px 12px" } }, label, bar);
    transfers.prepend(item);
    const request = new XMLHttpRequest();
    const query = new URLSearchParams({ dir, name: file.name, size: String(file.size) });
    request.open("POST", `${base}/upload?${query}`);
    request.setRequestHeader("X-ANX", "1");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = (event) => { if (event.lengthComputable) bar.value = Math.round(event.loaded / event.total * 100); };
    request.onload = () => {
      let result = {};
      try { result = JSON.parse(request.responseText); } catch { /* not JSON */ }
      if (request.status === 200) {
        bar.value = 100;
        label.replaceChildren(`已发送 ${file.name} → ${result.path || dir}`, " ", routeTag(result.route) || "");
        toast(`已发送 ${file.name}`);
        if (current?.path === dir) load(dir);
        setTimeout(() => item.remove(), 8000);
      } else {
        label.textContent = `${file.name} 发送失败：${result.error || `HTTP ${request.status}`}`;
        label.style.color = "var(--bad)";
      }
    };
    request.onerror = () => { label.textContent = `${file.name} 发送失败：连接中断`; label.style.color = "var(--bad)"; };
    request.send(file);
  }

  load("");
  return panel;
}
