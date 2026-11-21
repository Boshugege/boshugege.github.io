import assert from "node:assert/strict";
import test from "node:test";
import { filterPosts, groupPostsByYear, normalizeSearchText, stripSearchPunctuation } from "../src/lib/search.ts";

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

test("groups posts by year, newest year first, keeping order inside a year", () => {
  const groups = groupPostsByYear([
    { id: "x", date: "2026-04-08" },
    { id: "y", date: "2023-07-01" },
    { id: "z", date: "2026-01-20" },
    { id: "w", date: "2022-05-10" },
  ]);
  assert.deepEqual(groups.map((group) => [group.year, group.posts.map((post) => post.id)]), [
    ["2026", ["x", "z"]],
    ["2023", ["y"]],
    ["2022", ["w"]],
  ]);
  assert.deepEqual(groupPostsByYear([]), []);
});

test("full-text matching ignores CJK punctuation on both sides", () => {
  const index = new Map([["c", normalizeSearchText("今天 天气很好 我们去 Astro 公园")]]);
  assert.deepEqual(filterPosts(posts, "", "天气很好，我们去", index).map((post) => post.id), ["c"]);
  assert.deepEqual(filterPosts(posts, "", "Astro 公园", index).map((post) => post.id), ["c"]);
  assert.equal(stripSearchPunctuation("你好，世界。“引号”……完"), "你好 世界 引号 完");
  assert.deepEqual(filterPosts(posts, "", "，。", index).map((post) => post.id), []);
});
