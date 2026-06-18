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

async function readJpegDimensions(filePath) {
  let file;
  try {
    file = await fs.open(filePath, "r");
    const stat = await file.stat();
    const buffer = Buffer.alloc(Math.min(stat.size, 256 * 1024));
    await file.read(buffer, 0, buffer.length, 0);
    if (buffer[0] !== 0xff || buffer[1] !== 0xd8) return undefined;
    let offset = 2;
    while (offset + 9 < buffer.length) {
      if (buffer[offset] !== 0xff) return undefined;
      const marker = buffer[offset + 1];
      const length = buffer.readUInt16BE(offset + 2);
      if (marker >= 0xc0 && marker <= 0xc3) {
        return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) };
      }
      offset += 2 + length;
    }
  } catch {
    return undefined;
  } finally {
    await file?.close();
  }
}

async function readGifDimensions(filePath) {
  let file;
  try {
    file = await fs.open(filePath, "r");
    const header = Buffer.alloc(10);
    await file.read(header, 0, header.length, 0);
    if (!header.toString("ascii", 0, 6).match(/^GIF8[79]a$/)) return undefined;
    return { width: header.readUInt16LE(6), height: header.readUInt16LE(8) };
  } catch {
    return undefined;
  } finally {
    await file?.close();
  }
}

async function readWebpDimensions(filePath) {
  let file;
  try {
    file = await fs.open(filePath, "r");
    const header = Buffer.alloc(30);
    await file.read(header, 0, header.length, 0);
    if (header.toString("ascii", 0, 4) !== "RIFF" || header.toString("ascii", 8, 12) !== "WEBP") return undefined;
    const type = header.toString("ascii", 12, 16);
    if (type === "VP8 ") return { width: header.readUInt16LE(26) & 0x3fff, height: header.readUInt16LE(28) & 0x3fff };
    if (type === "VP8L") {
      const bits = header.readUInt32LE(21);
      return { width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1 };
    }
    if (type === "VP8X") return { width: header.readUIntLE(24, 3) + 1, height: header.readUIntLE(27, 3) + 1 };
  } catch {
    return undefined;
  } finally {
    await file?.close();
  }
}

async function readImageDimensions(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  if (extension === ".png") return readPngDimensions(filePath);
  if (extension === ".jpg" || extension === ".jpeg") return readJpegDimensions(filePath);
  if (extension === ".gif") return readGifDimensions(filePath);
  if (extension === ".webp") return readWebpDimensions(filePath);
}

function resolveImagePath(source, filePath) {
  const cleanSource = source.split(/[?#]/, 1)[0];
  if (!cleanSource || /^[a-z][a-z0-9+.-]*:/i.test(cleanSource)) return undefined;
  if (cleanSource.startsWith("/")) return path.join(process.cwd(), "src/static", cleanSource);
  if (!filePath) return undefined;
  return path.resolve(path.dirname(filePath), cleanSource);
}

async function collectImages(node, images) {
  if (node?.type === "element" && node.tagName === "img") images.push(node);
  for (const child of node?.children || []) await collectImages(child, images);
}

export default function rehypeImageAttributes() {
  return async (tree, file) => {
    const images = [];
    await collectImages(tree, images);
    await Promise.all(images.map(async (image) => {
      image.properties ||= {};
      image.properties.loading ||= "lazy";
      image.properties.decoding ||= "async";
      const source = String(image.properties.src || "");
      const imagePath = resolveImagePath(source, file?.path || file?.history?.[0]);
      if (!imagePath) return;
      const dimensions = await readImageDimensions(imagePath);
      if (dimensions) Object.assign(image.properties, dimensions);
    }));
  };
}
