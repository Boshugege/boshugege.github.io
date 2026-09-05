import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { brotliDecompressSync, gunzipSync } from "node:zlib";
import { compressAssets } from "../scripts/compress-build.mjs";

test("build compression preserves assets and produces decodable sidecars", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "pnc-compression-"));
  try {
    const script = Buffer.from("export const colors = ['red', 'green', 'blue'];\n".repeat(100));
    await fs.mkdir(path.join(directory, "nested"));
    await fs.writeFile(path.join(directory, "scene.js"), script);
    await fs.writeFile(path.join(directory, "nested", "scene.css"), "body { color: red; }\n".repeat(100));
    await fs.writeFile(path.join(directory, "small.js"), "export {};");
    await fs.writeFile(path.join(directory, "image.webp"), script);
    assert.equal(await compressAssets(directory), 2);
    assert.deepEqual(await fs.readFile(path.join(directory, "scene.js")), script);
    const br = await fs.readFile(path.join(directory, "scene.js.br"));
    const gz = await fs.readFile(path.join(directory, "scene.js.gz"));
    assert.deepEqual(brotliDecompressSync(br), script);
    assert.deepEqual(gunzipSync(gz), script);
    assert.ok(br.length < script.length);
    assert.ok(gz.length < script.length);
    await assert.rejects(fs.access(path.join(directory, "small.js.br")));
    await assert.rejects(fs.access(path.join(directory, "image.webp.br")));
    assert.equal(await compressAssets(directory), 2);
    await assert.rejects(fs.access(path.join(directory, "scene.js.br.br")));
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
