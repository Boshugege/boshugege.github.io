import { defineConfig } from "astro/config";
import mdx from "@astrojs/mdx";
import { unified } from "@astrojs/markdown-remark";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

export default defineConfig({
  site: "https://parityncsvt.top",
  output: "static",
  publicDir: "./src/static",
  prefetch: true,
  integrations: [mdx()],
  markdown: {
    processor: unified({
      remarkPlugins: [remarkMath],
      rehypePlugins: [rehypeKatex],
    }),
    syntaxHighlight: "shiki",
    shikiConfig: {
      theme: "github-light",
    },
  },
  build: {
    format: "file",
  },
});
