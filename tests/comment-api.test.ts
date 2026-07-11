import assert from "node:assert/strict";
import test from "node:test";

const api = await import("../services/comment-api/server.mjs");

test("parses common Chromium user agents", () => {
  const parsed = api.parseUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  );
  assert.equal(parsed.os_name, "Windows");
  assert.equal(parsed.os_version, "10.0");
  assert.equal(parsed.browser_name, "Chrome");
  assert.equal(parsed.browser_version, "126.0");
});

test("validates comment payload shape", () => {
  const parsed = api.validateCommentInput({
    slug: "/posts/cg-final-path-tracing.html",
    authorName: "PNC",
    content: "hello",
    turnstileToken: "token",
  });
  assert.equal(parsed.slug, "/posts/cg-final-path-tracing.html");
  assert.equal(parsed.authorName, "PNC");
});

test("rejects invalid post slugs", () => {
  assert.throws(() => api.validateCommentInput({
    slug: "https://example.com/posts/a.html",
    authorName: "PNC",
    content: "hello",
    turnstileToken: "token",
  }));
});

test("validates article view payloads and uses the configured calendar day", () => {
  assert.deepEqual(api.validateViewInput({ slug: "/posts/arch-linux.html" }), {
    slug: "/posts/arch-linux.html",
  });
  assert.equal(api.getViewDate(new Date("2026-07-10T16:30:00.000Z"), "Asia/Shanghai"), "2026-07-11");
});

test("summarizes Directus view aggregates for known posts", () => {
  const rows = [
    { post_slug: "/posts/a.html", count: "4" },
    { post_slug: "/posts/b.html", count: { "*": 7 } },
    { post_slug: "/posts/unknown.html", count: 100 },
  ];
  const catalog = new Map([
    ["/posts/a.html", "文章 A"],
    ["/posts/b.html", "文章 B"],
  ]);
  const stats = api.summarizeViewGroups(rows, catalog);

  assert.equal(stats.totalViews, 11);
  assert.deepEqual(stats.mostViewed, {
    slug: "/posts/b.html",
    title: "文章 B",
    views: 7,
  });
  assert.deepEqual(api.buildViewCounts(rows, catalog), {
    "/posts/a.html": 4,
    "/posts/b.html": 7,
  });
});
