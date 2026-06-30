import fs from "node:fs/promises";
import path from "node:path";

const dist = path.join(process.cwd(), "dist");
const assetDir = path.join(dist, "_astro");
const textExtensions = new Set([".css", ".html", ".js", ".json", ".svg", ".txt", ".webmanifest", ".xml"]);

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function collectFiles(directory) {
  const files = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const filePath = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await collectFiles(filePath));
    else if (entry.isFile()) files.push(filePath);
  }
  return files;
}

if (await exists(assetDir)) {
  const files = await collectFiles(dist);
  const textFiles = files.filter((file) => textExtensions.has(path.extname(file)));
  const text = (await Promise.all(textFiles.map((file) => fs.readFile(file, "utf8")))).join("\n");
  const staleAssets = files
    .filter((file) => path.dirname(file) === assetDir)
    .filter((file) => !text.includes(path.basename(file)));

  await Promise.all(staleAssets.map((file) => fs.rm(file)));
  if (staleAssets.length > 0) {
    console.log(`Removed ${staleAssets.length} unreferenced _astro asset(s).`);
  }
}
