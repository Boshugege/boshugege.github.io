# 纯静态 古早风 技术博客模板

说明（简体中文）

- 这是一个无需构建、可直接放到 GitHub Pages 的纯静态博客模板。
- 文章存放在 `posts/` 目录，每篇文章使用完整 HTML 文件并在 `<head>` 中添加以下 meta：
  - `<meta name="post-title" content="文章标题">`
  - `<meta name="post-date" content="YYYY-MM-DD">`
  - `<meta name="post-tags" content="标签1,标签2">`
  - `<meta name="post-excerpt" content="摘要（可选）">`
  - `<meta name="post-cover" content="封面图路径或 URL（可选）">`（未提供时回退到 `assets/img/icon.jpg`）

维护与部署

1. 新建或修改 `posts/*.html` 文件，确保 meta 存在。
2. 推荐使用统一 Python 脚本（`scripts/blog_builder.py`）：

   ```bash
   # 只重建站点（不处理 drafts）
   python .\scripts\blog_builder.py
   ```

   或一键把 `drafts/*.md` 全部转成文章并重建：

   ```bash
   python .\scripts\blog_builder.py --from-drafts
   ```

   该脚本会生成以下产物：
   - `index.html`：预渲染首页（无 JS 也可浏览）；
   - `archive.html`：全量归档页；
   - `index.json` 与 `search.json`：前端增强用数据；
   - `rss.xml`：RSS（RSS 2.0）订阅源；
   - `sitemap.xml`：站点地图。
   同时会根据 `SITE_URL`（或 `--site-url`）生成 canonical/OG/RSS/sitemap 的绝对链接；若未设置，会优先读取仓库根目录 `CNAME`。
   你可以直接编辑 `scripts/blog_builder.py` 顶部的变量来改站点名称、描述、个人信息、sitemap 策略等。

- `scripts/blog_builder.py`（Markdown -> post HTML + 全站刷新）
  - 使用方法：在 `drafts/` 中创建带 YAML front matter 的 Markdown（title/date/tags/excerpt/cover/slug），然后运行：

    ```bash
    python .\scripts\blog_builder.py --from-drafts
    ```

  - 注意：若系统安装了 `pandoc`，脚本会优先使用其渲染（更完整的 Markdown/数学/代码支持）。脚本会自动检测：
    - 已在 PATH 中的 `pandoc`；或
    - 环境变量 `PANDOC_PATH`（可指向 `pandoc.exe` 的绝对路径）；或
    - 默认安装目录 `%LOCALAPPDATA%\Pandoc\pandoc.exe`（Windows 的 winget/installer 常见路径）。
      未检测到时使用保守的内置回退转换（支持行内代码、代码块、$...$ 与 $$...$$ 数学标记，生成的 HTML 会包含 MathJax 与 highlight.js）。

  - 文件命名规则：脚本会把生成的 HTML 写入 `posts/`，默认文件名格式为 **`YYYY-MM-DD-<slug>.html`**（例如 `2026-01-20-sample.html`）。
  - `slug` 优先从 YAML 的 `slug:` 字段读取（若提供），否则默认使用源文件名（`xxx.md` 的 `xxx`）。
  - 支持可选 `cover:` 字段（例如 `cover: "assets/img/your-cover.jpg"` 或绝对 URL）。文章页 `og:image` / `twitter:image` 优先使用该封面；缺省时自动回退为 `assets/img/icon.jpg`。

  - 如果你通过 `winget` 安装并见到 Pandoc 在 `C:\Users\<you>\AppData\Local\Pandoc`：
    - 临时验证（在 PowerShell 中运行）：

      ```pwsh
      & 'C:\Users\boshu\AppData\Local\Pandoc\pandoc.exe' --version
      ```

    - 永久可用（推荐）：将 Pandoc 所在目录加入系统 PATH，或设置环境变量：
      ```pwsh
      setx PANDOC_PATH "C:\Users\boshu\AppData\Local\Pandoc\pandoc.exe"
      ```
      设置后重新打开终端以让环境变量生效，然后运行 `pandoc --version` 验证。

  - 生成的文件会输出到 `posts/`，并自动触发整站刷新（`index.html` / `archive.html` / `index.json` / `search.json` / `rss.xml` / `sitemap.xml`）。

  例如（PowerShell）：

  ```pwsh
  $env:SITE_URL = 'https://yourdomain.com'
  python .\scripts\blog_builder.py --from-drafts
  # 或者直接传参：
  python .\scripts\blog_builder.py --from-drafts --site-url 'https://yourdomain.com'
  ```

3. 将仓库推送到 GitHub，开启 Pages（通常使用 `main` 或 `gh-pages` 分支），站点即可上线。

说明：

- 本模板以最小依赖为目标。
- `scripts/blog_builder.py` 是统一入口（支持“转换 drafts + 构建站点”一体化）。
- 已提供 `manifest.webmanifest` 与 `robots.txt`（默认 sitemap 指向 `https://parityncsvt.top/sitemap.xml`，如换域名请同步修改）。

## VS Code 预览设置 🔧

- 推荐扩展：`ritwickdey.LiveServer`（Live Server）、`ecmel.vscode-html-css`（HTML/CSS 智能提示）、`formulahendry.auto-close-tag`（自动闭合标签）。在打开仓库后，VS Code 会提示安装这些工作区推荐扩展。
- Run and Debug 一键转换：选择配置 **Drafts -> Posts -> Build (Python)**，点击运行按钮即可把 `drafts/*.md` 全量转为 `posts/*.html` 并重建全站。
- 使用 Live Server：打开 `index.html`，点击右下角的 **Go Live**（或右键文件 -> **Open with Live Server**），浏览器会自动打开并在你保存文件时热重载。
- 如果你偏好不安装扩展：运行任务 -> 选择 **Start static server (Python)**（需要安装 Python）或 **Start static server (npx http-server)**（需要 Node.js），然后在浏览器打开 `http://localhost:8000`。
- 推荐设置：已为该工作区配置 `files.autoSave` 为 `afterDelay`（500ms），这样保存后 Live Server 会自动刷新；如不希望自动保存，可在设置中调整该项。

祝你写得愉快 🎉

## 每天随想（单文件增量写作）

- 你可以把每天随想都写在仓库根目录的 `notes.md`（单个文件，持续追加）。
- 推荐格式：

  ```md
  ## 2026-04-07 | 可选标题
  这里写正文，可以多段。
  ```

- 生成页面与首页预览数据：

  ```bash
  python scripts/build_notes_page.py
  ```

- 该命令会生成：
  - `notes.html`：时间线页面（单页浏览全部随想）；
  - `notes.json`：首页“最近随想”预览数据。
