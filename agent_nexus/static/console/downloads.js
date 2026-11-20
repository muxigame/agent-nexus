// The downloads page (docs/ui-spec.md §3.7), shared by the console (#/downloads) and the public /downloads.
import { el, mount, icon, fetchJSON, tokenField, emptyState, errorState, bytes, skeletonRows } from "/ui/ui.js";
import { ubuntuInstallCommand } from "/console/install-commands.js";


const PLATFORM_ICON = { windows: "monitor", linux: "terminal", macos: "laptop", any: "package" };

export async function renderDownloads(target, { inviteHref = "/#/devices?invite=1", publicPage = false } = {}) {
  mount(target, el("div", { class: "page-head" }, el("div", { class: "titles" }, el("h1", {}, "下载客户端"),
    el("p", {}, "在每台要接入的电脑上安装 Agent Nexus。客户端运行本机插件，并把它们接到这个网关。"))), skeletonRows(3));
  let data;
  try {
    data = await fetchJSON("/api/downloads", { quiet: true });
  } catch (error) {
    mount(target, errorState(error, () => renderDownloads(target, { inviteHref, publicPage })));
    return;
  }
  const items = data.items || [];
  const desktop = items.filter((item) => item.kind === "desktop" && item.latest);
  const main = desktop.find((item) => item.platform === "windows") || desktop[0];
  const others = items.filter((item) => item.latest && item !== main);
  const head = target.firstChild;
  const sections = [head];
  if (!items.length && !(data.links || []).length) {
    sections.push(el("section", { class: "card" }, el("div", { class: "card-body" }, emptyState({
      icon: "package", title: "还没有可下载的安装包",
      text: "在 gateway.json 配置 downloads.manifest_url（发布 CI 写到 OSS 的 downloads.json），或把发布产物复制到网关的 downloads.dir 目录（默认 <状态目录>/downloads）。",
    }))));
    mount(target, sections);
    return;
  }
  if (main) {
    const download = (cls) => el("a", { class: `btn btn-primary btn-lg ${cls}`, href: main.url, download: main.filename }, icon("download", { size: 18 }), "下载安装包");
    sections.push(el("section", { class: "card accent" }, el("div", { class: "card-body stack lg", style: { paddingTop: "24px" } },
      el("div", { class: "row top", style: { gap: "16px" } },
        el("span", { class: "avatar lg accent" }, icon(PLATFORM_ICON[main.platform] || "package", { size: 20 })),
        el("div", { class: "stack grow", style: { gap: "4px" } },
          el("div", { class: "row wrap" }, el("h2", { style: { fontSize: "18px" } }, main.label),
            main.version ? el("span", { class: "pill accent plain" }, `最新 ${main.version}`) : null),
          el("span", { class: "muted small" }, [main.requirements, bytes(main.size), main.published_at && `${main.published_at} 发布`].filter(Boolean).join(" · "))),
        download("hide-sm")),
      download("btn-block only-sm"),
      el("div", { class: "field" }, el("span", { class: "field-label" }, "SHA-256"), tokenField(main.sha256)))));
  }
  if (items.some((item) => item.latest && item.filename.endsWith("_amd64.deb"))) {
    sections.push(el("section", { class: "card", id: "ubuntu-install" }, el("div", { class: "card-head" }, el("h2", {}, "Ubuntu 一键下载并安装")),
      el("div", { class: "card-body stack" },
        el("span", { class: "field-label" }, "有桌面（22.04+）"),
        tokenField(ubuntuInstallCommand(), { multiline: true, copyLabel: "复制命令" }),
        el("span", { class: "field-label" }, "服务器（无桌面，不装 GTK/WebKit）"),
        tokenField(ubuntuInstallCommand({ server: true }), { multiline: true, copyLabel: "复制命令" }),
        el("span", { class: "hint" }, "命令总是下载最新版本。服务器装好后，用邀请码加入并作为服务运行：",
          el("span", { class: "mono" }, "agent-nexus join <邀请码> && agent-nexus service enable"),
          "（邀请对话框里有带邀请码的完整命令）。"))));
  }
  const steps = el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "安装之后")),
    el("div", { class: "card-body" }, el("div", { class: "steps" },
      step("运行安装包", el("span", {}, "Windows：运行 setup.exe，SmartScreen 提示时选择“更多信息 → 仍要运行”。Ubuntu：用上面的一键命令，然后从应用菜单启动（服务器见上面的无桌面命令）。安装后托盘出现 Agent Nexus 图标。")),
      step("用邀请码加入网关", el("span", {}, "在 ", el("a", { href: inviteHref }, "设备 → 邀请新设备"), " 生成邀请码，粘贴到客户端的“连接云端”页（或命令行 ", el("span", { class: "mono" }, "anx join <邀请码>"), "）。")),
      step("安装和启用插件", "在客户端“插件”页：标着“未安装”的插件点“安装”，选它的文件夹、Git 地址或压缩包；已安装的打开开关即可启动，需要配置的会提示你填写。"))));
  const otherCard = el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "其他平台与链接")),
    others.length || (data.links || []).length ? el("div", { class: "list inset" },
      others.map((item) => el("div", { class: "list-item" },
        el("span", { class: "avatar" }, icon(item.kind === "gateway" ? "server" : PLATFORM_ICON[item.platform] || "package", { size: 16 })),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title" }, item.label),
          el("span", { class: "sub" }, [item.version, bytes(item.size), item.requirements].filter(Boolean).join(" · "))),
        el("div", { class: "meta" }, el("a", { class: "btn btn-sm", href: item.url, download: item.filename, "aria-label": `下载 ${item.label}` }, icon("download", { size: 14 }))))),
      (data.links || []).map((link) => el("div", { class: "list-item" },
        el("span", { class: "avatar" }, icon("external-link", { size: 16 })),
        el("div", { class: "stack grow", style: { gap: 0 } }, el("span", { class: "title" }, link.label), link.description ? el("span", { class: "sub" }, link.description) : null),
        el("div", { class: "meta" }, el("a", { class: "btn btn-sm", href: link.url, target: "_blank", rel: "noopener noreferrer" }, "打开", icon("arrow-up-right", { size: 14 })))))
    ) : el("div", { class: "card-body" }, el("p", { class: "muted small" }, "目前只提供上面的安装包。")));
  sections.push(el("div", { class: "grid side" }, steps, otherCard));
  if (items.length) {
    sections.push(el("section", {}, el("div", { class: "section-head" }, el("h2", {}, "全部文件")),
      el("div", { class: "table-wrap" }, el("table", { class: "table stack-sm" },
        el("thead", {}, el("tr", {}, ["版本", "文件", "大小", "SHA-256", "发布时间", ""].map((h, i) => el("th", { class: i === 2 ? "num" : null }, h)))),
        el("tbody", {}, items.map((item) => el("tr", {},
          el("td", { class: "cell-first" }, el("span", { class: "mono strong" }, item.version || "—"), " ", item.latest ? el("span", { class: "pill accent plain" }, "最新") : null),
          el("td", { "data-label": "文件" }, el("span", { class: "mono" }, item.filename)),
          el("td", { "data-label": "大小", class: "num" }, bytes(item.size)),
          el("td", { "data-label": "SHA-256" }, el("span", { class: "mono faint", title: item.sha256 }, `${item.sha256.slice(0, 12)}…`)),
          el("td", { "data-label": "发布时间", class: "muted" }, item.published_at || "—"),
          el("td", { class: "actions" }, el("a", { class: "btn btn-sm", href: item.url, download: item.filename }, icon("download", { size: 14 }), "下载")))))))));
  }
  mount(target, sections);
}

function step(title, desc) {
  return el("div", { class: "step" }, el("div", { class: "step-body" }, el("span", { class: "step-title" }, title), el("span", { class: "step-desc" }, desc)));
}
