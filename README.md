# PNC's Blog

一个用 Astro + MDX 维护的静态个人博客。仓库只保存可重建的源码、配置、测试和部署入口；`dist/` 及其他静态构建产物不提交到 Git。

站点保留了原来的古早风布局和旧文章 URL，同时使用 Astro 的内容集合、图片管线、静态路由、全文 RSS、sitemap、全文搜索索引和按需加载的本地 KaTeX。本机部署还通过同源 `/api/*` 提供评论、按日去重的文章阅读次数与 About 动态站点统计。

阅读体验：首页文章按年份分组；搜索和标签筛选在手机上同样可用（标签是一行可横向滑动的列表），筛选时列表顶部会显示当前条件、命中篇数和“清除筛选”按钮。主题默认跟随系统的深浅色设置，手动切换后会记住选择。文章正文的链接使用链接色和浅下划线；图片可以点击放大（`Esc` 或点击任意处关闭）；长文章向下滚动后，右下角会出现“回到顶部”按钮。文章和每条随想的分享按钮旁有「观测」按钮：每位读者对每篇只能观测一次，显示累计次数。它依赖服务器上的计数 API（参考实现在 `ops/reactions-api/`，启用步骤见 [SERVER.md](SERVER.md) 第 9 节），API 不可用时按钮完全隐藏。

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
- `npm run test:404`：构建后启动临时预览服务，用 Chromium 运行 `tests/browser/` 下的全部浏览器测试：404 的桌面/手机截图、画布像素、动画交互和降级表现，Logo 与 Wordmark，320/375px 无横向溢出，首页筛选与清除，明暗主题，文章灯箱和回到顶部。首次运行需要 `npx playwright install chromium`。

## 404 彩蛋

`src/pages/404.astro` 构建为 `dist/404.html`，可在本地直接访问 `/404.html`。终端框中的 Three.js 场景把不同深度的零件正交投影成 `404`，旋转后通过 68 字符的灰度滤镜显示错位结构。画面只显示等宽字符，不叠加实体面底色或棱线；正侧面使用独立材质，字符网格保留桌面 6px、手机 4.5px 的尺寸，终端栏文字为 13–15px。支持暂停、重新对齐、鼠标微偏转和明暗主题，状态栏区分运行、对齐和暂停。每圈在正面停留 1.5 秒，期间及接近正面时禁用鼠标偏转，其余旋转仅在起止处短暂缓动。场景脚本只在 404 页面加载。

系统开启“减少动态效果”时默认静止；没有 JavaScript 或 WebGL 时显示静态字符画，返回首页链接仍然可用。动画脚本放在文档 head 中提前发现；同一页面只初始化一次，Astro 页面切换时释放并重建场景。测试截图保存在被 Git 忽略的 `test-results/`。

生产静态服务器将 `/404` 和其他未找到的静态请求交给 `404.html`，保留 HTTP 404 状态和原始地址，并使用 `Cache-Control: no-store`，避免 CDN 缓存过期的缺页结果。直接访问 `/404.html` 仍返回 200。此行为由服务器上的 Caddy 实现（见 SERVER.md 第 2 节），Astro 的本地预览测试不能代替生产路由验证。

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

正文支持 Markdown（含表格、删除线）、代码高亮（随明暗主题切换）、相对路径图片、`$...$` / `$$...$$` 数学公式，以及直接写 HTML（如 `<details>`、`<kbd>`、`<video>`）。

文章里无需 import 即可使用这些组件（定义在 `src/components/mdx/`，新增组件在其 `index.ts` 中注册）：

- `<Figure caption="说明">![alt](./a.jpg)</Figure>`：带说明文字的图片。
- `<Compare labels={["之前", "之后"]}>` + 相邻两行图片：左右对比图，每张图下方显示对应标签；手机上上下排列，标签仍跟着各自的图片。
- `<Compare mode="slider" initial={50} labels={["之前", "之后"]}>` + 两张同视角、同比例的图：卷帘对比。拖动分割线（鼠标、触控板、单指横滑）、点击跳转，或聚焦手柄后用 ←/→（Shift 加大步长）、Home/End 调整。显示宽度不超过较小那张图，低分辨率渲染不会被拉糊；两张图的标签显示在左上角和右上角，分割线靠近时自动淡出。没有 JavaScript、或图片不是正好两张时退回并排展示。滑块里的图片不会触发点击放大。
- `<Note>` / `<Note type="tip">` / `<Note type="warn" title="标题">`：提示框。

完整示例见草稿 `src/content/posts/2026-01-20-sample/index.mdx`，运行 `npm run dev` 后访问 `/posts/2026-01-20-sample.html`。

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

每天随想写在 `src/content/notes.md`。最快的发法：

```bash
npm run note -- "今天的想法，支持 **Markdown**" "可选标题"
```

它会在文件最上方加一段今天日期的随想；提交并推送即发布。也可以直接手写（手机上用 GitHub App 编辑这个文件也行）：

```md
## 2026-04-07 | 可选标题
这里写正文，支持完整 Markdown（列表、引用、代码、链接）。
```

构建会生成：

- `notes.html`：随想时间线。每条随想在站内的位置是 `notes.html#YYYY-MM-DD`（同一天多条依次为 `-2`、`-3`，新加的随想不会改变已有链接），点日期即可跳到。
- `notes/YYYY-MM-DD.html`：只用于分享的小页面，带这条随想自己的标题和摘要，供微信外的聊天软件、社交网站生成预览；浏览器打开后立即跳到 `notes.html#…`。站内不链接它，也不进 sitemap 和搜索引擎，「分享」按钮给出的就是这个链接。
- `notes.json`：首页最近随想预览数据。

`npm run dev` 运行时修改 `notes.md`，页面会自动刷新。

