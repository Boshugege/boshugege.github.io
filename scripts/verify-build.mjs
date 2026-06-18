import fs from "node:fs/promises";
import path from "node:path";

const root = process.cwd();
const dist = path.join(root, "dist");

async function read(relativePath) {
  return fs.readFile(path.join(dist, relativePath), "utf8");
}

async function assertFile(relativePath) {
  try {
    await fs.access(path.join(dist, relativePath));
  } catch {
    throw new Error(`Missing build output: ${relativePath}`);
  }
}

for (const file of [
  "index.html",
  "about.html",
  "notes.html",
  "index.json",
  "search.json",
  "rss.xml",
  "sitemap.xml",
]) {
  await assertFile(file);
}

const postSources = (await fs.readdir(path.join(root, "src/content/posts")))
  .filter((name) => /\.mdx?$/.test(name));
for (const source of postSources) {
  await assertFile(`posts/${source.replace(/\.mdx?$/, ".html")}`);
}

const indexHtml = await read("index.html");
const aboutHtml = await read("about.html");
const notesHtml = await read("notes.html");
const samplePost = await read("posts/2026-01-20-sample.html");
const imagePost = await read("posts/2023-first-half-conclusion.html");

if (!indexHtml.includes("data-post-list") || !indexHtml.includes("data-post-search")) {
  throw new Error("Home page is missing its progressively enhanced post directory");
}
if (!aboutHtml.includes("站点侧写") || !aboutHtml.includes("resume-content")) {
  throw new Error("About page is missing resume or site statistics content");
}
if (!notesHtml.includes("notes-timeline")) {
  throw new Error("Notes page is missing the notes timeline");
}
if (!samplePost.includes("katex.min.css") || !samplePost.includes("post-content")) {
  throw new Error("Math post is missing KaTeX or article markup");
}
if (!/<img[^>]+loading="lazy"[^>]+decoding="async"[^>]+width="\d+"[^>]+height="\d+"/.test(imagePost)) {
  throw new Error("Article images are missing lazy loading or intrinsic dimensions");
}
if (indexHtml.includes("katex.min.css") || aboutHtml.includes("katex.min.css")) {
  throw new Error("KaTeX must not load on pages without formulas");
}

for (const html of [indexHtml, aboutHtml, notesHtml, samplePost]) {
  if (html.includes("/assets/js/site.js")) {
    throw new Error("Legacy site.js is still referenced");
  }
}

const searchSize = (await fs.stat(path.join(dist, "search.json"))).size;
const indexSize = (await fs.stat(path.join(dist, "index.html"))).size;
if (searchSize > 120_000) throw new Error(`search.json exceeds 120 KB: ${searchSize}`);
if (indexSize > 40_000) throw new Error(`index.html exceeds 40 KB: ${indexSize}`);

console.log(`Verified ${postSources.length} posts and core static outputs.`);
