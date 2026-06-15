import { getCollection, type CollectionEntry } from "astro:content";
import crypto from "node:crypto";
import { getReadingStats, type ReadingStats } from "./reading";
import { formatDate, normalizeTags, site } from "./site";

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
  entry: CollectionEntry<"posts">;
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

export async function getAllPosts() {
  const entries = await getCollection("posts");
  return entries
    .map((entry): PostSummary => {
      const date = formatDate(entry.data.date);
      const slug = inferSlug(entry);
      const url = `posts/${slug}.html`;
      const tags = normalizeTags(entry.data.tags);
      const reading = getReadingStats(entry.body);
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
        reading,
        entry,
      };
    })
    .sort((a, b) => b.date.localeCompare(a.date));
}

export async function getPostBySlug(slug: string) {
  return (await getAllPosts()).find((post) => post.slug === slug);
}

export function topTags(posts: PostSummary[], limit = 5) {
  const counts = new Map<string, number>();
  for (const post of posts) {
    for (const tag of post.tags) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, limit);
}
