// Images in notes.md. Notes are rendered with createMarkdownProcessor
// (src/lib/notes.ts), outside the content pipeline that gives post images
// loading="lazy", decoding="async" and their intrinsic size. This rehype
// plugin does the same for Markdown images (raw HTML <img> is parsed after
// rehype plugins run, so it is left as written). The size is read from the
// file in src/static for images under /assets/, where the Telegram bot puts
// note images; a missing file fails the build instead of shipping a broken
// image.
import fs from "node:fs/promises";
import path from "node:path";
import { imageMetadata } from "astro/assets/utils";

interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

export interface NoteImageOptions {
  /** Astro's publicDir: /assets/x.webp is served from <staticDir>/assets/x.webp. */
  staticDir: string;
}

export interface ImageSize {
  width: number;
  height: number;
}

/**
 * The file path (relative to the static dir) an image src such as
 * /assets/notes/x.webp points at, or undefined for anything else: remote and
 * relative srcs, other site paths, and paths with empty, . or .. segments.
 */
export function localAssetPath(src: string): string | undefined {
  if (!src.startsWith("/assets/")) return undefined;
  let decoded: string;
  try {
    decoded = decodeURI(src.replace(/[?#].*$/s, ""));
  } catch {
    return undefined;
  }
  const segments = decoded.slice(1).split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === ".." || segment.includes("\\"))) {
    return undefined;
  }
  return segments.join("/");
}

/** Width and height of an image in the static dir; throws if it is missing or not an image. */
export async function staticImageSize(staticDir: string, assetPath: string): Promise<ImageSize> {
  let data: Uint8Array;
  try {
    data = await fs.readFile(path.join(staticDir, assetPath));
  } catch (error) {
    throw new Error(`notes.md: image /${assetPath} not found in ${path.relative(process.cwd(), staticDir) || staticDir}`, { cause: error });
  }
  const { width, height } = await imageMetadata(data, `/${assetPath}`);
  return { width, height };
}

function collectImages(node: HastNode, images: HastNode[]) {
  if (node.type === "element" && node.tagName === "img") images.push(node);
  for (const child of node.children || []) collectImages(child, images);
}

async function decorateImage(image: HastNode, staticDir: string) {
  const properties = (image.properties ||= {});
  properties.loading ??= "lazy";
  properties.decoding ??= "async";
  if (properties.width != null || properties.height != null) return;
  const asset = typeof properties.src === "string" ? localAssetPath(properties.src) : undefined;
  if (!asset) return;
  const { width, height } = await staticImageSize(staticDir, asset);
  properties.width = width;
  properties.height = height;
}

export function rehypeNoteImages({ staticDir }: NoteImageOptions) {
  return async (tree: HastNode) => {
    const images: HastNode[] = [];
    collectImages(tree, images);
    await Promise.all(images.map((image) => decorateImage(image, staticDir)));
  };
}
