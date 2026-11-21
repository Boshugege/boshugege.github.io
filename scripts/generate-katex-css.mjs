// Regenerates src/styles/katex-local.css from the installed KaTeX package:
// the stock stylesheet with font URLs pointing into node_modules (so Vite
// bundles them) and only the woff2 sources kept. Run after upgrading katex:
//   node scripts/generate-katex-css.mjs
import fs from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const source = require.resolve("katex/dist/katex.min.css");
const css = await fs.readFile(source, "utf8");
const fontSources = /src:url\(fonts\/([\w-]+)\.woff2\) format\("woff2"\)(?:,url\(fonts\/[\w-]+\.(?:woff|ttf)\) format\("(?:woff|truetype)"\))*/g;
let count = 0;
const local = css.replace(fontSources, (_, name) => {
  count++;
  return `src:url(../../node_modules/katex/dist/fonts/${name}.woff2) format("woff2")`;
});
if (count === 0 || /\.(?:woff|ttf)\)/.test(local)) {
  throw new Error("KaTeX stylesheet format changed; update scripts/generate-katex-css.mjs");
}
await fs.writeFile(
  new URL("../src/styles/katex-local.css", import.meta.url),
  `/* Generated from katex/dist/katex.min.css by scripts/generate-katex-css.mjs; keep only woff2 font sources. */\n${local}\n`,
);
console.log(`Wrote src/styles/katex-local.css (${count} font faces, KaTeX ${require("katex/package.json").version}).`);
