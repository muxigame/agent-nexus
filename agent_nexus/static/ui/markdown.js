// Markdown for the 对话 page (docs/agent-chat.md §17): a pure parser, text -> a tree of plain objects. No DOM and
// no HTML here: the DOM builder (renderTree in chat.js) only ever creates elements and text nodes from the tree, so
// model output can never inject markup. Raw HTML in the text stays literal text.
//
// Block nodes: {t:"p"|"h"(level)|"hr"|"pre"(lang,text,open)|"table"(align,head,rows)|"quote"|"ul"|"ol"(start)|"li"(task)}
// Inline nodes: {t:"text"|"code"|"strong"|"em"|"del"|"span"|"link"(href)|"file"(path,line,col,code)|"img"(src|path,alt)}

// ------------------------------------------------------------ paths (file links)
// Extensions that make a bare name (no slash) a candidate; only inside a code span.
const BARE_EXT = /\.(?:md|markdown|txt|log|json|jsonl|ya?ml|toml|ini|cfg|conf|env|csv|tsv|xml|html?|css|scss|less|js|mjs|cjs|jsx|ts|tsx|vue|svelte|py|pyi|rb|go|rs|java|kt|kts|swift|c|cc|cpp|cxx|h|hh|hpp|cs|php|sh|bash|zsh|ps1|psm1|bat|cmd|sql|lua|pl|r|dart|scala|gradle|proto|lock|patch|diff|png|jpe?g|gif|webp|bmp|svg|ico|avif|pdf|zip|gz|tgz|tar|docx?|xlsx?|pptx?)$/i;
const SEG = String.raw`[^\s\\/:*?"<>|\x60'()\[\]{}#，。、；：（）「」『』！？]+`;   // a name (CJK allowed, no spaces)
const LINE = String.raw`(?::(\d{1,7})(?::(\d{1,5}))?(?:-\d{1,7})?|#L(\d{1,7})(?:-L?\d{1,7})?)?`;
// absolute POSIX (/a/b), home (~/a), Windows drive (C:\a or C:/a), explicit relative (./a ../a), relative with a slash (a/b.c)
const PATH_BODY = String.raw`(?:~[\\/]|[A-Za-z]:[\\/]|\.{1,2}[\\/]|/)?(?:${SEG}[\\/])*${SEG}`;
const PATH_RE = new RegExp(String.raw`(?<![\w.:/\\~@%-])(${PATH_BODY})${LINE}`, "gu");

const ASCII_IN_CJK = /([A-Za-z0-9_.~-][\w.~-]*(?:[\\/][\w.-]+)+\.[A-Za-z0-9]{1,10})(?![A-Za-z0-9])/;

/** A UNC / network or device path (\\\\host\\share, //host/share, \\\\?\\UNC\\...): never a file link. A stat of one on
 *  Windows opens an SMB connection to a host the model named and sends the account's NTLM credentials. */
export const isNetworkPath = (raw) => /^[\\/]{2}/.test(String(raw || "").trim());

/** Is `raw` (no line suffix) worth a stat? Rules that keep prose, versions, URLs and routes out. */
export function pathShape(raw, { code = false } = {}) {
  if (!raw || raw.length > 1024) return false;
  if (isNetworkPath(raw)) return false;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) return false;               // a URL
  if (/^\d+(?:[./]\d+)+$/.test(raw)) return false;                        // 1.2.3, 3/4, dates
  if (/^v?\d+\.\d+/.test(raw) && !/[\\/]/.test(raw)) return false;        // v0.8.35
  const rooted = /^(?:~[\\/]|[A-Za-z]:[\\/]|\.{1,2}[\\/]|\/)/.test(raw);
  const slashes = (raw.match(/[\\/]/g) || []).length;
  const last = raw.split(/[\\/]/).pop();
  if (raw.startsWith("/") && slashes < 2 && !/\.\w{1,10}$/.test(last)) return false;   // "/s", "/mcp", "/compact"
  if (rooted) return true;
  if (slashes >= 1) return /\.\w{1,10}$/.test(last) || code;              // a/b.c in prose; a/b only in code
  return code && BARE_EXT.test(raw);                                      // `chat.js` in a code span only
}

