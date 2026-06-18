import fs from "node:fs/promises";
import path from "node:path";

async function readPngDimensions(filePath) {
  let file;
  try {
    file = await fs.open(filePath, "r");
    const header = Buffer.alloc(24);
    await file.read(header, 0, header.length, 0);
    if (header.toString("hex", 0, 8) !== "89504e470d0a1a0a") return undefined;
    return { width: header.readUInt32BE(16), height: header.readUInt32BE(20) };
  } catch {
    return undefined;
  } finally {
    await file?.close();
  }
}

async function collectImages(node, images) {
  if (node?.type === "element" && node.tagName === "img") images.push(node);
  for (const child of node?.children || []) await collectImages(child, images);
}

export default function rehypeImageAttributes() {
  return async (tree) => {
    const images = [];
    await collectImages(tree, images);
    await Promise.all(images.map(async (image) => {
      image.properties ||= {};
      image.properties.loading ||= "lazy";
      image.properties.decoding ||= "async";
      const source = String(image.properties.src || "");
      if (!source.startsWith("/assets/img/") || !source.toLowerCase().endsWith(".png")) return;
      const dimensions = await readPngDimensions(path.join(process.cwd(), "src/static", source));
      if (dimensions) Object.assign(image.properties, dimensions);
    }));
  };
}
