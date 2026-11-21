import { expect, test, type Locator, type Page } from "@playwright/test";

const POST = "/posts/cg-final-path-tracing.html";
const split = (slider: Locator) => slider.evaluate((element) => parseFloat(getComputedStyle(element).getPropertyValue("--split")));
const noHorizontalOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);

test("the path-tracing post has six aligned sliders that drag, jump and never open the lightbox", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(POST);
  const sliders = page.locator("[data-compare-slider]");
  await expect(sliders).toHaveCount(6);
  for (const slider of await sliders.all()) await expect(slider).toHaveClass(/is-ready/);

  const slider = sliders.first();
  await slider.scrollIntoViewIfNeeded();
  const stage = (await slider.locator("[data-compare-stage]").boundingBox())!;
  const handle = slider.locator("[data-compare-handle]");
  await expect(handle).toBeVisible();
  await expect(handle).toHaveAttribute("role", "slider");
  await expect(handle).toHaveAttribute("aria-valuenow", "50");

  // Both images occupy exactly the same box, so the comparison is pixel aligned.
  const boxes = await slider.locator("img").evaluateAll((images) => images.map((image) => {
    const box = image.getBoundingClientRect();
    return [box.x, box.y, box.width, box.height].map(Math.round);
  }));
  expect(boxes[0]).toEqual(boxes[1]);
  // Shown at the images' own width (420 px, never upscaled or squeezed by
  // the frame's border).
  expect(boxes[0][2]).toBe(420);
  // The 520 px / 1040 px pair is shown at the smaller width.
  expect(Math.round((await sliders.nth(4).locator("img").first().boundingBox())!.width)).toBe(520);

  // Drag the handle to a quarter.
  const knob = (await handle.boundingBox())!;
  await page.mouse.move(knob.x + knob.width / 2, knob.y + knob.height / 2);
  await page.mouse.down();
  await page.mouse.move(stage.x + stage.width * 0.25, knob.y + knob.height / 2, { steps: 8 });
  await page.mouse.up();
  expect(Math.abs(await split(slider) - 25)).toBeLessThan(1.5);
  await expect(handle).toHaveAttribute("aria-valuenow", /^2[4-6]$/);

  // Click anywhere on the image to jump there.
  await page.mouse.click(stage.x + stage.width * 0.8, stage.y + stage.height * 0.6);
  await expect.poll(() => split(slider)).toBeGreaterThan(78);
  await expect(page.locator("dialog.lightbox[open]")).toHaveCount(0);
  await expect(slider.locator("img[data-lightbox]")).toHaveCount(0);
  await page.screenshot({ path: "test-results/compare-slider-light.png" });
});

test("the slider handle works from the keyboard and fades the covered label", async ({ page }) => {
  await page.goto(POST);
  const slider = page.locator("[data-compare-slider]").first();
  const handle = slider.locator("[data-compare-handle]");
  const [before, after] = await slider.locator(".mdx-compare-label").all();
  await handle.focus();
  await page.keyboard.press("Home");
  expect(await split(slider)).toBe(0);
  await expect(before).toHaveClass(/is-faded/);
  await expect(after).not.toHaveClass(/is-faded/);
  await page.keyboard.press("ArrowRight");
  expect(await split(slider)).toBe(2);
  await page.keyboard.press("Shift+ArrowRight");
  expect(await split(slider)).toBe(12);
  await page.keyboard.press("End");
  expect(await split(slider)).toBe(100);
  await expect(after).toHaveClass(/is-faded/);
  await page.keyboard.press("ArrowLeft");
  expect(await split(slider)).toBe(98);
  await expect(handle).toHaveAttribute("aria-valuetext", /98%/);
  // Keyboard focus shows a white ring that stays visible on any render.
  expect(await handle.evaluate((element) => getComputedStyle(element).outlineColor)).toBe("rgb(255, 255, 255)");
});

