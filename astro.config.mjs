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
          .replace("src: ", `src: ${local}`);
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
    plugins: [readingFont()],
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
