import { expect, test, type Page } from "@playwright/test";

async function pixels(page: Page) {
  const image = await page.locator("[data-404-canvas]").screenshot();
  const statistics = await page.evaluate(async (base64) => {
    const image = new Image();
    image.src = `data:image/png;base64,${base64}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d")!;
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
    let count = 0;
    let left = canvas.width;
    let right = 0;
    let top = canvas.height;
    let bottom = 0;
    const background = data[(canvas.width * 2 + 2) * 4];
    for (let i = 0; i < data.length; i += 4) {
      const x = (i / 4) % canvas.width;
      const y = Math.floor(i / 4 / canvas.width);
      // Fractional element bounds can include one pixel of the terminal border.
      if (x < 2 || y < 2 || x >= canvas.width - 2 || y >= canvas.height - 2) continue;
      if (Math.abs(data[i] - background) < 40) continue;
      count++;
      left = Math.min(left, x);
      right = Math.max(right, x);
      top = Math.min(top, y);
      bottom = Math.max(bottom, y);
    }
    return {
      coverage: count / (canvas.width * canvas.height),
      left: left / canvas.width,
      right: right / canvas.width,
      top: top / canvas.height,
      bottom: bottom / canvas.height,
    };
  }, image.toString("base64"));
  return { image, statistics };
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
  { width: 390, height: 844 },
  { width: 320, height: 568 },
  { width: 844, height: 390 },
]) {
  test(`404 remains visible and framed at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && /WebGL|shader/i.test(message.text())) errors.push(message.text());
    });
    await page.setViewportSize(viewport);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/404.html");
    await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
    await expect(page.getByRole("button", { name: "继续旋转" })).toHaveAttribute("aria-pressed", "true");
    const { statistics } = await pixels(page);
    expect(statistics.coverage).toBeGreaterThan(0.01);
    expect(statistics.coverage).toBeLessThan(0.3);
    expect(statistics.left).toBeGreaterThan(0.06);
    expect(statistics.right).toBeLessThan(0.94);
    expect(statistics.top).toBeGreaterThan(0.05);
    expect(statistics.bottom).toBeLessThan(0.95);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const scene = await page.locator("[data-404-scene]").boundingBox();
    const terminal = await page.locator("[data-404-terminal]").boundingBox();
    const caption = await page.locator(".not-found-caption").boundingBox();
    expect(terminal!.y + terminal!.height).toBeLessThanOrEqual(caption!.y + 1);
    expect(scene!.x).toBeGreaterThan(terminal!.x);
    expect(scene!.x + scene!.width).toBeLessThan(terminal!.x + terminal!.width);
    await expect(page.locator("[data-404-terminal] [data-404-controls]")).toBeVisible();
    await expect(page.locator("[data-404-status]")).toHaveText("PAUSED");
    for (const selector of [".not-found-terminal-title", ".not-found-terminal-prompt", ".not-found-terminal-status"]) {
      expect(await page.locator(selector).evaluate((element) => parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(13);
    }
    await page.getByRole("button", { name: "继续旋转" }).hover();
    const tooltip = await page.locator("[data-404-pause-label]").boundingBox();
    expect(tooltip!.y + tooltip!.height).toBeLessThan(scene!.y);
    await page.screenshot({ path: testInfo.outputPath("light.png"), fullPage: true });
    await page.getByRole("button", { name: "切换到夜间主题" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const dark = await pixels(page);
    expect(dark.statistics.coverage).toBeGreaterThan(0.01);
    await page.screenshot({ path: testInfo.outputPath("dark.png"), fullPage: true });
    expect(errors).toEqual([]);
  });
}

test("rotation, pause, alignment, hover, and home navigation work", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/404.html");
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  await page.getByRole("button", { name: "暂停旋转" }).click();
  const aligned = await pixels(page);
  await page.waitForTimeout(350);
  expect((await pixels(page)).image.equals(aligned.image)).toBe(true);
  await page.getByRole("button", { name: "继续旋转" }).click();
  await page.waitForTimeout(7000);
  await page.getByRole("button", { name: "暂停旋转" }).click();
  const rotated = await pixels(page);
  expect(rotated.image.equals(aligned.image)).toBe(false);
  expect(rotated.statistics.coverage).toBeGreaterThan(0.01);
  expect(rotated.statistics.left).toBeGreaterThan(0.04);
  expect(rotated.statistics.right).toBeLessThan(0.96);
  await page.screenshot({ path: testInfo.outputPath("rotated.png"), fullPage: true });
  await page.getByRole("button", { name: "重新对齐" }).click();
  await page.waitForTimeout(2000);
  expect((await pixels(page)).image.equals(aligned.image)).toBe(true);
  await page.getByRole("button", { name: "继续旋转" }).click();
  await page.mouse.move(1040, 270);
  await page.waitForTimeout(2600);
  expect((await pixels(page)).image.equals(aligned.image)).toBe(false);
  await page.getByRole("link", { name: "返回首页" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.locator("[data-impossible-404]")).toHaveCount(0);
  await page.goBack();
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
});

test("hover stays exactly front-facing during the initial and recurring alignment hold", async ({ page }, testInfo) => {
  test.setTimeout(40_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/404.html");
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  const front = await pixels(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  const scene = await page.locator("[data-404-scene]").boundingBox();
  await page.mouse.move(scene!.x + scene!.width * 0.9, scene!.y + scene!.height * 0.2);
  await page.waitForTimeout(500);
  await expect(page.locator("[data-404-status]")).toHaveText("ALIGNED");
  expect((await pixels(page)).image.equals(front.image)).toBe(true);
  await expect(page.locator("[data-404-status]")).toHaveText("RUNNING");
  await page.waitForTimeout(1500);
  expect((await pixels(page)).image.equals(front.image)).toBe(false);
  await expect(page.locator("[data-404-status]")).toHaveText("ALIGNED", { timeout: 22_000 });
  expect((await pixels(page)).image.equals(front.image)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("hover-aligned.png"), fullPage: true });
});

test("unknown nested URLs return the custom page with HTTP 404", async ({ page }) => {
  const response = await page.goto("/this-page-does-not-exist/nothing.html");
  expect(response?.status()).toBe(404);
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  await expect(page.getByRole("link", { name: "返回首页" })).toHaveAttribute("href", "/");
});

test("mobile rotation stays framed and changing motion preference realigns it", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/404.html");
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  const front = await pixels(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.waitForTimeout(7500);
  await page.getByRole("button", { name: "暂停旋转" }).click();
  const side = await pixels(page);
  expect(side.image.equals(front.image)).toBe(false);
  expect(side.statistics.coverage).toBeGreaterThan(0.01);
  expect(side.statistics.left).toBeGreaterThan(0.04);
  expect(side.statistics.right).toBeLessThan(0.96);
  expect(side.statistics.top).toBeGreaterThan(0.04);
  expect(side.statistics.bottom).toBeLessThan(0.96);
  await page.screenshot({ path: testInfo.outputPath("mobile-rotated.png"), fullPage: true });
  await page.getByRole("button", { name: "继续旋转" }).click();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(page.getByRole("button", { name: "继续旋转" })).toHaveAttribute("aria-pressed", "true");
  expect((await pixels(page)).image.equals(front.image)).toBe(true);
});

test("no JavaScript retains a readable 404 and working home link", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 320, height: 568 } });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:4322/404.html");
  await expect(page.locator("[data-404-fallback]")).toBeVisible();
  await expect(page.locator("[data-404-controls]")).toBeHidden();
  await page.getByRole("link", { name: "返回首页" }).click();
  await expect(page).toHaveURL("http://127.0.0.1:4322/");
  await context.close();
});

