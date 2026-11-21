# SERVER.md：博客在 `personal` 上的运维手册

本文记录博客生产环境的**实际状态**（2026-11-21 完成瘦身并上线），以及日常操作、排障和回滚方法。文中嵌入的配置和脚本与服务器上 `/home/lyy/services/pnc-infra`（本地 git 仓库）里的文件逐字一致。改动时先改那边并提交，再同步到这里。

服务器还有一份全机手册 `~/SERVER.md` 和服务清单 `~/services.yaml`，监控、健康检查、公网探测和漂移告警都由那份清单驱动。**新增或移除任何 unit 或端口，先改 `~/services.yaml`**，否则约 10 分钟后会触发 `InventoryDrift` 告警。

---

## 0. 速览

| 组件 | 现在 | 瘦身前 |
| --- | --- | --- |
| 静态站 | `pnc-blog.service` → **Caddy 2.6.2**，`127.0.0.1:46213` | 同名服务跑自写的 `server.mjs` |
| 评论、阅读数、统计 | `pnc-api.service` → **Node + `node:sqlite`**，`127.0.0.1:46219`，数据在一个文件 `pnc.db` | `pnc-comment-api`（46215）→ Directus 容器 `pnc-cms`（46214）→ Directus 的 SQLite |
| 「观测」计数 | `pnc-reactions.service` → Node + `node:sqlite`，`127.0.0.1:46220`，数据 `reactions.db`（第 9 节） | 无 |
| 私人 Bot | `pnc-bot.service` → Python 3.13，Telegram 长轮询，不监听端口：发随想和草稿、评论通知、运维命令（第 10 节） | 无 |
| 部署 | Runner 调用 `deploy.sh`：临时 worktree 里 `npm ci` + `npm run verify`，**只构建一次**；保留最近 5 个 release | GitHub 托管 Runner 和服务器各构建一次；部署成功后删除所有旧 release |
| 回滚 | `deploy.sh --rollback`，实测 **0.3 秒** | 只能重新构建旧提交 |
| 备份 | `pnc-api-backup.timer`（04:30）和 `pnc-reactions-backup.timer`（04:45）每日在线备份两个数据库，保留 30 天 | 只有全机的 btrbk 快照 |
| 安全检查 | secret scanning + push protection、Dependabot、私密漏洞报告、`main` 分支保护、外部贡献者审批、自托管 CodeQL + npm audit、Runner 作业守卫 | 全部未开启 |
| 常驻内存 | Caddy + 一个 Node 进程 | `server.mjs` + `pnc-comment-api` + Directus 容器 |

```text
浏览器 ──HTTPS──► Cloudflare ──Tunnel (cloudflared)──► 127.0.0.1:46213  Caddy（pnc-blog.service）
                                                        ├─ file_server：pnc-blog/current → releases/<时间>-<SHA>
                                                        │    · .br / .gz 旁文件优先；/_astro/* 一年 immutable
                                                        │    · /404 与缺失路径 → 404.html，HTTP 404 + no-store
                                                        ├─ /api/reactions* ──► 127.0.0.1:46220  pnc-reactions（观测计数）
                                                        │                        └─ ~/services/pnc-reactions/data/reactions.db
                                                        └─ /api/*（去掉前缀）──► 127.0.0.1:46219  pnc-api（pnc-api.service）
                                                                                    └─ ~/services/pnc-api/data/pnc.db

push main / 手动运行 ──► 本机 Runner（pnc-github-actions-runner.service，作业守卫先检查）
    ├─ Verify and Deploy Site ──► deploy.sh：worktree → npm ci → npm run verify → 原子切换 → 健康检查 → 失败回滚
    └─ 部署结束后：Security Checks ──► CodeQL + npm audit（每周一也跑一次；pnc-bot 的提交跳过）

手机 Telegram 私聊 ──► pnc-bot.service（长轮询，经 mihomo）──► 专用 clone → git push（Deploy Key）→ 上面的部署流程
                                     └─► pnc.db（新评论通知、隐藏）、systemctl / journalctl、systemd-run deploy.sh
```

### 0.1 目录与端口

```text
/home/lyy/services/
├── pnc-infra/                  # 本地 git 仓库：Caddyfile、deploy.sh、作业守卫、api/、reactions/、bot/、systemd/、oneoff/
├── pnc-blog/
│   ├── deploy.sh -> ../pnc-infra/deploy.sh      # workflow 调用的路径不变
│   ├── repo/                   # 博客仓库的克隆，deploy.sh 从这里建临时 worktree
│   ├── releases/               # 最近 5 个构建产物
│   ├── current -> releases/…   # Caddy 的站点根目录
│   ├── logs/                   # 每次部署 / 回滚的完整日志（保留 30 个）
│   ├── state/                  # last-deploy.json、deploys.jsonl（Bot 和排障用）
│   ├── backups/pre-caddy-*/    # 切换前的 unit 文件
│   └── server.mjs, deploy.sh.pre-20261121       # 旧版，2026-12-05 后删除
├── pnc-api/                    # 0700
│   ├── .env                    # 0600，密钥（沿用旧评论 API 的 Turnstile 密钥和 HMAC 密钥）
│   ├── data/pnc.db             # 评论与阅读记录
│   └── backups/                # 每日备份 pnc-YYYYMMDD-HHMMSS.db.gz
├── pnc-reactions/              # 0700：server.mjs、lib.mjs、schema.sql（来自 pnc-infra/reactions）、.env（0600）、data/、backups/
├── pnc-bot/repo/               # Telegram Bot 专用的博客仓库 clone（Deploy Key 推送）；配置在 ~/.config/pnc-bot/，状态在 ~/.local/state/pnc-bot/
├── pnc-comment-api/            # 已停用（0700），2026-12-05 后删除
├── pnc-cms/                    # Directus 数据（已停用），2026-12-05 后归档并删除
└── actions-runner-pnc-blog/    # GitHub Actions Runner（.env 里配置作业守卫）
```

| 端口（127.0.0.1） | 用途 |
| --- | --- |
| 46213 | Caddy（博客，cloudflared 的上游） |
| 46219 | pnc-api |
| 46220 | pnc-reactions（观测计数） |
| 46214 / 46215 | 原 Directus / 原评论 API，已释放 |
| 2019 | naive-relay 容器里另一个 Caddy 的管理接口，与博客无关。博客的 Caddy 用 unix socket |

所有服务都是 `lyy` 的**用户级** systemd 单元（`systemctl --user …`，已开启 linger）。

---

## 1. 为什么瘦身

1. **自写静态服务器 `server.mjs`**：MIME 类型、`Accept-Encoding` 协商、预压缩旁文件、缓存头、404 状态码都是自己维护。Caddy 用约 50 行配置就能实现同样的行为，切换前逐项对比过（见 2.2）。
2. **两张表用三层服务**：Directus 只存评论和 `post_views` 两张表，却要跟一整套 CMS 的升级；`pnc-comment-api` 又是一层翻译。现在由一个约 300 行、零依赖的 `pnc-api` 直接读写 SQLite。
3. **重复构建**：GitHub 托管 Runner 构建的产物从来没有被发布过。现在只在服务器上构建一次。
4. **无法秒级回滚**：旧 `deploy.sh` 发布成功后删除所有旧 release。

---

## 2. Caddy（`pnc-blog.service`）

Caddy 来自 Debian 包 `caddy` 2.6.2，有安全更新时随 `apt upgrade` 一起升级。包自带的系统级 `caddy.service` 和 `caddy-api.service` 已经 `mask`，不会去占 80 端口或读取 `/etc/caddy/Caddyfile`。博客只用 `/usr/bin/caddy` 这个程序，由下面的用户单元启动。

### 2.1 `Caddyfile`

```caddyfile
# PNC's Blog: static site + /api proxy (replaces pnc-blog's server.mjs).
# cloudflared → 127.0.0.1:46213, plain HTTP on loopback; TLS ends at Cloudflare.
# Written for Debian's caddy 2.6; check with:
#   caddy validate --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile
{
	# Port 2019 belongs to another Caddy on this host; use a private socket.
	admin unix//run/user/1000/pnc-caddy/admin.sock
	auto_https off
}

http://:46213 {
	bind 127.0.0.1
	root * /home/lyy/services/pnc-blog/current

	header {
		X-Content-Type-Options nosniff
		-Server
	}

	# 0. 观测 counter (pnc-reactions accepts the /api prefix itself). handle
	#    sorts before handle_path, so this wins over the /api/* block below.
	handle /api/reactions* {
		reverse_proxy 127.0.0.1:46220
	}

	# 1. Comments, views and stats. The API's routes have no /api prefix.
	handle_path /api/* {
		reverse_proxy 127.0.0.1:46219
	}

	# 2. /404 is the error page with a real 404 status (/404.html itself is 200).
	handle /404 {
		error 404
	}

	# 3. Files from the current release (read-only: GET and HEAD).
	handle {
		@write not method GET HEAD
		header @write Allow "GET, HEAD"
		respond @write "Method Not Allowed" 405
		@hashed path /_astro/* /assets/*
		header @hashed Cache-Control "public, max-age=31536000, immutable, no-transform"
		header ?Cache-Control "public, max-age=60"
		# JS/CSS have .br/.gz variants: every response varies by encoding.
		@compressible path *.js *.css
		header @compressible {
			Vary Accept-Encoding
			defer
		}
		@xml path *.xml
		header @xml Content-Type "application/xml; charset=utf-8"
		@manifest path *.webmanifest
		header @manifest Content-Type "application/manifest+json; charset=utf-8"
		file_server {
			# dist/_astro ships .br / .gz sidecars for JS and CSS.
			precompressed br gzip
		}
	}

	# Missing files and /404: the custom page, HTTP 404, never cached by the CDN.
	handle_errors {
		header {
			X-Content-Type-Options nosniff
			Cache-Control no-store
			-Server
		}
		@notFound expression {err.status_code} == 404
		handle @notFound {
			rewrite * /404.html
			file_server
		}
		handle {
			respond "{err.status_code} {err.status_text}"
		}
	}
}
```

`systemd/pnc-blog.service`（`~/.config/systemd/user/pnc-blog.service` 是指向它的链接；旁边 `pnc-blog.service.d/10-hardening.conf` 里的沙箱设置仍然生效）：

```ini
[Unit]
Description=PNC's Blog static site (Caddy)
After=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/caddy run --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile
Restart=always
RestartSec=3
RuntimeDirectory=pnc-caddy
StateDirectory=pnc-caddy
Environment=XDG_DATA_HOME=%S/pnc-caddy XDG_CONFIG_HOME=%S/pnc-caddy
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
MemoryMax=512M

[Install]
WantedBy=default.target
```

### 2.2 与 `server.mjs` 的对比结果

切换前，Caddy 先在临时端口上和 `server.mjs` 并行运行，对 28 个路径分别用 `br, gzip` 和 `identity` 两种编码请求，共比对 56 项：状态码、`Content-Type`、`Cache-Control`、`Content-Encoding`、`Vary`、`X-Content-Type-Options` 和响应体哈希。结果：

- 所有响应体逐字节一致。404 的状态码和 `no-store`、`/404.html` 的 200、预压缩旁文件的选择、哈希资源的一年缓存都一致。
- 有意保留的差异：
  - `/_astro/*` 和 `/assets/*` 统一带 `no-transform`（旧版只在压缩响应上带）。这些资源都是已优化的图片、字体或预压缩文件，效果相同。
  - `robots.txt` 返回 `text/plain`（旧版误返回 `application/octet-stream`）。
  - API 响应多了 `X-Content-Type-Options: nosniff`。
- 切换时补上的行为：静态路径只允许 GET 和 HEAD，其他方法返回 405；`.xml` 用 `application/xml`；JS 和 CSS 不论是否压缩都带 `Vary: Accept-Encoding`；错误页也带 `nosniff`。

### 2.3 常用操作

```bash
caddy validate --config ~/services/pnc-infra/Caddyfile --adapter caddyfile   # 改完先校验
systemctl --user reload pnc-blog      # 热重载（经 unix socket 管理接口）
journalctl --user -u pnc-blog -f
curl -sI http://127.0.0.1:46213/404 | head -n 5   # 应为 404 + cache-control: no-store
```

发布新 release 不需要重载 Caddy：`root` 指向 `current` 符号链接，每个请求都会重新解析路径。

---

## 3. 评论与统计 API（`pnc-api.service`）

### 3.1 接口（与旧评论 API 完全一致，Caddy 去掉 `/api` 前缀后转发）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/healthz` | — | `{ "ok": true }` |
| `GET /api/config` | — | `{ "turnstileSiteKey": "…" }` |
| `GET /api/comments?slug=/posts/x.html` | — | `{ comments: [{ id, author_name, content, browser_name, browser_version, os_name, os_version, country_code, created_at }] }`，只含 `visible`，按时间升序，最多 100 条 |
| `POST /api/comments` | `{ slug, authorName, content, turnstileToken }` | `201 { ok: true }`；出错返回 4xx 和 `{ "error": "中文提示" }`（频率超限 429） |
| `GET /api/views` | — | `{ views: { "/posts/x.html": 12, … } }`，只含当前 release 里存在的文章 |
| `GET /api/views?slug=…` | — | `{ views: 12 }` |
| `POST /api/views` | `{ slug }` | `{ views, counted }`：同一 IP + User-Agent、同一文章、同一天（Asia/Shanghai）只计一次 |
| `GET /api/stats` | — | `{ totalViews, mostViewed: { slug, title, views } \| null, commentCount }` |

