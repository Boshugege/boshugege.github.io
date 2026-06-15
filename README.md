# Astro + MDX 古早风博客

说明（简体中文）

- 这是一个使用 Astro + MDX 重构的静态博客，保留原来的古早风布局和旧文章 URL。
- 新文章写在 `src/content/posts/*.mdx`，支持 Markdown、MDX、代码高亮和 `$...$` / `$$...$$` 数学公式。
- 所有历史文章都已经迁移到 MDX；根目录的 HTML/JSON/RSS 是构建产物，可直接由 GitHub Pages 从仓库根目录发布。

维护与部署

1. 安装依赖：

   ```bash
   npm install
   ```

2. 本地预览：

   ```bash
   npm run dev
   ```

3. 生成静态站点：

   ```bash
   npm run build
   ```

   Astro 会先生成 `dist/`，然后同步到仓库根目录，包括：
   - `index.html` / `about.html` / `notes.html`
   - `posts/*.html`
   - `index.json` / `search.json` / `notes.json`
   - `rss.xml` / `sitemap.xml`

## 写文章

在 `src/content/posts/` 新建 `.mdx` 文件，例如：

````mdx
---
title: "示例：代码与公式"
date: "2026-01-20"
tags: ["数学", "代码"]
excerpt: "示例文章，包含行内代码、代码块、行内数学与独立数学公式。"
---

行内代码：`x = 42`。

行内数学：$e^{i\theta}=\cos\theta+i\sin\theta$。

```python
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
```

$$
\int_0^{\infty} e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}
$$
````

默认输出 URL 由文件名决定：`src/content/posts/arch-linux.mdx` 会生成 `/posts/arch-linux.html`。想让 URL 带日期，就把文件命名为 `2026-01-20-sample.mdx`。

## 写简历页

`about.html` 的正文来自 `src/content/about/resume.mdx`。直接编辑这个文件即可更新个人介绍、教育经历、项目经历和联系方式；页面布局、侧边栏、SEO 和构建输出仍由 Astro 处理。正文下方的网站数据由 Astro 在构建时读取文章和随想自动生成。

## 每天随想（单文件增量写作）

- 你可以把每天随想都写在 `src/content/notes.md`（单个文件，持续追加）。
- 推荐格式：

  ```md
  ## 2026-04-07 | 可选标题
  这里写正文，可以多段。
  ```

- 生成页面与首页预览数据：

  ```bash
  npm run build
  ```

- 该命令会生成：
  - `notes.html`：时间线页面（单页浏览全部随想）；
  - `notes.json`：首页“最近随想”预览数据。
