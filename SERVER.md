# SERVER.md：博客在 `personal` 上的运维手册

本文记录博客生产环境的**实际状态**（2026-11-21 完成瘦身并上线），以及日常操作、排障和回滚方法。文中嵌入的配置和脚本与服务器上 `/home/lyy/services/pnc-infra`（本地 git 仓库）里的文件逐字一致。改动时先改那边并提交，再同步到这里。

服务器还有一份全机手册 `~/SERVER.md` 和服务清单 `~/services.yaml`，监控、健康检查、公网探测和漂移告警都由那份清单驱动。**新增或移除任何 unit 或端口，先改 `~/services.yaml`**，否则约 10 分钟后会触发 `InventoryDrift` 告警。

---

## 0. 速览

| 组件 | 现在 | 瘦身前 |
| --- | --- | --- |
| 静态站 | `pnc-blog.service` → **Caddy 2.6.2**，`127.0.0.1:46213` | 同名服务跑自写的 `server.mjs` |
| 评论、阅读数、统计 | `pnc-api.service` → **Node + `node:sqlite`**，`127.0.0.1:46219`，数据在一个文件 `pnc.db` | `pnc-comment-api`（46215）→ Directus 容器 `pnc-cms`（46214）→ Directus 的 SQLite |
| 部署 | Runner 调用 `deploy.sh`：临时 worktree 里 `npm ci` + `npm run verify`，**只构建一次**；保留最近 5 个 release | GitHub 托管 Runner 和服务器各构建一次；部署成功后删除所有旧 release |
| 回滚 | `deploy.sh --rollback`，实测 **0.3 秒** | 只能重新构建旧提交 |
| 备份 | `pnc-api-backup.timer` 每日 04:30 在线备份 `pnc.db`，保留 30 天 | 只有全机的 btrbk 快照 |
| 安全检查 | secret scanning + push protection、Dependabot、私密漏洞报告、`main` 分支保护、外部贡献者审批、自托管 CodeQL + npm audit、Runner 作业守卫 | 全部未开启 |
| 常驻内存 | Caddy + 一个 Node 进程 | `server.mjs` + `pnc-comment-api` + Directus 容器 |

```text
浏览器 ──HTTPS──► Cloudflare ──Tunnel (cloudflared)──► 127.0.0.1:46213  Caddy（pnc-blog.service）
                                                        ├─ file_server：pnc-blog/current → releases/<时间>-<SHA>
                                                        │    · .br / .gz 旁文件优先；/_astro/* 一年 immutable
                                                        │    · /404 与缺失路径 → 404.html，HTTP 404 + no-store
                                                        └─ /api/*（去掉前缀）──► 127.0.0.1:46219  pnc-api（pnc-api.service）
                                                                                    └─ ~/services/pnc-api/data/pnc.db

push main / 手动运行 ──► 本机 Runner（pnc-github-actions-runner.service，作业守卫先检查）
    ├─ Verify and Deploy Site ──► deploy.sh：worktree → npm ci → npm run verify → 原子切换 → 健康检查 → 失败回滚
    └─ 部署结束后：Security Checks ──► CodeQL + npm audit（每周一也跑一次）
```

### 0.1 目录与端口

```text
/home/lyy/services/
├── pnc-infra/                  # 本地 git 仓库：Caddyfile、deploy.sh、作业守卫、api/、systemd/、oneoff/
├── pnc-blog/
│   ├── deploy.sh -> ../pnc-infra/deploy.sh      # workflow 调用的路径不变
│   ├── repo/                   # 博客仓库的克隆，deploy.sh 从这里建临时 worktree
│   ├── releases/               # 最近 5 个构建产物
│   ├── current -> releases/…   # Caddy 的站点根目录
│   ├── backups/pre-caddy-*/    # 切换前的 unit 文件
│   └── server.mjs, deploy.sh.pre-20261121       # 旧版，2026-12-05 后删除
├── pnc-api/                    # 0700
│   ├── .env                    # 0600，密钥（沿用旧评论 API 的 Turnstile 密钥和 HMAC 密钥）
│   ├── data/pnc.db             # 评论与阅读记录
│   └── backups/                # 每日备份 pnc-YYYYMMDD-HHMMSS.db.gz
├── pnc-comment-api/            # 已停用（0700），2026-12-05 后删除
├── pnc-cms/                    # Directus 数据（已停用），2026-12-05 后归档并删除
└── actions-runner-pnc-blog/    # GitHub Actions Runner（.env 里配置作业守卫）
```

