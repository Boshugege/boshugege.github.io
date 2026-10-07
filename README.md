# PNC's Blog

一个用 Astro + MDX 维护的静态个人博客。仓库只保存可重建的源码、配置、测试和部署入口；`dist/` 及其他静态构建产物不提交到 Git。

站点保留了原来的古早风布局和旧文章 URL，同时使用 Astro 的内容集合、图片管线、静态路由、RSS/sitemap、全文搜索索引和按需加载的本地 KaTeX。本机部署还通过同源 `/api/*` 提供评论、按日去重的文章阅读次数与 About 动态站点统计。

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

生成生产静态文件：

```bash
npm run build
```

常用命令说明：

- `npm run dev`：启动 Astro dev server。
- `npm run build`：生成 `dist/`，并清理未引用的 `_astro` 资产。
- `npm run verify`：类型检查、单元测试、构建 `dist/`，并检查静态输出契约。
- `npm run test:404`：构建后启动临时预览服务，使用 Chromium 检查 404 的桌面/手机截图、画布像素、动画交互和降级表现。首次运行需要 `npx playwright install chromium`。

## 404 彩蛋

`src/pages/404.astro` 构建为 `dist/404.html`，可在本地直接访问 `/404.html`。终端框中的 Three.js 场景把不同深度的零件正交投影成 `404`，旋转后通过 68 字符的灰度滤镜显示错位结构。画面只显示等宽字符，不叠加实体面底色或棱线；正侧面使用独立材质，字符网格保留桌面 6px、手机 4.5px 的尺寸，终端栏文字为 13–15px。支持暂停、重新对齐、鼠标微偏转和明暗主题，状态栏区分运行、对齐和暂停。每圈在正面停留 1.5 秒，期间及接近正面时禁用鼠标偏转，其余旋转仅在起止处短暂缓动。场景脚本只在 404 页面加载。

系统开启“减少动态效果”时默认静止；没有 JavaScript 或 WebGL 时显示静态字符画，返回首页链接仍然可用。动画脚本放在文档 head 中提前发现；同一页面只初始化一次，Astro 页面切换时释放并重建场景。测试截图保存在被 Git 忽略的 `test-results/`。

生产静态服务器将 `/404` 和其他未找到的静态请求交给 `404.html`，保留 HTTP 404 状态和原始地址，并使用 `Cache-Control: no-store`，避免 CDN 缓存过期的缺页结果。直接访问 `/404.html` 仍返回 200。此行为由服务器上的 `server.mjs` 实现，Astro 的本地预览测试不能代替生产路由验证。

## 写文章

新建文章（生成草稿，`npm run dev` 可预览，构建时不发布）：

```bash
npm run new -- my-slug "文章标题"
```

写完后把 `draft: true` 改成 `false` 或删掉该行，再推送。

文章源码放在 `src/content/posts/`：

```text
src/content/posts/my-post/
├── index.mdx
├── cover.jpg
└── screenshot.png
```

每篇文章使用独立目录，目录名就是文章 slug：

```text
src/content/posts/arch-linux/index.mdx -> /posts/arch-linux.html
```

文章引用的封面、插图和其他附件放在同一目录，通过相对路径引用：

```md
![截图](./screenshot.png)
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
toc: true
centerImages: false
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
- `draft: true` 会让文章从构建输出中排除，但 `npm run dev` 中仍可预览。
- `toc: false` 可关闭文章自动目录；默认开启，且只有 H2–H4 标题达到 3 个时才显示。
- `centerImages: true` 让正文图片居中显示。

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

正文就是普通 Markdown，直接按标题分段写即可。

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

## 修改外观

整站外观集中在 `src/styles/global.css` 顶部的设计变量里：

- `--bg` / `--panel` / `--text` / `--muted` / `--border` / `--link`：颜色（下方 `[data-theme="dark"]` 是深色版本）。
- `--font-sans` / `--font-serif` / `--font-mono`：正文、标题、代码字体。标题英文用自托管的 Source Serif 4（`@fontsource-variable/source-serif-4`），中文回退到系统宋体；不依赖 Google Fonts。
- `--radius`：卡片、按钮圆角。
- `--divider`：列表与段落间的分隔线样式。

站点名称、简介、侧栏文字、邮箱等写在 `src/lib/site.ts`。

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
└── verify-build.mjs     # 检查静态输出契约
```

