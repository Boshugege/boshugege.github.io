#!/usr/bin/env node
// npm run links — fetch previews for links in hand-written notes.
//
// The Telegram bot does this when a note is posted (ops/telegram-bot/
// pnc_bot/linkpreview.py). For notes written in an editor, this script finds
// every bare https:// URL on a line of its own in src/content/notes.md that
// has no entry in src/data/link-previews.json yet, fetches its title,
// description and thumbnail once, and stores them (thumbnails as WebP under
// src/static/assets/notes/). The site build itself never touches the network.
//
//   npm run links             fetch what is missing
//   npm run links -- --all    fetch every link again
//
// Behind a proxy, Node 24+ honours HTTPS_PROXY with NODE_USE_ENV_PROXY=1.
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";
import { classifyLink } from "../src/lib/link-providers.ts";

const root = path.resolve(import.meta.dirname, "..");
const notesPath = path.join(root, "src/content/notes.md");
const previewsPath = path.join(root, "src/data/link-previews.json");
const assetDir = path.join(root, "src/static/assets/notes");
const USER_AGENT = "Mozilla/5.0 (compatible; LinkPreview/1.0)";
const TIMEOUT = 15_000;
const LIMITS = { title: 120, description: 200, site: 40, author: 60, language: 30 };
const SITES = { youtube: "YouTube", bilibili: "哔哩哔哩", x: "X", github: "GitHub" };

