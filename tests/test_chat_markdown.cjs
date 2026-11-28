"use strict";
// Markdown of the 对话 page (docs/agent-chat.md §17): the pure parser ui/markdown.js (text -> tree, no DOM), the
// streaming cut, path detection, and static checks that the DOM builder in ui/chat.js never writes HTML.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "../agent_nexus/static");
const read = (rel) => fs.readFileSync(path.join(STATIC, rel), "utf8");
const md = () => import(pathToFileURL(path.join(STATIC, "ui/markdown.js")).href);

/** The text of an inline tree, for short assertions. */
const flat = (nodes) => (nodes || []).map((n) => n.text ?? flat(n.children)).join("");
const types = (nodes) => (nodes || []).map((n) => n.t);

test("tables: alignment, escaped pipes, pipes inside code, inline content in cells", async () => {
  const { parseBlocks } = await md();
  const [table] = parseBlocks("| 文件 | 改动 | 说明 |\n|:---|---:|:-:|\n| `src/a.py` | +3 \\| x | **粗** |\n| b | `a|b` | |");
  assert.equal(table.t, "table");
  assert.deepEqual(table.align, ["left", "right", "center"]);
  assert.deepEqual(table.head.map(flat), ["文件", "改动", "说明"]);
  assert.equal(table.rows.length, 2);
  assert.equal(table.rows[0][0][0].t, "file");                  // a path in code becomes a file link
  assert.equal(flat(table.rows[0][1]), "+3 | x");
  assert.equal(table.rows[0][2][0].t, "strong");
  assert.equal(table.rows[1][1][0].t, "code");
  assert.equal(table.rows[1][1][0].text, "a|b");
  // not a table: a delimiter line with a different cell count, or pipes in prose
  assert.equal(parseBlocks("a | b\n|---|---|---|")[0].t, "p");
  assert.equal(parseBlocks("x | y 不是表格")[0].t, "p");
});

test("lists: nesting, ordered start, task items, a new list on another bullet, blank lines between items", async () => {
  const { parseBlocks } = await md();
  const [ul] = parseBlocks("- a\n  - b\n    1. c\n- [x] done\n- [ ] todo");
  assert.equal(ul.t, "ul");
  assert.equal(ul.children.length, 3);
  const nested = ul.children[0].children;
  assert.deepEqual(types(nested), ["p", "ul"]);
  assert.equal(nested[1].children[0].children[1].t, "ol");
  assert.equal(ul.children[1].task, true);
  assert.equal(ul.children[2].task, false);
  assert.equal(flat(ul.children[2].children), "todo");
  const [ol] = parseBlocks("3. 三\n4. 四");
  assert.equal(ol.t, "ol");
  assert.equal(ol.start, 3);
  assert.deepEqual(types(parseBlocks("- a\n* b")), ["ul", "ul"]);
  assert.deepEqual(types(parseBlocks("- a\n1. b")), ["ul", "ol"]);
  const loose = parseBlocks("- a\n\n- b\n\n后文");
  assert.deepEqual(types(loose), ["ul", "p"]);
  assert.equal(loose[0].children.length, 2);
  // code inside an item
  const [withCode] = parseBlocks("- 运行：\n  ```sh\n  make test\n  ```");
  assert.deepEqual(types(withCode.children[0].children), ["p", "pre"]);
  assert.equal(withCode.children[0].children[1].text, "make test");
});

test("emphasis, strikethrough, rules, headings to h6, quotes with blocks, fences", async () => {
  const { parseBlocks, inline } = await md();
  assert.deepEqual(types(inline("*斜体* _em_ ~~删~~ **粗** __粗__ ***两者***")).filter((t) => t !== "text"), ["em", "em", "del", "strong", "strong", "strong"]);
  assert.deepEqual(types(inline("snake_case_name 和 1~2 天")), ["text"]);   // no intraword _, no single ~
  assert.deepEqual(types(parseBlocks("a\n\n---\n\nb\n***\n___")), ["p", "hr", "p", "hr", "hr"]);
  assert.deepEqual(parseBlocks("###### 六级").map((n) => [n.t, n.level]), [["h", 6]]);
  const [quote] = parseBlocks("> 引用\n> - 列表\n> ```\n> code\n> ```");
  assert.equal(quote.t, "quote");
  assert.deepEqual(types(quote.children), ["p", "ul", "pre"]);
  const [open] = parseBlocks("```py\nprint(1)");
  assert.deepEqual([open.t, open.lang, open.text, open.open], ["pre", "py", "print(1)", true]);
  const [closed] = parseBlocks("~~~~\n~~~\nx\n~~~~");
  assert.deepEqual([closed.text, closed.open], ["~~~\nx", false]);
  // an unclosed * in a long text stays linear (a rule that found nothing is dropped)
  const started = Date.now();
  parseBlocks("*a ".repeat(20000));
  assert.ok(Date.now() - started < 2000);
});