核心约定：

- `src/` 是唯一前端源码树。
- `dist/` 是 Astro 构建输出。
- `dist/`、`.astro/`、`node_modules/` 由 Git 忽略。
- 生产部署从指定 Git commit 重新构建 `dist/`，不读取仓库中的预生成 HTML。

## 生产服务

本仓库是博客文章、随想、About 和前端实现的权威来源。服务器上的正式静态站服务位于 `/home/lyy/services/pnc-blog`：

- `pnc-blog.service` 运行 `server.mjs`，只监听 `127.0.0.1:46213`。
- `deploy.sh` 获取 `origin/main`，在临时 worktree 中验证并构建指定提交，再将 `current` 原子切换到新 release。
- 构建为较大的 `_astro/*.js` 和 CSS 生成 Brotli/gzip 旁文件。`server.mjs` 按 `Accept-Encoding` 返回压缩内容和正确的 `Content-Type`、`Content-Length`、`Vary`，旧版本没有旁文件时仍可返回原文件。
- 哈希资源保持一年不可变缓存；源站预压缩响应使用 `no-transform` 避免 CDN 再压缩。Cloudflare 仍负责边缘缓存和客户端编码兼容，不需要缓存 `/api/*` 或错误页面。
- `releases/` 在部署健康检查期间保留旧版本用于回滚；部署成功后只保留当前 release。

`main` push 会先在 GitHub 托管 Runner 上执行完整验证；通过后，由标签为 `pnc-blog` 的本机 self-hosted Runner 调用生产 `deploy.sh`。部署脚本只构建该次 workflow 已验证的提交 SHA，健康检查覆盖首页、自定义 404 内容及状态码、评论 API；失败会自动回滚，成功后只保留当前 release。

`/home/lyy/services/pnc-cms` 是正式的 Directus 管理服务，保存 CMS 配置、评论与阅读数据；文章源码仍保存在本仓库。`/home/lyy/services/pnc-comment-api` 是正式的评论与统计 API 服务。博客静态服务器把公开的 `/api/*` 请求转发给它，浏览器不直接访问 Directus。

动态统计使用 Directus 的 `post_views` collection：同一 IP + User-Agent、同一文章、同一天只计一次。文章页负责记录阅读，首页批量读取各文章次数，About 页面读取总阅读、最多阅读文章和公开评论数量；接口不可用时保留静态排版和占位值。

后端不属于本仓库，由服务器上的 `/home/lyy/services` 独立维护并每日备份。本仓库只维护浏览器端的 `/api/*` 调用和接口不可用时的降级体验。

## 构建与验证细节

`npm run verify` 会执行：

```bash
npm run typecheck
npm test
npm run build
node scripts/verify-build.mjs
```

验证脚本会检查：

- 首页、About、Notes、RSS、sitemap、JSON 索引存在。
- 每个非草稿的 `src/content/posts/**/*.mdx` 都生成对应的 `posts/*.html`。
- 文章检查按内容特征（公式、图片、封面、目录）挑选文章，不依赖具体文章名；增删、改名文章不会让验证失败。
- 数学文章加载本地 KaTeX CSS，非数学页面不加载 KaTeX。
- 文章图片有懒加载、解码和尺寸属性。
- 文章封面元数据使用 Astro 优化后的公开图片。
- 首页与文章页保留阅读次数挂载点，About 保留四项动态统计挂载点。
- 搜索索引和首页 HTML 没有超过体积预算。

## 发布流程

1. 修改 `src/` 下的源码或内容。
2. 运行 `npm run verify`。
3. 检查 `git status`，确认只有源码、配置和测试变更。
4. 提交并推送到 `main`。
5. GitHub 托管 Runner 验证该提交；通过后，自托管 Runner 调用服务器上的 `deploy.sh`，在临时 worktree 中重新构建并发布 `dist/`。
