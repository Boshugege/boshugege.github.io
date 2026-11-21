# pnc-reactions：「观测」计数 API（参考实现）

文章和随想下的「观测」按钮背后的服务：每位访客（IPv4 地址或 IPv6 /64）对每篇文章、每条随想只计一次。Node ≥ 22.13，零依赖，数据是一个 SQLite 文件（`node:sqlite`）。这个目录不参与站点构建，不会进入 `dist/`，也不含任何密钥。

| 文件 | 作用 |
| --- | --- |
| `server.mjs` | HTTP 服务：`createReactionsServer(config)` / `loadConfig(env)`，直接运行时才监听 |
| `lib.mjs` | 纯函数：IP 归一化、访客哈希、挑战签名、工作量证明校验、限流器、熔断器 |
| `schema.sql` | `reactions(target, visitor_hash, created_at)`，主键 `(target, visitor_hash)` 就是去重规则 |
| `.env.example` | 全部配置项、默认值和说明 |
| `pnc-reactions.service` | systemd 用户单元（与 `pnc-api` 相同的加固项） |

防滥刷分五层：目标必须存在于当前发布的 `index.json` / `notes.json`；挑战和写入只接受本站页面（`Origin` / `Referer` + `Sec-Fetch-Site`）；每次写入附带绑定访客和目标、60 秒内一次性的 HMAC 挑战及约 14 bit 的工作量证明；数据库按访客永久去重；进程内滑动窗口限流，单目标或全站突增时熔断并发 Telegram 告警。

## 本地运行

```bash
npm run build   # 生成 dist/index.json 和 dist/notes.json 作为白名单
cd ops/reactions-api
CHALLENGE_SECRET="$(openssl rand -hex 32)" VISITOR_SALT="$(openssl rand -hex 32)" \
DB_PATH="$(mktemp -d)/reactions.db" \
BLOG_INDEX_FILE=../../dist/index.json NOTES_INDEX_FILE=../../dist/notes.json \
node server.mjs

curl -s http://127.0.0.1:46220/reactions/healthz
curl -s 'http://127.0.0.1:46220/reactions?prefix=note:'
curl -s -H 'Referer: https://parityncsvt.top/posts/' -H 'Sec-Fetch-Site: same-origin' \
  'http://127.0.0.1:46220/reactions/challenge?target=/posts/<slug>.html'
```

测试：`node --experimental-strip-types --test tests/reactions-api.test.ts`（在仓库根目录运行，`npm test` 也会包含它）。

接口契约、Caddy 反代、Cloudflare 边缘限流规则和完整部署步骤见仓库根目录 `SERVER.md` 第 9 节。
