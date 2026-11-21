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

test("ignores shell variables in inline code on the same line", () => {
  assert.equal(getPostFeatures("先看 `$HOME` 和 `$PATH` 两个变量。").hasMath, false);
  assert.equal(getPostFeatures("`echo $HOME`，再 `cd $PATH`").hasMath, false);
});

test("ignores dollars inside multi-backtick inline code", () => {
  assert.equal(getPostFeatures("运行 ``echo `$A` $B`` 即可").hasMath, false);
});

test("still finds math next to inline code", () => {
  assert.equal(getPostFeatures("变量 `$HOME` 与公式 $a+b$ 同行").hasMath, true);
  assert.equal(getPostFeatures("`$x`\n\n$$\nE = mc^2\n$$").hasMath, true);
});
