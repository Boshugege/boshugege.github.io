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
