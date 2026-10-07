// Usage: npm run new -- my-slug "文章标题"
// Creates src/content/posts/<slug>/index.mdx as a draft. Set `draft: false`
// (or delete the line) when the post is ready to publish.
import fs from "node:fs/promises";
import path from "node:path";

const [slug, title = slug] = process.argv.slice(2);
if (!slug || !/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
  console.error('Usage: npm run new -- my-slug "文章标题"  (slug: lowercase letters, digits, dashes)');
  process.exit(1);
}

const dir = path.join("src/content/posts", slug);
const file = path.join(dir, "index.mdx");
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());

await fs.mkdir(dir, { recursive: true });
await fs.writeFile(file, `---
title: ${JSON.stringify(title)}
date: "${today}"
tags: []
excerpt: ""
draft: true
---

{/* 图片放在同一目录，用 ![说明](./image.png) 引用。 */}

在这里写正文。
`, { flag: "wx" }).catch((error) => {
  if (error.code === "EEXIST") {
    console.error(`${file} already exists.`);
    process.exit(1);
  }
  throw error;
});

console.log(`Created ${file} (draft). Preview it with: npm run dev`);
