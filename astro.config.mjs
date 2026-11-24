import { fileURLToPath } from "node:url";
import { defineConfig } from "astro/config";
import mdx from "@astrojs/mdx";
import { unified } from "@astrojs/markdown-remark";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

// 正文字体: rename the self-hosted fallback fonts and make each face try the
// system font first, so devices that already have Times New Roman / 宋体-简
// (macOS, iOS, and Windows for Latin) download nothing. Without local(),
// Chrome fetches fallback web fonts even when an earlier system font is used.
const READING_FONTS = {
  "/@fontsource/tinos/": {
    family: "PNC Times",
    local: {
      "400 normal": ["Times New Roman", "TimesNewRomanPSMT"],
      "400 italic": ["Times New Roman Italic", "TimesNewRomanPS-ItalicMT"],
      "700 normal": ["Times New Roman Bold", "TimesNewRomanPS-BoldMT"],
    },
  },
  "/@fontsource/noto-serif-sc/": {
    family: "PNC Song",
    local: {
      "400 normal": ["Songti SC Regular", "STSongti-SC-Regular"],
      "700 normal": ["Songti SC Bold", "STSongti-SC-Bold"],
    },
  },
};

function readingFont() {
  return {
    name: "pnc-reading-font",
    enforce: "pre",
    transform(code, id) {
      const file = id.split("?")[0];
      const font = Object.entries(READING_FONTS).find(([dir]) => file.includes(dir))?.[1];
      if (!font || !file.endsWith(".css")) return;
      return code.replace(/@font-face \{[^}]*\}/g, (face) => {
        const weight = /font-weight: (\d+)/.exec(face)?.[1];
        const style = /font-style: (\w+)/.exec(face)?.[1];
        const local = (font.local[`${weight} ${style}`] || []).map((name) => `local("${name}"), `).join("");
        return face
          .replace(/font-family: '[^']+'/, `font-family: '${font.family}'`)
          // Every browser that runs this site reads woff2; dropping the woff
          // fallback keeps ~200 unused files (8 MB) out of dist/_astro.
          .replace(/,\s*url\([^)]*\.woff\)\s*format\(['"]woff['"]\)/g, "")
          .replace("src: ", `src: ${local}`);
      });
    },
  };
}

// notes.md is read with fs, outside Vite's module graph, so the dev server
// would not notice edits. Reload the page when it changes.
function notesReload() {
  return {
    name: "pnc-notes-reload",
    apply: "serve",
    configureServer(server) {
      // The notes and the link previews shown in them (src/lib/link-cards.ts).
      const files = ["./src/content/notes.md", "./src/data/link-previews.json"].map((file) => fileURLToPath(new URL(file, import.meta.url)));
      server.watcher.add(files);
      server.watcher.on("change", (file) => {
        if (files.includes(file)) server.ws.send({ type: "full-reload" });
      });
    },
  };
}

export default defineConfig({
  site: "https://parityncsvt.top",
  output: "static",
  publicDir: "./src/static",
  prefetch: true,
  integrations: [mdx()],
  vite: {
    plugins: [readingFont(), notesReload()],
    build: {
      // Browsers the CSS must keep working in. Without targets the CSS
      // minifier drops prefixed fallbacks (-webkit-backdrop-filter,
      // -webkit-mask-image) that Safari before 18 still needs.
      cssTarget: ["chrome107", "edge107", "firefox104", "safari15.4", "ios15.4"],
    },
  },
  markdown: {
    processor: unified({
      remarkPlugins: [remarkMath],
      rehypePlugins: [rehypeKatex],
    }),
    syntaxHighlight: "shiki",
    shikiConfig: {
      // Colors are emitted as --shiki-light / --shiki-dark and picked by
      // the site theme in content.css.
      themes: { light: "github-light", dark: "github-dark" },
      defaultColor: false,
    },
  },
  build: {
    format: "file",
  },
});
