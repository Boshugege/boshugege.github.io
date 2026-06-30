import fs from "node:fs/promises";
import path from "node:path";

const rootDir = process.cwd();
const notesPath = path.join(rootDir, "src/content/notes.md");

export interface NoteEntry {
  date: string;
  title: string;
  body: string;
  html: string;
}

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (char) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char] as string;
  });
}

function inlineMarkdown(value: string) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
}

function markdownToHtml(markdown: string) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let paragraph: string[] = [];
  let code: string[] | null = null;

  function flushParagraph() {
    if (!paragraph.length) return;
    out.push(`<p>${inlineMarkdown(paragraph.join("\n")).replace(/\n/g, "<br/>")}</p>`);
    paragraph = [];
  }

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (code) {
        out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = null;
      } else {
        flushParagraph();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(line);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      continue;
    }
    if (line.startsWith("### ")) {
      flushParagraph();
      out.push(`<h3>${inlineMarkdown(line.slice(4))}</h3>`);
      continue;
    }
    paragraph.push(line);
  }

  flushParagraph();
  return out.join("\n");
}

export async function getNotes() {
  const raw = await fs.readFile(notesPath, "utf8");
  const matches = [...raw.matchAll(/^##\s+(\d{4}-\d{2}-\d{2})(?:\s+\|\s*(.+))?\s*$/gm)];
  return matches.map((match, index): NoteEntry => {
    const start = (match.index || 0) + match[0].length;
    const end = matches[index + 1]?.index ?? raw.length;
    const body = raw.slice(start, end).trim();
    return {
      date: match[1],
      title: (match[2] || "").trim(),
      body,
      html: markdownToHtml(body),
    };
  }).sort((a, b) => b.date.localeCompare(a.date));
}
