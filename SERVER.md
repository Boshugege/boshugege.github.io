# SERVER.md：`personal` 服务器运维与瘦身方案

本文是博客生产环境（服务器 `personal`）的运维手册和改造方案。**这里只有方案和模板，本仓库不含任何服务器代码，文中的改造都还没有在 `personal` 上落地。** 照着第 7 节的顺序一步步做，每一步都能单独验证、单独回滚。

模板的验证情况：

| 模板 | 本地验证 |
| --- | --- |
| `pnc-api`（`server.mjs` + `schema.sql`） | Node 26 + `node:sqlite` 跑过 `/api/*` 全部接口：评论读写、Turnstile（用 Cloudflare 的测试密钥）、频率限制、按日去重计数、站点统计 |
| `migrate-from-directus.mjs` | 用两种可能的 Directus 导出结构（逐条阅读记录 / 计数器）跑过，可重复执行 |
| `backup.sh` / `restore.sh` | 在线备份、完整性检查、恢复、拒绝损坏备份 |
| `deploy.sh` | 在 macOS 上用 GNU 工具替身和模拟的静态服务跑过：连续发布、只保留 5 个 release、按 SHA 复用、`--rollback`、verify 失败不影响线上、健康检查失败自动回滚 |
| `Caddyfile` | **未验证**（本机没有 Caddy）。上线前必须在服务器上跑 `caddy validate`，并按 3.3 节逐条 `curl` 对比 |

---

## 0. 速览

| | 现在 | 瘦身后 |
| --- | --- | --- |
| 静态服务 | 自写 `server.mjs`（`pnc-blog.service`） | Caddy，一份约 40 行的 `Caddyfile` |
| 评论与阅读数 | `pnc-comment-api` → Directus（`pnc-cms`）→ Directus 的数据库 | 一个 `pnc-api` 进程 + 一个 SQLite 文件 |
| 常驻进程 | 静态服务 + 评论 API + Directus（可能还有它的数据库） | Caddy + `pnc-api` |
| CI 构建 | GitHub 托管 Runner 构建一次，服务器再构建一次 | 只在 self-hosted Runner 上构建一次（本仓库的 workflow 已改好） |
| 回滚 | 发布成功后旧 release 被删，回滚要重新构建 | 保留最近 5 个 release，`deploy.sh --rollback` 秒级切换 |
| 备份 | `/home/lyy/services` 每日备份 | 另加 `pnc.db` 每日在线备份，保留 30 天，可一条命令恢复 |
| 配置管理 | 散在服务器各处 | 私有仓库 `pnc-blog-infra` 版本化，密钥只放 `.env` |

---

## 1. 现状与冗余

### 1.1 现状

（来自 README 的“生产服务”一节）

```text
浏览器 ──HTTPS──► Cloudflare（边缘缓存）──► 127.0.0.1:46213  pnc-blog.service（server.mjs）
                                              ├─ 静态文件：/home/lyy/services/pnc-blog/current → releases/<id>
                                              └─ /api/* ──► pnc-comment-api ──REST──► pnc-cms（Directus）──► Directus 数据库

push main ──► GitHub 托管 Runner：npm ci + npm run verify            ← 第 1 次构建
          └─► self-hosted Runner（pnc-blog）──► deploy.sh：
                 临时 worktree + npm run verify + 构建                 ← 第 2 次构建
                 → 原子切换 current → 健康检查 → 删除所有旧 release
```

### 1.2 维护负担在哪里

1. **自写静态服务器 `server.mjs`**：MIME 类型、`Accept-Encoding` 协商、预压缩旁文件、`Content-Length` / `Vary`、缓存头、404 页面的状态码和 `no-store`，全都是自己写、自己测、自己修。这些正是通用 Web 服务器的基本功能，Caddy 用一个配置块就能做到，而且经过大量生产环境检验。
2. **为两张表维护三层服务**：Directus 只存评论和 `post_views` 两张表，却要承担一整套 CMS（管理后台、权限系统、扩展、数据库迁移、每次升级的兼容性检查），常驻内存通常是几百 MB。`pnc-comment-api` 又是夹在浏览器和 Directus 之间的翻译层。三层任何一层挂了，评论都会失效，排查时要看三份日志。
3. **重复构建**：每次 push，GitHub 托管 Runner 和服务器各跑一次完整的 `npm ci` + `npm run verify`。两次结果没有关联，服务器上那次才是真正发布的产物，前一次只是多花几分钟。（**本仓库的 `.github/workflows/build-site.yml` 已经改为只在 self-hosted Runner 上执行**，见第 5.5 节。）
4. **无法秒级回滚**：部署成功后只保留当前 release。发现问题再回滚，只能针对旧提交重新跑一遍完整构建，花几分钟，而且依赖 npm 源和网络都正常。
5. **配置没有版本化**：`server.mjs`、`deploy.sh`、systemd 单元和各服务配置只存在于服务器上，改错了很难看出改了什么，也没法回到之前的版本。

---

## 2. 目标架构

全部仍在 `personal` 本机自托管，不依赖任何外部云算力：

```text
浏览器 ──HTTPS──► Cloudflare ──► 127.0.0.1:46213  Caddy（pnc-blog.service）
                                   ├─ file_server：current/
                                   │    · 优先返回 .br / .gz 旁文件
                                   │    · /_astro/* 一年 immutable
                                   │    · /404 与缺失路径 → 404.html，HTTP 404 + no-store
                                   └─ /api/* ──► 127.0.0.1:46214  pnc-api（Node + node:sqlite）
                                                     └─ /home/lyy/services/pnc-api/data/pnc.db（单文件）

push main / workflow_dispatch ──► self-hosted Runner ──► deploy.sh
    临时 worktree → npm ci → npm run verify（唯一一次构建）→ 原子切换 current
    → 健康检查（首页、文章、/404、缺失页、RSS、/api/*）→ 失败自动回滚 → 保留最近 5 个 release

systemd timer（每天 04:30）──► backup.sh：sqlite3 .backup → 完整性检查 → gzip，保留 30 天
```

### 2.1 目录约定

