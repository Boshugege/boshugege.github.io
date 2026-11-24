// Link previews in notes. A bare URL on a line of its own becomes a card
// (title, description, thumbnail); YouTube, Bilibili and X links become a
// cover that loads the provider's player on click (src/scripts/link-embed.ts).
// Links inside a sentence and [text](url) links stay as written.
//
// Nothing is fetched at build time. The title, description and thumbnail come
// from src/data/link-previews.json, which the Telegram bot fills in when a
// note is posted (or `npm run links` for hand-written notes). A link without
// an entry still gets a card, showing its address only. Thumbnails are local
// files under /assets/notes/, so reading a note contacts no third party.
import { existsSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { classifyLink, embedSrc, type LinkKind, type LinkTarget } from "./link-providers.ts";

interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

export interface LinkPreview {
  kind?: string;
  title?: string;
  description?: string;
  site?: string;
  author?: string;
  /** Where a short link (b23.tv) leads; used to find the player. */
  url?: string;
  /** /assets/notes/<name>.webp */
  image?: string;
  stars?: number;
  language?: string;
  fetched?: string;
}

export type LinkPreviews = Map<string, LinkPreview>;

export interface LinkCardOptions {
  /** The previews, or a function returning the current ones (the dev server re-reads the file). */
  previews: LinkPreviews | (() => LinkPreviews);
  /**
   * Astro's publicDir. When given, a thumbnail whose file is gone (a note
   * was undone, a file was removed by hand) is left out instead of failing
   * the build in rehypeNoteImages: a preview is never worth a failed deploy.
   */
  staticDir?: string;
}

const THUMBNAIL = /^\/assets\/notes\/[0-9A-Za-z][0-9A-Za-z._-]*\.webp$/;
const SITE_NAMES: Record<LinkKind, string> = { link: "", youtube: "YouTube", bilibili: "哔哩哔哩", x: "X", github: "GitHub" };
const PLAY_LABELS: Partial<Record<LinkKind, string>> = { youtube: "播放视频", bilibili: "播放视频" };

/** One spelling per link, so the JSON key and the rendered href meet. */
export function linkKey(url: string): string | undefined {
  try {
    const parsed = new URL(url.trim());
    return parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

function text(value: unknown, limit: number) {
  if (typeof value !== "string") return undefined;
  const clean = value.replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, limit) : undefined;
}

/** Read link-previews.json. A missing file means no previews; a broken one fails the build. */
export async function loadLinkPreviews(file: string): Promise<LinkPreviews> {
  let raw: string;
  try {
    raw = await fs.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return new Map();
    throw error;
  }
  return parseLinkPreviews(raw);
}

export function parseLinkPreviews(raw: string): LinkPreviews {
  const data = JSON.parse(raw) as { version?: unknown; links?: unknown };
  if (data?.version !== 1 || !data.links || typeof data.links !== "object" || Array.isArray(data.links)) {
    throw new Error("link-previews.json: expected { \"version\": 1, \"links\": { … } }");
  }
  const previews: LinkPreviews = new Map();
  for (const [url, value] of Object.entries(data.links as Record<string, Record<string, unknown>>)) {
    const key = linkKey(url);
    if (!key || !value || typeof value !== "object") throw new Error(`link-previews.json: bad entry ${JSON.stringify(url)}`);
    const image = text(value.image, 200);
    if (image && !THUMBNAIL.test(image)) throw new Error(`link-previews.json: ${url}: image must be a .webp under /assets/notes/`);
    previews.set(key, {
      title: text(value.title, 160),
      description: text(value.description, 260),
      site: text(value.site, 60),
      author: text(value.author, 80),
      url: typeof value.url === "string" ? linkKey(value.url) : undefined,
      image,
      stars: typeof value.stars === "number" && Number.isInteger(value.stars) && value.stars >= 0 ? value.stars : undefined,
      language: text(value.language, 40),
    });
  }
  return previews;
}

// --- finding the links -------------------------------------------------------

interface Line {
  /** What separated this line from the one before: "\n" text, or <br> plus "\n". */
  separator: HastNode[];
  nodes: HastNode[];
}

function isElement(node: HastNode | undefined, tagName: string) {
  return node?.type === "element" && node.tagName === tagName;
}

function isBlank(node: HastNode) {
  return node.type === "text" && !(node.value || "").trim();
}

/** The lines of a paragraph as the reader sees its source: split at newlines and <br>. */
function paragraphLines(paragraph: HastNode): Line[] {
  const lines: Line[] = [{ separator: [], nodes: [] }];
  for (const child of paragraph.children || []) {
    const current = lines[lines.length - 1];
    if (isElement(child, "br")) {
      lines.push({ separator: [child], nodes: [] });
    } else if (child.type === "text" && (child.value || "").includes("\n")) {
      const parts = (child.value || "").split("\n");
      if (parts[0]) current.nodes.push({ type: "text", value: parts[0] });
      for (const [index, part] of parts.slice(1).entries()) {
        const last = lines[lines.length - 1];
        // The newline right after a <br> belongs to that break.
        if (index === 0 && !last.nodes.length && isElement(last.separator[last.separator.length - 1], "br")) {
          last.separator.push({ type: "text", value: "\n" });
        } else {
          lines.push({ separator: [{ type: "text", value: "\n" }], nodes: [] });
        }
        if (part) lines[lines.length - 1].nodes.push({ type: "text", value: part });
      }
    } else {
      current.nodes.push(child);
    }
  }
  return lines;
}

/** The address of a line that is nothing but a bare URL (an autolink), else undefined. */
function bareLink(line: Line): string | undefined {
  const nodes = line.nodes.filter((node) => !isBlank(node));
  if (nodes.length !== 1 || !isElement(nodes[0], "a")) return undefined;
  const anchor = nodes[0];
  const href = anchor.properties?.href;
  const children = anchor.children || [];
  if (typeof href !== "string" || children.length !== 1 || children[0].type !== "text") return undefined;
  const key = linkKey(href);
  // An autolink shows its own address; [text](url) shows something else.
  return key && key === linkKey(children[0].value || "") ? href : undefined;
}

// --- building the cards ------------------------------------------------------

function element(tagName: string, properties: Record<string, unknown>, children: Array<HastNode | undefined | false>): HastNode {
  return { type: "element", tagName, properties, children: children.filter(Boolean) as HastNode[] };
}

function span(className: string, value: string | undefined) {
  return value ? element("span", { className: [className] }, [{ type: "text", value }]) : undefined;
}

const EXTERNAL = { target: "_blank", rel: ["noopener", "noreferrer"] };

function formatStars(stars: number) {
  if (stars < 1000) return String(stars);
  const thousands = stars / 1000;
  return `${thousands >= 100 ? Math.round(thousands) : Math.round(thousands * 10) / 10}k`;
}

/** example.com/some/path, for links nobody fetched a title for. */
function addressLabel(href: string) {
  try {
    const url = new URL(href);
    const path = decodeURI(url.pathname).replace(/\/$/, "");
    const label = url.hostname.replace(/^www\./, "") + path;
    return label.length > 80 ? `${label.slice(0, 79)}…` : label;
  } catch {
    return href;
  }
}

function hostLabel(href: string) {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function thumbnail(preview: LinkPreview | undefined, className: string) {
  // width and height are added by rehypeNoteImages, which runs after this plugin.
  return preview?.image ? element("img", { className: [className], src: preview.image, alt: "" }, []) : undefined;
}

function cardText(title: string, description: string | undefined, site: string) {
  return element("span", { className: ["link-card-text"] }, [
    span("link-card-title", title),
    span("link-card-desc", description),
    span("link-card-site", site),
  ]);
}

function linkCard(href: string, kind: LinkKind, title: string, description: string | undefined, site: string, image?: HastNode) {
  return element("a", { className: ["link-card"], href, dataLinkKind: kind, ...EXTERNAL }, [cardText(title, description, site), image]);
}

function renderLink(href: string, target: LinkTarget, preview: LinkPreview | undefined): HastNode {
  const { kind, ids } = target;
  if (kind === "github") {
    const site = ["GitHub", preview?.stars != null && `★ ${formatStars(preview.stars)}`, preview?.language].filter(Boolean).join(" · ");
    return linkCard(href, kind, preview?.title || `${ids.owner}/${ids.repo}`, preview?.description, site, thumbnail(preview, "link-card-avatar"));
  }
  if (kind === "link") {
    const host = hostLabel(href);
    const site = preview?.site && preview.site.toLowerCase() !== host.toLowerCase() ? `${preview.site} · ${host}` : host;
    return linkCard(href, kind, preview?.title || addressLabel(href), preview?.description, site, thumbnail(preview, "link-card-thumb"));
  }

  const player = embedSrc(target, preview?.url || href);
  const site = [SITE_NAMES[kind], preview?.author].filter(Boolean).join(" · ");
  if (kind === "x") {
    const title = preview?.title || `@${ids.user} 的帖子`;
    return element("div", { className: ["link-embed", "link-embed-post"], dataLinkKind: kind, dataEmbedSrc: player, dataEmbedTitle: title }, [
      linkCard(href, kind, title, preview?.description, "X"),
      // Shown by link-embed.ts: without scripts the card above is the whole preview.
      element("button", { type: "button", className: ["link-embed-load"], hidden: true }, [{ type: "text", value: "在此展开原帖" }]),
    ]);
  }
  const title = preview?.title || `${SITE_NAMES[kind]} 视频`;
  const cover = element("a", { className: ["link-embed-cover"], href, ariaLabel: `${PLAY_LABELS[kind]}：${title}`, ...EXTERNAL }, [
    thumbnail(preview, "link-embed-thumb"),
    element("span", { className: ["link-embed-play"], ariaHidden: "true" }, []),
  ]);
  const caption = element("span", { className: ["link-embed-caption"] }, [
    element("a", { className: ["link-embed-title"], href, ...EXTERNAL }, [{ type: "text", value: title }]),
    span("link-embed-site", site),
  ]);
  // No player (a b23.tv link nobody resolved): the cover is a plain link.
  const properties: Record<string, unknown> = { className: ["link-embed", "link-embed-video"], dataLinkKind: kind };
  if (player) Object.assign(properties, { dataEmbedSrc: player, dataEmbedTitle: title });
  return element("div", properties, [cover, caption]);
}

function targetOf(href: string, preview: LinkPreview | undefined): LinkTarget {
  const target = classifyLink(href);
  // A short link is known by where the bot saw it lead.
  if (preview?.url && (target.kind === "link" || target.ids.short)) {
    const resolved = classifyLink(preview.url);
    if (resolved.kind !== "link" && !resolved.ids.short) return resolved;
  }
  return target;
}

function paragraphOf(original: HastNode, lines: Line[]): HastNode | undefined {
  const children: HastNode[] = [];
  for (const [index, line] of lines.entries()) {
    if (index > 0) children.push(...line.separator);
    children.push(...line.nodes);
  }
  return children.some((node) => !isBlank(node) && !isElement(node, "br")) ? { ...original, children } : undefined;
}

function withExistingImage(preview: LinkPreview | undefined, staticDir: string | undefined) {
  if (!preview?.image || !staticDir || existsSync(path.join(staticDir, preview.image))) return preview;
  return { ...preview, image: undefined };
}

function splitParagraph(paragraph: HastNode, previews: LinkPreviews, staticDir?: string): HastNode[] {
  const lines = paragraphLines(paragraph);
  const links = lines.map(bareLink);
  if (!links.some(Boolean)) return [paragraph];
  const result: HastNode[] = [];
  let run: Line[] = [];
  const flush = () => {
    while (run.length && !run[run.length - 1].nodes.some((node) => !isBlank(node))) run.pop();
    while (run.length && !run[0].nodes.some((node) => !isBlank(node))) run.shift();
    const rest = run.length ? paragraphOf(paragraph, run) : undefined;
    if (rest) result.push(rest, { type: "text", value: "\n" });
    run = [];
  };
  for (const [index, line] of lines.entries()) {
    const href = links[index];
    if (!href) {
      run.push(line);
      continue;
    }
    flush();
    const preview = withExistingImage(previews.get(linkKey(href)!), staticDir);
    result.push(renderLink(href, targetOf(href, preview), preview), { type: "text", value: "\n" });
  }
  flush();
  result.pop(); // the newline after the last block
  return result;
}

/** Turn bare-URL lines in top-level paragraphs into link cards and embeds. */
export function rehypeLinkCards({ previews, staticDir }: LinkCardOptions) {
  return (tree: HastNode) => {
    const current = typeof previews === "function" ? previews() : previews;
    tree.children = (tree.children || []).flatMap((node) => (isElement(node, "p") ? splitParagraph(node, current, staticDir) : [node]));
  };
}
