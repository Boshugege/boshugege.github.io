import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createMarkdownProcessor } from "@astrojs/markdown-remark";
import { linkKey, parseLinkPreviews, rehypeLinkCards, type LinkPreviews } from "../src/lib/link-cards.ts";
import { classifyLink, embedSrc, parseStartTime } from "../src/lib/link-providers.ts";

function previewsOf(links: Record<string, Record<string, unknown>>): LinkPreviews {
  return parseLinkPreviews(JSON.stringify({ version: 1, links }));
}

async function render(source: string, previews: LinkPreviews = new Map()) {
  const markdown = await createMarkdownProcessor({ syntaxHighlight: false, rehypePlugins: [[rehypeLinkCards, { previews }]] });
  return (await markdown.render(source)).code;
}

test("classifyLink recognises the providers from the URL alone", () => {
  const youtube = { kind: "youtube", ids: { id: "dQw4w9WgXcQ" } };
  for (const url of [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&t=43s",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
  ]) assert.deepEqual(classifyLink(url), youtube, url);
  assert.deepEqual(classifyLink("https://www.bilibili.com/video/BV1GJ411x7h7/?p=2"), { kind: "bilibili", ids: { bvid: "BV1GJ411x7h7" } });
  assert.deepEqual(classifyLink("https://m.bilibili.com/video/av170001"), { kind: "bilibili", ids: { aid: "170001" } });
  assert.deepEqual(classifyLink("https://b23.tv/AbCd123"), { kind: "bilibili", ids: { short: "AbCd123" } });
  assert.deepEqual(classifyLink("https://x.com/jack/status/20"), { kind: "x", ids: { user: "jack", id: "20" } });
  assert.deepEqual(classifyLink("https://twitter.com/jack/status/20?s=20"), { kind: "x", ids: { user: "jack", id: "20" } });
  assert.deepEqual(classifyLink("https://mobile.twitter.com/jack/status/20/photo/1"), { kind: "x", ids: { user: "jack", id: "20" } });
  assert.deepEqual(classifyLink("https://github.com/withastro/astro"), { kind: "github", ids: { owner: "withastro", repo: "astro" } });
  assert.deepEqual(classifyLink("https://github.com/withastro/astro.git/"), { kind: "github", ids: { owner: "withastro", repo: "astro" } });
});

test("classifyLink treats everything else as a plain link", () => {
  for (const url of [
    "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/@channel",
    "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
    "https://www.bilibili.com/read/cv123",
    "https://b23.tv/a/b",
    "https://x.com/jack",
    "https://x.com/jack/status/abc",
    "https://github.com/withastro",
    "https://github.com/withastro/astro/issues/1",
    "https://github.com/topics/astro",
    "https://gist.github.com/a/b",
    "https://example.com/",
    "not a url",
  ]) assert.deepEqual(classifyLink(url), { kind: "link", ids: {} }, url);
});

test("embedSrc builds the provider's player address", () => {
  const src = (url: string) => embedSrc(classifyLink(url), url);
  assert.equal(src("https://youtu.be/dQw4w9WgXcQ"), "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0");
  assert.equal(src("https://youtu.be/dQw4w9WgXcQ?t=1m30s"), "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0&start=90");
  assert.equal(src("https://www.bilibili.com/video/BV1GJ411x7h7?p=3&t=12"), "https://player.bilibili.com/player.html?bvid=BV1GJ411x7h7&p=3&autoplay=1&t=12");
  assert.equal(src("https://www.bilibili.com/video/av170001"), "https://player.bilibili.com/player.html?aid=170001&p=1&autoplay=1");
  assert.equal(src("https://x.com/jack/status/20"), "https://platform.twitter.com/embed/Tweet.html?id=20&dnt=true");
  assert.equal(src("https://b23.tv/AbCd123"), undefined);
  assert.equal(src("https://github.com/withastro/astro"), undefined);
  assert.equal(src("https://example.com/"), undefined);
  assert.deepEqual(["90", "90s", "1m30s", "1h2m3s", "", "abc", "1x", null].map(parseStartTime), [90, 90, 90, 3723, 0, 0, 0, 0]);
});