迁移前，新旧两套 API 读同一份数据，逐个接口对比过响应（包括错误响应的状态码和文案），结果完全一致：总阅读 357、最多阅读的文章 49 次、2 条评论。

和旧版相比的改进：

- 评论频率限制仍是“同一 IP、同一文章，每 10 分钟最多 3 次尝试”，超限现在返回 429（旧版返回 400）。
- 阅读记录新增一道上限：同一 IP（IPv6 按 /64 合并）、同一文章、每天最多 5 条（`VIEW_LIMIT_PER_ADDRESS`）。User-Agent 由客户端自报，没有这道上限的话，脚本换着 UA 就能把阅读数刷上去。
- Turnstile 校验超时（10 秒）会返回 503 和提示，旧版会挂住请求。

### 3.2 `schema.sql`

列名沿用 Directus 的集合，迁移时可以直接 `INSERT … SELECT`：

```sql
-- pnc-api: comments and reading records for PNC's Blog in one SQLite file.
-- Columns match the former Directus collections, so old rows import as-is.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS comments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  post_slug       TEXT NOT NULL,              -- site path, e.g. /posts/arch-linux.html
  author_name     TEXT NOT NULL,
  content         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'visible',  -- only 'visible' is shown; use 'hidden' to take one down
  browser_name    TEXT,
  browser_version TEXT,
  os_name         TEXT,
  os_version      TEXT,
  country_code    TEXT,                       -- from Cloudflare's CF-IPCountry
  referer         TEXT,
  ip_hash         TEXT,                       -- HMAC(ip); the raw IP is never stored
  user_agent_hash TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_by_post ON comments (post_slug, status, created_at);

-- One row per reader, post and day (Asia/Shanghai): view_key is
-- HMAC(slug \n visitor_hash \n day), so the UNIQUE constraint is the
-- "count once a day" rule.
CREATE TABLE IF NOT EXISTS post_views (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  post_slug    TEXT NOT NULL,
  view_key     TEXT NOT NULL UNIQUE,
  visitor_hash TEXT NOT NULL,
  viewed_on    TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  ip_hash      TEXT                         -- HMAC(ip, IPv6 cut to /64): caps rows per address
);
CREATE INDEX IF NOT EXISTS post_views_by_post ON post_views (post_slug);
```

### 3.3 `server.mjs`

```js
// pnc-api: comments, reading counts and site stats for PNC's Blog.
// One process, one SQLite file (node:sqlite), no dependencies, Node >= 22.13.
// Same HTTP contract as the former Directus-backed comment API: Caddy strips
// the /api prefix, so routes are /healthz, /config, /comments, /views, /stats.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const env = process.env;
// Secrets may come from the environment or from NAME_FILE.
const setting = (name, fallback = "") => {
  if (env[name]) return String(env[name]).trim();
  if (env[`${name}_FILE`]) return fs.readFileSync(env[`${name}_FILE`], "utf8").trim();
  return fallback;
};

const config = {
  host: env.HOST || "127.0.0.1",
  port: Number.parseInt(env.PORT || "46219", 10),
  dbPath: env.DB_PATH || "/home/lyy/services/pnc-api/data/pnc.db",
  siteOrigin: (env.SITE_ORIGIN || "https://parityncsvt.top").replace(/\/$/, ""),
  blogIndexFile: env.BLOG_INDEX_FILE || "/home/lyy/services/pnc-blog/current/index.json",
  viewTimeZone: env.VIEW_TIME_ZONE || "Asia/Shanghai",
  turnstileSiteKey: setting("TURNSTILE_SITE_KEY"),
  turnstileSecretKey: setting("TURNSTILE_SECRET_KEY"),
  hashSecret: setting("COMMENT_HASH_SECRET"),
  rateLimitWindowMs: Number.parseInt(env.COMMENT_RATE_LIMIT_WINDOW_MS || "600000", 10),
  rateLimitMax: Number.parseInt(env.COMMENT_RATE_LIMIT_MAX || "3", 10),
  // Reading records one address may add per post and day. The User-Agent is
  // client-chosen, so without a cap a script could inflate counts at will.
  viewLimitPerAddress: Number.parseInt(env.VIEW_LIMIT_PER_ADDRESS || "5", 10),
};
if (!config.hashSecret) throw new Error("COMMENT_HASH_SECRET is required");

fs.mkdirSync(path.dirname(config.dbPath), { recursive: true });
const db = new DatabaseSync(config.dbPath);
db.exec("PRAGMA busy_timeout = 5000; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;");
db.exec(fs.readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
// Databases created before post_views.ip_hash existed get the column here.
if (!db.prepare("PRAGMA table_info(post_views)").all().some((column) => column.name === "ip_hash")) {
  db.exec("ALTER TABLE post_views ADD COLUMN ip_hash TEXT");
}
db.exec("CREATE INDEX IF NOT EXISTS post_views_by_address ON post_views (post_slug, viewed_on, ip_hash)");

const sql = {
  comments: db.prepare(`SELECT id, author_name, content, browser_name, browser_version, os_name, os_version,
    country_code, created_at FROM comments WHERE post_slug = ? AND status = 'visible' ORDER BY created_at, id LIMIT 100`),
  addComment: db.prepare(`INSERT INTO comments (post_slug, author_name, content, status, browser_name, browser_version,
    os_name, os_version, country_code, referer, ip_hash, user_agent_hash, created_at)
    VALUES (?, ?, ?, 'visible', ?, ?, ?, ?, ?, ?, ?, ?, ?)`),
  commentCount: db.prepare("SELECT COUNT(*) AS n FROM comments WHERE status = 'visible'"),
  addView: db.prepare(`INSERT OR IGNORE INTO post_views (post_slug, view_key, visitor_hash, viewed_on, created_at, ip_hash)
    VALUES (?, ?, ?, ?, ?, ?)`),
  addressViews: db.prepare("SELECT COUNT(*) AS n FROM post_views WHERE post_slug = ? AND viewed_on = ? AND ip_hash = ?"),
  postViews: db.prepare("SELECT COUNT(*) AS n FROM post_views WHERE post_slug = ?"),
  viewGroups: db.prepare("SELECT post_slug AS slug, COUNT(*) AS n FROM post_views GROUP BY post_slug"),
};

class ClientError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

const SLUG = /^\/posts\/[a-zA-Z0-9._-]+\.html$/;
const hmac = (value) => crypto.createHmac("sha256", config.hashSecret).update(String(value)).digest("hex");

function validSlug(value) {
  const slug = String(value || "").trim();
  if (!SLUG.test(slug)) throw new ClientError("文章地址无效。");
  return slug;
}

// Titles of the posts in the deployed release; re-read at most once a minute.
let catalog = { expires: 0, posts: new Map() };
function postCatalog() {
  if (Date.now() < catalog.expires) return catalog.posts;
  const posts = new Map();
  try {
    for (const row of JSON.parse(fs.readFileSync(config.blogIndexFile, "utf8"))) {
      const slug = `/${String(row?.url || "").replace(/^\/+/, "")}`;
      if (SLUG.test(slug)) posts.set(slug, String(row?.title || slug));
    }
  } catch (error) {
    if (error.code !== "ENOENT") console.warn(`Unable to read blog index: ${error.message}`);
  }
  catalog = { expires: Date.now() + 60_000, posts };
  return posts;
}
function assertKnownPost(slug) {
  const posts = postCatalog();
  if (posts.size > 0 && !posts.has(slug)) throw new ClientError("文章地址无效。");
}
function visibleGroups() {
  const posts = postCatalog();
  return sql.viewGroups.all().filter((row) => SLUG.test(row.slug) && row.n > 0 && (posts.size === 0 || posts.has(row.slug)));
}

// Only Caddy on this host connects; it passes Cloudflare's headers through.
function clientIp(req) {
  return String(req.headers["cf-connecting-ip"] || String(req.headers["x-forwarded-for"] || "").split(",")[0].trim()
    || req.socket.remoteAddress || "");
}

// One key per IPv4 address or IPv6 /64 (a single host usually owns a /64).
function addressKey(ip) {
  const address = ip.replace(/^::ffff:/, "");
  if (!address.includes(":")) return address;
  const [head, tail = ""] = address.split("::");
  const left = head ? head.split(":") : [];
  const right = tail ? tail.split(":") : [];
  const groups = [...left, ...Array(Math.max(0, 8 - left.length - right.length)).fill("0"), ...right];
  return groups.slice(0, 4).map((group) => group.toLowerCase().replace(/^0+(?=.)/, "")).join(":");
}

function viewDate(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: config.viewTimeZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function parseUserAgent(ua = "") {
  const systems = [["Windows", /Windows NT ([\d.]+)/], ["macOS", /Mac OS X ([\d_]+)/], ["iOS", /(?:iPhone|iPad).*OS ([\d_]+)/],
    ["Android", /Android ([\d.]+)/], ["Linux", /Linux/]];
  const browsers = [["Edge", /Edg\/([\d.]+)/], ["Chrome", /Chrome\/([\d.]+)/], ["Firefox", /Firefox\/([\d.]+)/],
    ["Safari", /Version\/([\d.]+).*Safari/]];
  const system = systems.find(([, pattern]) => pattern.test(ua));
  const browser = browsers.find(([, pattern]) => pattern.test(ua));
  return {
    os_name: system?.[0] || "Unknown",
    os_version: system?.[1].exec(ua)?.[1]?.replaceAll("_", ".") || "",
    browser_name: browser?.[0] || "Unknown",
    browser_version: browser?.[1].exec(ua)?.[1]?.split(".").slice(0, 2).join(".") || "",
  };
}

// Attempts per visitor and post, counted before Turnstile so a flood of bad
// tokens is cut off without calling Cloudflare.
const attempts = new Map();
function checkRateLimit(ip, slug) {
  const now = Date.now();
  const key = `${hmac(ip)}:${slug}`;
  const recent = (attempts.get(key) || []).filter((time) => now - time < config.rateLimitWindowMs);
  if (recent.length >= config.rateLimitMax) throw new ClientError("评论发送太频繁，请稍后再试。", 429);
  recent.push(now);
  attempts.set(key, recent);
  if (attempts.size > 10_000) {
    for (const [entry, times] of attempts) if (!times.some((time) => now - time < config.rateLimitWindowMs)) attempts.delete(entry);
  }
}

async function verifyTurnstile(token, ip) {
  if (!config.turnstileSecretKey || config.turnstileSecretKey === "change-me") {
    throw new Error("Turnstile secret is not configured");
  }
  const form = new FormData();
  form.set("secret", config.turnstileSecretKey);
  form.set("response", token);
  if (ip) form.set("remoteip", ip);
  let body = {};
  try {
    const response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST", body: form, signal: AbortSignal.timeout(10_000),
    });
    body = await response.json();
  } catch {
    throw new ClientError("人机验证服务暂时不可用，请稍后再试。", 503);
  }
  if (!body.success) throw new ClientError("人机验证失败。");
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16 * 1024) throw new ClientError("请求体过大。", 413);
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw new ClientError("请求格式无效。");
  }
}

const routes = {
  "GET /healthz": () => [200, { ok: true }],

  "GET /config": () => [200, { turnstileSiteKey: config.turnstileSiteKey === "change-me" ? "" : config.turnstileSiteKey }],

  "GET /comments": (req, url) => {
    const slug = url.searchParams.get("slug") || "";
    if (!SLUG.test(slug)) throw new ClientError("文章地址无效。");
    return [200, { comments: sql.comments.all(slug) }];
  },

  "POST /comments": async (req) => {
    const input = await readJson(req);
    const slug = validSlug(input?.slug);
    const authorName = String(input?.authorName || "").trim();
    const content = String(input?.content || "").trim();
    const token = String(input?.turnstileToken || "").trim();
    if (authorName.length < 1 || authorName.length > 40) throw new ClientError("用户名长度需要在 1 到 40 个字符之间。");
    if (content.length < 1 || content.length > 1200) throw new ClientError("评论长度需要在 1 到 1200 个字符之间。");
    if (!token) throw new ClientError("请先完成人机验证。");
    const ip = clientIp(req);
    checkRateLimit(ip, slug);
    await verifyTurnstile(token, ip);
    const userAgent = String(req.headers["user-agent"] || "");
    const ua = parseUserAgent(userAgent);
    const country = String(req.headers["cf-ipcountry"] || "").slice(0, 2).toUpperCase();
    const referer = String(req.headers.referer || "").slice(0, 500);
    sql.addComment.run(slug, authorName, content, ua.browser_name, ua.browser_version, ua.os_name, ua.os_version,
      /^[A-Z]{2}$/.test(country) ? country : null, referer || null, hmac(ip), hmac(userAgent), new Date().toISOString());
    return [201, { ok: true }];
  },

  "GET /views": (req, url) => {
    const one = url.searchParams.get("slug");
    if (one) {
      const slug = validSlug(one);
      assertKnownPost(slug);
      return [200, { views: sql.postViews.get(slug).n }];
    }
    return [200, { views: Object.fromEntries(visibleGroups().map((row) => [row.slug, row.n])) }];
  },

  "POST /views": async (req) => {
    const slug = validSlug((await readJson(req))?.slug);
    assertKnownPost(slug);
    const ip = clientIp(req);
    const visitorHash = hmac(`${ip}\n${String(req.headers["user-agent"] || "")}`);
    const viewedOn = viewDate();
    const ipHash = hmac(addressKey(ip));
    let counted = false;
    if (sql.addressViews.get(slug, viewedOn, ipHash).n < config.viewLimitPerAddress) {
      counted = sql.addView.run(slug, hmac(`${slug}\n${visitorHash}\n${viewedOn}`), visitorHash, viewedOn,
        new Date().toISOString(), ipHash).changes > 0;
    }
    return [200, { views: sql.postViews.get(slug).n, counted }];
  },

  "GET /stats": () => {
    const groups = visibleGroups();
    const top = [...groups].sort((a, b) => b.n - a.n || a.slug.localeCompare(b.slug))[0];
    return [200, {
      totalViews: groups.reduce((sum, row) => sum + row.n, 0),
      mostViewed: top ? {
        slug: top.slug,
        title: postCatalog().get(top.slug) || top.slug.split("/").pop().replace(/\.html$/, ""),
        views: top.n,
      } : null,
      commentCount: sql.commentCount.get().n,
    }];
  },
};

function send(res, status, body) {
  const json = body === undefined ? "" : JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "access-control-allow-origin": config.siteOrigin,
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
    vary: "Origin",
  });
  res.end(json);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://localhost");
  // Accept /api/... too, for direct calls that skip Caddy.
  const pathname = url.pathname.replace(/^\/api(?=\/|$)/, "").replace(/\/$/, "") || "/";
  if (req.method === "OPTIONS") return send(res, 204);
  const route = routes[`${req.method} ${pathname}`];
  try {
    if (!route) return send(res, 404, { error: "Not found" });
    const [status, body] = await route(req, url);
    send(res, status, body);
  } catch (error) {
    if (!(error instanceof ClientError)) console.error(error);
    send(res, error instanceof ClientError ? error.status : 500, { error: error instanceof ClientError ? error.message : "服务器错误" });
  }
});

server.listen(config.port, config.host, () => {
  console.log(`pnc-api listening on http://${config.host}:${config.port} (db: ${config.dbPath})`);
});
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => server.close(() => { db.close(); process.exit(0); }));
}
```

`systemd/pnc-api.service`：

```ini
[Unit]
Description=PNC's Blog comments and stats API (SQLite)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/lyy/services/pnc-infra/api
EnvironmentFile=/home/lyy/services/pnc-api/.env
ExecStart=/usr/bin/node --disable-warning=ExperimentalWarning /home/lyy/services/pnc-infra/api/server.mjs
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/lyy/services/pnc-api/data
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
MemoryMax=256M