```text
/home/lyy/services/
├── pnc-infra/                 # 私有仓库 pnc-blog-infra 的检出（见 5.1），只放代码和配置
├── pnc-blog/                  # 静态站的运行状态
│   ├── deploy.sh -> ../pnc-infra/deploy.sh   # workflow 调用的路径保持不变
│   ├── repo.git/              # 博客仓库的 bare clone，deploy.sh 自动创建
│   ├── releases/              # 最近 5 个构建产物，目录名 <时间>-<12 位 SHA>
│   └── current -> releases/…  # Caddy 的站点根目录
└── pnc-api/
    ├── .env                   # 密钥（chmod 600，不入库）
    └── data/pnc.db            # 评论与阅读数
/home/lyy/backups/pnc-api/     # pnc.db 的每日备份
```

---

## 3. Caddy 替代 `server.mjs`

### 3.1 `Caddyfile`

要求 Caddy ≥ 2.7（`client_ip_headers`）。可以用发行版或 Caddy 官方仓库的包，也可以直接用官方的单个二进制文件。

```caddyfile
# PNC's Blog: static site + /api proxy. Replaces pnc-blog's server.mjs.
# Cloudflare → 127.0.0.1:46213 (plain HTTP on loopback; TLS ends at Cloudflare).
{
	admin localhost:2019
	auto_https off
	persist_config off
	servers {
		# Only local processes (cloudflared) connect; take the visitor's IP from Cloudflare.
		trusted_proxies static 127.0.0.1/32 ::1/128
		client_ip_headers CF-Connecting-IP X-Forwarded-For
	}
}

http://:46213 {
	bind 127.0.0.1
	root * /home/lyy/services/pnc-blog/current

	header {
		X-Content-Type-Options nosniff
		Referrer-Policy strict-origin-when-cross-origin
		-Server
	}

	# 1. Comments, views and stats: never cached anywhere.
	handle /api/* {
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:46214 {
			header_up X-Real-IP {client_ip}
		}
	}

	# 2. /404 is the error page with a real 404 status (/404.html itself stays 200).
	handle /404 {
		error 404
	}

	# 3. Everything else is a file from the current release.
	handle {
		@hashed path /_astro/*
		header @hashed Cache-Control "public, max-age=31536000, immutable, no-transform"
		header ?Cache-Control "public, max-age=0, must-revalidate"
		file_server {
			# Serve dist's .br / .gz sidecars when the client accepts them.
			precompressed br gzip
		}
	}

	# Missing files and /404: the custom page, HTTP 404, never cached by the CDN.
	handle_errors {
		@notFound expression {err.status_code} == 404
		handle @notFound {
			root * /home/lyy/services/pnc-blog/current
			rewrite * /404.html
			header Cache-Control "no-store"
			file_server
		}
		handle {
			header Cache-Control "no-store"
			respond "{err.status_code} {err.status_text}"
		}
	}
}
```

### 3.2 与 `server.mjs` 的行为对照

| 需求 | 实现方式 |
| --- | --- |
| 只监听本机 46213 | `http://:46213` + `bind 127.0.0.1`；TLS 在 Cloudflare 终止 |
| `.br` / `.gz` 旁文件 | `file_server { precompressed br gzip }`：按 `Accept-Encoding` 选择旁文件，自动设置 `Content-Encoding`、`Vary`、正确的 `Content-Type` 和长度；没有旁文件的旧 release 会回退到原文件 |
| 哈希资源一年缓存 | `/_astro/*` 设置 `public, max-age=31536000, immutable, no-transform`（`no-transform` 防止 Cloudflare 重复压缩） |
| 其他文件 | 默认 `max-age=0, must-revalidate`，靠 Caddy 自带的 `ETag` / `Last-Modified` 做条件请求 |
| `/404` 和所有缺失路径 | 返回 `404.html`，**状态码保持 404**，`Cache-Control: no-store`；`handle_errors` 中的 `file_server` 会沿用原始错误的状态码 |
| 直接访问 `/404.html` | 作为普通文件返回 200（与现在一致） |
| `/api/*` | 反向代理到 `pnc-api`，统一 `no-store`；把 Cloudflare 提供的真实 IP 通过 `X-Real-IP` 转发给后端 |
| 发布切换 | `root` 指向 `current` 符号链接，每个请求都会重新解析路径；`deploy.sh` 切换链接后立即生效，无需 reload |

### 3.3 切换步骤（可随时退回 `server.mjs`）

```bash
# 1) 安装并校验
caddy version                                   # ≥ v2.7
caddy validate --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile

# 2) 先在临时端口上并行跑一份，和 server.mjs 逐项比较
sed 's/:46213/:46220/' /home/lyy/services/pnc-infra/Caddyfile > /tmp/Caddyfile.test
caddy run --config /tmp/Caddyfile.test --adapter caddyfile &
for port in 46213 46220; do
  echo "== $port"
  curl -sI  "http://127.0.0.1:$port/"                         | grep -iE '^(HTTP|content-type|cache-control)'
  curl -sI  "http://127.0.0.1:$port/404"                      | grep -iE '^(HTTP|cache-control)'
  curl -sI  "http://127.0.0.1:$port/nope/missing.html"        | grep -iE '^(HTTP|cache-control)'
  curl -sI  "http://127.0.0.1:$port/404.html"                 | grep -iE '^HTTP'
  asset=$(ls /home/lyy/services/pnc-blog/current/_astro/*.js | head -n 1 | xargs basename)
  curl -sI -H 'Accept-Encoding: br, gzip' "http://127.0.0.1:$port/_astro/$asset" \
                                                              | grep -iE '^(HTTP|content-encoding|content-type|cache-control|vary)'
  curl -s   "http://127.0.0.1:$port/api/stats"; echo
done
kill %1

# 3) 切换：沿用服务名 pnc-blog.service，只换 ExecStart（单元文件见 5.2）
sudo systemctl link /home/lyy/services/pnc-infra/systemd/pnc-blog.service   # 覆盖前先备份旧单元文件
sudo systemctl daemon-reload && sudo systemctl restart pnc-blog
```

预期结果：首页 200；`/404` 和 `/nope/missing.html` 返回 **404** 且带 `cache-control: no-store`；`/404.html` 返回 200；`_astro` 下的 JS 带 `content-encoding: br`、`immutable` 和 `vary: Accept-Encoding`。

**回滚**：把备份的旧 `pnc-blog.service` 放回去，然后 `sudo systemctl daemon-reload && sudo systemctl restart pnc-blog`。`server.mjs` 删除前先保留两周。

---

## 4. 用单文件 SQLite 替代 Directus 和中间层

