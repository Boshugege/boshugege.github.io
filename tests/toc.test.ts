import assert from "node:assert/strict";
import test from "node:test";
import { buildTableOfContents } from "../src/lib/toc.ts";

test("builds nested H2 and H3 sections", () => {
  const toc = buildTableOfContents([
    { depth: 2, slug: "one", text: "One" },
    { depth: 3, slug: "one-a", text: "One A" },
    { depth: 2, slug: "two", text: "Two" },
  ]);

  assert.equal(toc?.count, 3);
  assert.deepEqual(toc?.items, [
    {
      depth: 2,
      slug: "one",
      text: "One",
      children: [{ depth: 3, slug: "one-a", text: "One A", children: [] }],
    },
    { depth: 2, slug: "two", text: "Two", children: [] },
  ]);
});

test("normalizes legacy articles that begin at H3 or H4", () => {
  const toc = buildTableOfContents([
    { depth: 3, slug: "one", text: "One" },
    { depth: 4, slug: "one-a", text: "One A" },
    { depth: 3, slug: "two", text: "Two" },
  ]);

  assert.equal(toc?.items.length, 2);
  assert.equal(toc?.items[0].children[0].slug, "one-a");
});

test("nests skipped heading levels under the nearest shallower heading", () => {
  const toc = buildTableOfContents([
    { depth: 2, slug: "one", text: "One" },
    { depth: 4, slug: "deep", text: "Deep" },
    { depth: 2, slug: "two", text: "Two" },
  ]);

  assert.equal(toc?.items[0].children[0].slug, "deep");
});

test("preserves duplicate-title slugs supplied by Astro", () => {
  const toc = buildTableOfContents([
    { depth: 2, slug: "repeat", text: "Repeat" },
    { depth: 2, slug: "repeat-1", text: "Repeat" },
    { depth: 2, slug: "repeat-2", text: "Repeat" },
  ]);

  assert.deepEqual(toc?.items.map((item) => item.slug), ["repeat", "repeat-1", "repeat-2"]);
});

test("filters headings outside H2 through H4", () => {
  const toc = buildTableOfContents([
    { depth: 1, slug: "title", text: "Title" },
    { depth: 2, slug: "one", text: "One" },
    { depth: 3, slug: "two", text: "Two" },
    { depth: 4, slug: "three", text: "Three" },
    { depth: 5, slug: "detail", text: "Detail" },
  ]);

  assert.equal(toc?.count, 3);
  assert.deepEqual(toc?.items[0].children[0].children[0].slug, "three");
});

test("omits disabled and short tables of contents", () => {
  const headings = [
    { depth: 2, slug: "one", text: "One" },
    { depth: 2, slug: "two", text: "Two" },
  ];

  assert.equal(buildTableOfContents(headings), undefined);
  assert.equal(buildTableOfContents([...headings, { depth: 2, slug: "three", text: "Three" }], false), undefined);
});
