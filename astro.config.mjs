import { defineConfig } from "astro/config";
import mdx from "@astrojs/mdx";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeImageAttributes from "./src/plugins/rehype-image-attributes.mjs";

export default defineConfig({
  site: "https://parityncsvt.top",
  output: "static",
  publicDir: "./src/static",
  prefetch: true,
  integrations: [mdx()],
  markdown: {
    syntaxHighlight: "shiki",
    shikiConfig: {
      theme: "github-light",
    },
    remarkPlugins: [remarkMath],
    rehypePlugins: [rehypeKatex, rehypeImageAttributes],
  },
  build: {
    format: "file",
  },
});
