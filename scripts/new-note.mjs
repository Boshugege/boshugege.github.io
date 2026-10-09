// Usage: npm run note -- "正文" ["可选标题"]
// Adds a note dated today (Asia/Shanghai) to the top of src/content/notes.md.
import fs from "node:fs/promises";

const [text, title] = process.argv.slice(2);
if (!text?.trim()) {
  console.error('Usage: npm run note -- "正文" ["可选标题"]');
  process.exit(1);
}

const file = "src/content/notes.md";
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
const heading = title?.trim() ? `## ${today} | ${title.trim()}` : `## ${today}`;
const entry = `${heading}\n\n${text.trim().replace(/\\n/g, "\n")}\n\n`;

const raw = await fs.readFile(file, "utf8");
const first = raw.search(/^## \d{4}-\d{2}-\d{2}/m);
const next = first < 0 ? `${raw.trimEnd()}\n\n${entry}` : raw.slice(0, first) + entry + raw.slice(first);
await fs.writeFile(file, next);

console.log(`Added ${heading} to ${file}. Commit and push to publish.`);