[Install]
WantedBy=default.target
```

`.env`（`~/services/pnc-api/.env`，0600）的键：`HOST`、`PORT=46219`、`DB_PATH`、`BLOG_INDEX_FILE`、`VIEW_TIME_ZONE`、`SITE_ORIGIN`、`TURNSTILE_SITE_KEY`、`TURNSTILE_SECRET_KEY`、`COMMENT_HASH_SECRET`、`COMMENT_RATE_LIMIT_WINDOW_MS`、`COMMENT_RATE_LIMIT_MAX`，可选 `VIEW_LIMIT_PER_ADDRESS`。后 6 个值是直接从旧 `pnc-comment-api/.env` 复制的，**`COMMENT_HASH_SECRET` 不要换**：阅读去重的 `view_key` 是用它算的，换掉后当天的阅读会被重新计数。

`node:sqlite` 在 Node 22.13 之后不需要额外参数，但仍属实验性 API，所以单元里用 `--disable-warning=ExperimentalWarning` 关掉了警告。

### 3.4 管理命令

```bash
DB=~/services/pnc-api/data/pnc.db

# 最近 20 条评论
sqlite3 -box "$DB" "SELECT id, post_slug, author_name, substr(content, 1, 40) AS content, status, created_at
                    FROM comments ORDER BY id DESC LIMIT 20;"

# 隐藏（可恢复）/ 恢复 / 彻底删除
sqlite3 "$DB" "UPDATE comments SET status = 'hidden' WHERE id = 42;"
sqlite3 "$DB" "UPDATE comments SET status = 'visible' WHERE id = 42;"
sqlite3 "$DB" "DELETE FROM comments WHERE id = 42;"

# 同一来源的批量垃圾评论：先看，再隐藏
sqlite3 -box "$DB" "SELECT ip_hash, COUNT(*) FROM comments GROUP BY ip_hash ORDER BY 2 DESC LIMIT 5;"
sqlite3 "$DB" "UPDATE comments SET status = 'hidden' WHERE ip_hash = '…';"

# 每篇文章的阅读数
sqlite3 -box "$DB" "SELECT post_slug, COUNT(*) AS views FROM post_views GROUP BY post_slug ORDER BY views DESC;"
```

数据库是 WAL 模式，API 运行时也可以直接执行这些命令。

### 3.5 从 Directus 迁移（已完成，记录备查）

Directus 本身用的就是 SQLite，所以不经过 REST，直接只读 `ATTACH` 它的数据库。脚本可以重复执行：评论保留原 id 并覆盖，阅读记录按 `view_key` 去重。

```sql
-- Copy comments and reading records from the Directus SQLite database into
-- pnc.db. Safe to run again: comments keep their ids, views dedupe on view_key.
--   sqlite3 /home/lyy/services/pnc-api/data/pnc.db < migrate-from-directus.sql
ATTACH 'file:/home/lyy/services/pnc-cms/data/directus.sqlite?mode=ro' AS directus;
BEGIN;
INSERT OR REPLACE INTO comments (id, post_slug, author_name, content, status, browser_name, browser_version,
  os_name, os_version, country_code, referer, ip_hash, user_agent_hash, created_at)
SELECT id, post_slug, author_name, content, status, browser_name, browser_version,
  os_name, os_version, country_code, referer, ip_hash, user_agent_hash, created_at
FROM directus.comments;
INSERT OR IGNORE INTO post_views (post_slug, view_key, visitor_hash, viewed_on, created_at)
SELECT post_slug, view_key, visitor_hash, viewed_on, created_at FROM directus.post_views ORDER BY id;
COMMIT;
SELECT 'comments', COUNT(*) FROM comments UNION ALL SELECT 'post_views', COUNT(*) FROM post_views;
```

2026-11-21 的过程：先导入一份快照，新 API 和旧 API 并行运行并逐个接口对比；正式切换时先停旧 API（评论写入暂停），再执行一次增量同步，然后切换。结果：2 条评论、372 条阅读记录。

### 3.6 备份与恢复

`pnc-api-backup.timer` 每天 04:30（随机延迟 10 分钟）运行 `backup.sh`。`sqlite3 .backup` 用的是在线备份 API，API 正在写入时也能得到一致的快照。备份文件在 `~/services/pnc-api/backups/`，保留 30 天，也在全机 btrbk 快照的范围内。

```bash
#!/usr/bin/env bash
# Online, consistent backup of pnc.db (safe while the API is writing).
set -Eeuo pipefail
umask 077
DB=${DB_PATH:-/home/lyy/services/pnc-api/data/pnc.db}
DEST=${BACKUP_DIR:-/home/lyy/services/pnc-api/backups}
KEEP_DAYS=${KEEP_DAYS:-30}

mkdir -p "$DEST"
stamp=$(date '+%Y%m%d-%H%M%S')
tmp="$DEST/.pnc-$stamp.db"
trap 'rm -f "$tmp"' EXIT
sqlite3 "$DB" ".backup '$tmp'"
[[ $(sqlite3 "file:$tmp?immutable=1" 'PRAGMA integrity_check;') == ok ]] || { echo "backup failed integrity_check" >&2; exit 1; }
gzip -9 "$tmp"
mv "$tmp.gz" "$DEST/pnc-$stamp.db.gz"
find "$DEST" -name 'pnc-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "backup: $DEST/pnc-$stamp.db.gz ($(stat -c %s "$DEST/pnc-$stamp.db.gz") bytes)"
```

```bash
#!/usr/bin/env bash
# Restore pnc.db from a backup made by backup.sh. Stops the API meanwhile and
# keeps the replaced database next to it.
#   restore.sh /home/lyy/backups/pnc-api/pnc-YYYYMMDD-HHMMSS.db.gz
set -Eeuo pipefail
BACKUP=${1:?usage: restore.sh path/to/pnc-YYYYMMDD-HHMMSS.db.gz}
DB=${DB_PATH:-/home/lyy/services/pnc-api/data/pnc.db}
SERVICE=${SERVICE:-pnc-api}
SYSTEMCTL=${SYSTEMCTL:-systemctl --user}

gunzip -c "$BACKUP" > "$DB.restoring"
[[ $(sqlite3 "file:$DB.restoring?immutable=1" 'PRAGMA integrity_check;') == ok ]] || { echo "backup is corrupt" >&2; rm -f "$DB.restoring"; exit 1; }
$SYSTEMCTL stop "$SERVICE"
if [[ -f $DB ]]; then
  # Fold the WAL into the old file first so the kept copy is complete.
  sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null
  mv "$DB" "$DB.before-restore-$(date '+%Y%m%d-%H%M%S')"
fi
rm -f "$DB-wal" "$DB-shm"
mv "$DB.restoring" "$DB"
$SYSTEMCTL start "$SERVICE"
echo "restored $BACKUP into $DB"
```

```ini
# systemd/pnc-api-backup.service
[Unit]
Description=Back up pnc.db (comments and reading records)

[Service]
Type=oneshot
Environment=BACKUP_DIR=/home/lyy/services/pnc-api/backups
ExecStart=/home/lyy/services/pnc-infra/api/backup.sh

# systemd/pnc-api-backup.timer
[Unit]
Description=Daily pnc.db backup

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
```

已经演练过恢复到临时路径：`DB_PATH=/tmp/x/pnc.db SYSTEMCTL=true restore.sh <备份>`，数据和完整性检查都正常。**建议每季度演练一次。**

---

## 4. 部署与回滚

### 4.1 Workflow（本仓库 `.github/workflows/`）

- `build-site.yml`（**Verify and Deploy Site**）：只由 push 到 `main` 和手动运行触发，跑在 `[self-hosted, linux, x64, pnc-blog]` 上。手动运行时可以填 `deploy_sha`，经过正则校验后以 `DEPLOY_SHA` 传给 `deploy.sh`。`concurrency: pnc-blog-production`，不取消正在进行的部署。
- `security.yml`（**Security Checks**）：每次部署结束后（`workflow_run`，只接受本仓库 push 或手动触发产生的运行）、每周一 11:23 以及手动运行时，执行 CodeQL（`security-extended`）和 `npm audit`。action 全部钉死到 commit SHA。作者为 `pnc-bot` 的提交（Telegram Bot 发的随想、随想图片、草稿和它们的撤销，第 10 节）部署后两项检查都跳过，并使用单独的 concurrency 组，避免发一条随想就取消正在进行的扫描；下一次别人的提交和每周的定时扫描仍会检查整个仓库。

两条 workflow 都没有 `pull_request` 触发器。

### 4.2 `deploy.sh`

```bash
#!/usr/bin/env bash
# Verify, build and publish one commit of PNC's Blog; keep the last few
# releases so going back is a symlink switch.
#
#   DEPLOY_SHA=<sha> deploy.sh    deploy a commit on main (default: origin/main)
#   deploy.sh --rollback [name]   serve the previous (or the named) release again
#                                 (back to the one it replaced if unhealthy)
#   deploy.sh --list              list releases, marking the one being served
#
# Every deploy and rollback attempt (not --list) leaves a trail for pnc-bot and
# monitoring:
#   logs/deploy-<time>-<sha12|rollback>.log  all of its output (newest $KEEP_LOGS kept)
#   state/last-deploy.json                   the attempt in progress, or the last one
#   state/deploys.jsonl                      one line per finished attempt
#   $DEPLOY_METRICS_DIR/pnc_blog_deploy.prom last result for node-exporter (if the dir exists)
# DEPLOY_TRIGGER (github-actions | pnc-bot | manual) and DEPLOY_REQUEST_ID are
# copied into the records. If another deploy is running, this one waits for
# it, up to DEPLOY_LOCK_WAIT seconds.
set -Eeuo pipefail
umask 022