| 端口（127.0.0.1） | 用途 |
| --- | --- |
| 46213 | Caddy（博客，cloudflared 的上游） |
| 46219 | pnc-api |
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
echo "backup: $DEST/pnc-$stamp.db.gz ($(sqlite3 "$DB" 'SELECT COUNT(*) FROM comments;') comments)"
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
echo "restored $BACKUP ($(sqlite3 "$DB" 'SELECT COUNT(*) FROM comments;') comments)"
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
- `security.yml`（**Security Checks**）：每次部署结束后（`workflow_run`，只接受本仓库 push 或手动触发产生的运行）、每周一 11:23 以及手动运行时，执行 CodeQL（`security-extended`）和 `npm audit`。action 全部钉死到 commit SHA。

两条 workflow 都没有 `pull_request` 触发器。

### 4.2 `deploy.sh`

```bash
#!/usr/bin/env bash
# Verify, build and publish one commit of PNC's Blog; keep the last few
# releases so going back is a symlink switch.
#
#   DEPLOY_SHA=<sha> deploy.sh    deploy a commit on main (default: origin/main)
#   deploy.sh --rollback [name]   serve the previous (or the named) release again
#   deploy.sh --list              list releases, marking the one being served
set -Eeuo pipefail
umask 022

APP_ROOT=${APP_ROOT:-/home/lyy/services/pnc-blog}
REPO_URL=${REPO_URL:-https://github.com/Boshugege/boshugege.github.io.git}
SITE_URL=${SITE_URL:-http://127.0.0.1:46213}
KEEP_RELEASES=${KEEP_RELEASES:-5}
REPO=${REPO:-$APP_ROOT/repo}
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
export ASTRO_TELEMETRY_DISABLED=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm_config_fund=false npm_config_audit=false

log() { printf '[deploy %s] %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

mkdir -p "$RELEASES"
exec 9>"$APP_ROOT/deploy.lock"
flock -n 9 || die "another deploy is running"

WORK=""
cleanup() {
  if [[ -n $WORK ]]; then
    git -C "$REPO" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    git -C "$REPO" worktree prune
  fi
}
trap cleanup EXIT

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
  --rollback)
    current=$(basename "$(serving)")
    target=${2:-$(ls -1 "$RELEASES" | sort | awk -v cur="$current" '$0 < cur' | tail -n 1)}
    [[ $target =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{12}$ && -d $RELEASES/$target ]] || die "no such release: ${target:-none} (see --list)"
    switch_to "$RELEASES/$target"
    health_check || die "health check failed on $target; current is still $target, see --list"
    exit 0
    ;;
  "") ;;
  *) die "unknown option: $1" ;;
esac

# 1. Fetch main and resolve the commit. Only commits on main are deployable.
[[ -d $REPO ]] || git clone --bare --quiet "$REPO_URL" "$REPO"
git -C "$REPO" fetch --quiet --prune origin '+refs/heads/main:refs/remotes/origin/main'
want=${DEPLOY_SHA:-origin/main}
[[ $want == origin/main || $want =~ ^[0-9a-f]{7,40}$ ]] || die "DEPLOY_SHA must be a 7-40 character hex SHA"
sha=$(git -C "$REPO" rev-parse --verify --quiet "${want}^{commit}") || die "unknown commit: $want"
git -C "$REPO" merge-base --is-ancestor "$sha" origin/main || die "$sha is not on main; refusing to deploy"
short=${sha:0:12}

# 2. Reuse a release already built from this commit (redeploy / rollback by
#    SHA takes seconds), otherwise build one in a throwaway worktree.
release=$(ls -1d "$RELEASES"/*-"$short" 2>/dev/null | sort | tail -n 1 || true)
if [[ -n $release ]]; then
  log "reusing $(basename "$release") for $short"
else
  WORK=$(mktemp -d "$APP_ROOT/.build.XXXXXX")
  git -C "$REPO" worktree add --detach --quiet "$WORK/src" "$sha"
  log "verifying and building $short"
  (cd "$WORK/src" && npm ci --prefer-offline && npm run verify)
  release=$RELEASES/$(date '+%Y%m%d-%H%M%S')-$short
  mv "$WORK/src/dist" "$release"
fi

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
- **保留最近 5 个 release**，正在服务的那个永远不会被删除；`flock` 防止并发部署。

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

也可以在 GitHub Actions 页面手动运行 **Verify and Deploy Site**，在 `deploy_sha` 中填写 `main` 上的历史提交。

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
| Code scanning（CodeQL） | 自托管 | `security.yml`。首次扫描报出 4 条，都是“标签过滤不彻底”，已在同一批改动中修复 |
| 私密漏洞报告 | 开启 | 见 [SECURITY.md](SECURITY.md) |
| 外部贡献者的 PR 运行 | 一律需要审批 | `fork-pr-contributor-approval = all_external_contributors` |
| `main` 分支规则 | 禁止删除和强制推送 | 不要求 PR 和状态检查，仍然可以直接 push |
| Workflow 权限 | 只读 | 各 workflow 显式声明 `permissions` |
| GitHub Pages | **待处理** | 用户主页仓库不能停用 Pages。把来源改为 GitHub Actions 可以停止每次 push 产生的失败构建（`pages-build-deployment`），这一步需要本人在 Settings → Pages 里操作 |

快速检查：

```bash
R=Boshugege/boshugege.github.io
gh api repos/$R --jq .security_and_analysis
gh api "repos/$R/dependabot/alerts?state=open" --jq length
gh api "repos/$R/code-scanning/alerts?state=open" --jq length
gh api "repos/$R/secret-scanning/alerts?state=open" --jq length
gh api repos/$R/actions/permissions/fork-pr-contributor-approval
gh api repos/$R/rulesets --jq '.[].name'
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
systemctl --user status pnc-blog pnc-api pnc-api-backup.timer pnc-github-actions-runner
~/services/pnc-blog/deploy.sh --list
curl -s http://127.0.0.1:46219/healthz; curl -s http://127.0.0.1:46213/api/stats