### 4.1 方案对比

| | 方案 1：自写 `pnc-api`（推荐） | 方案 2：Artalk |
| --- | --- | --- |
| 形态 | 一个约 250 行的 Node 文件，无依赖，`node:sqlite` | Go 单二进制，自带 SQLite 支持和管理后台 |
| 前端改动 | **无**：完整实现现有 `/api/config`、`/api/comments`、`/api/views`、`/api/stats` 契约 | 要换成 Artalk 的前端组件和接口；阅读数、About 统计都要重新对接 |
| 管理 | `sqlite3` 命令行（4.6 节） | 网页后台：审核、邮件通知、多管理员 |
| 反垃圾 | Turnstile + 按 IP 哈希限频 | 内置验证码（含 Turnstile 等），可接 AI / 关键词审核 |
| 维护面 | 代码完全由自己掌握，升级只涉及 Node | 需要跟进 Artalk 版本，前端样式要和本站冰蓝风格对齐 |
| 适合 | 评论量小，希望前端完全不动、依赖最少 | 评论多了以后需要审核后台、通知、表情、回复楼层等功能 |

结论：评论量和功能需求都很少，**先用方案 1**：前端零改动，迁移风险最低。如果以后需要回复楼层、邮件通知或网页审核，再评估 Artalk。数据都在一个 SQLite 文件里，到时导出也很方便。

