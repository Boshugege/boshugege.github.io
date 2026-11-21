import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createMarkdownProcessor } from "@astrojs/markdown-remark";
import { localAssetPath, rehypeNoteImages } from "../src/lib/note-images.ts";

// The first bytes of a lossy WebP, the format the Telegram bot stores note
// images in. Size readers stop after the frame header.
function webpHeader(width: number, height: number) {
  const data = Buffer.alloc(30);
  data.write("RIFF", 0, "latin1");
  data.writeUInt32LE(data.length - 8, 4);
  data.write("WEBPVP8 ", 8, "latin1");
  data.writeUInt32LE(data.length - 20, 16);
  data.set([0x30, 0x01, 0x00, 0x9d, 0x01, 0x2a], 20);
  data.writeUInt16LE(width, 26);
  data.writeUInt16LE(height, 28);
  return data;
}

async function withStaticDir(files: Record<string, Uint8Array>, run: (staticDir: string) => Promise<void>) {
  const staticDir = await fs.mkdtemp(path.join(os.tmpdir(), "pnc-note-images-"));
  try {
    for (const [name, data] of Object.entries(files)) {
      const file = path.join(staticDir, name);
      await fs.mkdir(path.dirname(file), { recursive: true });
      await fs.writeFile(file, data);
    }
    await run(staticDir);
  } finally {
    await fs.rm(staticDir, { recursive: true, force: true });
  }
}

async function renderNote(staticDir: string, source: string) {
  const markdown = await createMarkdownProcessor({ syntaxHighlight: false, rehypePlugins: [[rehypeNoteImages, { staticDir }]] });
  return (await markdown.render(source)).code;
}

function imageTags(html: string) {
  return [...html.matchAll(/<img\b[^>]*>/g)].map((match) => match[0]);
}

function imageTree(properties: Record<string, unknown>) {
  return { type: "root", children: [{ type: "element", tagName: "img", properties, children: [] }] };
}

test("note images load lazily and local ones reserve their intrinsic size", async () => {
  await withStaticDir({ "assets/notes/20261121-0123456789.webp": webpHeader(1600, 1200), "assets/notes/图 a.webp": webpHeader(640, 480) }, async (staticDir) => {
    const html = await renderNote(staticDir, [
      "![猫](/assets/notes/20261121-0123456789.webp)",
      "",
      "![](</assets/notes/图 a.webp?v=2>)",
      "",
      "![远程](https://example.com/a.png) ![站内](/favicon.svg) ![相对](./a.png)",
    ].join("\n"));
    const [local, encoded, ...others] = imageTags(html);
    assert.equal(others.length, 3);
    assert.match(local, /src="\/assets\/notes\/20261121-0123456789\.webp"/);
    assert.match(local, /alt="猫"/);
    for (const tag of [local, encoded]) {
      assert.match(tag, / loading="lazy"/);
      assert.match(tag, / decoding="async"/);
    }
    assert.match(local, / width="1600" height="1200"/);
    assert.match(encoded, / width="640" height="480"/);
    for (const tag of others) {
      assert.match(tag, / loading="lazy" decoding="async"/);
      assert.doesNotMatch(tag, /width=|height=/);
    }
  });
});

test("a note image missing from the static dir fails the render", async () => {
  await withStaticDir({}, async (staticDir) => {
    const transform = rehypeNoteImages({ staticDir });
    await assert.rejects(transform(imageTree({ src: "/assets/notes/missing.webp" })), /image \/assets\/notes\/missing\.webp not found/);
  });
});

test("a file that is not an image fails the render", async () => {
  await withStaticDir({ "assets/notes/broken.webp": new TextEncoder().encode("not an image") }, async (staticDir) => {
    const transform = rehypeNoteImages({ staticDir });
    await assert.rejects(transform(imageTree({ src: "/assets/notes/broken.webp" })));
  });
});

test("sizes and loading hints already on an image are kept", async () => {
  await withStaticDir({}, async (staticDir) => {
    const properties = { src: "/assets/notes/missing.webp", width: 10, loading: "eager" };
    await rehypeNoteImages({ staticDir })(imageTree(properties));
    assert.deepEqual(properties, { src: "/assets/notes/missing.webp", width: 10, loading: "eager", decoding: "async" });
  });
});

test("only well-formed /assets/ paths map to static files", () => {
  assert.equal(localAssetPath("/assets/notes/20261121-0123456789.webp"), "assets/notes/20261121-0123456789.webp");
  assert.equal(localAssetPath("/assets/notes/%E5%9B%BE%20a.webp?v=2#top"), "assets/notes/图 a.webp");
  for (const src of [
    "https://example.com/assets/x.webp",
    "//example.com/assets/x.webp",
    "./x.webp",
    "assets/x.webp",
    "/favicon.svg",
    "/assets/../../etc/passwd",
    "/assets/notes/%2E%2E/x.webp",
    "/assets/notes/./x.webp",
    "/assets//x.webp",
    "/assets/a\\..\\b.webp",
    "/assets/%E0%A4%A.webp",
  ]) {
    assert.equal(localAssetPath(src), undefined, src);
  }
});
