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

async function assertMissing(relativePath) {
  try {
    await fs.access(path.join(dist, relativePath));
  } catch {
    return;
  }
  throw new Error(`Unexpected build output: ${relativePath}`);
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
await assertMissing("now.json");

async function collectPostSources(directory, prefix = "") {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const sources = [];
  for (const entry of entries) {
    const relativePath = path.posix.join(prefix, entry.name);
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) sources.push(...await collectPostSources(fullPath, relativePath));
    else if (/\.mdx?$/.test(entry.name)) sources.push(relativePath);
  }
  return sources;
}

const postSources = await collectPostSources(path.join(root, "src/content/posts"));
for (const source of postSources) {
  await assertFile(`posts/${source.replace(/\/index\.mdx?$/, ".html").replace(/\.mdx?$/, ".html")}`);
}

const indexHtml = await read("index.html");
const aboutHtml = await read("about.html");
const notesHtml = await read("notes.html");
const samplePost = await read("posts/2026-01-20-sample.html");
const imagePost = await read("posts/2023-first-half-conclusion.html");
const coverPost = await read("posts/cg-final-path-tracing.html");

if (!indexHtml.includes("data-post-list") || !indexHtml.includes("data-post-search")) {
  throw new Error("Home page is missing its progressively enhanced post directory");
}
if (!aboutHtml.includes("站点侧写") || !aboutHtml.includes("resume-content")) {
  throw new Error("About page is missing resume or site statistics content");
}
if (!notesHtml.includes("notes-timeline")) {
  throw new Error("Notes page is missing the notes timeline");
}
if (!/href="\/_astro\/katex-local\.[^"]+\.css"/.test(samplePost) || !samplePost.includes("post-content")) {
  throw new Error("Math post is missing KaTeX or article markup");
}
if (!/<img[^>]+loading="lazy"[^>]+decoding="async"[^>]+width="\d+"[^>]+height="\d+"/.test(imagePost)) {
  throw new Error("Article images are missing lazy loading or intrinsic dimensions");
}
if (indexHtml.includes("katex.min") || aboutHtml.includes("katex.min")) {
  throw new Error("KaTeX must not load on pages without formulas");
}
if ([indexHtml, aboutHtml, notesHtml, samplePost, coverPost].some((html) => html.includes("cdn.jsdelivr.net/npm/katex"))) {
  throw new Error("KaTeX CSS must be bundled locally instead of loaded from a CDN");
}
if (coverPost.includes("src/content/posts") || !coverPost.includes('property="og:image:alt"') || !/property="og:image" content="https:\/\/parityncsvt\.top\/_astro\/bmw_hq\.[^"]+\.webp"/.test(coverPost)) {
  throw new Error("Article cover metadata is not using an optimized Astro image");
}
if (!coverPost.includes('"dateModified"') || !coverPost.includes('property="article:modified_time"')) {
  throw new Error("Article metadata is missing modified-time fields");
}

for (const html of [indexHtml, aboutHtml, notesHtml, samplePost]) {
  if (html.includes("/assets/js/site.js")) {
    throw new Error("Legacy site.js is still referenced");
  }
  if (html.includes("data-now-status") || html.includes("/now.json")) {
    throw new Error("Calendar status integration is still referenced");
  }
}

const searchSize = (await fs.stat(path.join(dist, "search.json"))).size;
const indexSize = (await fs.stat(path.join(dist, "index.html"))).size;
if (searchSize > 120_000) throw new Error(`search.json exceeds 120 KB: ${searchSize}`);
if (indexSize > 40_000) throw new Error(`index.html exceeds 40 KB: ${indexSize}`);
await assertMissing("assets/img/cidai/index.png");
await assertMissing("_astro/bmw_hq.DEJyjXyi.jpg");

console.log(`Verified ${postSources.length} posts and core static outputs.`);