`node:sqlite` 从 Node 22.13 起不需要额外参数，但仍属实验性 API，会打印 `ExperimentalWarning`，可以用 `--disable-warning=ExperimentalWarning` 关掉。如果不想用实验性 API，可以换成 [`better-sqlite3`](https://github.com/WiseLibs/better-sqlite3)（需要编译原生模块），把 `import { DatabaseSync } from "node:sqlite"` 换成 `import Database from "better-sqlite3"`，再把 `new DatabaseSync(…)` 换成 `new Database(…)`，其余用法相同。

### 4.2 接口契约（与 `src/scripts/*.ts` 一致）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/config` | — | `{ "turnstileSiteKey": "…" }` |
| `GET /api/comments?slug=/posts/x.html` | — | `{ "comments": [{ id, author_name, content, browser_name, browser_version, os_name, os_version, country_code, created_at }] }`，按时间升序，只返回 `published` |
| `POST /api/comments` | `{ slug, authorName, content, turnstileToken }` | `201 { ok, id }`；出错时返回 4xx 和 `{ "error": "中文提示" }`，前端直接显示 `error` |
| `GET /api/views` | — | `{ "views": { "/posts/x.html": 12, … } }` |
| `POST /api/views` | `{ slug }` | `{ "views": 12 }`：同一 IP + User-Agent、同一文章、同一天（Asia/Shanghai）只计一次 |
| `GET /api/stats` | — | `{ totalViews, commentCount, mostViewed: { slug, title, views } \| null }` |
| `GET /api/health` | — | `{ "ok": true }`（新增，用于监控） |

`slug` 一律是站内固定路径 `/posts/<slug>.html`（文章页现在显式传这个值，不再受 `canonical` 和开发服务器 URL 的影响）。`pnc-api` 读取当前 release 的 `index.json`，只接受实际存在的文章，并从中取出 `mostViewed` 的标题。

### 4.3 `schema.sql`

```sql
-- pnc-api schema: one SQLite file holds comments and reading counts.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS comments (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL,              -- site path, e.g. /posts/arch-linux.html
  author_name     TEXT NOT NULL,
  content         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'published'
                  CHECK (status IN ('published', 'pending', 'hidden')),
  browser_name    TEXT,
  browser_version TEXT,
  os_name         TEXT,
  os_version      TEXT,
  country_code    TEXT,                       -- from Cloudflare's CF-IPCountry
  visitor_hash    TEXT,                       -- sha256(salt|ip), never the raw IP
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS comments_by_post ON comments (slug, status, created_at);
CREATE INDEX IF NOT EXISTS comments_by_visitor ON comments (visitor_hash, created_at);

-- One row per reader, per post, per day (Asia/Shanghai): the primary key is
-- the "count once a day" rule, so INSERT OR IGNORE does the deduplication.
CREATE TABLE IF NOT EXISTS post_views (
  slug       TEXT NOT NULL,
  day        TEXT NOT NULL,                   -- YYYY-MM-DD
  visitor    TEXT NOT NULL,                   -- sha256(salt|ip|user-agent)
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (slug, day, visitor)
) WITHOUT ROWID;
```

### 4.4 `server.mjs`

```js
// pnc-api: comments, reading counts and site stats for PNC's Blog.
// One process, one SQLite file, no dependencies (Node >= 22.13).
// Serves the exact contract the static site calls under /api/*.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const env = process.env;
const config = {
  host: env.HOST || "127.0.0.1",
  port: Number(env.PORT || 46214),
  dbPath: env.DB_PATH || "/home/lyy/services/pnc-api/data/pnc.db",
  // The deployed site's own post list: known slugs and titles for /stats.
  siteIndex: env.SITE_INDEX || "/home/lyy/services/pnc-blog/current/index.json",
  turnstileSiteKey: env.TURNSTILE_SITE_KEY || "",
  turnstileSecret: env.TURNSTILE_SECRET_KEY || "",
  salt: env.HASH_SALT || "",
  timeZone: env.STATS_TIME_ZONE || "Asia/Shanghai",
  commentLimit: Number(env.COMMENT_LIMIT_PER_HOUR || 6),
};
if (!config.salt) throw new Error("HASH_SALT is required (any long random string)");

fs.mkdirSync(path.dirname(config.dbPath), { recursive: true });
const db = new DatabaseSync(config.dbPath);
db.exec("PRAGMA busy_timeout = 5000; PRAGMA synchronous = NORMAL;");
db.exec(fs.readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));

const sql = {
  comments: db.prepare(`SELECT id, author_name, content, browser_name, browser_version, os_name, os_version,
    country_code, created_at FROM comments WHERE slug = ? AND status = 'published' ORDER BY created_at, id`),
  addComment: db.prepare(`INSERT INTO comments (slug, author_name, content, browser_name, browser_version,
    os_name, os_version, country_code, visitor_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`),
  recentComments: db.prepare("SELECT COUNT(*) AS n FROM comments WHERE visitor_hash = ? AND created_at > ?"),
  addView: db.prepare("INSERT OR IGNORE INTO post_views (slug, day, visitor) VALUES (?, ?, ?)"),
  postViews: db.prepare("SELECT COUNT(*) AS n FROM post_views WHERE slug = ?"),
  allViews: db.prepare("SELECT slug, COUNT(*) AS n FROM post_views GROUP BY slug"),
  totalViews: db.prepare("SELECT COUNT(*) AS n FROM post_views"),
  commentCount: db.prepare("SELECT COUNT(*) AS n FROM comments WHERE status = 'published'"),
};

// --- site index: which slugs exist, and their titles ----------------------
let siteIndex = { mtime: 0, titles: new Map() };
function postTitles() {
  try {
    const { mtimeMs } = fs.statSync(config.siteIndex);
    if (mtimeMs !== siteIndex.mtime) {
      const posts = JSON.parse(fs.readFileSync(config.siteIndex, "utf8"));
      siteIndex = { mtime: mtimeMs, titles: new Map(posts.map((post) => [`/${post.url}`, post.title])) };
    }
  } catch {
    // Keep the last good index; without one, slugs are checked by shape only.
  }
  return siteIndex.titles;
}
const SLUG = /^\/posts\/[\w.%-]+\.html$/;
function knownSlug(value) {
  if (typeof value !== "string" || !SLUG.test(value)) return undefined;
  const titles = postTitles();
  return titles.size === 0 || titles.has(value) ? value : undefined;
}

// --- request helpers -------------------------------------------------------
function send(res, status, body) {
  const json = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(json),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(json);
}

async function readJson(req, limit = 16 * 1024) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw Object.assign(new Error("请求太大"), { status: 413 });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw Object.assign(new Error("请求格式错误"), { status: 400 });
  }
}

// Only Caddy (on this host) talks to us, so its forwarded headers are trusted.
function clientIp(req) {
  return String(req.headers["cf-connecting-ip"] || req.headers["x-real-ip"]
    || String(req.headers["x-forwarded-for"] || "").split(",")[0] || req.socket.remoteAddress || "").trim();
}
const hash = (...parts) => crypto.createHash("sha256").update([config.salt, ...parts].join("|")).digest("hex");

function today() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: config.timeZone }).format(new Date());
}

function parseUserAgent(ua = "") {
  const browsers = [
    ["Edge", /Edg(?:A|iOS)?\/([\d.]+)/], ["Opera", /OPR\/([\d.]+)/], ["Firefox", /(?:Firefox|FxiOS)\/([\d.]+)/],
    ["Chrome", /(?:Chrome|CriOS)\/([\d.]+)/], ["Safari", /Version\/([\d.]+).*Safari/],
  ];
  const systems = [
    ["iOS", /(?:iPhone|iPad|iPod).*? OS ([\d_]+)/], ["Android", /Android ([\d.]+)/],
    ["Windows", /Windows NT ([\d.]+)/], ["macOS", /Mac OS X ([\d_.]+)/], ["Linux", /Linux()/],
  ];
  const browser = browsers.find(([, pattern]) => pattern.test(ua));
  const system = systems.find(([, pattern]) => pattern.test(ua));
  const version = (entry) => entry?.[1].exec(ua)?.[1]?.replace(/_/g, ".").split(".").slice(0, 2).join(".") || null;
  return {
    browser_name: browser?.[0] || null,
    browser_version: version(browser),
    os_name: system?.[0] || null,
    os_version: version(system),
  };
}

async function verifyTurnstile(token, ip) {
  if (!config.turnstileSecret || typeof token !== "string" || !token) return false;
  try {
    const response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body: new URLSearchParams({ secret: config.turnstileSecret, response: token, remoteip: ip }),
      signal: AbortSignal.timeout(8000),
    });
    return (await response.json()).success === true;
  } catch {
    return false;
  }
}

// --- routes ------------------------------------------------------------------
const routes = {
  "GET /api/health": () => [200, { ok: true }],
  "GET /api/config": () => [200, { turnstileSiteKey: config.turnstileSiteKey }],

  "GET /api/comments": (req, url) => {
    const slug = knownSlug(url.searchParams.get("slug"));
    if (!slug) return [400, { error: "未知文章" }];
    return [200, { comments: sql.comments.all(slug) }];
  },

  "POST /api/comments": async (req) => {
    const body = await readJson(req);
    const slug = knownSlug(body.slug);
    const authorName = String(body.authorName || "").trim();
    const content = String(body.content || "").trim();
    if (!slug) return [400, { error: "未知文章" }];
    if (!authorName || authorName.length > 40) return [400, { error: "用户名需为 1–40 个字符" }];
    if (!content || content.length > 1200) return [400, { error: "评论需为 1–1200 个字符" }];
    const ip = clientIp(req);
    const visitor = hash(ip);
    const hourAgo = new Date(Date.now() - 3600_000).toISOString();
    if (sql.recentComments.get(visitor, hourAgo).n >= config.commentLimit) {
      return [429, { error: "评论太频繁，请稍后再试" }];
    }
    if (!await verifyTurnstile(body.turnstileToken, ip)) return [403, { error: "人机验证失败，请重试" }];
    const ua = parseUserAgent(String(req.headers["user-agent"] || ""));
    const country = String(req.headers["cf-ipcountry"] || "").toUpperCase();
    const { lastInsertRowid } = sql.addComment.run(slug, authorName, content, ua.browser_name, ua.browser_version,
      ua.os_name, ua.os_version, /^[A-Z]{2}$/.test(country) && country !== "XX" ? country : null, visitor);
    return [201, { ok: true, id: Number(lastInsertRowid) }];
  },

  "GET /api/views": () => [200, {
    views: Object.fromEntries(sql.allViews.all().map((row) => [row.slug, row.n])),
  }],

  "POST /api/views": async (req) => {
    const slug = knownSlug((await readJson(req)).slug);
    if (!slug) return [400, { error: "未知文章" }];
    sql.addView.run(slug, today(), hash(clientIp(req), String(req.headers["user-agent"] || "")));
    return [200, { views: sql.postViews.get(slug).n }];
  },

  "GET /api/stats": () => {
    const titles = postTitles();
    const top = sql.allViews.all()
      .filter((row) => titles.size === 0 || titles.has(row.slug))
      .sort((a, b) => b.n - a.n || a.slug.localeCompare(b.slug))[0];
    return [200, {
      totalViews: sql.totalViews.get().n,
      commentCount: sql.commentCount.get().n,
      mostViewed: top ? { slug: top.slug, title: titles.get(top.slug) || top.slug, views: top.n } : null,
    }];
  },
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://localhost");
  const route = routes[`${req.method} ${url.pathname.replace(/\/$/, "")}`];
  try {
    if (!route) return send(res, 404, { error: "Not found" });
    const [status, body] = await route(req, url);
    send(res, status, body);
  } catch (error) {
    if (!error.status) console.error(error);
    send(res, error.status || 500, { error: error.status ? error.message : "服务器错误" });
  }
});

server.listen(config.port, config.host, () => {
  console.log(`pnc-api listening on http://${config.host}:${config.port} (db: ${config.dbPath})`);
});
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => server.close(() => { db.close(); process.exit(0); }));
}
```

### 4.5 `.env`（复制为 `/home/lyy/services/pnc-api/.env`，`chmod 600`）

```ini
HOST=127.0.0.1
PORT=46214
DB_PATH=/home/lyy/services/pnc-api/data/pnc.db
SITE_INDEX=/home/lyy/services/pnc-blog/current/index.json
TURNSTILE_SITE_KEY=0x4AAAAAAA...        # 与现在 /api/config 返回的值相同
TURNSTILE_SECRET_KEY=0x4AAAAAAA...      # 从 pnc-comment-api 的配置中迁移过来
HASH_SALT=...                           # openssl rand -hex 32；更换后当天的阅读去重会重新计算
COMMENT_LIMIT_PER_HOUR=6
```

先用 `ss -ltnp | grep 4621` 确认 46214 没有被现在的 `pnc-comment-api` 占用；如有冲突，换一个端口，并同步修改 `Caddyfile`。

### 4.6 最小管理命令

```bash
DB=/home/lyy/services/pnc-api/data/pnc.db

