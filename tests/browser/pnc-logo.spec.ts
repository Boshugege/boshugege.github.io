import { expect, test } from "@playwright/test";

test("PNC faces, hover displacement, parity, themes and route reinitialization", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/index.html");
  const logo = page.locator("[data-pnc-logo]").first();
  await expect(logo.locator("[data-pnc-face]")).toHaveCount(4);
  await expect(logo).not.toHaveClass(/pnc-scanning/);
  await logo.hover({ position: { x: 27, y: 6 } });
  await expect(logo).toHaveClass(/pnc-hover/);
  await page.waitForTimeout(500);
  const top = await logo.locator('[data-pnc-face="top"]').evaluate((el) => getComputedStyle(el).transform);
  expect(top).toContain("-0.135"); // 1.8 rendered pixels / (32px / 2.4 SVG units)
  const url = page.url();
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  expect(page.url()).toBe(url);
  await expect(logo).not.toHaveClass(/pnc-breaking/);
  await page.mouse.move(600, 400);
  await expect(logo).not.toHaveClass(/pnc-hover/);
  await logo.focus();
  await page.keyboard.press("Enter");
  await expect(logo).toHaveClass(/pnc-breaking/);
  await expect(logo).not.toHaveClass(/pnc-breaking/);
  await page.locator("[data-theme-toggle]").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect.poll(() => logo.locator('[data-pnc-face="frame"]').evaluate((el) => getComputedStyle(el).fill)).toBe("rgb(51, 89, 119)");
  await page.screenshot({ path: "test-results/pnc-home-dark.png", fullPage: true });
  await page.locator('a[href="/about.html"]').first().click();
  await expect(page).toHaveURL(/about\.html/);
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  await page.locator('.site-brand').click();
  await expect(page).toHaveURL(/index\.html/);
  await page.goto("/posts/2026-01-20-sample.html");
  await expect(logo.locator("svg")).toHaveAttribute("width", "22");
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  await page.goto("/404.html");
  await expect(logo.locator("svg")).toHaveAttribute("width", "28");
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  expect(errors).toEqual([]);
});

test("mobile and reduced motion preserve navigation and disable transforms", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/index.html");
  const logo = page.locator("[data-pnc-logo]").first();
  await logo.hover();
  await logo.click();
  await expect(logo).not.toHaveClass(/pnc-breaking|pnc-hover|pnc-scanning/);
  expect(await logo.locator('.pnc-logo-tilt').evaluate((el) => getComputedStyle(el).transform)).toBe("none");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/pnc-home-mobile-light.png", fullPage: true });
  await page.goto("/posts/2026-01-20-sample.html");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/pnc-post-mobile-light.png", fullPage: true });
  await page.locator('.site-brand').click();
  await expect(page).toHaveURL(/index\.html/);
});

test("shimmer repeats and live motion changes clear animation state", async ({ page }) => {
  await page.goto("/index.html");
  const logo = page.locator("[data-pnc-logo]").first();
  await expect(logo).toHaveClass(/pnc-scanning/);
  await expect(logo).not.toHaveClass(/pnc-scanning/);
  await expect(logo).toHaveClass(/pnc-scanning/, { timeout: 8500 });
  await expect(logo).not.toHaveClass(/pnc-scanning/);
  await page.evaluate(() => {
    document.dispatchEvent(new Event("astro:page-load"));
    document.dispatchEvent(new Event("astro:page-load"));
  });
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  const frames = await logo.locator("svg").evaluate((el) => el.getAnimations()[0].effect!.getKeyframes());
  expect(frames.some((frame) => String(frame.transform).includes("scaleX(-1)"))).toBe(true);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(logo).not.toHaveClass(/pnc-breaking|pnc-hover|pnc-scanning/);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await logo.click();
  await expect(logo).toHaveClass(/pnc-breaking/);
  await expect(logo).not.toHaveClass(/pnc-breaking/);
});
