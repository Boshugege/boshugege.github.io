import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { brotliCompress, constants, gzip } from "node:zlib";

const brotli = promisify(brotliCompress);
const gz = promisify(gzip);

export async function compressAssets(directory) {
  let count = 0;
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      count += await compressAssets(file);
    } else if (entry.isFile() && /\.(js|css)$/.test(entry.name)) {
      const source = await fs.readFile(file);
      if (source.length < 1024) continue;
      const [br, compressed] = await Promise.all([
        brotli(source, { params: { [constants.BROTLI_PARAM_QUALITY]: 11 } }),
        gz(source, { level: 9 }),
      ]);
      await Promise.all([
        fs.writeFile(`${file}.br`, br),
        fs.writeFile(`${file}.gz`, compressed),
      ]);
      count++;
    }
  }
  return count;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const count = await compressAssets(path.join(process.cwd(), "dist", "_astro"));
  console.log(`Precompressed ${count} JavaScript/CSS assets with Brotli and gzip.`);
}
