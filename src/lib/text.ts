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
// (prose, link text, image alt text, component titles and captions, code,
// formulas, footnotes) and drops what only the renderer needs (MDX imports
// and comments, tag syntax, URLs, Markdown markers). Code keeps its exact
// characters, so `#include <stdio.h>` or `snake_case_` survive as written.
const FENCED_BLOCK = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\1[`~]*[ \t]*$/gm;
const INLINE_CODE = /(`+)((?:(?!\1)[^\n])+?)\1/g;
// Attributes whose values are text a reader sees: <Note title>, <Figure
// caption>, <img alt>, <Compare labels={["…", "…"]}>.
const TEXT_ATTRIBUTE = /\b(?:title|caption|alt|label|summary)=(?:"([^"]*)"|'([^']*)')/g;
const LABELS_ATTRIBUTE = /\blabels=\{\[([^\]]*)\]\}/g;
const TAG = /<\/?[A-Za-z][\w.:-]*(?:\s(?:[^<>{}"']|"[^"]*"|'[^']*'|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*)?\/?>/g;

/** Runs `clean` on the text between matches of `pattern`; `keep` turns each
 *  match into the text to keep verbatim. */
function splitClean(text: string, pattern: RegExp, keep: (match: RegExpMatchArray) => string, clean: (part: string) => string) {
  const parts: string[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    parts.push(clean(text.slice(last, match.index)), keep(match));
    last = (match.index || 0) + match[0].length;
  }
  parts.push(clean(text.slice(last)));
  return parts.join(" ");
}

function tagText(tag: string) {
  const values = [...tag.matchAll(TEXT_ATTRIBUTE)].map((match) => match[1] ?? match[2]);
  for (const labels of tag.matchAll(LABELS_ATTRIBUTE)) {
    values.push(...[...labels[1].matchAll(/"([^"]*)"|'([^']*)'/g)].map((match) => match[1] ?? match[2]));
  }
  return ` ${values.join(" ")} `;
}

export function toSearchText(markdown: string) {
  const source = markdown.replace(/\r\n?/g, "\n");
  return splitClean(
    source,
    FENCED_BLOCK,
    (match) => match[0].split("\n").slice(1, -1).join("\n"),
    (prose) => splitClean(prose, INLINE_CODE, (match) => match[2], cleanProse),
  ).replace(/\s+/g, " ").trim();
}

function cleanProse(markdown: string) {
  return markdown
    .replace(/^(?:import|export)\s.*$/gm, " ")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    // JSX/HTML tags, including multi-line ones with {expression} attributes;
    // their visible text attributes stay.
    .replace(TAG, tagText)
    // Images keep their alt text, links keep their label.
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    // Reference-style link definitions ([id]: https://...) go; footnote
    // definitions ([^1]: text) keep their text, markers go.
    .replace(/^\s*\[(?!\^)[^\]]+\]:\s+\S+.*$/gm, " ")
    .replace(/\[\^[^\]\s]+\]:?/g, " ")
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
