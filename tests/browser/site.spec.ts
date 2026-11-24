import { expect, test, type Page } from "@playwright/test";

const noHorizontalOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);

for (const width of [320, 375]) {
  test(`home keeps search, tags and filter status usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/index.html");
    const search = page.locator("[data-post-search]");
    await expect(search).toBeVisible();
    const strip = page.locator("[data-tag-strip]");
    await expect(strip).toBeVisible();
    // One scrolling row: every tag is reachable without the expand toggle.
    await expect(page.locator("[data-tag-toggle]")).toBeHidden();
    const lastTag = strip.locator("[data-tag]").last();
    await lastTag.scrollIntoViewIfNeeded();
    await expect(lastTag).toBeVisible();
    expect(await strip.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
    expect(await noHorizontalOverflow(page)).toBe(true);

    const tag = await strip.locator(".tags-list [data-tag]").first().getAttribute("data-tag");
    await strip.locator(`[data-tag="${tag}"]`).click();
    await expect(page).toHaveURL(new RegExp(`tag=${encodeURIComponent(tag!)}`));
    const status = page.locator("[data-filter-status]");
    await expect(status).toBeVisible();
    await expect(status).toContainText(tag!);
    await expect(status).toContainText(/\d+ 篇文章/);
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.screenshot({ path: `test-results/home-filter-mobile-${width}.png`, fullPage: true });

    await page.getByRole("button", { name: "清除筛选" }).click();
    await expect(status).toBeHidden();
    await expect(page).toHaveURL(/\/index\.html$/);
    await expect(page.locator(".dir-year").first()).toBeVisible();
  });
}

test("home groups posts by year, searches flat and clears filters", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/index.html");
  const years = await page.locator(".dir-year").evaluateAll((nodes) => nodes.map((node) => node.firstChild!.textContent!));
  expect(years.length).toBeGreaterThan(0);
  expect(years).toEqual([...years].sort().reverse());
  const total = await page.locator(".dir-item").count();
  await expect(page.locator("[data-filter-status]")).toBeHidden();

  const firstTitle = (await page.locator(".dir-item-title").first().textContent())!.trim();
  await page.locator("[data-post-search]").fill(firstTitle.slice(0, 4));
  await expect(page).toHaveURL(/\?q=/);
  await expect(page.locator("[data-filter-status]")).toContainText("搜索");
  await expect(page.locator(".dir-search-results .dir-item").first()).toBeVisible();
  await expect(page.locator(".dir-year")).toHaveCount(0);

  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.locator("[data-post-search]")).toHaveValue("");
  await expect(page.locator(".dir-item")).toHaveCount(total);
  await expect(page.locator(".dir-year")).toHaveCount(years.length);
});

test("theme toggle sits in the header row and theme-color follows the theme", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/index.html");
  const toggle = page.locator(".site-header [data-theme-toggle]");
  await expect(toggle).toHaveCount(1);
  const header = (await page.locator(".site-header").boundingBox())!;
  const box = (await toggle.boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(header.x + header.width + 1);
  expect(box.y).toBeGreaterThanOrEqual(header.y - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(header.y + header.height + 1);
  const themeColor = page.locator('meta[name="theme-color"]');
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(themeColor).toHaveAttribute("content", "#f3f6f8");
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(themeColor).toHaveAttribute("content", "#10161c");
});

test("without a saved choice the theme follows the system dark preference", async ({ browser }) => {
  const context = await browser.newContext({ colorScheme: "dark", baseURL: "http://127.0.0.1:4322" });
  const page = await context.newPage();
  await page.goto("/about.html");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute("content", "#10161c");
  await page.locator("[data-theme-toggle]").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await context.close();
});

test("post links stand out, images open in a lightbox and back-to-top appears", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/posts/cg-final-path-tracing.html");
  const colors = await page.evaluate(() => {
    const link = document.querySelector<HTMLElement>(".post-body p a:not(.heading-anchor)");
    const text = document.querySelector<HTMLElement>(".post-body p");
    return link && text ? [getComputedStyle(link).color, getComputedStyle(text).color] : undefined;
  });
  if (colors) expect(colors[0]).not.toBe(colors[1]);

  const image = page.locator("img[data-lightbox]").first();
  await image.scrollIntoViewIfNeeded();
  await expect(image).toHaveCSS("cursor", "zoom-in");
  await image.click();
  const dialog = page.locator("dialog.lightbox");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("img")).toHaveAttribute("src", /\/_astro\//);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await image.click();
  await expect(dialog).toBeVisible();
  await dialog.click({ position: { x: 5, y: 5 } });
  await expect(dialog).toBeHidden();

  const backToTop = page.locator("[data-back-to-top]");
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(backToTop).toBeHidden();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(backToTop).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await backToTop.click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect(backToTop).toBeHidden();
  expect(await noHorizontalOverflow(page)).toBe(true);
});

// The test adds an image to the served page, as src/lib/note-images.ts
// renders it, and looks at that one only (notes may hold other images and
// link thumbnails); the lightbox script is the page's own.
test("note images open in the lightbox", async ({ page }) => {
  await page.route("**/notes.html", async (route) => {
    const response = await route.fetch();
    const html = (await response.text()).replace(
      /<div class="note-body[^"]*"[^>]*>/,
      '$&<p><img src="/assets/img/avatar.png" alt="测试配图" loading="lazy" decoding="async" width="1024" height="1024"></p>',
    );
    await route.fulfill({ status: response.status(), contentType: "text/html; charset=utf-8", body: html });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/notes.html");
  const image = page.locator('.note-body img[alt="测试配图"]');
  await expect(image).toHaveCount(1);
  await expect(image).toHaveAttribute("data-lightbox", "");
  await expect(image).toHaveCSS("cursor", "zoom-in");
  await image.click();
  const dialog = page.locator("dialog.lightbox");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("img")).toHaveAttribute("src", /\/assets\/img\/avatar\.png$/);
  await expect(dialog.locator("figcaption")).toHaveText("测试配图");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(image).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeVisible();
  await dialog.click({ position: { x: 5, y: 5 } });
  await expect(dialog).toBeHidden();
  expect(await noHorizontalOverflow(page)).toBe(true);
});

// Link previews, with the markup src/lib/link-cards.ts renders (notes.md need
// not contain a link). The players are stubbed: the test never leaves the machine.
test("link previews load a player only on request", async ({ page }) => {
  const external = 'target="_blank" rel="noopener noreferrer"';
  const cards = [
    `<a class="link-card" href="https://example.com/page" data-link-kind="link" ${external}><span class="link-card-text"><span class="link-card-title">一个网页</span><span class="link-card-desc">描述</span><span class="link-card-site">example.com</span></span><img class="link-card-thumb" src="/assets/img/avatar.png" alt="" loading="lazy" decoding="async" width="1024" height="1024"></a>`,
    `<div class="link-embed link-embed-video" data-link-kind="youtube" data-embed-src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&amp;rel=0" data-embed-title="一个视频"><a class="link-embed-cover" href="https://youtu.be/dQw4w9WgXcQ" aria-label="播放视频：一个视频" ${external}><img class="link-embed-thumb" src="/assets/img/avatar.png" alt="" width="1024" height="1024"><span class="link-embed-play" aria-hidden="true"></span></a><span class="link-embed-caption"><a class="link-embed-title" href="https://youtu.be/dQw4w9WgXcQ" ${external}>一个视频</a><span class="link-embed-site">YouTube</span></span></div>`,
    `<div class="link-embed link-embed-post" data-link-kind="x" data-embed-src="https://platform.twitter.com/embed/Tweet.html?id=20&amp;dnt=true" data-embed-title="jack (@jack)"><a class="link-card" href="https://x.com/jack/status/20" data-link-kind="x" ${external}><span class="link-card-text"><span class="link-card-title">jack (@jack)</span><span class="link-card-site">X</span></span></a><button type="button" class="link-embed-load" hidden>在此展开原帖</button></div>`,
    // A player address this site does not embed must never be loaded.
    `<div class="link-embed link-embed-video" data-link-kind="youtube" data-embed-src="https://evil.example/embed"><a class="link-embed-cover" id="bad-cover" href="#bad" ${external}><span class="link-embed-play" aria-hidden="true"></span></a></div>`,
  ].join("\n");
  await page.route("**/notes.html", async (route) => {
    const response = await route.fetch();
    const html = (await response.text()).replace(/<div class="note-body[^"]*"[^>]*>/, `$&<div id="link-preview-test">${cards}</div>`);
    await route.fulfill({ status: response.status(), contentType: "text/html; charset=utf-8", body: html });
  });
  const requested: string[] = [];
  await page.route(/^https:\/\/(www\.youtube-nocookie\.com|platform\.twitter\.com|evil\.example)\//, async (route) => {
    const url = route.request().url();
    requested.push(url);
    const resize = '<script>parent.postMessage({ "twttr.embed": { method: "twttr.private.resize", params: [{ width: 550, height: 321 }] } }, "*")</script>';
    await route.fulfill({ contentType: "text/html", body: `<!doctype html><title>stub</title>${url.includes("twitter") ? resize : ""}` });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/notes.html");

  // Only the cards added above: real notes may hold previews of their own.
  const area = page.locator("#link-preview-test");
  // Nothing is requested from a provider before the reader asks.
  const card = area.locator('.link-card[data-link-kind="link"]');
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("target", "_blank");
  await expect(card.locator("img")).not.toHaveAttribute("data-lightbox", "");
  expect(requested).toEqual([]);
  await expect(page.locator("iframe")).toHaveCount(0);

  const video = area.locator('.link-embed-video[data-link-kind="youtube"]').first();
  await video.locator(".link-embed-cover").click();
  const player = video.locator("iframe.link-embed-frame");
  await expect(player).toHaveAttribute("src", "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0");
  await expect(player).toHaveAttribute("title", "一个视频");
  await expect(video.locator(".link-embed-cover")).toHaveCount(0);
  await expect(video.locator(".link-embed-title")).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/notes.html");

  const post = area.locator(".link-embed-post");
  const load = post.locator(".link-embed-load");
  await expect(load).toBeVisible();
  await load.click();
  await expect(post).toHaveClass(/is-loaded/);
  await expect(post.locator("iframe")).toHaveAttribute("src", /^https:\/\/platform\.twitter\.com\/embed\/Tweet\.html\?id=20&dnt=true&theme=(light|dark)&lang=zh-cn$/);
  await expect(post.locator("iframe")).toHaveCSS("height", "321px");
  await expect(post.locator(".link-card")).toBeHidden();
  await expect(load).toHaveCount(0);

  await page.locator("#bad-cover").click();
  await expect(area.locator("iframe")).toHaveCount(2);
  expect(requested.some((url) => url.includes("evil.example"))).toBe(false);
  expect(await noHorizontalOverflow(page)).toBe(true);
});

