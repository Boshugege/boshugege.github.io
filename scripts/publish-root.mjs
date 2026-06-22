import fs from "node:fs/promises";
import path from "node:path";

const root = process.cwd();
const dist = path.join(root, "dist");

const rootOutputs = [
  ".nojekyll",
  "_astro",
  "CNAME",
  "about.html",
  "assets",
  "index.html",
  "index.json",
  "manifest.webmanifest",
  "notes.html",
  "notes.json",
  "now.json",
  "posts",
  "robots.txt",
  "rss.xml",
  "search.json",
  "sitemap.xml",
];

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function copyDirectory(source, target) {
  await fs.mkdir(target, { recursive: true });
  for (const entry of await fs.readdir(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(target, entry.name);
    if (entry.isDirectory()) {
      await copyDirectory(from, to);
    } else if (entry.isFile()) {
      await fs.copyFile(from, to);
    }
  }
}

if (!(await exists(dist))) {
  throw new Error("dist does not exist. Run astro build before publishing.");
}

for (const item of rootOutputs) {
  await fs.rm(path.join(root, item), { recursive: true, force: true });
}

for (const entry of await fs.readdir(dist, { withFileTypes: true })) {
  const from = path.join(dist, entry.name);
  const to = path.join(root, entry.name);
  if (entry.isDirectory()) {
    await copyDirectory(from, to);
  } else if (entry.isFile()) {
    await fs.copyFile(from, to);
  }
}

console.log("Published dist to repository root.");
