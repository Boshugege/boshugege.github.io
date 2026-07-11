import assert from "node:assert/strict";
import test from "node:test";
import { formatSiteAge } from "../src/scripts/site-stats.ts";

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