test("a one-finger sideways swipe drags the divider, a vertical swipe scrolls instead", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true, baseURL: "http://127.0.0.1:4322" });
  const page = await context.newPage();
  await page.goto(POST);
  const slider = page.locator("[data-compare-slider]").first();
  await slider.scrollIntoViewIfNeeded();
  await expect(slider).toHaveClass(/is-ready/);
  const stage = (await slider.locator("[data-compare-stage]").boundingBox())!;
  // Real touch input (not synthetic pointer events), so the browser's
  // implicit pointer capture and touch-action apply as on a phone.
  const touch = await context.newCDPSession(page);
  const swipe = async (points: { x: number; y: number }[]) => {
    await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [points[0]] });
    for (const point of points.slice(1)) await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [point] });
    await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  };
  const y = stage.y + stage.height * 0.6;
  const x0 = stage.x + stage.width * 0.5;
  await swipe(Array.from({ length: 12 }, (_, step) => ({ x: x0 - step * stage.width * 0.025, y })));
  await expect.poll(() => split(slider)).toBeLessThan(25);
  expect(await split(slider)).toBeGreaterThan(18);
  const after = await split(slider);
  await swipe(Array.from({ length: 12 }, (_, step) => ({ x: x0, y: y - step * 18 })));
  await page.waitForTimeout(300);
  expect(await split(slider)).toBe(after);
  await context.close();
});

test("at 375 px a tap jumps, nothing overflows and the dark theme keeps the slider crisp", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 375, height: 812 }, hasTouch: true, baseURL: "http://127.0.0.1:4322" });
  const page = await context.newPage();
  await page.goto(POST);
  const slider = page.locator("[data-compare-slider]").nth(3);
  await slider.scrollIntoViewIfNeeded();
  await expect(slider).toHaveClass(/is-ready/);
  const stage = (await slider.locator("[data-compare-stage]").boundingBox())!;
  expect(stage.x).toBeGreaterThanOrEqual(0);
  expect(stage.x + stage.width).toBeLessThanOrEqual(375);
  expect(await noHorizontalOverflow(page)).toBe(true);
  await page.touchscreen.tap(stage.x + stage.width * 0.3, stage.y + stage.height / 2);
  await expect.poll(() => split(slider)).toBeLessThan(32);
  const frame = () => slider.locator("[data-compare-stage]").evaluate((element) => getComputedStyle(element).borderTopColor);
  const lightFrame = await frame();
  await page.locator("[data-theme-toggle]").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  // The frame follows the theme; the divider is light with a dark halo in
  // both, so it reads on white and on black parts of a render.
  expect(await frame()).not.toBe(lightFrame);
  const divider = await slider.locator(".mdx-compare-divider").evaluate((element) => [getComputedStyle(element).backgroundColor, getComputedStyle(element).boxShadow]);
  expect(divider[0]).toMatch(/rgba?\(240, 248, 255/);
  expect(divider[1]).toMatch(/rgba\(10, 16, 22/);
  await slider.screenshot({ path: "test-results/compare-slider-mobile-dark.png" });
  await context.close();
});

test("reduced motion jumps without animating, and sliders come back after a page swap", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(POST);
  const slider = page.locator("[data-compare-slider]").first();
  await slider.scrollIntoViewIfNeeded();
  const stage = (await slider.locator("[data-compare-stage]").boundingBox())!;
  await page.mouse.click(stage.x + stage.width * 0.7, stage.y + 20);
  await expect(slider).not.toHaveClass(/is-animating/);
  expect(Math.abs(await split(slider) - 70)).toBeLessThan(1.5);
  await page.locator(".post-topbar .site-brand").click();
  await expect(page).toHaveURL(/index\.html/);
  await page.goBack();
  await expect(page.locator("[data-compare-slider]").first()).toHaveClass(/is-ready/);
  await expect(page.locator("[data-compare-slider].is-ready")).toHaveCount(6);
});

test("without JavaScript the comparisons fall back to labelled side-by-side images", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:4322${POST}`);
  const slider = page.locator("[data-compare-slider]").first();
  await expect(slider).not.toHaveClass(/is-ready/);
  await expect(slider.locator("[data-compare-handle]")).toBeHidden();
  const [a, b] = await slider.locator("img").evaluateAll((images) => images.map((image) => image.getBoundingClientRect().x));
  expect(b).toBeGreaterThan(a);
  await expect(slider.locator(".mdx-compare-label").first()).toBeVisible();
  await context.close();
});
