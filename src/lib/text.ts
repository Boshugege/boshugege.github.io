// Plain-text summary for meta descriptions, RSS and search: drops Markdown
// and MDX syntax so previews never show backticks, links or tags.
export function toPlainText(markdown: string, maxLength = 160) {
  const text = markdown
    .replace(/^(import|export) .*$/gm, "")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/^```[\s\S]*?^```/gm, "")
    .replace(/<[^>]+>/g, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s?/gm, "")
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/`|\*\*|~~|\$+/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > maxLength ? `${text.slice(0, maxLength).trimEnd()}…` : text;
}

// Searchable text for search.json: keeps every word a reader might look for
// (prose, link text, image alt text, code, formulas) and drops what only the
// renderer needs (MDX imports and comments, tags, URLs, Markdown markers).
// Code blocks keep their contents verbatim (minus the fence lines), so
// `#include <stdio.h>` is not mistaken for an HTML tag.
const FENCED_BLOCK = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\1[`~]*[ \t]*$/gm;

export function toSearchText(markdown: string) {
  const source = markdown.replace(/\r\n?/g, "\n");
  const parts: string[] = [];
  let last = 0;
  for (const match of source.matchAll(FENCED_BLOCK)) {
    parts.push(cleanProse(source.slice(last, match.index)));
    parts.push(match[0].split("\n").slice(1, -1).join("\n"));
    last = (match.index || 0) + match[0].length;
  }
  parts.push(cleanProse(source.slice(last)));
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

function cleanProse(markdown: string) {
  return markdown
    .replace(/^(?:import|export)\s.*$/gm, " ")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    // JSX/HTML tags, including multi-line ones with {expression} attributes.
    .replace(/<\/?[A-Za-z][\w.:-]*(?:\s(?:[^<>{}"']|"[^"]*"|'[^']*'|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*)?\/?>/g, " ")
    // Images keep their alt text, links keep their label.
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    // Reference-style link definitions: [id]: https://...
    .replace(/^\s*\[[^\]]+\]:\s+\S+.*$/gm, " ")
    .replace(/<(https?:\/\/[^>\s]+)>/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/gm, "")
    // Table separator rows and cell borders.
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$/gm, " ")
    .replace(/\s*\|\s*/g, " ")
    .replace(/^\s*(?:[-*_]\s*){3,}$/gm, " ")
    .replace(/(\*\*|__|~~|`+|\$\$?)/g, " ")
    .replace(/(^|[\s(])[*_](?=\S)|(?<=\S)[*_](?=[\s).,;:!?，。；：！？]|$)/gm, "$1");
}