# 日志
journalctl --user -u pnc-blog -f
journalctl --user -u pnc-api -f
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
| `another deploy is running` | 确实有一次部署还在跑，用 `pgrep -af deploy.sh` 查看。 |
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

- [ ] **GitHub Pages**：在 Settings → Pages 把来源改为 *GitHub Actions*，停止每次 push 都会失败的旧版构建。自动操作被权限策略拦下了，需要本人确认。
- [ ] **Cloudflare**：在 Zero Trust → Networks → Tunnels → Public hostnames 中删除 `cms.parityncsvt.top`。Directus 已停用，这个主机名现在只会返回 502。
- [ ] **2026-12-05 后清理**：`~/services/pnc-comment-api`、`~/services/pnc-cms`（先把 `data/` 和 `uploads/` 归档到 `/srv/backup`）、`pnc-cms.container.retired`、Directus 镜像（`podman rmi`）、`~/services.yaml` 里的 `comment-api` 和 `cms` 条目、`pnc-blog/server.mjs`、`deploy.sh.pre-20261121`、`pnc-blog/backups/pre-caddy-*`。
- [ ] 可选：给 `pnc-infra` 加一个私有远端仓库（需要在服务器上配置部署密钥）。
- [ ] 可选：Runner 改用专用用户运行（5.3）。
