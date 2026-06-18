import assert from "node:assert/strict";
import test from "node:test";
import { getReadingStats } from "../src/lib/reading.ts";

test("counts CJK and Latin words while ignoring code and links", () => {
  const result = getReadingStats("中文 text words [label](https://example.com) `hidden`\n```js\nconst ignored = true\n```");
  assert.equal(result.wordCount, 4);
  assert.equal(result.readingMinutes, 1);
});

test("reading time never drops below one minute", () => {
  assert.deepEqual(getReadingStats(""), { wordCount: 0, readingMinutes: 1 });
});
