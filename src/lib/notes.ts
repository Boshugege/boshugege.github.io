import fs from "node:fs/promises";
import path from "node:path";
import { createMarkdownProcessor } from "@astrojs/markdown-remark";
import { toPlainText } from "./text";

const notesPath = path.join(process.cwd(), "src/content/notes.md");

// Each note in notes.md starts with "## YYYY-MM-DD" or "## YYYY-MM-DD | 标题".
const HEADING = /^##\s+(\d{4}-\d{2}-\d{2})(?:\s+\|\s*(.+))?\s*$/gm;

export interface NoteEntry {
  /** URL id: the date, plus -2, -3… for later notes on the same day. */
  id: string;
  url: string;
  date: string;
  title: string;
  body: string;
  summary: string;
  html: string;
}

let processor: ReturnType<typeof createMarkdownProcessor> | undefined;
function getProcessor() {
  return processor ||= createMarkdownProcessor({
    syntaxHighlight: "shiki",
    shikiConfig: { themes: { light: "github-light", dark: "github-dark" }, defaultColor: false },
  });
}

let cache: Promise<NoteEntry[]> | undefined;
export function getNotes() {
  return cache ||= loadNotes();
}

async function loadNotes() {
  const raw = (await fs.readFile(notesPath, "utf8")).replace(/\r\n/g, "\n");
  const matches = [...raw.matchAll(HEADING)];
  const markdown = await getProcessor();
  // New notes go on top, so number same-day notes from the bottom (oldest
  // first): adding a note never changes the URL of one already shared.
  const ids = new Map<RegExpMatchArray, string>();
  const seen = new Map<string, number>();
  for (const match of [...matches].reverse()) {
    const count = (seen.get(match[1]) || 0) + 1;
    seen.set(match[1], count);
    ids.set(match, count === 1 ? match[1] : `${match[1]}-${count}`);
  }
  const notes = await Promise.all(matches.map(async (match, index): Promise<NoteEntry> => {
    const date = match[1];
    const id = ids.get(match)!;
    const start = (match.index || 0) + match[0].length;
    const end = matches[index + 1]?.index ?? raw.length;
    const body = raw.slice(start, end).trim();
    const { code } = await markdown.render(body);
    return {
      id,
      url: `/notes/${id}.html`,
      date,
      title: (match[2] || "").trim(),
      body,
      summary: toPlainText(body, 120),
      html: code,
    };
  }));
  return notes.sort((a, b) => b.date.localeCompare(a.date));
}