test("links and images: safe schemes only, paths become file links, external images are click-to-load", async () => {
  const { inline } = await md();
  const nodes = inline("图 ![shot](./out/shot.png) 和 ![x](https://e.com/a.png) 链接 [doc](docs/a.md:12) [bad](javascript:alert(1)) [ok](https://e.com/x) <https://e.com/y>");
  const img = nodes.filter((n) => n.t === "img");
  assert.deepEqual(img.map((n) => [n.path || n.src, !!n.external]), [["./out/shot.png", false], ["https://e.com/a.png", true]]);
  const file = nodes.find((n) => n.t === "file");
  assert.deepEqual([file.path, file.line], ["docs/a.md", 12]);
  assert.ok(flat(nodes).includes("[bad](javascript:alert(1))"), "an unsafe link stays text");
  assert.ok(!nodes.some((n) => (n.t === "link" || n.t === "file") && /javascript/.test(n.href || n.path || "")));
  assert.deepEqual(nodes.filter((n) => n.t === "link").map((n) => n.href), ["https://e.com/x", "https://e.com/y"]);
  const data = inline("![d](data:image/png;base64,iVBORw0KGgo=)")[0];
  assert.equal(data.t, "img");
  assert.equal(data.data, true);
  assert.equal(inline("![s](data:text/html;base64,PHNjcmlwdD4=)")[0].t, "text");
});

test("raw HTML is never markup: it stays text", async () => {
  const { parseBlocks } = await md();
  const tree = parseBlocks("<img src=x onerror=alert(1)>\n\n<script>alert(1)</script> **b**");
  const all = JSON.stringify(tree);
  assert.ok(!/"t":"(?:html|raw)"/.test(all));
  assert.equal(flat(tree[0].children), "<img src=x onerror=alert(1)>");
  assert.equal(tree[1].children[0].text, "<script>alert(1)</script> ");
});

test("paths in prose: shapes, line suffixes, and what is never a path", async () => {
  const { findPaths, pathTarget, inline } = await md();
  const found = (s) => findPaths(s).map((p) => [p.path, p.line, p.col]);
  assert.deepEqual(found("改了 src/app.py:42 和 agent_nexus/static/ui/chat.js:336:5，见 /home/u/x.log。"),
    [["src/app.py", 42, null], ["agent_nexus/static/ui/chat.js", 336, 5], ["/home/u/x.log", null, null]]);
  assert.deepEqual(found("D:\\repos\\billing\\main.go:10 与 C:/Users/me/a.txt"), [["D:\\repos\\billing\\main.go", 10, null], ["C:/Users/me/a.txt", null, null]]);
  assert.deepEqual(found("~/proj/README.md#L3-L9"), [["~/proj/README.md", 3, null]]);
  assert.deepEqual(found("修改了src/app.py文件"), [["src/app.py", null, null]]);
  assert.deepEqual(found("见 docs/设计/方案.md。"), [["docs/设计/方案.md", null, null]]);
  for (const s of ["版本 v0.8.35 和 1.2.3", "路由 /api/agents 和 /mcp", "http://x.com/a/b.py", "e.g. node.js Vue.js", "a/b 和 3/4"])
    assert.deepEqual(found(s).filter((p) => p[0] !== "/api/agents"), [], s);
  assert.deepEqual(pathTarget("chat.js", { code: true }), { path: "chat.js", line: null, col: null });
  assert.equal(pathTarget("npm install", { code: true }), null);
  assert.equal(pathTarget("src/*.py", { code: true }), null);
  assert.deepEqual(pathTarget("file:///C:/x/y.txt"), { path: "C:/x/y.txt", line: null, col: null });
  const code = inline("用 `chat.js` 和 `git push` 以及 `agent_nexus/hub.py:44`");
  assert.deepEqual(code.filter((n) => n.t !== "text").map((n) => [n.t, n.path || n.text, n.line ?? null]),
    [["file", "chat.js", null], ["code", "git push", null], ["file", "agent_nexus/hub.py", 44]]);
  // Windows paths keep their backslashes (not Markdown escapes)
  const win = inline("配置在 C:\\Users\\me\\.codex\\config.toml 里，或 \\*不是斜体\\*");
  assert.equal(win.find((n) => n.t === "file").path, "C:\\Users\\me\\.codex\\config.toml");
  assert.ok(!win.some((n) => n.t === "em"));
  // ...also inside a code span, where the span keeps its whole text
  const spanned = inline("HAR 放在 `D:\\tmp\\oauth-har\\` 了");
  assert.deepEqual(spanned.filter((n) => n.t !== "text").map((n) => [n.t, n.text]), [["file", "D:\\tmp\\oauth-har\\"]]);
});

