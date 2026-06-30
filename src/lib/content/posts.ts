import { getCollection, type CollectionEntry } from "astro:content";
import { getImage } from "astro:assets";
import crypto from "node:crypto";
import { getReadingStats, type ReadingStats } from "../reading";
import { formatDate, normalizeTags, site } from "../site";
import type { PostIndexDocument, PostSearchDocument } from "../search";
import { getPostFeatures, type PostFeatures } from "./features";

export interface PostSummary {
  id: string;
  title: string;
  date: string;
  updated?: string;
  tags: string[];
  excerpt: string;
  cover: string;
  coverAlt: string;
  canonical?: string;
  url: string;
  slug: string;
  content: string;
  reading: ReadingStats;
  features: PostFeatures;
  entry: CollectionEntry<"posts">;
}

function stableId(value: string) {
  return crypto.createHash("sha256").update(value.toLowerCase()).digest("hex").slice(0, 32);
}

async function normalizeCover(cover?: CollectionEntry<"posts">["data"]["cover"]) {
  if (!cover) return site.defaultCover;
  const image = await getImage({ src: cover, format: "webp" });
  return image.src;
}

function inferSlug(entry: CollectionEntry<"posts">) {
  return entry.id.replace(/\/index\.(md|mdx)$/, "").replace(/\.(md|mdx)$/, "");
}

export async function getAllPosts() {
  const entries = await getCollection("posts");
  const posts = await Promise.all(entries
    .filter((entry) => !entry.data.draft)
    .map(async (entry): Promise<PostSummary> => {
      const date = formatDate(entry.data.date);
      const updated = entry.data.updated ? formatDate(entry.data.updated) : undefined;
      const slug = inferSlug(entry);
      const url = `posts/${slug}.html`;
      const tags = normalizeTags(entry.data.tags);
      return {
        id: stableId(url),
        title: entry.data.title,
        date,
        updated,
        tags,
        excerpt: entry.data.excerpt || entry.body.slice(0, 180).replace(/\s+/g, " "),
        cover: await normalizeCover(entry.data.cover),
        coverAlt: entry.data.coverAlt || site.defaultCoverAlt,
        canonical: entry.data.canonical,
        url,
        slug,
        content: entry.body,
        reading: getReadingStats(entry.body),
        features: getPostFeatures(entry.body),
        entry,
      };
    }));
  return posts.sort((a, b) => b.date.localeCompare(a.date));
}

export function toPostIndexDocument(post: PostSummary): PostIndexDocument {
  return {
    id: post.id,
    title: post.title,
    date: post.date,
    tags: post.tags,
    excerpt: post.excerpt,
    cover: post.cover.replace(/^\//, ""),
    coverAlt: post.coverAlt,
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
