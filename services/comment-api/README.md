# PNC 评论 API（仓库内实现）

这是博客仓库中维护和测试的交互 API 实现，提供评论、文章阅读计数和站点聚合统计。浏览器始终通过博客域名的 `/api/*` 使用这些功能，不能直接访问 Directus。

当前正式运行的服务源码位于 `/home/lyy/services/pnc-comment-api`，由 `pnc-comment-api.service` 启动。修改本目录不会自动部署到该服务；需要采用此实现时，应显式更新生产服务源码、环境配置和服务单元。

## 必需配置

- `DIRECTUS_URL`
- `DIRECTUS_TOKEN` 或 `DIRECTUS_TOKEN_FILE`；也支持 `DIRECTUS_EMAIL` 与 `DIRECTUS_PASSWORD` 登录。
- `TURNSTILE_SITE_KEY` 或 `TURNSTILE_SITE_KEY_FILE`
- `TURNSTILE_SECRET_KEY` 或 `TURNSTILE_SECRET_KEY_FILE`
- `COMMENT_HASH_SECRET` 或 `COMMENT_HASH_SECRET_FILE`
- `SITE_ORIGIN`
- `BLOG_INDEX_FILE`：已部署站点的 `index.json`，用于限制可计数的文章并解析标题。
- `VIEW_TIME_ZONE`：阅读次数按天去重使用的时区，默认 `Asia/Shanghai`。

优先使用权限最小的 `DIRECTUS_TOKEN`。该身份需要读取与创建 `post_views`、读取 `comments` 的权限。无论使用哪种认证方式，浏览器都不得获得 Directus 凭据或写权限。

## 接口

- `GET /healthz`：仅检查 Node 进程可响应。
- `GET /config`：返回公开 Turnstile site key。
- `GET /comments?slug=/posts/<slug>.html`：最多返回 100 条可见评论。
- `POST /comments`：校验输入、内存限流和 Turnstile 后创建评论。
- `GET /views`：批量读取全部文章的阅读次数；传入 `slug` 时只读取单篇文章。
- `POST /views`：记录一次按访客和日期去重的阅读，并返回文章累计次数。
- `GET /stats`：返回总阅读次数、最多阅读文章与公开评论数量。

`pnc-blog/server.mjs` 会把公开 `/api/*` 去掉 `/api` 前缀后转发到这些内部路径。

## 限制与隐私

- 用户名 1–40 字符，正文 1–1200 字符，请求体最大 16 KiB。
- 默认同 IP hash + 文章 10 分钟最多 3 次；限流保存在单进程内存，重启后清空。
- 同一 IP + User-Agent、同一文章、同一天只记录一次阅读。
- 只保存 IP 和 User-Agent 派生的 HMAC-SHA256，不保存原始 IP。
- `COMMENT_HASH_SECRET` 轮换后，新旧 hash 不可直接关联。
- `healthz` 不会验证 Directus 写权限或 Turnstile 可用性，发布服务变更后仍需进行真实读写检查。
