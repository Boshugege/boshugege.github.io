import { getCollection, type CollectionEntry } from "astro:content";
import crypto from "node:crypto";
import { getReadingStats, type ReadingStats } from "../reading";
import { formatDate, normalizeTags, site } from "../site";

export interface PostFeatures {
  codeBlocks: number;
  images: number;
  hasMath: boolean;
}

export interface PostSummary {
  id: string;
  title: string;
  date: string;
  tags: string[];
  excerpt: string;
  cover: string;
  url: string;
  slug: string;
  content: string;
  reading: ReadingStats;
  features: PostFeatures;
  entry: CollectionEntry<"posts">;
}

export interface PostIndexDocument {
  id: string;
  title: string;
  date: string;
  tags: string[];
  excerpt: string;
  cover: string;
  url: string;
  slug: string;
  wordCount: number;
  readingMinutes: number;
}

export interface PostSearchDocument {
  id: string;
  title: string;
  date: string;
  tags: string[];
  excerpt: string;
  url: string;
  content: string;
}

function stableId(value: string) {
  return crypto.createHash("sha256").update(value.toLowerCase()).digest("hex").slice(0, 32);
}

function normalizeCover(cover?: string) {
  const value = (cover || "").trim();
  if (!value) return site.defaultCover;
  if (/^https?:\/\//i.test(value)) return value;
  return `/${value.replace(/^\//, "")}`;
}

function inferSlug(entry: CollectionEntry<"posts">) {
  return entry.id.replace(/\.(md|mdx)$/, "");
}

export function getPostFeatures(content: string): PostFeatures {
  const prose = content.replace(/^```[^\n]*\n[\s\S]*?^```\s*$/gm, "");
  return {
    codeBlocks: Math.floor((content.match(/^```/gm)?.length || 0) / 2),
    images: content.match(/!\[[^\]]*\]\([^)]+\)/g)?.length || 0,
    hasMath: /\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$/m.test(prose),
  };
}

export async function getAllPosts() {
  const entries = await getCollection("posts");
  return entries
    .map((entry): PostSummary => {
      const date = formatDate(entry.data.date);
      const slug = inferSlug(entry);
      const url = `posts/${slug}.html`;
      const tags = normalizeTags(entry.data.tags);
      return {
        id: stableId(url),
        title: entry.data.title,
        date,
        tags,
        excerpt: entry.data.excerpt || entry.body.slice(0, 180).replace(/\s+/g, " "),
        cover: normalizeCover(entry.data.cover),
        url,
        slug,
        content: entry.body,
        reading: getReadingStats(entry.body),
        features: getPostFeatures(entry.body),
        entry,
      };
    })
    .sort((a, b) => b.date.localeCompare(a.date));
}

export function toPostIndexDocument(post: PostSummary): PostIndexDocument {
  return {
    id: post.id,
    title: post.title,
    date: post.date,
    tags: post.tags,
    excerpt: post.excerpt,
    cover: post.cover.replace(/^\//, ""),
    url: post.url,
    slug: post.slug,
    wordCount: post.reading.wordCount,
    readingMinutes: post.reading.readingMinutes,
  };
}

export function toPostSearchDocument(post: PostSummary): PostSearchDocument {
  return {
    id: post.id,
    title: post.title,
    date: post.date,
    tags: post.tags,
    excerpt: post.excerpt,
    url: post.url,
    content: post.content || "",
  };
}
