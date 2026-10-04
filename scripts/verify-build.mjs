import fs from "node:fs/promises";
import path from "node:path";
import { brotliDecompressSync, gunzipSync } from "node:zlib";

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
  "404.html",
  "index.json",
  "search.json",
  "rss.xml",
  "sitemap.xml",
]) {
  await assertFile(file);
}
await assertMissing("now.json");
await assertMissing("CNAME");
await assertMissing(".nojekyll");

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
const notFoundHtml = await read("404.html");
const samplePost = await read("posts/2026-01-20-sample.html");
const imagePost = await read("posts/2023-first-half-conclusion.html");
const coverPost = await read("posts/cg-final-path-tracing.html");
const legacyHeadingPost = await read("posts/wuxieyu.html");
const globalCss = await fs.readFile(path.join(root, "src/styles/global.css"), "utf8");

for (const marker of ["data-impossible-404", "data-404-terminal", "data-404-status", "data-404-canvas", "data-404-fallback", "data-404-pause", "data-404-align"]) {
  if (!notFoundHtml.includes(marker)) throw new Error(`404 page is missing ${marker}`);
}
if (!notFoundHtml.includes('name="robots" content="noindex, follow"') || !notFoundHtml.includes("返回首页")) {
  throw new Error("404 page is missing its indexing policy or home link");
}
if (indexHtml.includes("data-impossible-404")) {
  throw new Error("The 404 scene must not appear on the homepage");
}
const animationScript = notFoundHtml.match(/src="(\/_astro\/404\.[^"]+\.js)"/)?.[1];
if (!animationScript || notFoundHtml.indexOf(animationScript) > notFoundHtml.indexOf("</head>")) {
  throw new Error("The 404 animation script must be discovered in the document head");
}
const animationPath = path.join(dist, animationScript.slice(1));
const animation = await fs.readFile(animationPath);
for (const [suffix, decompress] of [["br", brotliDecompressSync], ["gz", gunzipSync]]) {
  const compressed = await fs.readFile(`${animationPath}.${suffix}`);
  if (compressed.length >= animation.length || !decompress(compressed).equals(animation)) {
    throw new Error(`The 404 animation has an invalid ${suffix} sidecar`);
  }
}

if (!indexHtml.includes("data-post-list") || !indexHtml.includes("data-post-search")) {
  throw new Error("Home page is missing its progressively enhanced post directory");
}
const aboutIndex = indexHtml.indexOf('class="about"');
const sidebarToolsIndex = indexHtml.indexOf('class="sidebar-home-tools"');
const contentIndex = indexHtml.indexOf('class="content"');
if (aboutIndex < 0 || sidebarToolsIndex < aboutIndex || contentIndex < sidebarToolsIndex) {
  throw new Error("Home search and tags must appear below About Me in the desktop sidebar");
}
if (!/@media \(max-width: 800px\)[\s\S]*?\.sidebar-home-tools\s*\{\s*display:\s*none;/.test(globalCss)) {
  throw new Error("Home search and tags must be hidden on mobile");
}
if (!indexHtml.includes("data-post-list-view") || !indexHtml.includes("阅读：")) {
  throw new Error("Home article metadata is missing dynamic view counts");
}
if (!aboutHtml.includes("站点侧写") || !aboutHtml.includes("resume-content")) {
  throw new Error("About page is missing resume or site statistics content");
}
for (const key of ["totalViews", "siteAge", "mostViewed", "commentCount"]) {
  if (!aboutHtml.includes(`data-stat-key="${key}"`)) {
    throw new Error(`About page is missing dynamic statistic: ${key}`);
  }
}
if (!notesHtml.includes("notes-timeline")) {
  throw new Error("Notes page is missing the notes timeline");
}
if (!/href="\/_astro\/katex-local\.[^"]+\.css"/.test(samplePost) || !samplePost.includes("post-content")) {
  throw new Error("Math post is missing KaTeX or article markup");
}
if (!samplePost.includes("data-post-view") || !samplePost.includes("data-post-view-count") || !samplePost.includes("阅读：")) {
  throw new Error("Article metadata is missing its dynamic view count");
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

function getAttributeValues(html, attribute) {
  return [...html.matchAll(new RegExp(`${attribute}="([^"]+)"`, "g"))].map((match) => match[1]);
}

function assertTableOfContents(html, expectedHeadingCount, label) {
  const tocSlugs = getAttributeValues(html, "data-toc-link");
  const uniqueSlugs = [...new Set(tocSlugs)];
  if (uniqueSlugs.length !== expectedHeadingCount || tocSlugs.length !== expectedHeadingCount * 2) {
    throw new Error(`${label} table of contents does not render matching inline and rail links`);
  }
  for (const slug of uniqueSlugs) {
    if (!html.includes(`id="${slug}"`)) {
      throw new Error(`${label} table of contents points to a missing heading: ${slug}`);
    }
  }
}

assertTableOfContents(coverPost, 13, "Long article");
assertTableOfContents(legacyHeadingPost, 3, "Legacy H4 article");
if (samplePost.includes("data-post-toc")) {
  throw new Error("Short article should not render a table of contents");
}
if (!coverPost.includes("post-toc-inline") || !coverPost.includes("post-toc-rail")) {
  throw new Error("Article table of contents is missing a responsive variant");
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

// Identity assets and references must ship together on every branded route.
const manifest = JSON.parse(await read("manifest.webmanifest"));
for (const icon of manifest.icons) await assertFile(icon.src.split("?")[0].replace(/^\//, ""));
for (const file of ["favicon.svg", "apple-touch-icon.png", "assets/img/avatar.png"]) await assertFile(file);
await assertMissing("assets/img/icon.jpg");
for (const html of [indexHtml, aboutHtml, notesHtml, samplePost, notFoundHtml]) {
  for (const backing of ["rear-x", "rear-y", "bottom"]) {
    if (!html.includes(`data-pnc-backing="${backing}"`)) throw new Error(`Missing PNC ${backing} backing`);
  }
  for (const face of ["frame", "top", "left", "right"]) {
    if (!html.includes(`data-pnc-face="${face}"`)) throw new Error(`Missing PNC ${face} face`);
  }
  if (!html.includes('href="/favicon.svg?v=cube"') || !html.includes('href="/apple-touch-icon.png?v=cube"') || html.includes("/assets/img/icon.jpg")) {
    throw new Error("Page has stale or missing identity icon references");
  }
  for (const match of html.matchAll(/(?:src|href)="(\/(?:assets\/img\/[^"?#]+|favicon\.svg|apple-touch-icon\.png))(?:\?[^"]*)?"/g)) {
    await assertFile(match[1].slice(1));
  }
}
console.log(`Verified PNC identity assets; index.html is ${indexSize} / 40,000 bytes.`);

if (!aboutHtml.includes("data-pnc-wordmark") || !aboutHtml.includes("data-pnc-proximity") || aboutHtml.indexOf("data-pnc-wordmark") > aboutHtml.indexOf('class="resume-content"')) {
  throw new Error("About page is missing its introductory PNC wordmark");
}