/** Path candidates in plain text: [{start, end, path, line, col}]. Trailing punctuation is never part of it. */
export function findPaths(text, { code = false } = {}) {
  const out = [];
  for (const m of text.matchAll(PATH_RE)) {
    let raw = m[1];
    raw = raw.replace(/[.,;:!?'"]+$/u, "");                                // "see a/b.py." -> a/b.py
    if (!pathShape(raw, { code })) {
      const ascii = raw.match(ASCII_IN_CJK);                               // "修改了src/app.py文件" -> src/app.py
      if (!ascii || !pathShape(ascii[1], { code })) continue;
      const start = m.index + raw.indexOf(ascii[1]);
      out.push({ start, end: start + ascii[1].length, path: ascii[1], line: null, col: null });
      continue;
    }
    const suffix = m[1] === raw ? m[0].slice(m[1].length) : "";
    const line = Number(m[2] || m[4]) || null;
    const col = Number(m[3]) || null;
    out.push({ start: m.index, end: m.index + raw.length + suffix.length, path: raw, line, col });
  }
  return out;
}

/** A whole code span / link target that is a path ("src/a.py:12", "file:///C:/x"), else null. */
export function pathTarget(text, { code = false } = {}) {
  let s = String(text || "").trim();
  if (/^[a-z][a-z0-9+.-]+:/i.test(s) && !/^file:\/\//i.test(s)) return null;   // another scheme (data:, javascript:, mailto:)
  if (/^file:\/\//i.test(s)) {
    try { s = decodeURIComponent(s.replace(/^file:\/\/(?:localhost)?/i, "")); } catch { return null; }
    if (!s.startsWith("/")) return null;                                   // file://host/share: a network host
    if (/^\/[A-Za-z]:\//.test(s)) s = s.slice(1);
    code = true;
  }
  const m = s.match(new RegExp(String.raw`^(.+?)${LINE}$`, "u"));
  if (!m) return null;
  const raw = m[1];
  if (isNetworkPath(raw)) return null;
  if (/\s/.test(raw) && !code) return null;
  if (/[*?"<>|]/.test(raw)) return null;                                  // globs are not files
  if (!pathShape(raw, { code: true }) && !(code && /[\\/]/.test(raw))) return null;
  return { path: raw, line: Number(m[2] || m[4]) || null, col: Number(m[3]) || null };
}

// ------------------------------------------------------------ inline
const PUA = /[\uE000-\uE0FF]/g;                 // placeholders; the same characters in the text are dropped first
const SAFE_HREF = /^(?:https?:\/\/|mailto:)/i;

const CODE_SPAN = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g;
function tokenizeAtoms(text) {
  // code spans, escapes, images, links, autolinks, bare URLs: atomic, never parsed for emphasis
  const atoms = [];
  const put = (node) => { atoms.push(node); return String.fromCharCode(0xE000 + atoms.length - 1); };
  const ATOM = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\\([!-\/:-@\[-`{-~])|!\[([^\]\n]*)\]\(\s*<?([^\s)>]+)>?(?:\s+"[^"\n]*")?\s*\)|\[((?:[^\[\]\n]|\[[^\]\n]*\])+)\]\(\s*<([^>\n]+)>\s*\)|\[((?:[^\[\]\n]|\[[^\]\n]*\])+)\]\(\s*([^\s)]+(?:\([^\s)]*\))?)(?:\s+"[^"\n]*")?\s*\)|<(https?:\/\/[^\s<>]+)>|(https?:\/\/[^\s<>()（）]+[^\s<>().,;:!?'"）。，、])/g;
  // Windows paths first: their backslashes are separators, not Markdown escapes (C:\\Users\\me\\.codex)
  const WIN = /(?<![\w\\])(?:[A-Za-z]:|~)\\[^\s<>"|*?\x60]+/g;
  // code spans first: a Windows path inside one (`D:\\tmp\\x\\`) is the span's own text
  const codeSpan = (all, ticks, code) => {
    const body = code.length > 2 && code.startsWith(" ") && code.endsWith(" ") ? code.slice(1, -1) : code;
    const target = atomsLimit(atoms) && pathTarget(body, { code: true });
    return put(target ? { t: "file", code: true, text: body, ...target } : { t: "code", text: body });
  };
  const protectedText = text.replace(PUA, "").replace(CODE_SPAN, codeSpan).replace(WIN, (all) => {
    const found = findPaths(all);
    if (found.length !== 1 || found[0].start !== 0) return all;
    const p = found[0];
    return put({ t: "file", text: all.slice(0, p.end), path: p.path, line: p.line, col: p.col }) + all.slice(p.end);
  });
  const marked = protectedText.replace(ATOM, (all, ticks, code, esc, alt, src, ltext1, lhref1, ltext2, lhref2, auto, bare) => {
    if (ticks) return codeSpan(all, ticks, code);
    if (esc) return put({ t: "text", text: esc });
    if (src !== undefined) {
      if (SAFE_HREF.test(src) && !/^mailto:/i.test(src)) return put({ t: "img", src, alt, external: true });
      if (/^data:image\/(?:png|jpe?g|gif|webp|bmp|avif|svg\+xml);base64,/i.test(src)) return put({ t: "img", src, alt, data: true });
      const target = pathTarget(src, { code: true });
      return put(target ? { t: "img", alt, ...target } : { t: "text", text: all });
    }
    const ltext = ltext1 ?? ltext2, href = lhref1 ?? lhref2;
    if (ltext !== undefined) {
      if (SAFE_HREF.test(href)) return put({ t: "link", href, children: inline(ltext, { noLinks: true }) });
      const target = pathTarget(href, { code: true });
      return put(target ? { t: "file", children: inline(ltext, { noLinks: true }), ...target } : { t: "text", text: all });
    }
    if (auto || bare) return put({ t: "link", href: auto || bare, children: [{ t: "text", text: auto || bare }] });
    return all;
  });
  return { marked, atoms };
}
const atomsLimit = (atoms) => atoms.length < 0x100;   // at most 256 atoms per inline run (more stay literal text)

// emphasis on the placeholder string (L = a letter/number: no intraword "_")
const EMPH = [
  [/\*\*\*(?=\S)([\s\S]{0,800}?\S)\*\*\*/u, (c) => ({ t: "strong", children: [{ t: "em", children: c }] })],
  [/\*\*(?=\S)([\s\S]{0,800}?\S)\*\*/u, (c) => ({ t: "strong", children: c })],
  [/(?<![\p{L}\p{N}_])__(?=\S)([\s\S]{0,800}?\S)__(?![\p{L}\p{N}_])/u, (c) => ({ t: "strong", children: c })],
  [/~~(?=\S)([\s\S]{0,800}?\S)~~/u, (c) => ({ t: "del", children: c })],
  [/(?<![*\\])\*(?=[^\s*])([\s\S]{0,800}?[^\s*\\])\*(?!\*)/u, (c) => ({ t: "em", children: c })],
  [/(?<![\p{L}\p{N}_\\])_(?=[^\s_])([\s\S]{0,800}?[^\s_])_(?![\p{L}\p{N}_])/u, (c) => ({ t: "em", children: c })],
];

// Linear-ish: a rule that found nothing in `s` cannot find anything in a suffix of it, so it is dropped
// (an unclosed "*" in a 60 KB message must not cost a quadratic rescan).
function emphasis(s, atoms, depth = 0, rules = EMPH) {
  const out = [];
  while (s) {
    if (depth > 8 || !rules.length) { out.push(...expand(s, atoms)); break; }
    let best = null;
    const live = [];
    for (const rule of rules) {
      const m = s.match(rule[0]);
      if (!m) continue;
      live.push(rule);
      if (!best || m.index < best.m.index) best = { m, make: rule[1] };
    }
    rules = live;
    if (!best) { out.push(...expand(s, atoms)); break; }
    const { m, make } = best;
    out.push(...expand(s.slice(0, m.index), atoms), make(emphasis(m[1], atoms, depth + 1)));
    s = s.slice(m.index + m[0].length);
  }
  return out;
}

function expand(s, atoms) {
  const out = [];
  let buf = "";
  for (const ch of s) {
    const code = ch.charCodeAt(0);
    if (code >= 0xE000 && code < 0xE000 + atoms.length) { if (buf) out.push(...textWithPaths(buf)); buf = ""; out.push(atoms[code - 0xE000]); }
    else buf += ch;
  }
  if (buf) out.push(...textWithPaths(buf));
  return out;
}

function textWithPaths(text) {
  const out = [];
  let last = 0;
  for (const p of findPaths(text)) {
    if (p.start > last) out.push({ t: "text", text: text.slice(last, p.start) });
    out.push({ t: "file", text: text.slice(p.start, p.end), path: p.path, line: p.line, col: p.col });
    last = p.end;
  }
  if (last < text.length) out.push({ t: "text", text: text.slice(last) });
  return out;
}

export function inline(text, { noLinks = false } = {}) {
  const { marked, atoms } = tokenizeAtoms(String(text || ""));
  const nodes = emphasis(marked, atoms);
  return noLinks ? nodes.map((n) => (n.t === "link" || n.t === "file" ? (n.children ? { t: "span", children: n.children } : { t: "text", text: n.text }) : n)) : nodes;
}

// ------------------------------------------------------------ blocks
const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const ITEM = /^( *)([-*+]|\d{1,9}[.)])([ \t]+|$)(.*)$/;
const QUOTE = /^ {0,3}>[ ]?/;
const DELIM = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

export function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "", ticks = 0;
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") { cur += "|"; i += 1; continue; }
    if (ch === "`") ticks ^= 1;                      // a pipe inside `code` does not split (models rarely escape it)
    if (ch === "|" && !ticks) { cells.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

function isTableStart(lines, i) {
  const head = lines[i], delim = lines[i + 1];
  if (delim === undefined || !head.includes("|") || !DELIM.test(delim) || !delim.includes("-")) return false;
  if (!delim.includes("|") && !head.trim().startsWith("|")) return false;
  return splitRow(head).length === splitRow(delim).length;
}

const indentOf = (line) => line.match(/^ */)[0].length;
const detab = (line) => line.replace(/^\t+/, (tabs) => "    ".repeat(tabs.length));

export function parseBlocks(text, depth = 0) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n").map(detab);
  const out = [];
  let para = [];
  const flush = () => { if (para.length) { out.push({ t: "p", children: inline(para.join("\n")) }); para = []; } };
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
      flush();
      const mark = fence[1];
      const close = new RegExp(`^ {0,3}${mark[0] === "`" ? "`" : "~"}{${mark.length},}\\s*$`);
      const buf = [];
      for (i += 1; i < lines.length && !close.test(lines[i]); i += 1) buf.push(lines[i]);
      out.push({ t: "pre", lang: fence[2] || "", text: buf.join("\n"), open: i >= lines.length });   // open: still streaming
      i += 1;
      continue;
    }
    if (!line.trim()) { flush(); i += 1; continue; }
    if (HR.test(line)) { flush(); out.push({ t: "hr" }); i += 1; continue; }
    const h = line.match(HEADING);
    if (h) { flush(); out.push({ t: "h", level: h[1].length, children: inline(h[2]) }); i += 1; continue; }
    if (isTableStart(lines, i)) {
      flush();
      const head = splitRow(lines[i]);
      const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : ""));
      const rows = [];
      for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes("|") && rows.length < 2000; i += 1) {
        const cells = splitRow(lines[i]);
        rows.push(head.map((_, c) => inline(cells[c] ?? "")));
      }
      out.push({ t: "table", align, head: head.map((c) => inline(c)), rows });
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const buf = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i += 1) buf.push(lines[i].replace(QUOTE, ""));
      out.push({ t: "quote", children: depth < 6 ? parseBlocks(buf.join("\n"), depth + 1) : [{ t: "p", children: inline(buf.join("\n")) }] });
      continue;
    }
    const item = line.match(ITEM);
    if (item && (item[4].trim() || !para.length) && !(para.length && /^\d/.test(item[2]) && item[2] !== "1." && item[2] !== "1)")) {
      flush();
      const parsed = parseList(lines, i, depth);
      out.push(parsed.node);
      i = parsed.next;
      continue;
    }
    para.push(line);
    i += 1;
  }
  flush();
  return out;
}

function parseList(lines, start, depth) {
  const first = lines[start].match(ITEM);
  const ordered = /\d/.test(first[2]);
  const bullet = ordered ? first[2].slice(-1) : first[2];
  const base = first[1].length;
  const items = [];
  let i = start;
  while (i < lines.length) {
    const m = lines[i].match(ITEM);
    if (!m || m[1].length !== base || /\d/.test(m[2]) !== ordered || (ordered ? m[2].slice(-1) : m[2]) !== bullet) break;
    const content = base + m[2].length + Math.min(Math.max(m[3].length, 1), 4);
    const body = [m[4]];
    let j = i + 1;
    for (; j < lines.length; j += 1) {
      const l = lines[j];
      if (!l.trim()) {                                        // a blank line: the item goes on only if indented text follows
        const next = lines.slice(j + 1).find((x) => x.trim());
        if (next !== undefined && indentOf(next) >= content) { body.push(""); continue; }
        break;
      }
      if (indentOf(l) >= Math.min(content, base + 2)) { body.push(l.slice(Math.min(indentOf(l), content))); continue; }
      if (l.match(ITEM) || HR.test(l) || HEADING.test(l) || FENCE.test(l) || QUOTE.test(l)) break;
      body.push(l.trim());                                    // a lazy continuation line
    }
    let task = null;
    const tm = body[0].match(/^\[([ xX])\][ \t]+/);
    if (tm) { task = tm[1] !== " "; body[0] = body[0].slice(tm[0].length); }
    const blocks = depth < 8 ? parseBlocks(body.join("\n"), depth + 1) : [{ t: "p", children: inline(body.join("\n")) }];
    items.push({ t: "li", task, children: blocks.length === 1 && blocks[0].t === "p" ? blocks[0].children : blocks, tight: blocks.length === 1 });
    i = j;
    if (i < lines.length && !lines[i].trim()) {
      const k = lines.slice(i).findIndex((x) => x.trim());
      if (k > 0 && lines[i + k].match(ITEM)?.[1].length === base) i += k;   // blank lines between items
    }
  }
  return { node: { t: ordered ? "ol" : "ul", start: ordered ? parseInt(first[2], 10) : null, children: items }, next: i };
}

// ------------------------------------------------------------ streaming
/**
 * Where a streaming text can be cut so that text.slice(0, cut) parses exactly as it does inside the whole text:
 * after a blank line outside a code fence, before a complete line that starts a new top-level block (not indented,
 * not a list item -- those may continue a list across the blank line). Only complete lines count; `from` is a cut
 * found before (the text only grows). Returns `from` when there is no new cut.
 */
export function stableCut(text, from = 0) {
  let cut = from, pos = from, fence = null, blank = false;
  while (pos < text.length) {
    const end = text.indexOf("\n", pos);
    if (end < 0) break;                                       // the last line may still grow
    const line = detab(text.slice(pos, end).replace(/\r$/, ""));
    if (fence) {
      if (fence.test(line)) fence = null;
    } else if (!line.trim()) {
      blank = true;
    } else {
      if (blank && indentOf(line) === 0 && !ITEM.test(line)) cut = pos;
      blank = false;
      const f = line.match(FENCE);
      if (f) fence = new RegExp(`^ {0,3}${f[1][0] === "`" ? "`" : "~"}{${f[1].length},}\\s*$`);
    }
    pos = end + 1;
  }
  return cut;
}