## 修改外观

整站外观集中在 `src/styles/global.css` 顶部的设计变量里：

- `--bg` / `--panel` / `--text` / `--muted` / `--border` / `--link`：颜色（下方 `[data-theme="dark"]` 是深色版本）。
- `--font-sans` / `--font-serif` / `--font-body` / `--font-mono`：界面、标题、文章正文、代码字体。正文是宋体 + Times New Roman：苹果设备用系统自带的宋体-简和 Times New Roman，其他平台自动下载自托管的思源宋体（Noto Serif SC，按字符分片，只下载页面用到的字）和 Tinos（与 Times New Roman 等宽的免费字体），见 `src/styles/reading-font.css`。标题英文用自托管的 Source Serif 4（`@fontsource-variable/source-serif-4`），中文回退到系统宋体；不依赖 Google Fonts。
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

本仓库是博客文章、随想、About 和前端实现的权威来源。服务器上的实际部署、配置文件、回滚和排障方法见 [SERVER.md](SERVER.md)。下面是概要（2026-11-21 起）：

- `pnc-blog.service` 运行 **Caddy**，只监听 `127.0.0.1:46213`，由 Cloudflare Tunnel 接入。它直接托管 `/home/lyy/services/pnc-blog/current`（指向 `releases/` 中的某个 release）：按 `Accept-Encoding` 返回预压缩的 `.br` / `.gz` 旁文件；哈希资源一年不可变缓存，并带 `no-transform`，避免 CDN 重复压缩；`/404` 和缺失路径返回 `404.html`，状态码保持 404，带 `Cache-Control: no-store`。
- `/api/*` 由 Caddy 转发给 `pnc-api.service`（`127.0.0.1:46219`）：一个零依赖的 Node 进程，评论和阅读记录存在单个 SQLite 文件里，每天在线备份。它取代了原来的 Directus 和 `pnc-comment-api`，接口不变。阅读次数按同一 IP + User-Agent、同一文章、同一天只计一次。文章页负责记录阅读，首页批量读取各文章次数，About 页面读取总阅读、最多阅读文章和公开评论数量；接口不可用时保留静态排版和占位值。
- `/api/reactions*` 由 Caddy 转发给 `pnc-reactions.service`（`127.0.0.1:46220`）：文章和随想「观测」按钮的计数服务，代码就是本仓库的 `ops/reactions-api/`，部署和防刷设计见 SERVER.md 第 9 节。
- 部署完全不使用 GitHub 托管的算力：push 到 `main`（或在 Actions 页面手动运行 workflow）后，标签为 `pnc-blog` 的本机 self-hosted Runner 调用 `deploy.sh`，在临时 worktree 里执行一次 `npm run verify`（验证和构建一起完成），然后发布这次的产物。`releases/` 保留最近 5 个版本，`deploy.sh --rollback` 一秒内就能切回上一个版本；手动运行 workflow 时也可以在 `deploy_sha` 中填写 `main` 上的历史提交。健康检查覆盖首页、文章、自定义 404 的内容和状态码、RSS 以及 `/api/*`，失败会自动回滚。

因为仓库是公开的，workflow 不响应 `pull_request`。但 fork 的 PR 可以自带 workflow 文件，所以另有两道保护：外部贡献者的 PR 运行必须经维护者批准；服务器上 Runner 的作业守卫只放行本仓库 `main` 上的 push、手动、定时和 `workflow_run` 作业（见 SERVER.md 5.2）。PR 不会自动跑检查，合并前请在本地执行 `npm run verify`。

安全检查：开启了 secret scanning（含 push protection）、Dependabot 告警、私密漏洞报告和 `main` 分支保护；`.github/workflows/security.yml` 在每次部署后和每周一在自托管 Runner 上跑 CodeQL 与 `npm audit`。报告漏洞的方式见 [SECURITY.md](SECURITY.md)。

服务器端的配置和脚本（Caddyfile、`deploy.sh`、`pnc-api`、systemd 单元、作业守卫）保存在服务器上的 `/home/lyy/services/pnc-infra`（本地 git 仓库），不在本仓库里；SERVER.md 收录了它们的当前版本。

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
- 首页在手机上保留搜索和标签、有筛选状态条、文章按年份倒序分组，主题按钮位于站点标题行。
- About 的写作年份图从最新年份逐年连续排到建站年份，没有空缺。
- RSS 包含全部文章的全文（`content:encoded`），链接和图片都是绝对地址，引用的 `_astro` 资源确实存在。
- `_astro` 只输出 woff2 字体，不输出 woff。

### 依赖固定说明

`package.json` 的 `overrides` 里有两项：

- `katex`：让 `rehype-katex` 使用与本站相同的 KaTeX 版本（0.19，修复了 0.16 的安全告警）。升级 KaTeX 后运行 `node scripts/generate-katex-css.mjs` 重新生成 `src/styles/katex-local.css`。
- `@astrojs/compiler-binding`：固定在 0.5.2。0.5.3 的 Linux 原生二进制需要 AVX2，生产服务器的 Xeon E5-2450 v2 不支持，构建会直接以 `Illegal instruction`（退出码 132）崩溃；0.5.2 与 0.5.3 的 JS 和类型完全相同，只有原生文件不同。升级 astro 后，先在服务器上确认新版本能构建，再去掉这项。

## 发布流程

1. 修改 `src/` 下的源码或内容。
2. 运行 `npm run verify`。
3. 检查 `git status`，确认只有源码、配置和测试变更。
4. 提交并推送到 `main`。
5. 自托管 Runner 调用服务器上的 `deploy.sh`，在临时 worktree 中验证、构建并发布 `dist/`（全程不使用 GitHub 托管 Runner）。