test("a bare URL on its own line becomes a card; other links stay as written", async () => {
  const html = await render([
    "看看这个",
    "https://example.com/some/page",
    "后面的话",
    "",
    "句中 https://example.com/inline 不变，[文字](https://example.com/text) 也不变。",
    "",
    "- https://example.com/in-list",
    "",
    "> https://example.com/in-quote",
    "",
    "`https://example.com/code`",
    "",
    "http://example.com/plain-http",
  ].join("\n"));
  const cards = [...html.matchAll(/<a class="link-card"[^>]*>/g)].map((match) => match[0]);
  assert.equal(cards.length, 1);
  assert.match(cards[0], /href="https:\/\/example\.com\/some\/page"/);
  assert.match(cards[0], /data-link-kind="link"/);
  assert.match(cards[0], /target="_blank" rel="noopener noreferrer"/);
  // The text around the card stays in paragraphs of its own, in order.
  assert.match(html, /<p>看看这个<\/p>\s*<a class="link-card"[\s\S]*<\/a>\s*<p>后面的话<\/p>/);
  assert.match(html, /<span class="link-card-title">example\.com\/some\/page<\/span>/);
  assert.match(html, /<span class="link-card-site">example\.com<\/span>/);
  for (const untouched of ["inline", "text", "in-list", "in-quote", "plain-http"]) {
    assert.match(html, new RegExp(`<a href="https?://example\\.com/${untouched}">`), untouched);
  }
});

test("line breaks and several links in one paragraph", async () => {
  const html = await render("第一行  \nhttps://example.com/a\nhttps://example.com/b\n第二行  \n第三行");
  assert.equal([...html.matchAll(/class="link-card"/g)].length, 2);
  assert.match(html, /^<p>第一行<\/p>\s*<a class="link-card"/);
  // The hard break between the remaining lines survives.
  assert.match(html, /<p>第二行<br>\s*第三行<\/p>$/);
  assert.equal(await render("<https://example.com/a>"), await render("https://example.com/a"));
  assert.doesNotMatch(await render("https://example.com/a"), /<p>/);
});