# 最近 20 条评论
sqlite3 -box "$DB" "SELECT id, slug, author_name, substr(content, 1, 40) AS content, status, created_at
                    FROM comments ORDER BY id DESC LIMIT 20;"

# 隐藏（可恢复）/ 恢复 / 彻底删除
sqlite3 "$DB" "UPDATE comments SET status = 'hidden' WHERE id = 42;"
sqlite3 "$DB" "UPDATE comments SET status = 'published' WHERE id = 42;"
sqlite3 "$DB" "DELETE FROM comments WHERE id = 42;"

# 同一来源的批量垃圾：先看，再隐藏
sqlite3 -box "$DB" "SELECT visitor_hash, COUNT(*) FROM comments GROUP BY visitor_hash ORDER BY 2 DESC LIMIT 5;"
sqlite3 "$DB" "UPDATE comments SET status = 'hidden' WHERE visitor_hash = '…';"

# 每篇文章的阅读数
sqlite3 -box "$DB" "SELECT slug, COUNT(*) AS views FROM post_views GROUP BY slug ORDER BY views DESC;"
```

API 运行时也可以直接执行这些命令（WAL 模式支持一写多读，`busy_timeout` 会处理短暂的锁等待）。

### 4.7 从 Directus 迁移

`migrate-from-directus.mjs`，与 `schema.sql` 放在同一目录：

```js
// Import a Directus export (GET /items/<collection>?limit=-1) into pnc.db.
// Usage: node migrate-from-directus.mjs comments.json post_views.json [--write]
// Without --write it only prints what it would import.
// Field names are matched loosely; check `GET /fields/<collection>` first
// and extend the pick(...) lists below if your fields are named differently.
import fs from "node:fs";
import { DatabaseSync } from "node:sqlite";

const [commentsFile, viewsFile] = process.argv.slice(2).filter((arg) => !arg.startsWith("--"));
const write = process.argv.includes("--write");
const dbPath = process.env.DB_PATH || "/home/lyy/services/pnc-api/data/pnc.db";
if (!commentsFile || !viewsFile) {
  console.error("usage: node migrate-from-directus.mjs comments.json post_views.json [--write]");
  process.exit(1);
}

const rows = (file) => {
  const json = JSON.parse(fs.readFileSync(file, "utf8"));
  return Array.isArray(json) ? json : json.data;
};
const pick = (row, ...keys) => keys.map((key) => row[key]).find((value) => value !== undefined && value !== null && value !== "");
// Old data may store "arch-linux", "posts/arch-linux.html" or "/posts/arch-linux.html".
const slugOf = (value) => {
  const slug = String(value || "").trim().replace(/^https?:\/\/[^/]+/, "");
  if (!slug) return undefined;
  if (/^\/?posts\/.+\.html$/.test(slug)) return `/${slug.replace(/^\//, "")}`;
  return `/posts/${slug.replace(/^\//, "").replace(/\.html$/, "")}.html`;
};
const isoDate = (value) => (value ? new Date(value).toISOString() : new Date().toISOString());
const STATUS = { published: "published", approved: "published", draft: "pending", pending: "pending", archived: "hidden", hidden: "hidden", spam: "hidden" };

const comments = rows(commentsFile).map((row) => ({
  id: Number.isInteger(row.id) ? row.id : null,
  slug: slugOf(pick(row, "slug", "post_slug", "post", "path")),
  author_name: String(pick(row, "author_name", "authorName", "author", "name") || "匿名"),
  content: String(pick(row, "content", "body", "text") || ""),
  status: STATUS[String(pick(row, "status") || "published").toLowerCase()] || "pending",
  browser_name: pick(row, "browser_name") ?? null,
  browser_version: pick(row, "browser_version") ?? null,
  os_name: pick(row, "os_name") ?? null,
  os_version: pick(row, "os_version") ?? null,
  country_code: pick(row, "country_code", "country") ?? null,
  created_at: isoDate(pick(row, "created_at", "date_created", "createdAt")),
}));

// Either one row per reader per day (slug + day + visitor hash), or one
// counter row per post (slug + views); counters become that many rows.
const views = [];
for (const row of rows(viewsFile)) {
  const slug = slugOf(pick(row, "slug", "post_slug", "post", "path"));
  if (!slug) continue;
  const count = pick(row, "views", "count", "total");
  const visitor = pick(row, "visitor", "visitor_hash", "fingerprint", "ip_hash", "hash");
  if (visitor === undefined && count !== undefined) {
    for (let i = 0; i < Number(count); i++) views.push({ slug, day: "legacy", visitor: `legacy:${row.id ?? slug}:${i}` });
  } else {
    const day = String(pick(row, "day", "date", "viewed_on", "date_created", "created_at") || "legacy").slice(0, 10);
    views.push({ slug, day, visitor: String(visitor ?? `legacy:${row.id}`) });
  }
}

