"use strict";

// Agent Board — Markdown rendering (pure; node-tested by tools/test_agent_board_web_markdown.py).
// Loaded before app.js; no build step, no external code (CSP default-src 'self').

function escapeText(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function safeMarkdownUrl(value) {
  try {
    const url = new URL(String(value), window.location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function renderInlineMarkdown(value, {links=true}={}) {
  const tokens = [];
  const stash = html => `${tokens.push(html) - 1}`;
  let source = String(value ?? "").replace(/[]/g, "�");
  source = source.replace(/`([^`\n]+)`/g, (_match, code) => stash(`<code>${escapeText(code)}</code>`));
  source = source.replace(/\[([^\]\n]+)\]\(([^\s)]+)\)/g, (_match, label, target) => {
    const href = safeMarkdownUrl(target);
    if (!href) return `${label} (${target})`;
    const safeLabel = escapeText(label);
    return stash(links
      ? `<a href="${escapeText(href)}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`
      : `<span class="markdown-link" title="${escapeText(href)}">${safeLabel}</span>`);
  });
  let output = escapeText(source);
  output = output.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  output = output.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  output = output.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  output = output.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  output = output.replace(/(^|[^\w_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
  return output.replace(/(\d+)/g, (_match, index) => tokens[Number(index)] || "");
}

function markdownBlockStart(line) {
  return /^\s*$|^```|^#{1,4}\s+|^\s*>\s?|^\s*[-*+]\s+|^\s*\d+\.\s+|^\s*(?:---+|___+|\*\*\*+)\s*$|^\s*\|/.test(line);
}

function isTableRow(line) {
  return /^\s*\|/.test(line);
}

function splitTableRow(line) {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|") && !trimmed.endsWith("\\|")) trimmed = trimmed.slice(0, -1);
  const cells = [];
  let current = "";
  for (let i = 0; i < trimmed.length; i += 1) {
    const char = trimmed[i];
    if (char === "\\" && trimmed[i + 1] === "|") { current += "|"; i += 1; continue; }
    if (char === "|") { cells.push(current.trim()); current = ""; continue; }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

function tableSeparatorAlignments(line) {
  if (!/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(line)) return null;
  return splitTableRow(line).map(cell => {
    const left = cell.startsWith(":"), right = cell.endsWith(":");
    return left && right ? "center" : right ? "right" : left ? "left" : "";
  });
}

function renderMarkdown(value) {
  const lines = String(value ?? "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const fence = line.match(/^```([A-Za-z0-9_-]*)\s*$/);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const language = fence[1] ? ` class="language-${escapeText(fence[1])}"` : "";
      html.push(`<pre><code${language}>${escapeText(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length + 2;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      html.push(`<blockquote>${quote.map(item => renderInlineMarkdown(item)).join("<br>")}</blockquote>`);
      continue;
    }
    if (isTableRow(line) && index + 1 < lines.length) {
      const alignments = index + 1 < lines.length ? tableSeparatorAlignments(lines[index + 1]) : null;
      if (alignments) {
        const headerCells = splitTableRow(line);
        index += 2;
        const bodyRows = [];
        while (index < lines.length && isTableRow(lines[index])) {
          bodyRows.push(splitTableRow(lines[index]));
          index += 1;
        }
        const alignAttr = align => (align ? ` style="text-align:${align}"` : "");
        const headHtml = headerCells.map((cell, i) => `<th${alignAttr(alignments[i])}>${renderInlineMarkdown(cell)}</th>`).join("");
        const bodyHtml = bodyRows.map(row => `<tr>${headerCells.map((_cell, i) => `<td${alignAttr(alignments[i])}>${renderInlineMarkdown(row[i] ?? "")}</td>`).join("")}</tr>`).join("");
        html.push(`<div class="table-wrap"><table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`);
        continue;
      }
    }
    const list = line.match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
    if (list) {
      const ordered = /\d+\./.test(list[1]);
      const tag = ordered ? "ol" : "ul";
      const items = [];
      const pattern = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
      while (index < lines.length) {
        const item = lines[index].match(pattern);
        if (!item) break;
        index += 1;
        const wrapped = [item[1]];
        while (index < lines.length && lines[index].trim() && !pattern.test(lines[index])) {
          const continuation = lines[index].match(/^\s{2,}(.+)$/);
          if (!continuation) break;
          wrapped.push(continuation[1]);
          index += 1;
        }
        items.push(`<li>${renderInlineMarkdown(wrapped.join(" "))}</li>`);
      }
      html.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && !markdownBlockStart(lines[index])) paragraph.push(lines[index++]);
    html.push(`<p>${paragraph.map(item => renderInlineMarkdown(item)).join("<br>")}</p>`);
  }
  return html.join("");
}
