import assert from "node:assert/strict";
import test from "node:test";
import { toPlainText, toSearchText } from "../src/lib/text.ts";

test("search text drops MDX scaffolding but keeps searchable words", () => {
  const source = [
    'import Demo from "./Demo.astro";',
    "{/* 草稿批注 */}",
    "## 安装 Arch",
    "",
    "先看 [官方 Wiki](https://wiki.archlinux.org/title/Installation_guide) 和 ![分区示意](./disk.png)。",
    "",
    '<Compare labels={["之前", "之后"]}>',
    "![before](./a.jpg)",
    "</Compare>",
    "",
    '<Note type="tip" title="小技巧">用 **pacstrap** 安装 `base`。</Note>',
    "",
    "```bash",
    "pacman -Syu linux-zen",
    "```",
    "",
    "| 项目 | 结果 |",
    "| --- | ---: |",
    "| CPU | i5-8600K |",
    "",
    "- [x] 完成 _引导_",
    "> 引用 $e^{i\\pi}$",
  ].join("\n");
  const text = toSearchText(source);
  for (const word of ["安装 Arch", "官方 Wiki", "分区示意", "before", "pacstrap", "base", "pacman -Syu linux-zen", "CPU i5-8600K", "完成 引导", "引用 e^{i\\pi}"]) {
    assert.ok(text.includes(word), `missing "${word}" in: ${text}`);
  }
  for (const noise of ["import", "草稿批注", "https://", "./disk.png", "<Note", "labels=", "```", "|", "---", "**", "[x]", "$", "##", "\n"]) {
    assert.ok(!text.includes(noise), `unexpected "${noise}" in: ${text}`);
  }
});

test("search text keeps code verbatim inside fences", () => {
  const text = toSearchText("看代码：\n\n```c\n#include <stdio.h>\nint main() { return a | b; }\n```\n\n~~~\nVec<String>\n~~~");
  assert.equal(text, "看代码： #include <stdio.h> int main() { return a | b; } Vec<String>");
});

test("search text keeps bare URLs and collapses whitespace", () => {
  assert.equal(toSearchText("见 <https://rsshub.app>\n\n\n  第二段   内容"), "见 https://rsshub.app 第二段 内容");
});

test("plain-text summaries stay unchanged", () => {
  assert.equal(toPlainText("## 标题\n\n**粗体** 和 [链接](https://example.com)", 160), "标题 粗体 和 链接");
});