const skipped = comments.filter((comment) => !comment.slug || !comment.content);
console.log(`comments: ${comments.length - skipped.length} to import, ${skipped.length} without slug/content skipped`);
console.log(`post_views: ${views.length} rows across ${new Set(views.map((view) => view.slug)).size} posts`);
if (!write) {
  console.log("dry run; add --write to import into", dbPath);
  process.exit(0);
}

const db = new DatabaseSync(dbPath);
db.exec(fs.readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
const addComment = db.prepare(`INSERT OR REPLACE INTO comments (id, slug, author_name, content, status, browser_name,
  browser_version, os_name, os_version, country_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
const addView = db.prepare("INSERT OR IGNORE INTO post_views (slug, day, visitor) VALUES (?, ?, ?)");
db.exec("BEGIN");
try {
  for (const c of comments) {
    if (!c.slug || !c.content) continue;
    addComment.run(c.id, c.slug, c.author_name, c.content, c.status, c.browser_name, c.browser_version,
      c.os_name, c.os_version, c.country_code, c.created_at);
  }
  for (const v of views) addView.run(v.slug, v.day, v.visitor);
  db.exec("COMMIT");
} catch (error) {
  db.exec("ROLLBACK");
  throw error;
}
const count = (table) => db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n;
console.log(`imported: ${count("comments")} comments, ${count("post_views")} view rows in ${dbPath}`);
```

步骤：

```bash
cd /home/lyy/services/pnc-infra/api
export DIRECTUS_URL=http://127.0.0.1:8055        # pnc-cms 的本机地址
export DIRECTUS_TOKEN=…                          # 管理员账号的 static token
auth=(-H "Authorization: Bearer $DIRECTUS_TOKEN")

# 1) 确认集合名和字段名（评论集合不一定叫 comments）
curl -fsS "${auth[@]}" "$DIRECTUS_URL/collections" | jq -r '.data[].collection' | grep -v '^directus_'
curl -fsS "${auth[@]}" "$DIRECTUS_URL/fields/post_views" | jq -r '.data[].field'

# 2) 导出（limit=-1 表示全部）
curl -fsS "${auth[@]}" "$DIRECTUS_URL/items/comments?limit=-1&sort=id"   > /tmp/comments.json
curl -fsS "${auth[@]}" "$DIRECTUS_URL/items/post_views?limit=-1&sort=id" > /tmp/post_views.json

# 3) 先试运行，看数量是否和 Directus 后台一致，再写入
node migrate-from-directus.mjs /tmp/comments.json /tmp/post_views.json
DB_PATH=/home/lyy/services/pnc-api/data/pnc.db node migrate-from-directus.mjs /tmp/comments.json /tmp/post_views.json --write

# 4) 新 API 与旧 API 并行运行，逐项对比
sudo systemctl start pnc-api
for path in views stats "comments?slug=/posts/arch-linux.html"; do
  diff <(curl -s "http://127.0.0.1:46213/api/$path" | jq -S .) \
       <(curl -s "http://127.0.0.1:46214/api/$path" | jq -S .) && echo "same: $path"
done
```

`stats` 中的 `mostViewed.title` 可能因为来源不同而有细微差异，`views` 的计数必须完全一致。

正式切换（避免导出之后新增的评论丢失）：

1. 停止旧的 `pnc-comment-api`（这段时间评论区会显示“暂时无法加载”，前端已经做了降级）。
2. 重新执行导出和 `--write`。脚本可以重复执行，按 Directus 的 id 覆盖评论，阅读记录用 `INSERT OR IGNORE` 去重。
3. 确认 `Caddyfile` 中 `/api/*` 的 upstream 是 `127.0.0.1:46214`，然后执行 `sudo systemctl reload pnc-blog`。
4. 在浏览器里打开一篇文章，确认评论列表、阅读数以及 About 页的统计都正常。发一条测试评论，再用 4.6 节的命令删掉。
5. `sudo systemctl disable --now pnc-comment-api pnc-cms`（服务名以服务器上的实际名称为准）。两周内保留 Directus 的数据目录和数据库，确认无误后再归档删除。

**回滚**：`sudo systemctl enable --now pnc-cms pnc-comment-api`，把 `Caddyfile` 中的 upstream 改回旧端口后执行 `reload`。切换期间新增的评论可以从 `pnc.db` 中查出后手动补录。

### 4.8 备份与恢复

`backup.sh`：

```bash
#!/usr/bin/env bash
# Online, consistent backup of pnc.db (safe while the API is writing).
set -Eeuo pipefail
DB=${DB_PATH:-/home/lyy/services/pnc-api/data/pnc.db}
DEST=${BACKUP_DIR:-/home/lyy/backups/pnc-api}
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

`restore.sh`：

```bash
#!/usr/bin/env bash
# Restore pnc.db from a backup made by backup.sh. Stops the API meanwhile and
# keeps the replaced database next to it.
#   restore.sh /home/lyy/backups/pnc-api/pnc-YYYYMMDD-HHMMSS.db.gz
set -Eeuo pipefail
BACKUP=${1:?usage: restore.sh path/to/pnc-YYYYMMDD-HHMMSS.db.gz}
DB=${DB_PATH:-/home/lyy/services/pnc-api/data/pnc.db}
SERVICE=${SERVICE:-pnc-api}
SYSTEMCTL=${SYSTEMCTL:-sudo systemctl}

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

`sqlite3 .backup` 使用 SQLite 的在线备份 API，API 正在写入时也能得到一致的快照，不需要停服务。备份目录如果不在 `/home/lyy/services` 的每日备份范围内，记得加进去，或者另外 `rsync` 到其他机器。**每季度实际执行一次 `restore.sh`**（可以把 `DB_PATH` 指向临时文件来演练），确认备份可用。

---

## 5. 统一部署与一键回滚

### 5.1 私有仓库 `pnc-blog-infra`

```text
pnc-blog-infra/                  # 私有仓库，检出到 /home/lyy/services/pnc-infra
├── Caddyfile
├── deploy.sh
├── api/
│   ├── server.mjs
│   ├── schema.sql
│   ├── migrate-from-directus.mjs
│   ├── backup.sh
│   └── restore.sh
├── systemd/
│   ├── pnc-blog.service         # Caddy
│   ├── pnc-api.service
│   ├── pnc-api-backup.service
│   └── pnc-api-backup.timer
├── .env.example                 # 4.5 节，值留空
├── .gitignore                   # .env  *.db  *.db-*
└── README.md                    # 指回本文
```

规则：**密钥只放在服务器的 `.env` 里**（Turnstile secret、`HASH_SALT`），仓库里只提交 `.env.example`。服务器上的改动先提交到仓库，再在服务器上 `git pull`，这样每次变更都有记录，可以审查，也可以回退。

### 5.2 systemd 单元

用 systemd 而不是 docker-compose：现有服务本来就由 systemd 管理，Runner 也直接在宿主机上构建。两个进程用不着再加一层容器运行时。

`systemd/pnc-blog.service`（沿用原服务名，cloudflared 和其他配置都不用改）：

```ini
[Unit]
Description=PNC's Blog static site (Caddy)
After=network-online.target
Wants=network-online.target

[Service]
User=lyy
Group=lyy
ExecStart=/usr/bin/caddy run --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config /home/lyy/services/pnc-infra/Caddyfile --adapter caddyfile --force
Restart=on-failure
StateDirectory=pnc-caddy
Environment=XDG_DATA_HOME=%S/pnc-caddy XDG_CONFIG_HOME=%S/pnc-caddy
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

`systemd/pnc-api.service`：

```ini
[Unit]
Description=PNC's Blog comments and stats API (SQLite)
After=network-online.target

[Service]
User=lyy
Group=lyy
WorkingDirectory=/home/lyy/services/pnc-infra/api
EnvironmentFile=/home/lyy/services/pnc-api/.env
ExecStart=/usr/bin/node --disable-warning=ExperimentalWarning server.mjs
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/lyy/services/pnc-api/data

[Install]
WantedBy=multi-user.target
```

`systemd/pnc-api-backup.service` 和 `systemd/pnc-api-backup.timer`：

```ini
# pnc-api-backup.service
[Unit]
Description=Back up pnc.db

[Service]
Type=oneshot
User=lyy
ExecStart=/home/lyy/services/pnc-infra/api/backup.sh

# pnc-api-backup.timer
[Unit]
Description=Daily pnc.db backup

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
```

安装：

```bash
cd /home/lyy/services/pnc-infra
sudo systemctl link "$PWD"/systemd/pnc-api.service "$PWD"/systemd/pnc-api-backup.service "$PWD"/systemd/pnc-api-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now pnc-api pnc-api-backup.timer
systemctl list-timers pnc-api-backup.timer
```

### 5.3 `deploy.sh`

替换 `/home/lyy/services/pnc-blog/deploy.sh`，改成指向仓库中文件的符号链接，这样 workflow 里的调用路径不变：

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
REPO=$APP_ROOT/repo.git
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
export ASTRO_TELEMETRY_DISABLED=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm_config_fund=false npm_config_audit=false

log() { printf '[deploy %s] %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

mkdir -p "$RELEASES"
exec 9>"$APP_ROOT/.deploy.lock"
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
    check "$SITE_URL/rss.xml" 200 '<content:encoded>' &&
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
    [[ -n $target && -d $RELEASES/$target ]] || die "no release to roll back to (see --list)"
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

- **验证和构建只做一次**：`npm run verify` 已经包含 `typecheck`、单元测试、`build` 和 `verify-build.mjs`，发布的就是这次验证过的 `dist/`。
- **只发布 main 上的提交**：`merge-base --is-ancestor` 会拒绝其他分支或 fork 的 SHA；短 SHA 会被解析成完整 SHA。
- **同一提交不重复构建**：`releases/` 里已有该 SHA 的产物时直接复用，重新部署或按 SHA 回滚都只需几秒。
- **原子切换**：用新符号链接 `rename(2)` 覆盖旧链接，任何时刻 `current` 都指向一个完整的 release。
- **健康检查覆盖面**：首页、任意一篇文章、`/404`（必须是 404 状态码加 `no-store`）、随机缺失路径、`/404.html`（200）、RSS 全文、`/api/config`、`/api/views`、`/api/stats`。失败会重试 3 次，仍失败就切回上一个 release，并以非零状态退出（Actions 中显示为失败）。
- **保留最近 5 个 release**，当前正在使用的那个永远不会被删除；`flock` 防止两次部署同时进行（workflow 的 `concurrency` 之外再加一道保险）。
- 首次运行会自动 `git clone --bare` 到 `repo.git/`，旧 deploy.sh 使用的检出目录确认不再需要后可以删除。

### 5.4 回滚

```bash
# 一条命令，秒级：切回上一个 release，并重新跑健康检查
/home/lyy/services/pnc-blog/deploy.sh --rollback

# 查看可用 release（* 为当前），或者回滚到指定的 release
/home/lyy/services/pnc-blog/deploy.sh --list
/home/lyy/services/pnc-blog/deploy.sh --rollback 20261121-103000-40937e89e910

# deploy.sh 本身出问题时的纯手工版（GNU coreutils）
cd /home/lyy/services/pnc-blog && ln -sfn "$PWD/releases/<name>" .current.tmp && mv -Tf .current.tmp current
```

也可以在 GitHub 的 Actions 页面选择 **Verify and Deploy Site → Run workflow**，在 `deploy_sha` 中填写 main 上的历史提交。如果这个提交的 release 还在，几秒内就能切回去；不在的话会重新构建。

### 5.5 本仓库中的 workflow

`.github/workflows/build-site.yml` 已经改为：

- 只由 `push` 到 `main` 和 `workflow_dispatch` 触发，**没有 `pull_request`**；
- 只有一个 job，运行在 `[self-hosted, linux, x64, pnc-blog]` 上，**不再使用 GitHub 托管的 `ubuntu-latest`**；
- `deploy_sha` 输入经过正则校验后，以环境变量 `DEPLOY_SHA` 的形式传给 `deploy.sh`（不会直接拼进 shell 命令，防止注入）；
- `concurrency: pnc-blog-production`，不取消正在进行的部署，push 和手动触发共用一个队列；
- job 级别 `if: github.ref == 'refs/heads/main'`：从其他分支手动触发时不会部署。

> 旧的 `deploy.sh` 也能处理 push 事件传入的完整 SHA。但填写短 SHA 回滚、复用已有 release 这两项功能，需要先换成 5.3 节的新版本。另外，PR 现在不会自动跑检查了，合并前请在本地执行 `npm run verify`。

---

## 6. Self-Hosted Runner 安全与运维速查

### 6.1 公开仓库的权限控制

- [ ] workflow 只有 `push: main` 和 `workflow_dispatch` 两个触发器。以后加新的 workflow，**不要**用 `pull_request` / `pull_request_target`、`issue_comment` 这类外部用户能触发的事件，也不要让它们跑在 `pnc-blog` 标签的 Runner 上。
- [ ] Settings → Actions → General → *Fork pull request workflows from outside collaborators* 设为 **Require approval for all external contributors**（多一层保护，防止以后有人误加触发器）。
- [ ] Settings → Actions → General → *Workflow permissions* 设为 **Read repository contents**；workflow 里也显式写了 `permissions: contents: read`。部署不需要任何 GitHub Secret。
- [ ] Settings → Branches：保护 `main`，禁止 force push 和删除分支。能 push 到 main 的人，实际上就能在服务器上执行代码。
- [ ] `workflow_dispatch` 只有有写权限的协作者能触发，定期检查协作者列表。
- [ ] Runner 只注册在本仓库（仓库级 Runner），不要注册成组织级 Runner 给其他仓库共用。

### 6.2 以非 root、最小权限运行

- Runner 服务绝对不能以 root 运行（不要设置 `RUNNER_ALLOW_RUNASROOT`）。安装命令：`sudo ./svc.sh install <用户>`。
- 最简单的做法（也就是现状）：Runner 以 `lyy` 运行。它能做的事等于 `lyy` 能做的事。
- **更严格的做法（推荐）**：新建一个专门的用户，例如 `gha-pnc`，只授权它以 `lyy` 身份执行部署脚本：

  ```bash
  sudo useradd --system --create-home --shell /usr/sbin/nologin gha-pnc
  # 用 gha-pnc 身份重新注册并安装 Runner：sudo ./svc.sh install gha-pnc
  echo 'gha-pnc ALL=(lyy) NOPASSWD:SETENV: /home/lyy/services/pnc-blog/deploy.sh' | sudo tee /etc/sudoers.d/pnc-deploy
  sudo chmod 440 /etc/sudoers.d/pnc-deploy && sudo visudo -c
  ```

  然后把 workflow 最后一步改为 `sudo -u lyy --preserve-env=DEPLOY_SHA /home/lyy/services/pnc-blog/deploy.sh`。这样 Runner 自己既读不到 `.env`，也碰不到数据库。
- `deploy.sh` 和 `pnc-infra/` 只允许 `lyy` 写入（`chmod 755`）。Runner 用户不应该能修改它们。
- Runner 不需要 checkout 代码（`deploy.sh` 自己从 GitHub 拉取），所以 `_work/` 目录基本是空的。

### 6.3 日志与排查

```bash
# Runner 服务
systemctl status 'actions.runner.*'
journalctl -u 'actions.runner.*' -n 200 --no-pager
ls -t ~/actions-runner/_diag/ | head          # Runner_*.log（连接）、Worker_*.log（单次 job）

# 部署：完整日志在 Actions 页面；服务器上查看 release 与当前指向
/home/lyy/services/pnc-blog/deploy.sh --list
readlink /home/lyy/services/pnc-blog/current

# 站点和 API
journalctl -u pnc-blog -f
journalctl -u pnc-api -f
curl -sI http://127.0.0.1:46213/404 | head -n 5
curl -s  http://127.0.0.1:46214/api/health
```

| 现象 | 原因与处理 |
| --- | --- |
| job 一直显示 *Waiting for a runner* | Runner 离线或标签不匹配。查看 `systemctl status 'actions.runner.*'`，必要时 `sudo ./svc.sh stop && sudo ./svc.sh start`；在 Settings → Actions → Runners 里确认标签包含 `pnc-blog`。 |
| `another deploy is running` | 确实有一次部署还在运行（锁会随进程退出自动释放）。用 `pgrep -af deploy.sh` 查看，等它结束。 |
| `is not on main; refusing to deploy` | `deploy_sha` 不是 main 上的提交。先合并到 main。 |
| `npm ci` 报 `EBADENGINE` 或网络错误 | Node 版本必须 ≥ 22.12（`node -v`）；检查 npm 源和网络，`npm cache verify`。 |
| `npm run verify` 失败 | 和本地 `npm run verify` 的报错一样。修好后再 push，线上不受影响。 |
| health check 在 `/api/*` 失败 | `pnc-api` 没有运行，查看 `systemctl status pnc-api`。脚本已经自动回滚了静态站，修好 API 后重新运行 workflow 即可。 |
| health check 在 `/404` 失败 | Caddy 配置被改动过，`/404` 返回了 200，或者缺少 `no-store`。用 `caddy validate` 和 3.3 节的 `curl` 检查。 |
| 磁盘空间不足 | `du -sh /home/lyy/services/pnc-blog/releases ~/.npm`；可以调小 `KEEP_RELEASES`，或执行 `npm cache clean --force`。 |

---

## 7. 落地顺序

每一步都单独验证，可以单独回滚。建议隔几天做一步。

1. **建立 `pnc-blog-infra` 私有仓库**，把本文的模板放进去，在服务器上检出到 `/home/lyy/services/pnc-infra`。备份现有的 `server.mjs`、`deploy.sh` 和 systemd 单元文件。
2. **换掉 `deploy.sh`**（第 5.3 节），然后在 Actions 中手动触发一次 `workflow_dispatch`。确认 `--list` 能看到 release，`--rollback` 和重新部署都正常。
3. **Caddy 替代 `server.mjs`**（第 3 节）：在临时端口并行对比，确认一致后再切换。
4. **上线 `pnc-api`**（第 4 节）：并行运行，迁移，对比，切换，然后停掉 Directus 和 `pnc-comment-api`，两周后清理。
5. **启用备份 timer**，并做一次恢复演练（第 4.8 节）。
6. **加固 Runner**（第 6 节的检查清单；可选：改用专门的 `gha-pnc` 用户）。
7. 更新 README 的“生产服务”一节，写明新的服务组成。
