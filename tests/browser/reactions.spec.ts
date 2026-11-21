import { createHash } from "node:crypto";
import { expect, test, type Page, type Route } from "@playwright/test";

const POST = "/posts/cg-final-path-tracing.html";
const TARGET = "/posts/cg-final-path-tracing.html";
const DIFFICULTY = 10;

function zeroBits(text: string) {
  let bits = 0;
  for (const byte of createHash("sha256").update(text).digest()) {
    if (byte !== 0) return bits + Math.clz32(byte) - 24;
    bits += 8;
  }
  return bits;
}

/** A stand-in for ops/reactions-api that really checks the proof of work. */
async function mockApi(page: Page, initial: Record<string, number>) {
  const counts = new Map(Object.entries(initial));
  const posts: { target: string; nonce: string; challenge: string }[] = [];
  const reads: string[] = [];
  await page.route(/\/api\/reactions(?:\/challenge)?(?:\?.*)?$/, async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/challenge")) {
      return route.fulfill({ json: { challenge: `v1.mock-${url.searchParams.get("target")}.sig`, difficulty: DIFFICULTY, expiresAt: Date.now() + 60_000 } });
    }
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      posts.push(body);
      if (zeroBits(`${body.challenge}:${body.nonce}`) < DIFFICULTY) return route.fulfill({ status: 403, json: { error: "工作量证明无效" } });
      counts.set(body.target, (counts.get(body.target) || 0) + 1);
      return route.fulfill({ json: { target: body.target, count: counts.get(body.target), observed: true, counted: true } });
    }
    reads.push(url.search);
    const prefix = url.searchParams.get("prefix");
    const target = url.searchParams.get("target");
    const visible = [...counts].filter(([key]) => (prefix ? key.startsWith(prefix) : key === target));
    return route.fulfill({ json: { counts: Object.fromEntries(visible), observed: [] } });
  });
  return { posts, reads };
}

test("without the reactions API the observe buttons stay hidden and the layout is unchanged", async ({ page }) => {
  await page.goto(POST);
  await page.waitForLoadState("networkidle");
  await expect(page.locator("[data-observe]")).toBeHidden();
  await expect(page.locator(".post-meta [data-share]")).toBeVisible();
  await page.goto("/notes.html");
  await page.waitForLoadState("networkidle");
  const buttons = page.locator("[data-observe]");
  expect(await buttons.count()).toBeGreaterThan(0);
  for (const button of await buttons.all()) await expect(button).toBeHidden();
});

test("observing solves the challenge, adds one, and stays observed after a reload", async ({ page }) => {
  const api = await mockApi(page, { [TARGET]: 3 });
  await page.goto(POST);
  const button = page.locator(".post-meta [data-observe]");
  await expect(button).toBeVisible();
  await expect(button).toHaveAttribute("aria-pressed", "false");
  await expect(button.locator("[data-observe-label]")).toHaveText("观测");
  await expect(button.locator("[data-observe-count]")).toHaveText("3");

  // A double click must not send two reactions.
  await button.dblclick();
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(button).toHaveAttribute("aria-disabled", "true");
  await expect(button.locator("[data-observe-label]")).toHaveText("已观测");
  await expect(button.locator("[data-observe-count]")).toHaveText("4");
  await page.locator(".post-meta").screenshot({ path: "test-results/observe-post.png" });
  expect(api.posts).toHaveLength(1);
  expect(api.posts[0].target).toBe(TARGET);
  expect(zeroBits(`${api.posts[0].challenge}:${api.posts[0].nonce}`)).toBeGreaterThanOrEqual(DIFFICULTY);

  // One reaction per reader: another click (the button is aria-disabled now,
  // so Playwright will not click it normally) sends nothing.
  await button.dispatchEvent("click");
  await page.waitForTimeout(200);
  expect(api.posts).toHaveLength(1);

  await page.reload();
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(button.locator("[data-observe-count]")).toHaveText("4");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("the notes page loads every note's count in a single request", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const ids = await (await page.request.get("/notes.json")).json().then((data: { entries: { url: string }[] }) => data.entries.map((entry) => `note:${entry.url.split("#")[1]}`));
  const api = await mockApi(page, { [ids[0]]: 2 });
  await page.goto("/notes.html");
  const buttons = page.locator("[data-observe]");
  await expect(buttons.first()).toBeVisible();
  await expect(buttons).toHaveCount(ids.length);
  expect(api.reads).toEqual(["?prefix=note%3A"]);
  await expect(page.locator(`[data-observe-target="${ids[0]}"] [data-observe-count]`)).toHaveText("2");
  await page.locator(".note-item").first().screenshot({ path: "test-results/observe-note-mobile.png" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("a failed reaction leaves the button usable and says so briefly", async ({ page }) => {
  await page.route(/\/api\/reactions(?:\/challenge)?(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/challenge")) return route.fulfill({ status: 429, json: { error: "太频繁" } });
    return route.fulfill({ json: { counts: { [TARGET]: 1 }, observed: [] } });
  });
  await page.goto(POST);
  const button = page.locator(".post-meta [data-observe]");
  await expect(button).toBeVisible();
  await button.click();
  await expect(button.locator("[data-observe-label]")).toHaveText("稍后再试");
  await expect(button).toHaveAttribute("aria-pressed", "false");
  await expect(button.locator("[data-observe-label]")).toHaveText("观测", { timeout: 4000 });
});

test("a post the API cannot vouch for keeps its button hidden", async ({ page }) => {
  await page.route(/\/api\/reactions(?:\?.*)?$/, (route) => route.fulfill({ json: { counts: {}, observed: [] } }));
  await page.goto(POST);
  await page.waitForLoadState("networkidle");
  await expect(page.locator(".post-meta [data-observe]")).toBeHidden();
});

test("what the server reports for a shared address is shown but not remembered", async ({ page }) => {
  let observed = [TARGET];
  await page.route(/\/api\/reactions(?:\?.*)?$/, (route) => route.fulfill({ json: { counts: { [TARGET]: 5 }, observed } }));
  await page.goto(POST);
  const button = page.locator(".post-meta [data-observe]");
  await expect(button).toHaveAttribute("aria-pressed", "true");
  observed = [];
  await page.reload();
  await expect(button).toBeVisible();
  await expect(button).toHaveAttribute("aria-pressed", "false");
});