test("a preview supplies the title, description, site and thumbnail", async () => {
  const previews = previewsOf({
    "https://example.com/中文/page?a=1&b=2": {
      kind: "link",
      title: "标题 <b>&",
      description: "描述\n第二行",
      site: "Example",
      image: "/assets/notes/20261124-0123456789.webp",
      fetched: "2026-11-24",
    },
  });
  const html = await render("https://example.com/中文/page?a=1&b=2", previews);
  assert.match(html, /<span class="link-card-title">标题 &#x3C;b>&#x26;<\/span>/);
  assert.match(html, /<span class="link-card-desc">描述 第二行<\/span>/);
  assert.match(html, /<span class="link-card-site">Example · example\.com<\/span>/);
  assert.match(html, /<img class="link-card-thumb" src="\/assets\/notes\/20261124-0123456789\.webp" alt="">/);
});

test("videos become a cover that names the player to load", async () => {
  const previews = previewsOf({
    "https://youtu.be/dQw4w9WgXcQ?t=43": { title: "A video", author: "A channel", image: "/assets/notes/20261124-0123456789.webp" },
    "https://b23.tv/AbCd123": { title: "B 站视频", author: "UP 主", url: "https://www.bilibili.com/video/BV1GJ411x7h7?p=2" },
  });
  const youtube = await render("https://youtu.be/dQw4w9WgXcQ?t=43", previews);
  assert.match(youtube, /<div class="link-embed link-embed-video" data-link-kind="youtube" data-embed-src="https:\/\/www\.youtube-nocookie\.com\/embed\/dQw4w9WgXcQ\?autoplay=1&#x26;rel=0&#x26;start=43" data-embed-title="A video">/);
  assert.match(youtube, /<a class="link-embed-cover" href="https:\/\/youtu\.be\/dQw4w9WgXcQ\?t=43" aria-label="播放视频：A video"/);
  assert.match(youtube, /<img class="link-embed-thumb" src="\/assets\/notes\/20261124-0123456789\.webp" alt="">/);
  assert.match(youtube, /<span class="link-embed-site">YouTube · A channel<\/span>/);

  // A short link plays once the bot has recorded where it leads…
  const resolved = await render("https://b23.tv/AbCd123", previews);
  assert.match(resolved, /data-link-kind="bilibili" data-embed-src="https:\/\/player\.bilibili\.com\/player\.html\?bvid=BV1GJ411x7h7&#x26;p=2&#x26;autoplay=1"/);
  // …and is a plain cover link until then.
  const unresolved = await render("https://b23.tv/AbCd123");
  assert.match(unresolved, /<div class="link-embed link-embed-video" data-link-kind="bilibili">/);
  assert.doesNotMatch(unresolved, /data-embed-src/);
  assert.match(unresolved, /哔哩哔哩 视频/);
});

test("X posts and GitHub repositories", async () => {
  const previews = previewsOf({
    "https://x.com/jack/status/20": { title: "jack (@jack)", description: "just setting up my twttr" },
    "https://github.com/withastro/astro": { title: "withastro/astro", description: "The web framework", stars: 54321, language: "TypeScript", image: "/assets/notes/20261124-0123456789.webp" },
  });
  const post = await render("https://x.com/jack/status/20", previews);
  assert.match(post, /<div class="link-embed link-embed-post" data-link-kind="x" data-embed-src="https:\/\/platform\.twitter\.com\/embed\/Tweet\.html\?id=20&#x26;dnt=true"/);
  assert.match(post, /<span class="link-card-desc">just setting up my twttr<\/span>/);
  assert.match(post, /<button type="button" class="link-embed-load" hidden>在此展开原帖<\/button>/);
  assert.match(await render("https://x.com/jack/status/20"), /<span class="link-card-title">@jack 的帖子<\/span>/);

  const repo = await render("https://github.com/withastro/astro", previews);
  assert.match(repo, /<a class="link-card" href="https:\/\/github\.com\/withastro\/astro" data-link-kind="github"/);
  assert.match(repo, /<span class="link-card-site">GitHub · ★ 54\.3k · TypeScript<\/span>/);
  assert.match(repo, /<img class="link-card-avatar"/);
  const bare = await render("https://github.com/withastro/astro");
  assert.match(bare, /<span class="link-card-title">withastro\/astro<\/span>/);
  assert.match(bare, /<span class="link-card-site">GitHub<\/span>/);
});

test("a thumbnail whose file is missing is left out when the static dir is known", async () => {
  const staticDir = await fs.mkdtemp(path.join(os.tmpdir(), "pnc-link-cards-"));
  try {
    await fs.mkdir(path.join(staticDir, "assets/notes"), { recursive: true });
    await fs.writeFile(path.join(staticDir, "assets/notes/20261124-0123456789.webp"), "x");
    const previews = previewsOf({
      "https://example.com/kept": { title: "有图", image: "/assets/notes/20261124-0123456789.webp" },
      "https://example.com/gone": { title: "没图", image: "/assets/notes/20261124-aaaaaaaaaa.webp" },
    });
    const markdown = await createMarkdownProcessor({ syntaxHighlight: false, rehypePlugins: [[rehypeLinkCards, { previews, staticDir }]] });
    const html = (await markdown.render("https://example.com/kept\n\nhttps://example.com/gone")).code;
    assert.deepEqual([...html.matchAll(/<img[^>]*src="([^"]*)"/g)].map((match) => match[1]), ["/assets/notes/20261124-0123456789.webp"]);
    assert.match(html, /没图/);
  } finally {
    await fs.rm(staticDir, { recursive: true, force: true });
  }
});

test("the previews can be a function, read again on every render", async () => {
  let current = previewsOf({});
  const markdown = await createMarkdownProcessor({ syntaxHighlight: false, rehypePlugins: [[rehypeLinkCards, { previews: () => current }]] });
  assert.doesNotMatch((await markdown.render("https://example.com/a")).code, /新标题/);
  current = previewsOf({ "https://example.com/a": { title: "新标题" } });
  assert.match((await markdown.render("https://example.com/a")).code, /新标题/);
});

test("parseLinkPreviews rejects files the site should not render", () => {
  assert.equal(previewsOf({}).size, 0);
  assert.throws(() => parseLinkPreviews("{"), SyntaxError);
  assert.throws(() => parseLinkPreviews(JSON.stringify({ version: 2, links: {} })), /version/);
  assert.throws(() => parseLinkPreviews(JSON.stringify({ version: 1, links: [] })), /links/);
  assert.throws(() => previewsOf({ "http://example.com/": {} }), /bad entry/);
  assert.throws(() => previewsOf({ "https://example.com/": { image: "https://tracker.example/pixel.png" } }), /image must be/);
  assert.throws(() => previewsOf({ "https://example.com/": { image: "/assets/notes/../x.webp" } }), /image must be/);
  // Unknown or mistyped fields are dropped, not rendered.
  const [preview] = [...previewsOf({ "https://example.com/": { title: 3, stars: -1, description: "  ", extra: "x" } }).values()];
  assert.deepEqual(Object.values(preview).filter((value) => value !== undefined), []);
  assert.equal(linkKey(" https://example.com/中文 "), "https://example.com/%E4%B8%AD%E6%96%87");
  assert.equal(linkKey("ftp://example.com/"), undefined);
});