/** Bare https URLs alone on a line, outside code blocks, in order. */
export function standaloneLinks(markdown) {
  const links = [];
  let fence;
  for (const line of markdown.split("\n")) {
    const mark = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (fence) {
      if (mark && mark[0] === fence[0] && mark.length >= fence.length && !line.trim().slice(mark.length).trim()) fence = undefined;
      continue;
    }
    if (mark) {
      fence = mark;
      continue;
    }
    if (/^(?: {4}|\t)/.test(line)) continue;
    const match = /^<?(https:\/\/[^\s<>]+)>?$/.exec(line.trim());
    if (match && !/[.,;:!?'"*_~]$/.test(match[1]) && !links.includes(match[1])) links.push(match[1]);
  }
  return links;
}

function clean(value, limit) {
  if (typeof value !== "string") return "";
  const text = value
    .replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  // Some sites fill an empty description with a dash.
  if (!/[\p{L}\p{N}]/u.test(text)) return "";
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function decodeEntities(text) {
  const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, code) => {
    if (code[0] !== "#") return named[code.toLowerCase()] ?? whole;
    const point = code[1].toLowerCase() === "x" ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
    return Number.isFinite(point) && point > 0 && point <= 0x10ffff ? String.fromCodePoint(point) : whole;
  });
}

async function get(url, accept) {
  const target = new URL(url);
  if (target.protocol !== "https:") throw new Error("only https links are fetched");
  const response = await fetch(target, {
    headers: { "user-agent": USER_AGENT, accept, "accept-language": "zh-CN,zh;q=0.9,en;q=0.8" },
    signal: AbortSignal.timeout(TIMEOUT),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response;
}

const getJson = async (url) => (await get(url, "application/json")).json();

async function getImage(url) {
  try {
    const response = await get(String(url).replace(/^http:/, "https:"), "image/*");
    if (!(response.headers.get("content-type") || "").startsWith("image/")) return undefined;
    const data = Buffer.from(await response.arrayBuffer());
    return data.length && data.length <= 5 * 1024 * 1024 ? data : undefined;
  } catch {
    return undefined;
  }
}

function metaTags(html) {
  const tags = {};
  for (const [tag] of html.matchAll(/<meta\b[^>]*>/gi)) {
    const attribute = (name) => {
      const match = new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, "i").exec(tag);
      return match ? decodeEntities(match[1] ?? match[2] ?? match[3] ?? "") : undefined;
    };
    const key = (attribute("property") || attribute("name") || "").toLowerCase();
    const content = attribute("content");
    if (key && content && !(key in tags)) tags[key] = content;
  }
  return tags;
}

async function fetchPage(url) {
  const response = await get(url, "text/html,application/xhtml+xml");
  if (!/^(text\/html|application\/xhtml\+xml)/i.test(response.headers.get("content-type") || "")) throw new Error("not a web page");
  const html = (await response.text()).slice(0, 512 * 1024);
  const tags = metaTags(html);
  const title = tags["og:title"] || tags["twitter:title"] || decodeEntities(/<title[^>]*>([^<]*)<\/title>/i.exec(html)?.[1] || "");
  const image = tags["og:image:secure_url"] || tags["og:image"] || tags["twitter:image"];
  return {
    title,
    description: tags["og:description"] || tags["twitter:description"] || tags.description || "",
    site: tags["og:site_name"] || new URL(response.url).hostname.replace(/^www\./, ""),
    image: image ? await getImage(new URL(image, response.url)) : undefined,
    finalUrl: response.url,
  };
}

async function youtube(ids) {
  const watch = `https://www.youtube.com/watch?v=${ids.id}`;
  const info = await getJson(`https://www.youtube.com/oembed?url=${encodeURIComponent(watch)}&format=json`).catch(() => ({}));
  let image;
  for (const name of ["maxresdefault", "mqdefault", "hqdefault"]) {
    image = await getImage(`https://i.ytimg.com/vi/${ids.id}/${name}.jpg`);
    if (image) break;
  }
  return { title: info.title, author: info.author_name, image };
}

async function bilibili(ids, url) {
  let canonical;
  if (ids.short) {
    const page = await get(url, "text/html");
    const resolved = classifyLink(page.url);
    if (resolved.kind !== "bilibili" || resolved.ids.short) throw new Error("short link did not lead to a video");
    ids = resolved.ids;
    const part = new URL(page.url).searchParams.get("p");
    canonical = `https://www.bilibili.com/video/${ids.bvid || `av${ids.aid}`}${/^\d+$/.test(part || "") ? `?p=${part}` : ""}`;
  }
  const query = ids.bvid ? `bvid=${ids.bvid}` : `aid=${ids.aid}`;
  const info = await getJson(`https://api.bilibili.com/x/web-interface/view?${query}`);
  if (info.code !== 0 || !info.data) throw new Error(`bilibili: ${info.message || info.code}`);
  return { title: info.data.title, description: info.data.desc, author: info.data.owner?.name, image: await getImage(info.data.pic), canonical };
}

async function xPost(ids) {
  const post = `https://twitter.com/${ids.user}/status/${ids.id}`;
  const info = await getJson(`https://publish.twitter.com/oembed?url=${encodeURIComponent(post)}&omit_script=1&dnt=1`);
  const body = /<p[^>]*>([\s\S]*?)<\/p>/i.exec(info.html || "")?.[1] || "";
  const text = decodeEntities(body.replace(/<br\s*\/?>/gi, " ").replace(/<[^>]+>/g, ""));
  return { title: `${info.author_name || ids.user} (@${ids.user})`, description: text, author: info.author_name };
}

async function github(ids) {
  const response = await get(`https://api.github.com/repos/${ids.owner}/${ids.repo}`, "application/vnd.github+json");
  const info = await response.json();
  return {
    title: info.full_name,
    description: info.description,
    stars: Number.isInteger(info.stargazers_count) ? info.stargazers_count : undefined,
    language: info.language,
    image: info.owner?.avatar_url ? await getImage(info.owner.avatar_url) : undefined,
  };
}

async function fetchPreview(url) {
  const { kind, ids } = classifyLink(url);
  const provider = { youtube, bilibili, x: xPost, github }[kind];
  if (provider) {
    try {
      return { kind, site: SITES[kind], ...(await provider(ids, url)) };
    } catch (error) {
      console.warn(`  ${kind}: ${error.message}; reading the page instead`);
    }
  }
  return { kind, ...(await fetchPage(url)) };
}

// Video covers are shown large, card thumbnails small, avatars smaller still.
const WIDTHS = { youtube: 960, bilibili: 960, github: 120 };

// Named by the link as well as the picture: two links sharing one picture
// (two repositories of one owner) keep a file each, so removing a note never
// takes another note's thumbnail with it.
async function storeImage(url, data, kind, date) {
  const webp = await sharp(data).rotate().resize({ width: WIDTHS[kind] || 480, withoutEnlargement: true }).webp({ quality: 80 }).toBuffer();
  const name = `${date.replaceAll("-", "")}-${crypto.createHash("sha256").update(url).update("\0").update(webp).digest("hex").slice(0, 10)}.webp`;
  await fs.mkdir(assetDir, { recursive: true });
  await fs.writeFile(path.join(assetDir, name), webp);
  return `/assets/notes/${name}`;
}

async function main() {
  const all = process.argv.includes("--all");
  const links = standaloneLinks((await fs.readFile(notesPath, "utf8")).replace(/\r\n/g, "\n"));
  const file = JSON.parse(await fs.readFile(previewsPath, "utf8").catch(() => '{"version":1,"links":{}}'));
  const todo = links.filter((url) => all || !(url in file.links));
  if (!todo.length) {
    console.log(`Nothing to fetch: ${links.length} link(s) in notes.md, all with a preview.`);
    return;
  }
  const date = new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
  let stored = 0;
  for (const url of todo) {
    console.log(url);
    try {
      const preview = await fetchPreview(url);
      const entry = { kind: preview.kind, fetched: date };
      for (const [field, limit] of Object.entries(LIMITS)) {
        const value = clean(preview[field], limit);
        if (value) entry[field] = value;
      }
      if (preview.canonical) entry.url = preview.canonical;
      if (Number.isInteger(preview.stars) && preview.stars >= 0) entry.stars = preview.stars;
      if (preview.image) entry.image = await storeImage(url, preview.image, preview.kind, date).catch(() => undefined);
      if (!entry.image) delete entry.image;
      if (!entry.title && !entry.description && !entry.image) throw new Error("the page offers no title, description or image");
      file.links[url] = entry;
      stored += 1;
      console.log(`  ${entry.title || "(no title)"}${entry.image ? " · thumbnail" : ""}`);
    } catch (error) {
      console.warn(`  skipped: ${error.message}`);
    }
  }
  // Same bytes as the bot writes (Python json.dumps with sort_keys), so the
  // two never reorder each other's entries.
  const sortKeys = (value) => (value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys(value[key])]))
    : value);
  await fs.writeFile(previewsPath, `${JSON.stringify(sortKeys({ version: 1, links: file.links }), null, 2)}\n`);
  console.log(`Stored ${stored} of ${todo.length} preview(s) in ${path.relative(root, previewsPath)}.`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === import.meta.filename) await main();