APP_ROOT=${APP_ROOT:-/home/lyy/services/pnc-blog}
REPO_URL=${REPO_URL:-https://github.com/Boshugege/boshugege.github.io.git}
SITE_URL=${SITE_URL:-http://127.0.0.1:46213}
KEEP_RELEASES=${KEEP_RELEASES:-5}
KEEP_LOGS=${KEEP_LOGS:-30}
LOCK_WAIT=${DEPLOY_LOCK_WAIT:-1200}
METRICS_DIR=${DEPLOY_METRICS_DIR:-/home/lyy/observability/data/node-textfile}
REPO=${REPO:-$APP_ROOT/repo}
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
LOGS=$APP_ROOT/logs
STATE=$APP_ROOT/state
export ASTRO_TELEMETRY_DISABLED=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm_config_fund=false npm_config_audit=false
# Build with the same npm as the Actions runner, whose PATH has ~/.local/bin
# (npm 12) before /usr/bin (Debian's npm 10). systemd user units, where
# pnc-bot starts deploys, get a PATH without ~/.local/bin.
[[ :$PATH: == *":${HOME:-/home/lyy}/.local/bin:"* ]] || PATH=${HOME:-/home/lyy}/.local/bin:$PATH

# The attempt being recorded (see record()).
MODE="" SHA="" RELEASE_NAME="" ERROR="" FAILED_AT="" LOG_FILE="" STAMP="" STARTED_AT="" ATTEMPT_ID=""
TEE_PID="" HAVE_LOCK=0
TRIGGER=${DEPLOY_TRIGGER:-}
if [[ -z $TRIGGER ]]; then
  if [[ ${GITHUB_ACTIONS:-} == true ]]; then TRIGGER=github-actions; else TRIGGER=manual; fi
fi
REQUEST_ID=${DEPLOY_REQUEST_ID:-}

log() { printf '[deploy %s] %s\n' "$(date '+%F %T')" "$*"; }
# fail STATUS MESSAGE: stop here; MESSAGE becomes the attempt's error.
fail() {
  local status=$1
  shift
  ERROR=$*
  log "ERROR: $*"
  exit "$status"
}
die() { fail 1 "$@"; }
# Anything else that stops the script under `set -e` is named in the record
# (see on_exit). Only remembered here: this trap also runs inside $(...).
trap 'FAILED_AT=${FAILED_AT:-"line $LINENO: $BASH_COMMAND (exit $?)"}' ERR
trap 'fail 130 "interrupted (SIGINT)"' INT
trap 'fail 143 "terminated (SIGTERM)"' TERM
trap 'fail 129 "hung up (SIGHUP)"' HUP

[[ $LOCK_WAIT =~ ^[0-9]+$ ]] || die "DEPLOY_LOCK_WAIT must be a whole number of seconds"
mkdir -p "$RELEASES"

WORK=""
cleanup() {
  if [[ -n $WORK ]]; then
    git -C "$REPO" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    git -C "$REPO" worktree prune
  fi
}

utc_now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# The attempt as one line of JSON. jq quotes every value, so error texts and
# caller-supplied ids can hold any characters.
record() { # RESULT [FINISHED_AT]
  jq -cn --arg id "$ATTEMPT_ID" --arg mode "$MODE" --arg sha "$SHA" --arg release "$RELEASE_NAME" \
    --arg result "$1" --arg started_at "$STARTED_AT" --arg finished_at "${2:-}" --arg log "$LOG_FILE" \
    --arg error "${ERROR:0:2000}" --arg trigger "$TRIGGER" --arg request "$REQUEST_ID" '
    def nullable: if . == "" then null else . end;
    {id: $id, mode: $mode, sha: ($sha | nullable), release: ($release | nullable), result: $result,
     started_at: $started_at, finished_at: ($finished_at | nullable), log: $log, error: $error,
     trigger: $trigger, request: $request}'
}

# write_atomic FILE TEXT: readers see the old file or the new one, never a
# partial one. The temporary name does not end in .prom, so node-exporter
# skips it.
write_atomic() {
  local tmp=$1.tmp.$$
  if printf '%s\n' "$2" >"$tmp" && mv -fT -- "$tmp" "$1"; then return 0; fi
  rm -f -- "$tmp"
  return 1
}

write_running() {
  local json
  if json=$(record running) && write_atomic "$STATE/last-deploy.json" "$json"; then return 0; fi
  log "WARNING: could not write $STATE/last-deploy.json"
}

metric_text() { # SUCCESS(0|1) FINISHED_EPOCH
  cat <<EOF
# HELP pnc_blog_deploy_last_success Whether the last deploy or rollback of PNC's Blog succeeded (1) or failed (0).
# TYPE pnc_blog_deploy_last_success gauge
pnc_blog_deploy_last_success $1
# HELP pnc_blog_deploy_last_finish_timestamp_seconds When the last deploy or rollback of PNC's Blog finished, as a Unix time.
# TYPE pnc_blog_deploy_last_finish_timestamp_seconds gauge
pnc_blog_deploy_last_finish_timestamp_seconds $2
EOF
}

# Open this attempt's log under a name no other attempt has (created with
# O_EXCL; on a clash, try again in the next second), then copy everything the
# script and its commands print into it. stdout still reaches the caller.
# tee ignores the signals that stop a deploy (Ctrl-C, a cancelled run hit
# the whole process group) so the script can still log how it ended; tee
# exits when the script closes its output (see on_exit).
begin_attempt() { # LABEL
  mkdir -p "$LOGS" "$STATE"
  for _ in 1 2 3 4 5; do
    STAMP=$(date '+%Y%m%d-%H%M%S')
    if (set -o noclobber && : >"$LOGS/deploy-$STAMP-$1.log") 2>/dev/null; then
      LOG_FILE=$LOGS/deploy-$STAMP-$1.log
      break
    fi
    sleep 1
  done
  [[ -n $LOG_FILE ]] || die "cannot create a log file in $LOGS"
  ATTEMPT_ID=$STAMP-$$
  STARTED_AT=$(utc_now)
  exec > >(trap '' INT TERM HUP && exec tee -a -- "$LOG_FILE") 2>&1
  TEE_PID=$!
}

# A deploy log is opened before its commit is known; once it is, rename the
# log to carry the commit. ln (unlike mv) never replaces an existing file.
name_log() { # SHA12
  local named=$LOGS/deploy-$STAMP-$1.log
  if ln -- "$LOG_FILE" "$named" 2>/dev/null; then
    rm -f -- "$LOG_FILE"
    LOG_FILE=$named
  fi
}

# Keep the newest $KEEP_LOGS logs (their names sort by start time), and
# always this attempt's.
prune_logs() {
  local name
  find "$LOGS" -maxdepth 1 -type f -name 'deploy-*.log' -printf '%f\n' | LC_ALL=C sort -r |
    tail -n +"$((KEEP_LOGS + 1))" | while read -r name; do
      [[ $LOGS/$name == "$LOG_FILE" ]] || rm -f -- "${LOGS:?}/$name"
    done
}

# One deploy at a time: wait for a running one to finish. flock runs in the
# background so that a signal (a cancelled run) is handled while waiting; the
# lock is taken on fd 9, which this shell shares, so it outlives flock.
take_lock() {
  exec 9>"$APP_ROOT/deploy.lock"
  if ! flock -n 9; then
    log "another deploy is running; waiting up to ${LOCK_WAIT}s for it to finish"
    flock -w "$LOCK_WAIT" 9 >/dev/null 2>&1 &
    wait "$!" || die "another deploy was still running after ${LOCK_WAIT}s; giving up"
    log "the other deploy has finished; continuing"
  fi
  HAVE_LOCK=1
}

# Full SHA of a release's commit if the deploy clone knows it, else nothing.
full_sha() { git -C "$REPO" rev-parse --verify --quiet "$1^{commit}" 2>/dev/null || true; }

# EXIT trap: remove the build worktree, then record how the attempt ended.
# The log is completed first, so whoever sees the record can read all of it.
on_exit() {
  local status=$? result=success succeeded=1 now finished json
  trap - ERR
  trap '' INT TERM HUP
  set +e
  cleanup
  [[ -n $LOG_FILE ]] || return 0
  if ((status != 0)); then
    result=failure succeeded=0
    if [[ -z $ERROR ]]; then # not stopped by fail(), which has said why
      ERROR=${FAILED_AT:-"exited with status $status"}
      log "ERROR: $ERROR"
    fi
  else
    ERROR=""
  fi
  exec >/dev/null 2>&1 # end of output: tee writes the rest of the log and exits
  [[ -z $TEE_PID ]] || wait "$TEE_PID"
  now=$(date +%s)
  finished=$(date -u -d "@$now" '+%Y-%m-%dT%H:%M:%SZ')
  # last-deploy.json and the metric belong to the lock holder: an attempt that
  # gave up waiting must not overwrite what the running deploy reports.
  if json=$(record "$result" "$finished"); then
    printf '%s\n' "$json" >>"$STATE/deploys.jsonl" || note "WARNING: could not append to $STATE/deploys.jsonl"
    if ((HAVE_LOCK)); then
      write_atomic "$STATE/last-deploy.json" "$json" || note "WARNING: could not write $STATE/last-deploy.json"
    fi
  else
    note "WARNING: could not build the deploy record (is jq installed?)"
  fi
  if ((HAVE_LOCK)) && [[ -d $METRICS_DIR ]]; then
    write_atomic "$METRICS_DIR/pnc_blog_deploy.prom" "$(metric_text "$succeeded" "$now")" ||
      note "WARNING: could not write $METRICS_DIR/pnc_blog_deploy.prom"
  fi
}
# After the log is closed, a last word can only be appended to the file.
note() { printf '[deploy %s] %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE"; }
trap on_exit EXIT

serving() { readlink "$CURRENT" 2>/dev/null || true; }

# rename(2) of a new symlink over the old one: requests see either the old
# or the new release, never a missing one.
switch_to() {
  ln -sfn "$1" "$APP_ROOT/.current.tmp"
  mv -Tf "$APP_ROOT/.current.tmp" "$CURRENT"
  log "now serving $(basename "$1")"
}

# check URL STATUS [BODY_MARKER] [HEADER_REGEX]
check() {
  local url=$1 want=$2 marker=${3:-} header=${4:-} status
  status=$(curl -sS --max-time 10 -o "$WORK_CHECK.body" -D "$WORK_CHECK.head" -w '%{http_code}' "$url") || {
    log "check failed: $url unreachable"; return 1; }
  if [[ $status != "$want" ]]; then log "check failed: $url returned $status, expected $want"; return 1; fi
  if [[ -n $marker ]] && ! grep -qF -- "$marker" "$WORK_CHECK.body"; then
    log "check failed: $url body lacks $marker"; return 1; fi
  if [[ -n $header ]] && ! tr -d '\r' < "$WORK_CHECK.head" | grep -qiE -- "$header"; then
    log "check failed: $url headers lack $header"; return 1; fi
}

# Any one post from the release being served (posts can be renamed freely).
first_post() { grep -o '"url":"posts/[^"]*"' "$CURRENT/index.json" | head -n 1 | cut -d '"' -f 4; }

run_checks() {
  check "$SITE_URL/" 200 'data-post-list' &&
    check "$SITE_URL/index.html" 200 'data-post-list' &&
    check "$SITE_URL/$(first_post)" 200 'data-post-view' &&
    check "$SITE_URL/404" 404 'data-impossible-404' '^cache-control:.*no-store' &&
    check "$SITE_URL/deploy-check-$RANDOM/missing.html" 404 'data-impossible-404' '^cache-control:.*no-store' &&
    check "$SITE_URL/404.html" 200 'data-impossible-404' &&
    check "$SITE_URL/rss.xml" 200 '<rss' &&
    check "$SITE_URL/api/healthz" 200 '"ok":true' &&
    check "$SITE_URL/api/config" 200 'turnstileSiteKey' &&
    check "$SITE_URL/api/views" 200 '"views"' &&
    check "$SITE_URL/api/stats" 200 '"totalViews"'
}

health_check() {
  WORK_CHECK=$(mktemp "${TMPDIR:-/tmp}/pnc-check.XXXXXX")
  local attempt
  for attempt in 1 2 3; do
    if run_checks; then rm -f "$WORK_CHECK"*; log "health check passed"; return 0; fi
    sleep 2
  done
  rm -f "$WORK_CHECK"*
  return 1
}

# Keep the newest $KEEP_RELEASES releases, and always the one being served.
prune() {
  local current dir
  current=$(serving)
  ls -1 "$RELEASES" | sort -r | tail -n +"$((KEEP_RELEASES + 1))" | while read -r dir; do
    [[ $RELEASES/$dir == "$current" ]] && continue
    rm -rf -- "${RELEASES:?}/$dir"
    log "removed old release $dir"
  done
}

case ${1:-} in
  --list)
    current=$(serving)
    ls -1 "$RELEASES" | sort -r | while read -r dir; do
      if [[ $RELEASES/$dir == "$current" ]]; then echo "* $dir"; else echo "  $dir"; fi
    done
    exit 0
    ;;
  --rollback) MODE=rollback ;;
  "") MODE=deploy ;;
  *) die "unknown option: $1" ;;
esac

if [[ $MODE == rollback ]]; then begin_attempt rollback; else begin_attempt unresolved; fi
take_lock
prune_logs || log "WARNING: could not prune old logs in $LOGS"
write_running

if [[ $MODE == rollback ]]; then
  previous=$(serving)
  current=$(basename "$previous")
  target=${2:-$(ls -1 "$RELEASES" | sort | awk -v cur="$current" '$0 < cur' | tail -n 1)}
  [[ $target =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{12}$ && -d $RELEASES/$target ]] || die "no such release: ${target:-none} (see --list)"
  RELEASE_NAME=$target
  SHA=$(full_sha "${target##*-}")
  write_running
  switch_to "$RELEASES/$target"
  if ! health_check; then
    # Like a deploy: an unhealthy release does not stay. Without a release to
    # go back to (none was served, or it is gone) the target stays.
    if [[ -n $previous && -d $previous && $previous != "$RELEASES/$target" ]]; then
      switch_to "$previous"
      log "rolled back to $(basename "$previous")"
      die "health check failed on $target; switched back to $(basename "$previous")"
    fi
    die "health check failed on $target; current is still $target, see --list"
  fi
  exit 0
fi

