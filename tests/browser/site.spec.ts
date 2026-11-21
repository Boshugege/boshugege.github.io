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

test("comparison labels stay paired with their images on phones", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/posts/cg-final-path-tracing.html");
  const items = page.locator(".mdx-compare").first().locator(".mdx-compare-item");
  await expect(items).toHaveCount(2);
  for (const item of await items.all()) {
    await expect(item.locator("figcaption")).toBeVisible();
    const image = (await item.locator("img").boundingBox())!;
    const label = (await item.locator("figcaption").boundingBox())!;
    expect(label.y).toBeGreaterThanOrEqual(image.y + image.height - 1);
  }
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