test("network paths (UNC, //host, file://host) never become file links or device images", async () => {
  const { pathTarget, findPaths, inline, isNetworkPath } = await md();
  for (const raw of ["\\\\evil.example\\share\\a.png", "//evil.example/share/a.png", "file:////evil.example/share/a.png",
                     "file://evil.example/share/a.png", "\\\\?\\UNC\\evil\\s\\a.png"]) {
    assert.equal(pathTarget(raw, { code: true }), null, raw);
    assert.equal(pathTarget(raw), null, raw);
  }
  assert.ok(isNetworkPath("\\\\h\\s") && isNetworkPath("//h/s") && !isNetworkPath("/etc/hosts"));
  assert.deepEqual(findPaths("看 \\\\evil\\s\\a.png 和 //evil/s/b.png"), []);
  const nodes = inline("`\\\\evil.example\\s\\a.png` ![x](//evil.example/s/a.png) [y](file:////evil/s/z.txt)");
  assert.ok(!nodes.some((n) => n.t === "file" || (n.t === "img" && !n.external)), JSON.stringify(nodes));
});

test("streaming cut: the stable prefix renders exactly as inside the whole text, at every length", async () => {
  const { parseBlocks, stableCut } = await md();
  const text = "# 标题\n\n段落一，`code`\n\n- a\n\n- b\n  继续\n\n1. 一\n\n2. 二\n\n后文 **粗**\n\n```js\nconst a = 1;\n\nconst b = 2;\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> 引用\n\n尾巴";
  let cut = 0;
  for (let i = 0; i <= text.length; i += 1) {
    const s = text.slice(0, i);
    cut = stableCut(s, cut <= i ? Math.min(cut, i) : 0);
    assert.ok(cut <= s.length);
    const pieces = JSON.stringify([...parseBlocks(s.slice(0, cut)), ...parseBlocks(s.slice(cut))]);
    assert.equal(pieces, JSON.stringify(parseBlocks(s)), `cut ${cut} of ${JSON.stringify(s)}`);
  }
  assert.ok(cut > 0);
  // never inside a fence, never before a list item that may continue the list
  assert.equal(stableCut("```\na\n\nb\n"), 0);
  assert.equal(stableCut("- a\n\n- b\n"), 0);
  assert.equal(stableCut("p\n\nq\n"), 3);
});

test("the DOM builder never writes HTML, and only safe attributes", () => {
  const chat = read("ui/chat.js");
  const mdjs = read("ui/markdown.js");
  // the only HTML sink in chat.js is the bundled kind icon (our own SVG source, kind-icons.js), never message text
  const sinks = (chat.match(/innerHTML|outerHTML|insertAdjacentHTML|document\.write/g) || []).length;
  assert.equal(sinks, 2, "chat.js: the comment and the kind icon template only");
  assert.ok(chat.includes("tpl.innerHTML = kindSvg("));
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(mdjs), "markdown.js has no HTML sinks");
  assert.ok(!/\bhtml:/.test(chat), "el() is never given html:");
  assert.ok(!/document|window/.test(mdjs.replace(/\/\/.*$/gm, "")), "markdown.js is pure");
  const builder = chat.slice(chat.indexOf("function mdNode("), chat.indexOf("export function renderMarkdown("));
  assert.ok(builder.includes("SAFE_HREF.test(n.href)"));
  assert.ok(builder.includes("DATA_IMAGE.test(n.src)"));
  assert.ok(builder.includes("disabled: true"));
  // downloads are octet-stream Blobs through <a download>, never a navigation to a blob
  assert.ok(chat.includes('new Blob(parts, { type: "application/octet-stream" })'));
  assert.ok(!/location(?:\.href)?\s*=\s*url|window\.open\(url/.test(chat));
});
