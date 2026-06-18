import assert from "node:assert/strict";
import test from "node:test";
import { getPostFeatures } from "../src/lib/content/features.ts";

test("detects prose math without treating shell variables as math", () => {
  const content = "```ini\nServer = /$repo/$arch\n```\n\n公式 $x + y$。";
  assert.deepEqual(getPostFeatures(content), { codeBlocks: 1, images: 0, hasMath: true });
});

test("ignores dollar expressions inside fenced code", () => {
  const content = "```sh\necho $HOME && echo $PATH\n```";
  assert.equal(getPostFeatures(content).hasMath, false);
});
