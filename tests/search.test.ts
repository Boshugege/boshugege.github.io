import assert from "node:assert/strict";
import test from "node:test";
import { filterPosts } from "../src/lib/search.ts";

const posts = [
  { id: "a", title: "Astro 指南", date: "2026-01-01", tags: ["技术"], excerpt: "静态博客" },
  { id: "b", title: "年度总结", date: "2026-02-01", tags: ["生活"], excerpt: "关于 Astro 的使用" },
  { id: "c", title: "旧文章", date: "2025-01-01", tags: ["技术"], excerpt: "其他内容" },
];

test("title matches rank ahead of excerpt matches", () => {
  assert.deepEqual(filterPosts(posts, "", "astro").map((post) => post.id), ["a", "b"]);
});

test("tag filters work without a search query", () => {
  assert.deepEqual(filterPosts(posts, "技术", "").map((post) => post.id), ["a", "c"]);
});

test("full text adds otherwise missing matches", () => {
  assert.deepEqual(filterPosts(posts, "", "深层词", new Map([["c", "正文中的深层词"]])).map((post) => post.id), ["c"]);
});
