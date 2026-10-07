import { expect, test } from "@playwright/test";

test("About has a closed cube lockup with independent SVG IDs and hover pop-up", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/about.html");
  const wordmark = page.locator("[data-pnc-wordmark]");
  const logo = wordmark.locator("[data-pnc-logo]");
  await expect(page.locator("[data-pnc-logo]")).toHaveCount(2);
  await expect(wordmark).toContainText("ParityNonconservation");
  await expect(logo.locator("[data-pnc-backing]")).toHaveCount(3);
  await expect(logo.locator("[data-pnc-face]")).toHaveCount(4);
  const validIds = await page.evaluate(() => {
    const ids = [...document.querySelectorAll('[data-pnc-logo] [id]')].map((el) => el.id);
    const refs = [...document.querySelectorAll('[data-pnc-logo] use')].map((el) => el.getAttribute('href')!.slice(1));
    return new Set(ids).size === ids.length && refs.every((id) => document.getElementById(id));
  });
  expect(validIds).toBeTruthy();
  // The logo never tilts; hovering it only pops the faces apart.
  await page.mouse.move(10, 10);
  await expect(logo).not.toHaveClass(/pnc-hover/);
  expect(await logo.locator('[data-pnc-face="top"]').evaluate((el) => getComputedStyle(el).transform)).toBe("none");
  await logo.hover();
  await expect(logo).toHaveClass(/pnc-hover/);
  await expect.poll(() => logo.locator('[data-pnc-face="top"]').evaluate((el) => getComputedStyle(el).transform)).not.toBe("none");
  await page.mouse.move(1300, 900);
  await expect(logo).not.toHaveClass(/pnc-hover/);
  await page.screenshot({ path: "test-results/pnc-about-light.png" });
  await page.locator('[data-theme-toggle]').click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.waitForTimeout(300);
  await page.screenshot({ path: "test-results/pnc-about-dark.png" });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await logo.hover();
  await expect(logo).not.toHaveClass(/pnc-hover/);
  await page.locator('.site-brand').click();
  await expect(page).toHaveURL(/index\.html/);
  await page.locator('a[href="/about.html"]').first().click();
  await expect(page).toHaveURL(/about\.html/);
  await page.waitForLoadState("networkidle");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await logo.hover();
  await expect(logo).toHaveClass(/pnc-hover/);
  expect(errors).toEqual([]);
});

for (const width of [320, 375]) {
  test(`About horizontal identity fits ${width}px and remains static with reduced motion`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/about.html");
    const wordmark = page.locator('[data-pnc-wordmark]');
    await expect(wordmark).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const logo = wordmark.locator('[data-pnc-logo]');
    await logo.hover();
    await expect(logo).not.toHaveClass(/pnc-hover|pnc-scanning/);
    await expect.poll(() => logo.locator('.pnc-logo-svg').evaluate((el) => getComputedStyle(el).transform)).toBe("none");
    await page.mouse.move(width - 2, 895);
    await page.screenshot({ path: `test-results/pnc-about-mobile-${width}.png` });
  });
}
