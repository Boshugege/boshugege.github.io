import assert from "node:assert/strict";
import test from "node:test";
import { removeElements, toFeedHtml } from "../src/lib/feed.ts";

const page = "https://parityncsvt.top/posts/demo.html";

test("feed HTML uses absolute URLs for links, images and srcset", () => {
  const html = '<p><a href="/posts/other.html">旧文</a> <a href="#install">跳转</a> <a href="mailto:a@b.c">信</a> <a href="https://example.com/x">外链</a></p>'
    + '<img src="/_astro/a.webp" srcset="/_astro/a-400.webp 400w, /_astro/a-800.webp 800w" alt="图">'
    + '<video poster="./poster.jpg"></video>';
  const feed = toFeedHtml(html, page);
  assert.match(feed, /href="https:\/\/parityncsvt\.top\/posts\/other\.html"/);
  assert.match(feed, /href="https:\/\/parityncsvt\.top\/posts\/demo\.html#install"/);
  assert.match(feed, /href="mailto:a@b\.c"/);
  assert.match(feed, /href="https:\/\/example\.com\/x"/);
  assert.match(feed, /src="https:\/\/parityncsvt\.top\/_astro\/a\.webp"/);
  assert.match(feed, /srcset="https:\/\/parityncsvt\.top\/_astro\/a-400\.webp 400w, https:\/\/parityncsvt\.top\/_astro\/a-800\.webp 800w"/);
  assert.match(feed, /poster="https:\/\/parityncsvt\.top\/posts\/poster\.jpg"/);
});

test("feed HTML drops scripts, styles, Astro attributes and theme-only code colors", () => {
  const html = '<style>.x{color:red}</style><aside class="mdx-note" data-astro-cid-abc123><p data-astro-cid-abc123>提示</p></aside>'
    + '<script>alert(1)</script><pre class="astro-code"><code><span class="line"><span style="--shiki-light:#D73A49;--shiki-dark:#F97583">const</span></span></code></pre>';
  const feed = toFeedHtml(html, page);
  assert.equal(feed, '<aside class="mdx-note"><p>提示</p></aside><pre class="astro-code"><code><span class="line">const</span></code></pre>');
});

test("feed HTML keeps KaTeX MathML and drops the CSS-dependent HTML rendering", () => {
  const html = '<p>公式 <span class="katex"><span class="katex-mathml"><math><mi>x</mi></math></span><span class="katex-html" aria-hidden="true"><span class="base"><span class="mord">x</span></span></span></span> 结束</p>';
  assert.equal(
    toFeedHtml(html, page),
    '<p>公式 <span class="katex"><span class="katex-mathml"><math><mi>x</mi></math></span></span> 结束</p>',
  );
});

test("removeElements handles nesting and repeated matches", () => {
  const html = "a<span class=\"drop\"><span>1</span><span><span>2</span></span></span>b<span class=\"drop\">3</span>c<span>keep</span>";
  assert.equal(removeElements(html, /<span class="drop">/, "span"), "abc<span>keep</span>");
});