# 1. Fetch main and resolve the commit. Only commits on main are deployable.
[[ -d $REPO ]] || git clone --bare --quiet "$REPO_URL" "$REPO" || fail $? "git clone of $REPO_URL failed"
git -C "$REPO" fetch --quiet --prune origin '+refs/heads/main:refs/remotes/origin/main' || fail $? "git fetch of main failed"
want=${DEPLOY_SHA:-origin/main}
[[ $want == origin/main || $want =~ ^[0-9a-f]{7,40}$ ]] || die "DEPLOY_SHA must be a 7-40 character hex SHA"
sha=$(git -C "$REPO" rev-parse --verify --quiet "${want}^{commit}") || die "unknown commit: $want"
short=${sha:0:12}
SHA=$sha
name_log "$short"
write_running
git -C "$REPO" merge-base --is-ancestor "$sha" origin/main || die "$sha is not on main; refusing to deploy"

# 2. Reuse a release already built from this commit (redeploy / rollback by
#    SHA takes seconds), otherwise build one in a throwaway worktree.
release=$(ls -1d "$RELEASES"/*-"$short" 2>/dev/null | sort | tail -n 1 || true)
if [[ -n $release ]]; then
  log "reusing $(basename "$release") for $short"
else
  WORK=$(mktemp -d "$APP_ROOT/.build.XXXXXX")
  git -C "$REPO" worktree add --detach --quiet "$WORK/src" "$sha" || fail $? "git worktree add for $short failed"
  log "verifying and building $short"
  (cd "$WORK/src" && npm ci --prefer-offline) || fail $? "npm ci failed for $short (exit $?)"
  (cd "$WORK/src" && npm run verify) || fail $? "npm run verify failed for $short (exit $?)"
  release=$RELEASES/$(date '+%Y%m%d-%H%M%S')-$short
  mv "$WORK/src/dist" "$release" || fail $? "could not move the build of $short into $RELEASES"
fi
RELEASE_NAME=$(basename "$release")

# 3. Switch, check, and go back automatically if the new release is unhealthy.
previous=$(serving)
if [[ $previous == "$release" ]]; then
  log "$(basename "$release") is already being served"
else
  switch_to "$release"
fi
if ! health_check; then
  if [[ -n $previous && -d $previous && $previous != "$release" ]]; then
    switch_to "$previous"
    log "rolled back to $(basename "$previous")"
  fi
  die "deploy of $short failed its health check"
fi

prune
log "deployed $short"
```

要点：

- **只验证和构建一次**：`npm run verify`（类型检查、单测、构建、`verify-build.mjs`），发布的就是这次验证过的 `dist/`。
- **只发布 `main` 上的提交**：`DEPLOY_SHA` 必须是 7–40 位十六进制，并且是 `origin/main` 的祖先。
- **同一提交不重复构建**：已有该 SHA 的 release 时直接复用。
- **健康检查**：首页、任意一篇文章、`/404`（必须是 404 + `no-store`）、随机缺失路径、`/404.html`、`/rss.xml`、`/api/healthz`、`/api/config`、`/api/views`、`/api/stats`。失败重试 3 次，仍失败就切回上一个 release 并以非零状态退出。检查用的是所有 release 都有的标记，所以回滚到较早的版本也能通过。
- **保留最近 5 个 release**，正在服务的那个永远不会被删除。
- **一次只跑一个部署**：`flock` 加锁；另一次部署正在跑时，新的会等它结束（最多 `DEPLOY_LOCK_WAIT`，默认 1200 秒），不会直接失败。Runner 和 Bot 发起的部署因此可以排队。
- **每次部署或回滚都留下记录**（2026-11-21 起）：`logs/deploy-<时间>-<sha12>.log`（完整输出，保留 30 个）；`state/last-deploy.json`（进行中或最近一次）；`state/deploys.jsonl`（每次结束追加一行：`mode` `sha` `release` `result` `error` `trigger`（`github-actions` / `pnc-bot` / `manual`）`request` 等，用 `jq` 生成）；以及 `~/observability/data/node-textfile/pnc_blog_deploy.prom`（`pnc_blog_deploy_last_success`、`pnc_blog_deploy_last_finish_timestamp_seconds`）。最后这个指标驱动告警 `PncBlogDeployFailed`（规则在 `~/observability/prometheus/rules/pnc-blog.yml`），失败时发到告警群；下一次部署或回滚成功后自动恢复。Bot 读 `deploys.jsonl` 来回报"已上线"或"部署失败"（第 10 节）。
- **npm 与 Runner 一致**：`~/.local/bin` 不在 PATH 里时放到最前面（Runner 用 `~/.local/bin/npm` 12；systemd 用户单元的 PATH 只能找到 Debian 的 npm 10）。
- **`--rollback` 也会回退**：目标版本健康检查不通过时，切回原来的版本并以非零状态退出。
- 测试脚手架（假的 APP_ROOT、真的 git/flock/jq、桩 npm/curl，覆盖成功、构建失败、健康检查失败、回滚、等锁和超时、信号、JSON 转义、与旧版输出逐字一致等 362 项检查）在改动时跑过，没有放进仓库。

### 4.3 回滚

```bash
# 切回上一个 release，并重新跑健康检查（实测 0.3 秒）
~/services/pnc-blog/deploy.sh --rollback

# 查看 release（* 为当前），或者切到指定的 release
~/services/pnc-blog/deploy.sh --list
~/services/pnc-blog/deploy.sh --rollback 20261121-203929-3a727726a0e2

# 切回最新提交（复用已有 release，约 1 秒）
~/services/pnc-blog/deploy.sh

# 纯手工版（deploy.sh 本身出问题时）
cd ~/services/pnc-blog && ln -sfn "$PWD/releases/<名字>" .current.tmp && mv -Tf .current.tmp current
```

也可以在 GitHub Actions 页面手动运行 **Verify and Deploy Site**，在 `deploy_sha` 中填写 `main` 上的历史提交，或者在 Telegram 里给 Bot 发 `/deploy <sha>`（第 10 节）。`--rollback` 的目标健康检查不通过时会切回原来的版本。

### 4.4 依赖与 CPU：`Illegal instruction`

服务器的 CPU 是 Xeon E5-2450 v2（Ivy Bridge，x86-64-v2，**没有 AVX2**）。`@astrojs/compiler-binding` 0.5.3 的 Linux 原生文件需要 AVX2，一加载就会以 SIGILL（`Illegal instruction`，退出码 132）崩溃，`npm run build` 会悄无声息地失败。本仓库在 `package.json` 的 `overrides` 里把它固定在 0.5.2：两个版本的 JS 和类型完全相同，构建出的 `dist/` 除每次随机的 Logo id 外逐字节一致。

升级 astro 或其他带原生模块的依赖之前，先在服务器上试构建：

```bash
cd ~/services/pnc-blog/repo && W=$(mktemp -d ~/services/pnc-blog/.try.XXXXXX)
git fetch -q origin main && git worktree add -q --detach "$W/src" origin/main
cd "$W/src" && npm ci && npm run verify; echo "exit $?"
cd ~ && git -C ~/services/pnc-blog/repo worktree remove --force "$W/src" && rm -rf "$W"
```

找出是哪个原生文件崩溃：`find node_modules -name '*.node' -exec sh -c 'node -e "require(\"./$1\")" >/dev/null 2>&1 || echo "$? $1"' _ {} \;`

---

## 5. 安全检查

### 5.1 GitHub 仓库设置（2026-11-21 开启）

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Secret scanning + push protection | 开启 | 推送含密钥的提交会被直接拒绝 |
| 非服务商模式匹配、有效性检查 | 不可用 | 需要付费的 Secret Protection |
| Dependabot alerts | 开启 | 开启时报出 38 条（13 个包），已通过升级依赖全部修复 |
| Dependabot 自动修复 PR | 未开启 | 它跑在 GitHub 托管的算力上；由每周的 `npm audit` 代替 |
| Code scanning（CodeQL） | 自托管 | `security.yml`。首次扫描报出 4 条，都是“标签过滤不彻底”，已在同一批改动中修复。上线计数 API 后又报出 1 条 `js/file-system-race`（`ops/reactions-api/server.mjs` 先 stat 再读清单），已改为对同一文件句柄取 stat 再读取。注意：CodeQL 作业只上传结果，有告警时不会失败，要在 Security 页或用下面的命令查看 |
| 私密漏洞报告 | 开启 | 见 [SECURITY.md](SECURITY.md) |
| 外部贡献者的 PR 运行 | 一律需要审批 | `fork-pr-contributor-approval = all_external_contributors` |
| `main` 分支规则 | 禁止删除和强制推送 | 不要求 PR 和状态检查，仍然可以直接 push |
| Deploy key | 1 把，read-write（2026-11-21） | 「pnc-bot (personal server, notes/drafts)」，私钥只在服务器 `~/.config/pnc-bot/deploy_key`。只对本仓库有效，供 Telegram Bot 推送随想和草稿（第 10 节）。查看：`gh repo deploy-key list` |
| Workflow 权限 | 只读 | 各 workflow 显式声明 `permissions` |
| Action 必须钉到 commit SHA | 开启（2026-11-21） | `sha_pinning_required`：用标签引用 action 的 workflow 会被 GitHub 拒绝运行。这些作业跑在生产机器上，不能让上游改动的标签悄悄生效 |
| GitHub Pages | 来源改为 GitHub Actions（2026-11-21） | `boshugege.github.io` 是用户主页仓库，GitHub 不允许停用它的 Pages，删除后下一次 push 又会自动按分支构建（legacy，每次失败）。把 Source 改成 GitHub Actions 后，因为仓库里没有部署 Pages 的 workflow，push 不再触发任何 Pages 构建。以后不要在 Settings → Pages 改回 *Deploy from a branch* |

快速检查：

```bash
R=Boshugege/boshugege.github.io
gh api repos/$R --jq .security_and_analysis
gh api "repos/$R/dependabot/alerts?state=open" --jq length
gh api "repos/$R/code-scanning/alerts?state=open" --jq length
gh api "repos/$R/secret-scanning/alerts?state=open" --jq length
gh api repos/$R/actions/permissions/fork-pr-contributor-approval
gh api repos/$R/rulesets --jq '.[].name'
gh api repos/$R/actions/permissions          # sha_pinning_required: true
```

### 5.2 Runner 作业守卫（公开仓库必需）

去掉 `pull_request` 触发器**并不能**阻止 fork 的 PR 在自托管 Runner 上运行：PR 可以自带一个新的 workflow 文件，GitHub 会按 PR 里的文件运行它。防线有两道：

1. 外部贡献者的 PR 运行需要维护者批准（5.1）。**不要批准任何来源不明的 PR 运行。**
2. Runner 的 job-started 钩子：`~/services/actions-runner-pnc-blog/.env` 中的 `ACTIONS_RUNNER_HOOK_JOB_STARTED` 指向下面的脚本。它在作业的任何步骤执行之前运行，退出码非零就直接让作业失败。

```bash
#!/usr/bin/env bash
# Job-started hook of the self-hosted runner (ACTIONS_RUNNER_HOOK_JOB_STARTED
# in the runner's .env). The repository is public and a fork's pull request
# can bring its own workflow file, so the runner refuses every job except the
# trusted events on main of this repository, from the workflow files we ship.
# A non-zero exit fails the job before any of its steps run.
set -u
REPO=Boshugege/boshugege.github.io
ALLOWED_WORKFLOWS=" .github/workflows/build-site.yml .github/workflows/security.yml "

deny() {
  echo "::error::pnc runner guard refused this job ($1): repo=${GITHUB_REPOSITORY:-?} event=${GITHUB_EVENT_NAME:-?} ref=${GITHUB_REF:-?} workflow=${GITHUB_WORKFLOW_REF:-?}"
  exit 1
}

[[ ${GITHUB_REPOSITORY:-} == "$REPO" ]] || deny "other repository"
[[ ${GITHUB_REF:-} == refs/heads/main ]] || deny "not on main"
[[ -z ${GITHUB_HEAD_REF:-} ]] || deny "pull request"
case ${GITHUB_EVENT_NAME:-} in
  push | workflow_dispatch | schedule) ;;
  workflow_run)
    # Only follow-up runs of our own push / manual runs, never of a fork's run.
    [[ -r ${GITHUB_EVENT_PATH:-} ]] || deny "workflow_run without event payload"
    origin=$(node -e 'const e = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")).workflow_run || {}; console.log(`${e.event} ${(e.head_repository || {}).full_name}`)' "$GITHUB_EVENT_PATH" 2>/dev/null) || deny "unreadable event payload"
    [[ $origin == "push $REPO" || $origin == "workflow_dispatch $REPO" ]] || deny "workflow_run from: $origin"
    ;;
  *) deny "event not allowed" ;;
esac
workflow=${GITHUB_WORKFLOW_REF:-}
[[ $workflow == "$REPO/"*"@refs/heads/main" ]] || deny "workflow file not from main"
path=${workflow#"$REPO/"}
path=${path%@refs/heads/main}
[[ $ALLOWED_WORKFLOWS == *" $path "* ]] || deny "unknown workflow $path"
echo "pnc runner guard: allowed ${GITHUB_EVENT_NAME} ${path}"
```

本地测过 13 种情况，结果全部符合预期：放行 push 部署、手动部署、每周扫描、push 和手动运行之后的 `workflow_run`；拒绝 fork PR、PR 伪装成 `main` 的 ref、fork PR 触发的 `workflow_run`、fork 自己 push 触发的 `workflow_run`、缺少事件数据的 `workflow_run`、其他分支的手动运行、未登记的 workflow 文件、`issue_comment`、其他仓库。线上部署日志中可以看到 `pnc runner guard: allowed push .github/workflows/build-site.yml`。

**新增 workflow 文件时，要同时把它加进脚本里的 `ALLOWED_WORKFLOWS`**，否则会被拒绝。

### 5.3 Runner 运行身份

- Runner 以 `lyy` 的用户单元运行（`pnc-github-actions-runner.service`，非 root，`NoNewPrivileges`，`MemoryMax=4G`），只注册在本仓库，版本 2.338.2。
- 可选的进一步隔离：新建一个专用用户运行 Runner，只允许它以 `lyy` 身份执行部署脚本，而且不能带任何参数。sudoers 写成：

  ```text
  Defaults!/home/lyy/services/pnc-blog/deploy.sh env_keep += "DEPLOY_SHA"
  gha-pnc ALL=(lyy) NOPASSWD: /home/lyy/services/pnc-blog/deploy.sh ""
  ```

  不要用 `SETENV`，否则可以注入任意环境变量。然后把 workflow 最后一步改为 `sudo -u lyy /home/lyy/services/pnc-blog/deploy.sh`。CodeQL 作业也要能以这个用户运行。

### 5.4 服务器侧

- `~/services/pnc-api`、`~/services/pnc-comment-api` 都是 0700（后者原来是 `o+rwx`：`qbittorrent` 用户能进入 `/home/lyy`，也就能改这个服务的代码）。
- Caddy 和 pnc-api 都只监听回环地址。pnc-api 把 cloudflared 透传、Caddy 原样转发的 `CF-Connecting-IP` 当作访客 IP。本机进程直接访问 46213 时可以伪造这个头，但影响只限于阅读计数和频率限制。

---

## 6. 日常运维速查

```bash
# 状态
systemctl --user status pnc-blog pnc-api pnc-reactions pnc-bot pnc-api-backup.timer pnc-reactions-backup.timer pnc-github-actions-runner
~/services/pnc-blog/deploy.sh --list
curl -s http://127.0.0.1:46219/healthz; curl -s http://127.0.0.1:46213/api/stats; curl -s http://127.0.0.1:46213/api/reactions/healthz

# 日志
journalctl --user -u pnc-blog -f
journalctl --user -u pnc-api -f
journalctl --user -u pnc-bot -n 50 --no-pager
tail -n 5 ~/services/pnc-blog/state/deploys.jsonl | jq -c '{trigger, sha: .sha[0:7], result, error}'
journalctl --user -u pnc-github-actions-runner -n 100 --no-pager
ls -t ~/services/actions-runner-pnc-blog/_diag/ | head      # Runner_*.log、Worker_*.log

# 监控（~/services.yaml 驱动）
grep -E "^server_inventory_drift_items|pnc" ~/observability/data/node-textfile/systemd.prom
curl -s http://127.0.0.1:19090/api/v1/alerts | python3 -m json.tool | grep alertname
```

| 现象 | 原因与处理 |
| --- | --- |
| 部署作业在 *Set up runner* 失败，日志有 `pnc runner guard refused this job` | 作业不符合守卫规则：分支不是 `main`、事件类型不对，或 workflow 文件没有登记（5.2）。 |
| 构建无输出地失败，退出码 132 | 某个原生模块需要 AVX2，见 4.4。 |
| job 一直 *Waiting for a runner* | Runner 离线，或正在跑另一个作业（只有一个 Runner，CodeQL 首次约 7 分钟）。查看 `systemctl --user status pnc-github-actions-runner`。 |
| `another deploy is running; waiting…` | 另一次部署还在跑，这次会等它结束（最多 20 分钟）。用 `cat ~/services/pnc-blog/state/last-deploy.json` 查看是谁在跑。 |
| `PncBlogDeployFailed` 告警 | 最近一次部署或回滚失败，站点仍是上一个版本。`tail -n 40 "$(jq -r .log ~/services/pnc-blog/state/last-deploy.json)"`。 |
| Telegram Bot 相关 | 见 10.6。 |
| `is not on main; refusing to deploy` | `deploy_sha` 不是 `main` 上的提交。 |
| 健康检查在 `/api/*` 失败 | `pnc-api` 没在运行；脚本已经自动回滚了静态站。查看 `systemctl --user status pnc-api`。 |
| `InventoryDrift` 告警 | 新建或删除了 unit 或端口，却没有改 `~/services.yaml`。 |
| 评论区显示“人机验证没有加载出来” | 读者的浏览器或网络拦截了 `challenges.cloudflare.com`。只影响发表评论，评论照常显示。 |
| Playwright 测试报 `Process from config.webServer exited early` | astro 7.4 检测到 AI agent 时会把 `astro preview` 转到后台。`playwright.config.ts` 已加 `--ignore-lock`；如果还有残留进程，执行 `npx astro preview stop`。 |

---

## 7. 撤销瘦身（2026-12-05 之前可用）

旧组件都保留着：`server.mjs`、`pnc-comment-api`、Directus 的数据和 quadlet 文件（改名为 `.retired`），以及 `pnc-blog/backups/pre-caddy-*` 里切换前的 unit 文件。完整的切换脚本在 `pnc-infra/oneoff/cutover-2026-11-21.sh`。

```bash
# 1. 清单：恢复 comment-api 和 cms 的监控（或从 ~/services.yaml.bak-20261121-cutover 拷回），再处理服务
B=$(ls -d ~/services/pnc-blog/backups/pre-caddy-* | tail -n 1)
mv ~/.config/containers/systemd/pnc-cms.container.retired ~/.config/containers/systemd/pnc-cms.container
systemctl --user stop pnc-blog
rm ~/.config/systemd/user/pnc-blog.service && cp -a "$B/pnc-blog.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pnc-comment-api && systemctl --user start pnc-cms pnc-blog
```

切换之后的新评论只在 `pnc.db` 里，退回之前要先手动补到 Directus。

---

## 8. 待办

- [x] **GitHub Pages**：用户主页仓库不能停用 Pages，删除后 push 时又会自动启用；已把 Source 改为 GitHub Actions，不再产生失败的 `pages-build-deployment`（5.1）。
- [x] **Cloudflare**：`cms.parityncsvt.top` 已删除（2026-11-21，本人操作）。
- [ ] **2026-12-05 后清理**：`~/services/pnc-comment-api`、`~/services/pnc-cms`（先把 `data/` 和 `uploads/` 归档到 `/srv/backup`）、`pnc-cms.container.retired`、Directus 镜像（`podman rmi`）、`~/services.yaml` 里的 `comment-api` 和 `cms` 条目、`pnc-blog/server.mjs`、`deploy.sh.pre-20261121`、`pnc-blog/backups/pre-caddy-*`。
- [x] **启用「观测」计数 API**：2026-11-21 按第 9.4 节部署，见 9.7。
- [ ] 可选：在 Cloudflare 加第 9.4 节第 7 步的边缘速率限制规则（服务内的限流和熔断已经生效）。
- [ ] 可选：给 `pnc-infra` 加一个私有远端仓库（需要在服务器上配置部署密钥）。
- [ ] 可选：Runner 改用专用用户运行（5.3）。
- [x] **私人 Telegram Bot**：2026-11-22 上线（第 10 节）。
- [ ] 可选：服务器上 `gh` 登录的 token 是全账号的 `repo` + `workflow` 权限，本机任何以 `lyy` 运行的进程都能用它。Bot 没有用它（用的是 Deploy Key）；可以考虑换成只对必要仓库有效的细粒度 token。

---

## 9. 「观测」计数 API（`pnc-reactions`，2026-11-21 上线）

文章页的分享按钮旁、以及每条随想旁有一个「观测」按钮：每位读者对每篇文章、每条随想只能观测一次，按钮显示累计次数。前端已经在本仓库上线（`src/components/ObserveButton.astro`、`src/scripts/reactions.ts`、`src/lib/pow.ts`），后端参考实现放在本仓库的 `ops/reactions-api/`。这个目录不参与构建，`verify-build.mjs` 会检查它没有进入 `dist/`，里面也不含任何密钥。

生产环境按 9.4 部署，实际情况见 9.7。即使这个服务停掉，按钮也会自动隐藏，页面不受影响。所以先上线网站、后部署 API 是安全的，以后升级也是一样。

### 9.1 五层防滥刷（读者无感，不用验证码）

| 层 | 做法 | 挡住什么 |
| --- | --- | --- |
| 1. 目标白名单 | 只接受 `/posts/<slug>.html` 和 `note:<id>` 两种格式，而且必须出现在当前 release 的 `index.json` / `notes.json` 里（文件变化后 10 秒内重新读取）。两份清单都读不到时，写入直接拒绝（503） | 编造 key 往库里塞垃圾 |
| 2. 同源校验 | 申请挑战和提交观测时：如果带了 `Sec-Fetch-Site`，必须是 `same-origin`；并且 `Origin` 等于 `https://parityncsvt.top`，或者（没有 `Origin` 时）`Referer` 以 `https://parityncsvt.top/` 开头 | 别的网站借访客的浏览器代刷。脚本可以伪造这些头，所以这只是第一道门 |
| 3. 无感工作量证明 | `GET /api/reactions/challenge` 返回 HMAC 签名的挑战：绑定目标、访客、难度，60 秒过期，只能用一次（签名校验通过就作废，算错也不能重试）。浏览器要找到 nonce，使 `SHA-256(challenge + ":" + nonce)` 有 14 个前导零比特 | 每一次刷量都要花 CPU；挑战不能跨 IP 转用，也不能重放 |
| 4. 永久去重 | 表 `reactions` 的主键是 `(target, visitor_hash)`，`visitor_hash = HMAC(VISITOR_SALT, IPv4 地址或 IPv6 的 /64)`。重复提交是幂等的，返回当前计数和 `counted: false` | 同一地址重复计数；IPv6 轮换后缀绕过 |
| 5. 限流、熔断、告警 | 每个访客的滑动窗口：读计数 120 次/分，申请挑战 20 次/分，提交 10 次/分、60 次/时，超出返回 429 和 `Retry-After`。同一目标 10 分钟内新增超过 30 次，或全站超过 200 次，就熔断 15 分钟（写入返回 503），每次熔断通过现有的 Telegram 告警 Bot 发一条消息。另在 Cloudflare 边缘加一条速率限制（9.4 第 7 步） | 用大量地址分散刷量：至少会被限速，还会通知到你 |

几点取舍：

- **计的是“不同地址”，不是“不同的人”**：校园网这类共用一个 IPv4 出口的读者会被算成同一位。这正是防刷的代价。浏览器会在 `localStorage` 里记住自己观测过什么；服务端在 `observed` 字段里返回“这个地址观测过哪些”，所以换浏览器也会显示“已观测”。
- **难度取 14 bit，而不是 16 bit**：实测 Chromium 的 Web Crypto 在 M 系列 Mac 上算 65,536 次 SHA-256 需要 150–190 ms（浏览器分批并行计算），Node 22 更慢一些。16 bit 的期望耗时就在这个量级，手机上还要再乘 3–5，算不上“无感”。14 bit 在这台 Mac 上约 40 ms，手机上一两百毫秒，而每次刷量的成本仍然是 1.6 万次哈希。可以用 `POW_DIFFICULTY` 调整，范围 0–32；浏览器读取挑战里签名的难度，不需要重新部署前端。
- **同源校验依赖 `Referer`**：浏览器对同源 GET 不发送 `Origin`，所以申请挑战时只能靠 `Referer`。目前站点没有设置 `Referrer-Policy`，浏览器默认（`strict-origin-when-cross-origin`）会在同源请求中带上完整地址。以后如果加 `Referrer-Policy: no-referrer`，观测功能就会失效。
- **防重放记录在内存里**：服务重启会清空，但挑战 60 秒就过期，数据库的主键仍然保证同一地址只计一次，所以无害。

### 9.2 接口（Caddy 把 `/api/reactions*` 原样转发，服务同时接受带和不带 `/api` 前缀的路径）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/reactions/healthz` | — | `{ "ok": true }` |
| `GET /api/reactions?target=/posts/x.html` | — | `{ counts: { "/posts/x.html": 3 }, observed: [] }`（计数为 0 也会列出；目标不在白名单时返回 400） |
| `GET /api/reactions?prefix=note:` | — | `{ counts: { "note:2026-04-11": 2, … }, observed: ["note:…"] }`，只列出计数大于 0 的；`prefix` 只能是 `note:` 或 `/posts/`。随想页只发这一个请求；读不到对应清单时返回 503（按钮保持隐藏，而不是显示一排 0） |
| `GET /api/reactions/challenge?target=…` | 同源校验 | `{ challenge, difficulty, expiresAt }` |
| `POST /api/reactions` | `{ target, challenge, nonce }`（同源校验，正文最大 4 KB） | `{ target, count, observed: true, counted }` |

错误统一返回 `{ "error": "中文提示" }`：400 格式错误或目标不在白名单；403 同源校验失败，或挑战伪造、过期、属于别的访客或目标、被重放，或工作量不足；413 正文过大；429 限流（带 `Retry-After`）；503 熔断中（带 `Retry-After`）或两份清单都读不到；405 方法不对（带 `Allow`）。所有响应都带 `Cache-Control: no-store` 和 `nosniff`，不带任何 CORS 头。

挑战的格式是 `v1.<base64url(JSON {t, v, d, e, r})>.<base64url(HMAC-SHA256(CHALLENGE_SECRET, "v1." + payload))>`。其中 `t` 是目标，`v` 是访客哈希的前 16 位，`d` 是难度，`e` 是过期时间（毫秒），`r` 是 16 字节随机数。浏览器把它当作不透明字符串。`nonce` 是十进制字符串（1–16 位，不带前导零）。前后端使用同一套检查逻辑，`tests/reactions.test.ts` 会验证浏览器解出的 nonce 能通过服务端的 `verifyPow`。

### 9.3 配置

完整说明见 `ops/reactions-api/.env.example`：

- **必填**：`CHALLENGE_SECRET`、`VISITOR_SALT`。各至少 32 个字符，两者不能相同，也可以改用 `*_FILE` 从文件读取。**`VISITOR_SALT` 设定后不要再改**，去重依赖它。
- **默认值**：
  - 监听 `127.0.0.1:46220`；
  - 数据库 `~/services/pnc-reactions/data/reactions.db`；
  - 白名单读取 `~/services/pnc-blog/current/index.json` 和 `notes.json`；
  - `POW_DIFFICULTY=14`，挑战有效期 60 秒；
  - 各项限流和熔断阈值见 9.1。
- **告警**：`TELEGRAM_BOT_TOKEN(_FILE)`、`TELEGRAM_CHAT_ID(_FILE)`，留空就不发。

### 9.4 在 `personal` 上启用

```bash
# 0. 先登记清单，否则约 10 分钟后会触发 InventoryDrift。在 ~/services.yaml 的 pnc-api 条目后面加上：
#   - name: pnc-reactions
#     title: 观测计数 API pnc-reactions（Node + SQLite）
#     layer: app
#     scope: user
#     units: [pnc-reactions.service]
#     critical: true
#     listen: ["127.0.0.1:46220"]
#     public: Tunnel → parityncsvt.top/api/reactions*（经 pnc-blog）
#     data: [~/services/pnc-reactions/data]
#     health:
#       - {name: pnc-reactions, url: "http://127.0.0.1:46220/reactions/healthz"}

# 1. 代码：从博客仓库取出，放进 pnc-infra 版本化管理，再复制到运行目录
R=~/services/pnc-blog/repo && git -C "$R" fetch -q origin main
mkdir -p ~/services/pnc-infra/reactions
for f in server.mjs lib.mjs schema.sql pnc-reactions.service .env.example; do
  git -C "$R" show "origin/main:ops/reactions-api/$f" > ~/services/pnc-infra/reactions/$f
done
git -C ~/services/pnc-infra add reactions && git -C ~/services/pnc-infra commit -m "pnc-reactions from ops/reactions-api"
install -d -m 700 ~/services/pnc-reactions ~/services/pnc-reactions/data
cp ~/services/pnc-infra/reactions/{server.mjs,lib.mjs,schema.sql} ~/services/pnc-reactions/

# 2. 密钥（.env 为 0600；生成后只放在这个文件里，不进任何仓库）
( umask 077 && cp ~/services/pnc-infra/reactions/.env.example ~/services/pnc-reactions/.env )
sed -i "s/^CHALLENGE_SECRET=$/CHALLENGE_SECRET=$(openssl rand -hex 32)/; s/^VISITOR_SALT=$/VISITOR_SALT=$(openssl rand -hex 32)/" ~/services/pnc-reactions/.env
# Telegram：复用监控已有的告警 Bot，在 .env 里填 TELEGRAM_BOT_TOKEN_FILE / TELEGRAM_CHAT_ID_FILE，
# 指向 ~/observability/secrets 下现成的文件，不要复制令牌本身。

# 3. 启动
systemctl --user link ~/services/pnc-infra/reactions/pnc-reactions.service
systemctl --user daemon-reload && systemctl --user enable --now pnc-reactions
curl -s http://127.0.0.1:46220/reactions/healthz

# 4. Caddy：在 ~/services/pnc-infra/Caddyfile 的 handle_path /api/* 之前加上
#      handle /api/reactions* {
#          reverse_proxy 127.0.0.1:46220
#      }
#    （handle 在 Caddy 的指令顺序里排在 handle_path 之前，所以会先匹配；服务本身接受 /api 前缀。）
caddy validate --config ~/services/pnc-infra/Caddyfile --adapter caddyfile
caddy adapt --config ~/services/pnc-infra/Caddyfile --adapter caddyfile | grep -o '"/api/reactions\*"'
systemctl --user reload pnc-blog
git -C ~/services/pnc-infra commit -am "Caddy: route /api/reactions* to pnc-reactions"

# 5. 端到端
curl -s 'http://127.0.0.1:46213/api/reactions?prefix=note:'
curl -s -H 'Referer: https://parityncsvt.top/notes.html' -H 'Sec-Fetch-Site: same-origin' \
  "http://127.0.0.1:46213/api/reactions/challenge?target=note:$(jq -r '.entries[0].url' ~/services/pnc-blog/current/notes.json | cut -d'#' -f2)"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: https://evil.example' \
  "http://127.0.0.1:46213/api/reactions/challenge?target=/posts/arch-linux.html"   # 应为 403
#    然后在浏览器里打开一篇文章：按钮出现，点一下变成「已观测」，计数加一，刷新后仍是已观测。

# 6. 备份：btrbk 已经每天快照 /home。如果还想要单独的每日 SQLite 备份，可以照抄 pnc-api-backup 的
#    service/timer，设 Environment=DB_PATH=…/reactions.db BACKUP_DIR=~/services/pnc-reactions/backups。
#    注意 pnc-infra/api/backup.sh 最后一行统计用的是 comments 表，复用前改成通用的
#    SELECT COUNT(*) FROM sqlite_master。

# 7. Cloudflare 边缘限流（免费版）：Security → WAF → Rate limiting rules → Create rule
#    匹配：(http.request.uri.path eq "/api/reactions") or (http.request.uri.path eq "/api/reactions/challenge")
#    计数依据：IP；阈值：每 10 秒 20 次；动作：Block，持续 10 秒。
#    免费版只能建 1 条规则，计数周期和封禁时长固定 10 秒，表达式只能用 URI Path 和 Verified Bot 两个字段，
#    所以这里不区分请求方法。读者打开随想页是 1 个请求，每次观测再加 2 个（挑战 + 提交）：
#    10 秒内连点 9 条随想也只有 19 个请求。这条规则只挡在边缘的高频洪泛，
#    细粒度的限制交给服务里的每分钟、每小时滑动窗口。
```

### 9.5 日常操作

```bash
DB=~/services/pnc-reactions/data/reactions.db
sqlite3 -box "$DB" "SELECT target, COUNT(*) AS n FROM reactions GROUP BY target ORDER BY n DESC;"
sqlite3 -box "$DB" "SELECT target, COUNT(*) FROM reactions WHERE created_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour') GROUP BY target;"   # 最近一小时
sqlite3 "$DB" "DELETE FROM reactions WHERE target = '/posts/x.html' AND created_at > '2026-12-01T00:00:00';"           # 清掉一次刷量
journalctl --user -u pnc-reactions -n 50 --no-pager          # 熔断、Telegram 失败等记录
```

熔断后不需要手动处理：冷却 15 分钟后自动恢复。如果确认是误伤，`systemctl --user restart pnc-reactions` 会清空内存中的限流和熔断状态。

### 9.6 撤销

`systemctl --user disable --now pnc-reactions`，删掉 Caddyfile 里的 `handle /api/reactions*` 块并重载，再从 `~/services.yaml` 删掉对应条目。网站不需要改：API 不可用时，按钮会自动隐藏。

### 9.7 实际部署记录（2026-11-21）

- 网站：`12ec45c` 经 Runner 部署，服务器上 73 个测试全部通过，包括在 Node 22.23 上跑的 SQLite 后端测试。
- 服务：严格按 9.4 执行，代码取自 `origin/main` 的 `ops/reactions-api/`，在 `pnc-infra/reactions/` 版本化（含当前的 `README.md`）。所有配置用默认值，两个密钥新生成，只存在于 `.env`（0600）。
- 告警：`.env` 里的 `TELEGRAM_BOT_TOKEN_FILE` / `TELEGRAM_CHAT_ID_FILE` 指向 `~/observability/secrets/` 下监控告警 Bot 的现成文件，熔断时由同一个 Bot 通知。服务启动时会读取这两个文件，所以服务能正常运行就说明它们可读。上线时没有发送测试消息。
- Caddy：`handle /api/reactions*` 放在 `handle_path /api/*` 之前（第 2.1 节的 Caddyfile 已同步），`caddy adapt` 确认路由顺序为 `/api/reactions*`、`/api/*`、`/404`，热重载不中断服务。
- 监控（`~/services.yaml`，先登记、后建单元）：`pnc-reactions.service` 和 `pnc-reactions-backup.timer` 存活检查；本机健康检查 `pnc-reactions`；经 Cloudflare 的公网探测 `reactions-public`（`https://parityncsvt.top/api/reactions/healthz`）。失败时触发现有的 `server_local_http_probe_success == 0` / `server_public_direct_probe_success == 0` 告警。漂移为 0，没有告警。
- 备份：`pnc-infra/reactions/pnc-reactions-backup.{service,timer}` 每天 04:45 调用 `pnc-infra/api/backup.sh`（设置 `DB_PATH` 和 `BACKUP_DIR`），备份存入 `~/services/pnc-reactions/backups/`，保留 30 天。`backup.sh` 和 `restore.sh` 结尾的统计已改成通用写法（3.6）。
- 验证：
  - 直连和经 Cloudflare 都通过：计数、白名单（不存在的文章返回 400）。
  - 伪造 `Origin`、缺少 `Referer` 或 `Sec-Fetch-Site: cross-site` 的挑战请求都返回 403；响应带 `no-store`，Cloudflare 不缓存。
  - 浏览器真实点击：工作量证明加上两次经 Cloudflare 的请求，共约 0.8 秒，计数 0 → 1，刷新后仍是「已观测」。随想页只发一个 `?prefix=note:` 请求。
  - 测试写入的那条记录随后已删除，计数从 0 开始。

---

## 10. 私人 Telegram Bot（`pnc-bot`，2026-11-22 上线）

在手机上私聊 Bot 就能发随想（文字、单图、相册）、建文章草稿、管理评论、查看状态和日志、重新部署或回滚、重启服务。Bot 复用监控告警的 **@lyy_server_monitor_bot**（同一个 token），只回应主人的私聊。代码在本仓库 [`ops/telegram-bot/`](ops/telegram-bot/README.md)，部署副本在服务器的 `~/services/pnc-infra/bot/`。命令细节、文件结构、安装步骤和测试方法见那份 README，本节只记录架构、安全设计、配置清单和排障。

### 10.1 架构

```text
手机 Telegram（只认主人私聊）
   │
api.telegram.org ◄── 长轮询 getUpdates（经 mihomo 127.0.0.1:7890）── pnc-bot.service（Python 3.13 + 系统 Pillow，不监听端口）
   ▲                                                                   │
   │ sendMessage（同一个 token，只发不收）                               ├─ 随想 / 草稿：图片重编码去元数据 → ~/services/pnc-bot/repo（专用 clone）
   ├─ Alertmanager → 告警群                                            │     → git push（Deploy Key，只有本仓库写权限）→ main
   └─ observability-daily-report（每天 09:00）→ 告警群                  │     → Runner → deploy.sh → state/deploys.jsonl → Bot 回执「✅ 已上线」+ 链接
                                                                       ├─ 评论：每 30 秒只读查询 pnc.db，有新评论就私聊推送；隐藏 = status 改为 hidden
                                                                       ├─ /status /logs：systemctl、journalctl、本机健康检查、Alertmanager API
                                                                       └─ /deploy /restart：systemd-run --user 临时单元跑 deploy.sh；systemctl --user restart

deploy.sh（每次部署 / 回滚）──► logs/deploy-*.log、state/last-deploy.json、state/deploys.jsonl
                           └─► node-textfile/pnc_blog_deploy.prom ──► Prometheus「PncBlogDeployFailed」──► Alertmanager ──► 告警群
```

- **只有一个 getUpdates 消费者**：告警链路（Alertmanager、日报）只调用 `sendMessage`，没有 webhook，所以由 Bot 独占接收不会影响告警。Bot 永远不调用 `setWebhook` / `deleteWebhook` / `logOut` / `close`。别的进程（例如 `observability/scripts/get-telegram-chat-id.py`）同时 `getUpdates` 会让双方收到 `409 Conflict`，Bot 会每 30 秒重试。
- **服务异常仍由 Alertmanager 发到告警群**，Bot 不重复推送；`/status` 会列出正在触发的告警。**部署失败**有两路通知：Prometheus 规则 `PncBlogDeployFailed` 发到告警群；Bot 私聊回报（如果是 Bot 自己的提交或部署，就更新那条回执）。
- 发随想的全过程：`⏳ 处理中…` → `📝 已提交 <sha>，部署中…` → 部署记录出现，且本机确认 `/notes/<id>.html` 返回 200 → `✅ 已上线` + `https://parityncsvt.top/notes/<id>.html`；失败则显示 `❌ 部署失败`、错误摘要，以及 `/logs deploy`、`/undo` 提示。
- Bot 的提交作者是 `pnc-bot <pnc-bot@parityncsvt.top>`，带 `Pnc-Bot:` trailer。`security.yml` 跳过这些提交的 CodeQL 和 npm audit（4.1）。

### 10.2 命令速览

| 命令 | 作用 | 确认 |
| --- | --- | --- |
| 直接发文字 / 图片 / 相册，`/note 文本` | 新随想。首行 `# 标题` 或 `标题 \| 正文` 可带标题 | |
| 回复回执或自己的原消息、`/append [id] 文本` | 追加到那条随想（文字或图片） | |
| `/draft 标题[ \| slug]` + 正文 + 图片 | 新建 `src/content/posts/<slug>/index.mdx`（`draft: true`），图片放在同目录 | |
| `/list` | 最近 5 条随想及链接 | |
| `/undo` | 撤销 Bot 最近一次的随想、追加或草稿提交（`git revert`，图片一起移除，不改写历史） | ✅ |
| `/comments`、`/delcomment <id>` | 最近 10 条评论；隐藏一条（新评论通知上也有按钮） | 隐藏 ✅ |
| `/status`、`/logs [名称]` | 版本、服务、健康检查、告警、最近部署；`pnc-blog` `pnc-api` `pnc-reactions` `pnc-bot` `runner` `cloudflared` `deploy` 的最近日志 | |
| `/deploy [sha]` | 不带参数：部署开始时 main 的最新提交；带 sha：保留版本几秒切换，否则重新构建 | ✅ |
| `/restart <名称>` | 只限 `pnc-blog`（Caddy）、`pnc-api`、`pnc-reactions`，重启后做健康检查 | ✅ |
| `/help` | 命令说明 | |

确认按钮一次有效，120 秒过期，只认主人在私聊里点的。

### 10.3 安全设计

1. **严格单人鉴权**：只处理 `from.id`、`chat.id` 都等于 `PNC_BOT_ALLOWED_USER_ID` 且 `chat.type == "private"` 的消息和按钮。陌生人、群聊（包括告警群）、频道、编辑过的消息、内联查询等一律静默丢弃：不回复、不下载、不缓冲，日志只记 `ignored <类型>`，不含 id 和内容。命令菜单也只对主人的私聊设置（`setMyCommands` 的 chat scope）。user id 没填或者还是示例值时，Bot 拒绝启动。
2. **没有任意命令执行**：没有执行 shell 的入口。所有子进程都是固定 argv、不经过 shell，服务名只能来自常量表，SHA 先过 `^[0-9a-f]{7,40}$` 再交给 git 解析，回滚目标只能是 releases 目录里、部署记录显示最后一次成功的版本。`/undo` `/delcomment` `/deploy` `/restart` 都要二次确认。`/undo` 只认作者是 `pnc-bot`、带 `Pnc-Bot` trailer、只改动随想、随想图片或草稿的提交。
3. **隐私**：图片入库前一律重编码为 WebP（宽度不超过 1600，自动按 EXIF 方向摆正，色彩转 sRGB），不含 EXIF、XMP、GPS 或 ICC；写入前解析 RIFF 块再检查一遍。文件名用内容哈希，不暴露原文件名。日志里没有消息正文、文件 id、token 或用户 id；发到 Telegram 的日志尾部会去掉 token 形状的字符串。
4. **凭据隔离**：token 只从 `~/observability/secrets/telegram_bot_token` 读取（与告警共用，0600）；user id 在 `~/.config/pnc-bot/config.env`（0600）。推送用专门的 **Deploy Key**，只对本仓库有写权限，没有用服务器上 gh 登录的全账号 token。公开仓库里没有任何密钥。
5. **沙箱**：`pnc-bot.service` 设了 `ProtectSystem=strict` 和 `ProtectHome=read-only`，只能写自己的 clone、状态目录和评论库目录；另有 `NoNewPrivileges`、`PrivateTmp`、`MemoryMax=768M`。部署和重启通过用户管理器在沙箱外执行，Bot 发起的部署单元限制与 Runner 一致（`RuntimeMaxSec=1800`、`MemoryMax=4G`）。

### 10.4 配置清单（脱敏）

| 位置 | 权限 | 内容 | 进公开仓库？ |
| --- | --- | --- | --- |
| `~/.config/pnc-bot/config.env` | 0600 | `PNC_BOT_ALLOWED_USER_ID=<主人的数字 user id>`、`PNC_BOT_TOKEN_FILE=/home/lyy/observability/secrets/telegram_bot_token`、`PNC_BOT_PROXY=http://127.0.0.1:7890`；其余变量用默认值（见 `config.env.example`） | 否（仓库里只有占位的 example） |
| `~/observability/secrets/telegram_bot_token` | 0600 | Bot token，与 Alertmanager、日报共用，没有改动 | 否 |
| `~/.config/pnc-bot/deploy_key`、`.pub` | 0600 / 0644 | GitHub Deploy Key「pnc-bot (personal server, notes/drafts)」，read-write，只对 `Boshugege/boshugege.github.io` 有效。指纹 `SHA256:1w+SFaAMepq6T4ET0n6T5rt8Yq831r7FKiBtuUjAOTI` | 否 |
| `~/services/pnc-bot/repo` | 0755 | Bot 专用 clone：`core.sshCommand` 为 `ssh -F /dev/null -i …/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes …`（原因见 10.6），`user.name=pnc-bot`，`core.hooksPath=/dev/null` | — |
| `~/.local/state/pnc-bot/` | 0700 | `state.db`（update offset、评论游标、随想 ↔ 消息映射）、`git.lock` | 否 |
| `~/services/pnc-infra/bot/` | — | 代码部署副本，在 pnc-infra 的 git 里版本化 | 代码同 `ops/telegram-bot/` |
| `~/.config/systemd/user/pnc-bot.service` | 0644 | 用户单元（同 `ops/telegram-bot/pnc-bot.service`） | 是 |
| `~/services.yaml` → `pnc-bot` | — | 关键单元 `pnc-bot.service` 存活监控；不监听端口 | — |
| `~/services/pnc-blog/{logs,state}/` | — | deploy.sh 的每次部署日志（保留 30 个）、`last-deploy.json`、`deploys.jsonl` | — |
| `~/observability/prometheus/rules/pnc-blog.yml` | 0644 | `PncBlogDeployFailed`（`pnc_blog_deploy_last_success == 0` 持续 1 分钟，warning） | 否 |

### 10.5 安装与升级

首次安装按 README 的「配置与部署」执行（clone、配置、`--check-config`、`--selftest-command`、启用单元）。新建单元前**先在 `~/services.yaml` 登记**。升级：

```bash
# 在 Mac 上
rsync -a --delete --exclude __pycache__ --exclude tests ops/telegram-bot/ personal:services/pnc-infra/bot/
# 在服务器上
cd ~/services/pnc-infra && git add bot && git commit -m "bot: …"
python3 -I bot/bot.py --check-config   # 先 set -a; . ~/.config/pnc-bot/config.env; set +a
systemctl --user restart pnc-bot && journalctl --user -u pnc-bot -n 30 --no-pager
```

停用：`systemctl --user disable --now pnc-bot`，从 `~/services.yaml` 删掉条目，并在 GitHub 仓库的 Settings → Deploy keys 删除这把 key。

### 10.6 排障

```bash
systemctl --user status pnc-bot
journalctl --user -u pnc-bot -n 100 --no-pager          # 只有事件类型和结果，没有消息内容
set -a; . ~/.config/pnc-bot/config.env; set +a
python3 -I ~/services/pnc-infra/bot/bot.py --check-config
python3 -I ~/services/pnc-infra/bot/bot.py --selftest-command "/status"   # 不读 token、不连 Telegram
git -C ~/services/pnc-bot/repo log --author=pnc-bot -5 --format='%h %s'
tail -n 5 ~/services/pnc-blog/state/deploys.jsonl | jq -c '{trigger, sha: .sha[0:7], result, error}'
tail -n 40 "$(jq -r .log ~/services/pnc-blog/state/last-deploy.json)"
```

| 现象 | 原因与处理 |
| --- | --- |
| Bot 不回任何消息 | 先看 `systemctl --user status pnc-bot`。确认是在和 Bot **私聊**（群里一律忽略），并且 `PNC_BOT_ALLOWED_USER_ID` 是自己的 id。日志里有 `ignored message` 说明消息来自别的会话或别的账号。 |
| 日志反复出现 `409 Conflict` | 别的进程在用同一个 token 调 `getUpdates` 或设了 webhook。不要随手 `deleteWebhook`；先查 `getWebhookInfo`，停掉那个进程。 |
| Telegram 请求超时或 `TLS handshake timeout` | mihomo 代理不稳定，告警链路也会受影响。Bot 和 Alertmanager 都会自动重试，查 `systemctl status mihomo`。 |
| 服务里 git fetch/push 失败，手动在 shell 里却正常 | 用户单元的命名空间里，root 拥有的 `/etc/ssh/ssh_config.d/*.conf` 显示成 `nobody` 所有，ssh 因此拒绝运行。clone 的 `core.sshCommand` 必须带 `-F /dev/null`（10.4）。在 Bot 里发 `/list` 可以验证。 |
| `⚠️ 推送被拒` / 推送重试三次仍失败 | 同时有别的提交推到 main，Bot 会自动 fetch 后重做，三次都失败才报错。也可能是 deploy key 被删了或 GitHub 不可达，用 `ssh -F /dev/null -i ~/.config/pnc-bot/deploy_key -T git@github.com` 检查。 |
| 回执一直停在「部署中」 | 看 `/status` 和 `deploys.jsonl`：Runner 可能在排队（例如 CodeQL 正在跑），或部署失败但没有记录。超过 20 分钟 Bot 会说明「未确认上线」。 |
| `/undo` 说会冲突 | 在 Bot 的提交之后，有人在 notes.md 的相邻位置手动改过（例如在 Mac 上写了随想）。手动 `git revert` 并解决冲突。 |
| 想恢复被隐藏的评论 | `sqlite3 ~/services/pnc-api/data/pnc.db "UPDATE comments SET status='visible' WHERE id=<id>;"`（3.4） |

### 10.7 实际上线记录（2026-11-21 ~ 11-22）

- **方案确认后才动手**：先只读探查了告警 Bot（只调用 `sendMessage`，没有 webhook，没有进程在 `getUpdates`）、代理路径（容器经 49847，宿主机经 7890）、图片工具（Pillow 11.1，不支持 HEIF）和 Git 凭据（`gh` 的 token 是全账号权限，所以不用它），把探查结果和方案交给本人确认后才开始实现。
- **凭据**：Deploy Key 在服务器上生成，私钥没有离开过服务器。主人的 user id 用一次性绑定码从 `getUpdates` 读出，直接写进 `config.env`（0600），读完把这两条更新标记为已确认，Bot 启动后不会处理它们。
- **测试**：488 个单元和集成测试全部通过（跑在服务器的 Python 3.13 上，也跑了 `-X dev -W error` 严格模式）。集成测试把真实的 App、git（临时 bare 远端）、Pillow 和部署跟踪器接在一起，只把 Telegram 的 HTTP 层换成假的。
- **对抗式审查**：从安全隐私、内容和 Git、运行稳定性、运维部署和网站四个角度审查，共报出 30 个问题。逐条反驳后 2 个被推翻，确认 28 个：1 个高、6 个中、21 个低。修复分三组并行进行，每个问题都有"修复前失败、修复后通过"的回归测试。
  - 高：草稿正文里的 MDX `import`/`export` 行或行内三个反引号会让构建失败。现在草稿内容按 MDX 结构转义成字符引用；用约 16,400 份随机草稿以网站真实的 remark/rehype 配置编译，0 失败。
  - 中：
    - 部署跟踪改为先登记、后启动，记录不会在登记前被读走。
    - 回复追加在 Git 队列里解析，不会因为上一条还在提交而变成新随想。
    - `/deploy` 只回滚到最后一次部署成功的保留版本。
    - 草稿引用了不存在的图片时直接拒绝。
    - 中文标点旁的加粗、斜体在 CommonMark 规则下会显示成字面上的星号，这种位置改用 HTML 标签。
  - 安全底线（鉴权、无任意命令、EXIF 去除、凭据）没有发现问题。
- **沙箱实测**：发现 ssh 在 `ProtectSystem=strict` 的用户单元里拒绝运行（见 10.6），改用 `ssh -F /dev/null`。systemctl、journalctl、嵌套的 systemd-run、对 pnc.db 的读写、代理和本机健康检查在沙箱里都正常，写其他目录会被拒绝。
- **deploy.sh**：新版装好后，手动重部署（`manual`）和 Runner 部署 `31a31a4`（`github-actions`）都正确写出了日志、记录和指标。`PncBlogDeployFailed` 规则已经通过 `promtool check rules` 并加载。
- **上线**：先在 `~/services.yaml` 登记 `pnc-bot`，再启用单元，漂移为 0。验证：
  - Telegram 端：命令菜单只设在主人的私聊里，默认范围为空；没有 webhook；常驻内存约 37 MB。
  - 正式沙箱里的只读自检（`/status`、`/logs`、`/comments`、`/list`）输出正确。
  - **原有告警链路**：向 Alertmanager 发了一条 `PncBotAlertChainTest`（90 秒后自动恢复），触发和恢复两条消息都第一次就发到告警群（Telegram 请求 +2，失败 +0）。
- **待主人验收**：用手机发一条图文随想，做端到端验收（步骤见 README「用法」）。