test("WebGL failure retains the fallback", async ({ page }) => {
  await page.addInitScript(() => {
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type: string, ...args: unknown[]) {
      if (type.startsWith("webgl")) return null;
      return Reflect.apply(getContext, this, [type, ...args]);
    } as typeof getContext;
  });
  await page.goto("/404.html");
  await page.waitForLoadState("networkidle");
  await expect(page.locator("[data-404-fallback]")).toBeVisible();
  await expect(page.locator("[data-404-controls]")).toBeHidden();
  await expect(page.getByRole("link", { name: "返回首页" })).toBeVisible();
});

test("the scene initializes only once per page and survives repeated page-load events", async ({ page }) => {
  await page.addInitScript(() => {
    const state = window as Window & { sceneInitializations: number };
    state.sceneInitializations = 0;
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type: string, ...args: unknown[]) {
      if (type === "webgl2") state.sceneInitializations++;
      return Reflect.apply(getContext, this, [type, ...args]);
    } as typeof getContext;
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/404.html");
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  await page.waitForLoadState("load");
  const initializations = () => page.evaluate(() => (window as Window & { sceneInitializations: number }).sceneInitializations);
  expect(await initializations()).toBe(1);
  const front = await pixels(page);
  await page.evaluate(() => document.dispatchEvent(new Event("astro:page-load")));
  expect(await initializations()).toBe(1);
  expect((await pixels(page)).image.equals(front.image)).toBe(true);
  await page.locator(".not-found-header .site-brand").click();
  await expect(page).toHaveURL("/");
  await expect(page.locator("[data-404-canvas]")).toHaveCount(0);
  await page.goBack();
  await expect(page.locator("[data-impossible-404]")).toHaveAttribute("data-ready", "true");
  expect(await initializations()).toBe(2);
  expect((await pixels(page)).statistics.coverage).toBeGreaterThan(0.01);
});
