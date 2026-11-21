import assert from "node:assert/strict";
import test from "node:test";
import { formatSiteAge } from "../src/scripts/site-stats.ts";
import { countPostsByYear } from "../src/lib/content/year-counts.ts";

test("formats the blog age in whole years and months", () => {
  assert.equal(
    formatSiteAge("2022-05-10", new Date("2026-07-11T04:00:00.000Z"), "Asia/Shanghai"),
    "4 年 2 个月",
  );
});

test("handles a site younger than one month", () => {
  assert.equal(
    formatSiteAge("2026-07-10", new Date("2026-07-11T04:00:00.000Z"), "Asia/Shanghai"),
    "1 天",
  );
});

test("yearly post counts fill empty years between founding and the latest post", () => {
  const dates = ["2026-04-08", "2026-01-20", "2023-07-01", "2022-05-10", "2022-09-01"];
  assert.deepEqual(countPostsByYear(dates, 2022), [
    ["2026", 2],
    ["2025", 0],
    ["2024", 0],
    ["2023", 1],
    ["2022", 2],
  ]);
});

test("yearly post counts start at the founding year when it is older than every post", () => {
  assert.deepEqual(countPostsByYear(["2024-03-01"], 2022), [["2024", 1], ["2023", 0], ["2022", 0]]);
  assert.deepEqual(countPostsByYear(["2021-03-01", "2022-01-01"], 2022), [["2022", 1], ["2021", 1]]);
  assert.deepEqual(countPostsByYear([], 2022), []);
});
