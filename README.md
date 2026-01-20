# 纯静态 古早风 技术博客模板

说明（简体中文）

- 这是一个无需构建、可直接放到 GitHub Pages 的纯静态博客模板。
- 文章存放在 `posts/` 目录，每篇文章使用完整 HTML 文件并在 `<head>` 中添加以下 meta：
  - `<meta name="post-title" content="文章标题">`
  - `<meta name="post-date" content="YYYY-MM-DD">`
  - `<meta name="post-tags" content="标签1,标签2">`
  - `<meta name="post-excerpt" content="摘要（可选）">`

维护与部署

1. 新建或修改 `posts/*.html` 文件，确保 meta 存在。
2. 在 Windows 下运行：

   ```pwsh
   pwsh .\scripts\generate_index.ps1
   ```

   该脚本会先把每篇文章 `<head>` 中的 `post-tags` 写入正文（以链接到 `/index.html?tag=xxx` 的形式），随后扫描 `posts/` 目录并生成 `index.json`，并额外生成：
   - `tags.json`：按 tag 聚合的文章列表，便于客户端按 tag 加载或生成页面；
   - `rss.xml`：RSS（RSS 2.0）订阅源（支持通过 `SITE_URL` 环境变量或 `-SiteUrl` 参数设置站点根域名，以生成 RSS 中的绝对链接；否则生成相对链接）。

- 新增：`scripts/md2post.ps1`（Markdown -> post HTML）
  - 使用方法：在 `drafts/` 中创建一份带 YAML front matter 的 Markdown（title/date/tags/excerpt），然后运行：

    ```pwsh
    pwsh .\scripts\md2post.ps1 -Source .\drafts\your-post.md
    ```

  - 注意：若系统安装了 `pandoc`，脚本会优先使用其渲染（更完整的 Markdown/数学/代码支持）。脚本会自动检测：
    - 已在 PATH 中的 `pandoc`；或
    - 环境变量 `PANDOC_PATH`（可指向 `pandoc.exe` 的绝对路径）；或
    - 默认安装目录 `%LOCALAPPDATA%\Pandoc\pandoc.exe`（Windows 的 winget/installer 常见路径）。
      未检测到时使用保守的内置回退转换（支持行内代码、代码块、$...$ 与 $$...$$ 数学标记，生成的 HTML 会包含 MathJax 与 highlight.js）。

  - 文件命名规则：脚本会把生成的 HTML 写入 `posts/`，默认文件名格式为 **`YYYY-MM-DD-<slug>.html`**（例如 `2026-01-20-sample.html`）。
  - `slug` 优先从 YAML 的 `slug:` 字段读取（若提供），否则**默认使用源文件名**（`xxx.md` 的 `xxx`），并做规范化处理；脚本不再用 `title` 作为默认 slug。脚本会使用该日期来填充文章 `<meta name="post-date">`，并在正文中写入对应的 `日期 · 标签：...` 行。

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

  - 生成的文件会输出到 `posts/`，并自动更新 `index.json` / `tags.json` / `rss.xml`。

  例如（PowerShell）：

  ```pwsh
  $env:SITE_URL = 'https://yourdomain.com'
  pwsh .\scripts\generate_index.ps1
  # 或者直接传参：
  pwsh .\scripts\generate_index.ps1 -SiteUrl 'https://yourdomain.com'
  ```

  另外，仓库包含一个监控脚本 `scripts/watch_generate.ps1`，可持续监控 `posts/`：当你在 `posts/` 添加/修改 HTML 时，脚本会自动运行 `generate_index.ps1` 更新索引与 RSS：

  ```pwsh
  pwsh .\scripts\watch_generate.ps1
  ```

  在 VS Code 中也新增了任务：**Watch posts and generate index**，可以通过 **Terminal → Run Task…** 启动该任务以便在开发时自动同步索引与 RSS。

3. 将仓库推送到 GitHub，开启 Pages（通常使用 `main` 或 `gh-pages` 分支），站点即可上线。

说明：

- 本模板以最小依赖为目标。
- 如果你希望改为纯手动维护 `index.json`，也可以直接编辑该文件（节省自动化步骤）。

## VS Code 预览设置 🔧

- 推荐扩展：`ritwickdey.LiveServer`（Live Server）、`ecmel.vscode-html-css`（HTML/CSS 智能提示）、`formulahendry.auto-close-tag`（自动闭合标签）。在打开仓库后，VS Code 会提示安装这些工作区推荐扩展。
- 使用 Live Server：打开 `index.html`，点击右下角的 **Go Live**（或右键文件 -> **Open with Live Server**），浏览器会自动打开并在你保存文件时热重载。
- 如果你偏好不安装扩展：运行任务 -> 选择 **Start static server (Python)**（需要安装 Python）或 **Start static server (npx http-server)**（需要 Node.js），然后在浏览器打开 `http://localhost:8000`。
- 推荐设置：已为该工作区配置 `files.autoSave` 为 `afterDelay`（500ms），这样保存后 Live Server 会自动刷新；如不希望自动保存，可在设置中调整该项。

祝你写得愉快 🎉