// The side-by-side layout (also what a slider falls back to without
// JavaScript): on phones the images stack and each keeps its own label.
test("comparison labels stay paired with their images on phones", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 375, height: 812 } });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:4322/posts/cg-final-path-tracing.html");
  const items = page.locator(".mdx-compare").first().locator(".mdx-compare-item");
  await expect(items).toHaveCount(2);
  for (const item of await items.all()) {
    await expect(item.locator("figcaption")).toBeVisible();
    const image = (await item.locator("img").boundingBox())!;
    const label = (await item.locator("figcaption").boundingBox())!;
    expect(label.y).toBeGreaterThanOrEqual(image.y + image.height - 1);
  }
  const [first, second] = await items.evaluateAll((figures) => figures.map((figure) => figure.getBoundingClientRect().y));
  expect(second).toBeGreaterThan(first);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await context.close();
});

test("comments stay readable when the Turnstile script is blocked", async ({ page }) => {
  await page.route("**/challenges.cloudflare.com/**", (route) => route.abort("blockedbyclient"));
  await page.route("**/api/config", (route) => route.fulfill({ json: { turnstileSiteKey: "test-site-key" } }));
  await page.route("**/api/views", (route) => route.fulfill({ json: { views: 7, counted: true } }));
  await page.route("**/api/comments?**", (route) => route.fulfill({
    json: { comments: [{ id: 1, author_name: "读者", content: "一条旧评论", created_at: "2026-08-31T08:42:23.933Z" }] },
  }));
  await page.goto("/posts/cg-final-path-tracing.html");
  const comments = page.locator("[data-comments]");
  await expect(comments.locator(".comment-item")).toHaveCount(1);
  await expect(comments.locator(".comment-body")).toHaveText("一条旧评论");
  await expect(comments.locator("[data-comments-status]")).toHaveText("1 条评论");
  await expect(comments.locator(".comments-turnstile-note")).toContainText("暂时无法发表评论");
  await expect(comments.locator("[data-comments-submit]")).toBeDisabled();
  await expect(page.locator("[data-post-view-count]")).toHaveText("7");
});
