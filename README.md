# PNC's Blog

一个用 Astro + MDX 维护的静态个人博客。源码集中在 `src/`，仓库根目录是 GitHub Pages 的发布产物；不要直接手改根目录 HTML/JSON/RSS，改源码后运行构建同步。

站点保留了原来的古早风布局和旧文章 URL，同时使用 Astro 的内容集合、图片管线、静态路由、RSS/sitemap、全文搜索索引和按需加载的本地 KaTeX。

## 快速使用

安装依赖：

```bash
npm install
```

本地开发：

```bash
npm run dev
```

完整验证：

```bash
npm run verify
```

生成并发布到仓库根目录：

```bash
npm run build
```

常用命令说明：

- `npm run dev`：启动 Astro dev server。
- `npm run build:dist`：只生成 `dist/`，并清理未引用的 `_astro` 资产。
- `npm run build`：生成 `dist/`，清理构建垃圾，再同步到仓库根目录。
- `npm run verify`：类型检查、单元测试、构建 `dist/`，并检查静态输出契约。

## 写文章

文章源码放在 `src/content/posts/`：

```text
src/content/posts/example.mdx
src/content/posts/my-post/index.mdx
```

普通文件会生成同名 URL：

```text
src/content/posts/arch-linux.mdx -> /posts/arch-linux.html
```

目录式文章会生成目录名 URL，适合放就近图片：

```text
src/content/posts/my-post/
├── index.mdx
├── cover.jpg
└── screenshot.png
```

文章 frontmatter：

```mdx
---
title: "示例文章"
date: "2026-01-20"
updated: "2026-01-22"
tags: ["数学", "代码"]
excerpt: "首页、RSS 和 SEO 使用的摘要。"
cover: "./cover.jpg"
coverAlt: "封面图片说明"
canonical: "https://example.com/original.html"
draft: false
---
```

字段说明：

- `title`、`date` 是必填字段。
- `tags` 可以写数组，也可以写逗号分隔字符串，构建时会统一成数组。
- `updated` 会进入文章页元数据和 sitemap 的 `lastmod`。
- `excerpt` 用于首页索引、RSS、搜索摘要和 SEO 描述。
- `cover` 使用 Astro 的 `image()` schema 校验，推荐使用相对路径。
- `coverAlt` 会写入 Open Graph / Twitter 图片说明。
- `canonical` 可为转载或外部首发文章指定规范 URL。
- `draft: true` 会让文章从构建输出中排除。

正文支持 Markdown、MDX、代码高亮、相对路径图片，以及 `$...$` / `$$...$$` 数学公式。

## 图片规则

文章图片优先放在文章目录里，并用相对路径引用：

```mdx
![截图](./screenshot.png)
```

Astro 会在构建时优化这些图片，输出到 `/_astro/`，并生成 `width`、`height`、`loading`、`decoding` 等属性。文章封面也走同一套图片管线，避免 SEO 元数据指向不存在的源码路径。

只有真正需要固定公开路径的静态资源才放在 `src/static/`，例如：

```text
src/static/assets/img/icon.jpg
src/static/manifest.webmanifest
src/static/robots.txt
src/static/CNAME
```

`scripts/cleanup-build.mjs` 会在构建后清理 `dist/_astro` 中没有被 HTML、CSS、JS、JSON、XML 等文本输出引用的资产，避免优化过程中留下未使用的原图副产物。

## 数学公式

Markdown 中可以直接写：

```mdx
行内公式：$e^{i\theta}=\cos\theta+i\sin\theta$

$$
\int_0^{\infty} e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}
$$
```

构建会通过 `remark-math` + `rehype-katex` 渲染公式。站点只在检测到文章包含数学公式时加载 KaTeX 样式，并使用 `src/styles/katex-local.css` 本地打包的 woff2 字体，不依赖 CDN。

## About 和随想

About 页面正文来自：

```text
src/content/about/resume.mdx
```

该页面已预注册这些 MDX 组件：

- `ResumeSection`
- `Timeline`
- `ProjectList`
- `SkillList`
- `LinkList`
- `MetricStrip`

每天随想写在：

```text
src/content/notes.md
```

推荐格式：

```md
## 2026-04-07 | 可选标题
这里写正文，可以多段。
```

构建会生成：

- `notes.html`：随想时间线页面。
- `notes.json`：首页最近随想预览数据。

## 工程结构

```text
src/
├── components/          # Astro 组件
├── content/             # posts / about / notes 源内容
├── layouts/             # 文档布局、站点布局、文章布局
├── lib/                 # 内容规范化、搜索、阅读统计、站点配置
├── pages/               # Astro 页面和静态 JSON/XML endpoints
├── scripts/             # 浏览器端增强脚本
├── static/              # 固定公开路径静态文件
└── styles/              # 全局、文章、About、Notes、KaTeX 样式

scripts/
├── cleanup-build.mjs    # 删除 dist/_astro 中未引用的构建副产物
├── publish-root.mjs     # 将 dist 同步到仓库根目录
└── verify-build.mjs     # 检查静态输出契约
```

核心约定：

- `src/` 是唯一源码树。
- `dist/` 是 Astro 构建输出。
- 仓库根目录是 GitHub Pages 发布目标，由 `npm run build` 自动同步。
- `_astro/` 和根目录 HTML/JSON/XML 是发布产物，需要随构建结果一起提交。

## 构建与验证细节

`npm run verify` 会执行：

```bash
npm run typecheck
npm test
npm run build:dist
node scripts/verify-build.mjs
```

验证脚本会检查：

- 首页、About、Notes、RSS、sitemap、JSON 索引存在。
- 每个 `src/content/posts/**/*.mdx` 都生成对应的 `posts/*.html`。
- 数学文章加载本地 KaTeX CSS，非数学页面不加载 KaTeX。
- 文章图片有懒加载、解码和尺寸属性。
- 文章封面元数据使用 Astro 优化后的公开图片。
- 旧的 now-status / calendar 输出没有复活。
- 搜索索引和首页 HTML 没有超过体积预算。
- 已移除的旧静态垃圾不会重新出现在构建输出里。

## 发布流程

1. 修改 `src/` 下的源码或内容。
2. 运行 `npm run verify`。
3. 运行 `npm run build` 同步根目录发布产物。
4. 检查 `git status`，确认源码变更和生成产物都符合预期。
5. 提交。

如果只想检查源码和 `dist/`，不要刷新根目录产物，可以运行：

```bash
npm run build:dist
```
