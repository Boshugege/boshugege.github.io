// pnc-reactions: the "观测" counter for PNC's Blog. One reaction per visitor
// (IPv4 address or IPv6 /64) and post or note, kept in one SQLite file.
// Zero dependencies, Node >= 22.13 (node:sqlite). Caddy strips /api, so the
// routes are /reactions, /reactions/challenge and /reactions/healthz.
//
// No CAPTCHA; abuse is held off in layers: targets must exist in the deployed
// site, writes must come from the site's own pages, each write carries a
// short proof of work bound to the visitor, the database dedupes per visitor,
// and in-process rate limits plus a circuit breaker (with a Telegram alert)
// cap what one address or one burst can do.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  CircuitBreaker,
  NOTE_TARGET,
  POST_TARGET,
  SlidingWindowLimiter,
  TARGET_PREFIXES,
  isNonceSyntax,
  isTargetSyntax,
  signChallenge,
  verifyChallenge,
  verifyPow,
  visitorKey,
} from "./lib.mjs";

export const DEFAULTS = Object.freeze({
  host: "127.0.0.1",
  port: 46220,
  dbPath: "/home/lyy/services/pnc-reactions/data/reactions.db",
  siteOrigin: "https://parityncsvt.top",
  blogIndexFile: "/home/lyy/services/pnc-blog/current/index.json",
  notesIndexFile: "/home/lyy/services/pnc-blog/current/notes.json",
  indexCheckMs: 10_000,
  powDifficulty: 14,
  challengeTtlMs: 60_000,
  rateGetPerMinute: 120,
  rateChallengePerMinute: 20,
  ratePostPerMinute: 10,
  ratePostPerHour: 60,
  burstTargetLimit: 30,
  burstGlobalLimit: 200,
  burstWindowMs: 600_000,
  breakerCooldownMs: 900_000,
  telegramBotToken: "",
  telegramChatId: "",
});

const MIN_SECRET_LENGTH = 32;
const MAX_BODY_BYTES = 4096;
const MAX_USED_CHALLENGES = 50_000;
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

// ---------------------------------------------------------------------------
// Configuration

// NAME, or the trimmed contents of the file named by NAME_FILE.
function secretSetting(env, name) {
  if (env[name]) return String(env[name]).trim();
  if (env[`${name}_FILE`]) return fs.readFileSync(env[`${name}_FILE`], "utf8").trim();
  return "";
}

function intSetting(env, name, fallback) {
  const raw = env[name];
  if (raw === undefined || String(raw).trim() === "") return fallback;
  const value = Number(String(raw).trim());
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer, got "${raw}"`);
  return value;
}

// The only place that reads process.env (createReactionsServer never does).
export function loadConfig(env = process.env) {
  const text = (name, fallback) => (env[name] ? String(env[name]).trim() : fallback);
  return {
    host: text("HOST", DEFAULTS.host),
    port: intSetting(env, "PORT", DEFAULTS.port),
    dbPath: text("DB_PATH", DEFAULTS.dbPath),
    siteOrigin: text("SITE_ORIGIN", DEFAULTS.siteOrigin),
    blogIndexFile: text("BLOG_INDEX_FILE", DEFAULTS.blogIndexFile),
    notesIndexFile: text("NOTES_INDEX_FILE", DEFAULTS.notesIndexFile),
    indexCheckMs: intSetting(env, "INDEX_CHECK_MS", DEFAULTS.indexCheckMs),
    challengeSecret: secretSetting(env, "CHALLENGE_SECRET"),
    visitorSalt: secretSetting(env, "VISITOR_SALT"),
    powDifficulty: intSetting(env, "POW_DIFFICULTY", DEFAULTS.powDifficulty),
    challengeTtlMs: intSetting(env, "CHALLENGE_TTL_MS", DEFAULTS.challengeTtlMs),
    rateGetPerMinute: intSetting(env, "RATE_GET_PER_MINUTE", DEFAULTS.rateGetPerMinute),
    rateChallengePerMinute: intSetting(env, "RATE_CHALLENGE_PER_MINUTE", DEFAULTS.rateChallengePerMinute),
    ratePostPerMinute: intSetting(env, "RATE_POST_PER_MINUTE", DEFAULTS.ratePostPerMinute),
    ratePostPerHour: intSetting(env, "RATE_POST_PER_HOUR", DEFAULTS.ratePostPerHour),
    burstTargetLimit: intSetting(env, "BURST_TARGET_LIMIT", DEFAULTS.burstTargetLimit),
    burstGlobalLimit: intSetting(env, "BURST_GLOBAL_LIMIT", DEFAULTS.burstGlobalLimit),
    burstWindowMs: intSetting(env, "BURST_WINDOW_MS", DEFAULTS.burstWindowMs),
    breakerCooldownMs: intSetting(env, "BREAKER_COOLDOWN_MS", DEFAULTS.breakerCooldownMs),
    telegramBotToken: secretSetting(env, "TELEGRAM_BOT_TOKEN"),
    telegramChatId: secretSetting(env, "TELEGRAM_CHAT_ID"),
  };
}

// Without a regex: /\/+$/ on request paths backtracks quadratically.
function trimTrailingSlashes(text) {
  let end = text.length;
  while (end > 0 && text[end - 1] === "/") end--;
  return text.slice(0, end);
}

function resolveConfig(input) {
  const config = { ...DEFAULTS, ...input };
  config.siteOrigin = trimTrailingSlashes(String(config.siteOrigin));
  for (const name of ["challengeSecret", "visitorSalt"]) {
    if (typeof config[name] !== "string" || config[name].length < MIN_SECRET_LENGTH) {
      throw new Error(`${name} must be at least ${MIN_SECRET_LENGTH} characters (CHALLENGE_SECRET / VISITOR_SALT or their _FILE)`);
    }
  }
  if (config.challengeSecret === config.visitorSalt) throw new Error("challengeSecret and visitorSalt must differ");
  if (!Number.isInteger(config.powDifficulty) || config.powDifficulty < 0 || config.powDifficulty > 32) {
    throw new Error("powDifficulty must be an integer between 0 and 32");
  }
  for (const name of ["challengeTtlMs", "rateGetPerMinute", "rateChallengePerMinute", "ratePostPerMinute",
    "ratePostPerHour", "burstTargetLimit", "burstGlobalLimit", "burstWindowMs", "breakerCooldownMs"]) {
    if (!Number.isSafeInteger(config[name]) || config[name] < 1) throw new Error(`${name} must be a positive integer`);
  }
  if (typeof config.now !== "function") config.now = Date.now;
  return config;
}

// ---------------------------------------------------------------------------
// Alerts

// Sends one Telegram message; skipped when not configured, never throws.
export function telegramNotifier({ telegramBotToken, telegramChatId }) {
  if (!telegramBotToken || !telegramChatId) return async () => {};
  return async (text) => {
    try {
      const response = await fetch(`https://api.telegram.org/bot${telegramBotToken}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: telegramChatId, text }),
        signal: AbortSignal.timeout(10_000),
      });
      if (!response.ok) console.error(`Telegram alert failed: HTTP ${response.status}`);
    } catch (error) {
      console.error(`Telegram alert failed: ${error.message}`);
    }
  };
}

function breakerMessage({ scope, target: key, count, windowMs, until }) {
  const minutes = Math.round(windowMs / MINUTE);
  const resume = new Date(until).toISOString();
  // Already a whitelisted key; stripping line breaks keeps log lines honest anyway.
  const target = String(key).replace(/[\r\n]/g, "");
  return scope === "global"
    ? `[pnc-reactions] 全站熔断：${minutes} 分钟内新增 ${count} 次观测（最后一次：${target}），所有目标暂停写入至 ${resume}。`
    : `[pnc-reactions] 目标熔断：${target} 在 ${minutes} 分钟内新增 ${count} 次观测，暂停写入至 ${resume}。`;
}

// ---------------------------------------------------------------------------
// Whitelist: the posts and notes of the release Caddy is serving

// Re-reads an index file when its identity (inode, size, mtime) changes,
// checking at most once per checkMs. The file is opened once and both the
// identity and the contents come from that descriptor, so a release switch
// between the two cannot pair one file's identity with another's contents.
// A file that cannot be read yields null.
function watchedIndex(file, parse, checkMs, now) {
  let state = { checkedAt: -Infinity, signature: "", keys: null };
  return () => {
    const time = now();
    if (time - state.checkedAt < checkMs) return state.keys;
    state.checkedAt = time;
    let fd;
    try {
      fd = fs.openSync(file, "r");
    } catch (error) {
      // Log the first failure (including a wrong path at startup), not every check.
      if (state.signature !== "missing") console.warn(`Index unavailable: ${file} (${error.code || error.message})`);
      state = { checkedAt: time, signature: "missing", keys: null };
      return null;
    }
    try {
      const stat = fs.fstatSync(fd);
      const signature = `${stat.ino}:${stat.size}:${stat.mtimeMs}`;
      if (signature === state.signature) return state.keys;
      try {
        state = { checkedAt: time, signature, keys: parse(JSON.parse(fs.readFileSync(fd, "utf8"))) };
      } catch (error) {
        console.warn(`Unable to read index ${file}: ${error.message}`);
        state = { checkedAt: time, signature, keys: null };
      }
      return state.keys;
    } finally {
      fs.closeSync(fd);
    }
  };
}

// dist/index.json: [{ url: "posts/<slug>.html", ... }]
function postKeys(rows) {
  if (!Array.isArray(rows)) throw new Error("expected an array");
  const keys = new Set();
  for (const row of rows) {
    const key = `/${String(row?.url ?? "").replace(/^\/+/, "")}`;
    if (POST_TARGET.test(key)) keys.add(key);
  }
  return keys;
}

// dist/notes.json: { entries: [{ url: "/notes.html#<id>", ... }] }
function noteKeys(data) {
  if (!Array.isArray(data?.entries)) throw new Error("expected { entries: [] }");
  const keys = new Set();
  for (const entry of data.entries) {
    const key = `note:${String(entry?.url ?? "").split("#")[1] ?? ""}`;
    if (NOTE_TARGET.test(key)) keys.add(key);
  }
  return keys;
}

// ---------------------------------------------------------------------------
// HTTP helpers

class HttpError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

const retryAfter = (ms) => ({ "retry-after": String(Math.max(1, Math.ceil(ms / 1000))) });

function send(res, status, body, headers = {}) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    ...headers,
  });
  res.end(JSON.stringify(body));
}

// Only Caddy on this host connects; it passes Cloudflare's headers through.
function clientIp(req) {
  const first = (value) => String(value ?? "").split(",")[0].trim();
  return first(req.headers["cf-connecting-ip"]) || first(req.headers["x-forwarded-for"]) || req.socket.remoteAddress || "";
}

// Writes must come from the site's own pages. Browsers omit Origin on
// same-origin GETs, so the challenge request is matched by its Referer.
function assertSameOrigin(req, siteOrigin) {
  const site = req.headers["sec-fetch-site"];
  const origin = req.headers.origin;
  const referer = String(req.headers.referer ?? "");
  const fromSite = origin !== undefined ? origin === siteOrigin : referer.startsWith(`${siteOrigin}/`);
  if ((site !== undefined && site !== "same-origin") || !fromSite) {
    throw new HttpError(403, "请从博客页面内操作。");
  }
}

async function readJson(req) {
  if (Number(req.headers["content-length"] || 0) > MAX_BODY_BYTES) throw new HttpError(413, "请求体过大。", { connection: "close" });
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, "请求体过大。", { connection: "close" });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new HttpError(400, "请求格式无效。");
  }
}

// ---------------------------------------------------------------------------
// Server

/**
 * Builds the API without listening and without reading process.env.
 * `input` is a plain object (see DEFAULTS and loadConfig for the keys); tests
 * may also pass `now()` (epoch ms) and `notify(text)` (replaces Telegram).
 * Returns { server, config, listen(port?, host?) -> Promise<{ host, port }>, close() -> Promise }.
 */
export function createReactionsServer(input) {
  const config = resolveConfig(input);
  const { now } = config;
  const notify = typeof config.notify === "function" ? config.notify : telegramNotifier(config);

  fs.mkdirSync(path.dirname(config.dbPath), { recursive: true });
  const db = new DatabaseSync(config.dbPath);
  db.exec("PRAGMA busy_timeout = 5000; PRAGMA synchronous = NORMAL;");
  db.exec(fs.readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
  // Prefix reads are primary-key range scans: target >= prefix AND target <
  // prefix with its last character incremented ("note:" -> "note;"). The
  // prefixes are fixed constants and never reach LIKE, so no input can act
  // as a wildcard.
  const sql = {
    insert: db.prepare("INSERT OR IGNORE INTO reactions (target, visitor_hash, created_at) VALUES (?, ?, ?)"),
    count: db.prepare("SELECT COUNT(*) AS n FROM reactions WHERE target = ?"),
    observedOne: db.prepare("SELECT 1 AS hit FROM reactions WHERE target = ? AND visitor_hash = ?"),
    countsByPrefix: db.prepare(`SELECT target, COUNT(*) AS n FROM reactions
      WHERE target >= ? AND target < ? GROUP BY target`),
    observedByPrefix: db.prepare(`SELECT target FROM reactions
      WHERE visitor_hash = ? AND target >= ? AND target < ?`),
  };

  const posts = watchedIndex(config.blogIndexFile, postKeys, config.indexCheckMs, now);
  const notes = watchedIndex(config.notesIndexFile, noteKeys, config.indexCheckMs, now);
  // null when neither index can be read: writes then fail closed.
  const whitelist = () => {
    const postSet = posts();
    const noteSet = notes();
    if (!postSet && !noteSet) return null;
    return { has: (key) => Boolean(postSet?.has(key) || noteSet?.has(key)) };
  };

  const limits = {
    get: new SlidingWindowLimiter({ limit: config.rateGetPerMinute, windowMs: MINUTE }),
    challenge: new SlidingWindowLimiter({ limit: config.rateChallengePerMinute, windowMs: MINUTE }),
    postMinute: new SlidingWindowLimiter({ limit: config.ratePostPerMinute, windowMs: MINUTE }),
    postHour: new SlidingWindowLimiter({ limit: config.ratePostPerHour, windowMs: HOUR }),
  };
  const limit = (limiter, key) => {
    const { ok, retryAfterMs } = limiter.hit(key, now());
    if (!ok) throw new HttpError(429, "操作太频繁，请稍后再试。", retryAfter(retryAfterMs));
  };

  const breaker = new CircuitBreaker({
    targetLimit: config.burstTargetLimit,
    globalLimit: config.burstGlobalLimit,
    windowMs: config.burstWindowMs,
    cooldownMs: config.breakerCooldownMs,
    onOpen: (event) => {
      const text = breakerMessage(event);
      console.warn(text);
      return notify(text);
    },
  });
  const assertWritable = (target) => {
    if (breaker.isOpen(target, now())) {
      throw new HttpError(503, "观测暂时关闭，请稍后再试。", retryAfter(breaker.openUntil(target, now()) - now()));
    }
  };

  // Random ids of challenges already spent, until they expire. In memory on
  // purpose: a restart forgets them, which within a 60 s TTL can at worst let
  // one challenge be replayed by its own visitor, and the database still
  // dedupes that visitor. Insertion order is close to expiry order (fixed
  // TTL), so pruning walks from the front.
  const usedChallenges = new Map();
  const spendChallenge = (random, expiresAt) => {
    const time = now();
    for (const [key, expiry] of usedChallenges) {
      if (expiry > time && usedChallenges.size < MAX_USED_CHALLENGES) break;
      usedChallenges.delete(key);
    }
    usedChallenges.set(random, expiresAt);
  };

  const visitorOf = (req) => visitorKey(clientIp(req), config.visitorSalt);

  const requireTarget = (key, list) => {
    if (!isTargetSyntax(key)) throw new HttpError(400, "目标地址无效。");
    if (!list) throw new HttpError(503, "观测暂时不可用，请稍后再试。");
    if (!list.has(key)) throw new HttpError(400, "目标不存在。");
  };

  // path -> method -> handler. Maps, so no request text indexes a plain object.
  const routes = new Map(Object.entries({
    "/reactions/healthz": {
      GET: () => [200, { ok: true }],
    },

    "/reactions": {
      GET: (req, url) => {
        const visitor = visitorOf(req);
        limit(limits.get, visitor);
        const target = url.searchParams.get("target");
        const prefix = url.searchParams.get("prefix");
        if ((target === null) === (prefix === null)) throw new HttpError(400, "请提供 target 或 prefix 之一。");
        const list = whitelist();
        if (target !== null) {
          if (!isTargetSyntax(target)) throw new HttpError(400, "目标地址无效。");
          // Reads never fail because of a missing index; they just show nothing.
          if (!list) return [200, { counts: {}, observed: [] }];
          if (!list.has(target)) throw new HttpError(400, "目标不存在。");
          const seen = sql.observedOne.get(target, visitor);
          return [200, { counts: { [target]: Number(sql.count.get(target).n) }, observed: seen ? [target] : [] }];
        }
        if (!TARGET_PREFIXES.includes(prefix)) throw new HttpError(400, "前缀无效。");
        // Without that prefix's own index the counts would read as all zero:
        // say "unavailable" instead, so the page keeps its buttons hidden.
        const set = prefix === "note:" ? notes() : posts();
        if (!set) throw new HttpError(503, "观测暂时不可用，请稍后再试。");
        const upper = prefix.slice(0, -1) + String.fromCharCode(prefix.charCodeAt(prefix.length - 1) + 1);
        const counts = Object.fromEntries(sql.countsByPrefix.all(prefix, upper)
          .filter((row) => row.n > 0 && set.has(row.target))
          .map((row) => [row.target, Number(row.n)]));
        const observed = sql.observedByPrefix.all(visitor, prefix, upper)
          .map((row) => row.target)
          .filter((key) => set.has(key))
          .sort();
        return [200, { counts, observed }];
      },

      POST: async (req) => {
        assertSameOrigin(req, config.siteOrigin);
        const visitor = visitorOf(req);
        limit(limits.postMinute, visitor);
        limit(limits.postHour, visitor);

        const body = await readJson(req);
        const target = body?.target;
        const challenge = body?.challenge;
        // The browser sends a decimal string; a plain safe integer is the same text.
        const nonce = Number.isSafeInteger(body?.nonce) && body.nonce >= 0 ? String(body.nonce) : body?.nonce;
        if (!isTargetSyntax(target)) throw new HttpError(400, "目标地址无效。");
        if (typeof challenge !== "string" || challenge.length === 0 || challenge.length > 512) {
          throw new HttpError(400, "验证数据无效。");
        }
        if (!isNonceSyntax(nonce)) throw new HttpError(400, "验证数据无效。");
        requireTarget(target, whitelist());
        assertWritable(target);

        const payload = verifyChallenge(challenge, config.challengeSecret);
        const time = now();
        if (!payload || payload.target !== target || payload.visitor !== visitor.slice(0, 16)
          || payload.difficulty < config.powDifficulty) {
          throw new HttpError(403, "验证无效，请刷新页面后重试。");
        }
        if (payload.expiresAt <= time) throw new HttpError(403, "验证已过期，请重试。");
        if (usedChallenges.has(payload.random)) throw new HttpError(403, "验证已使用，请重试。");
        // Spent even if the work is wrong: one POST per challenge, so the
        // server never checks many guesses for one puzzle.
        spendChallenge(payload.random, payload.expiresAt);
        if (!verifyPow(challenge, nonce, payload.difficulty)) throw new HttpError(403, "验证未通过，请重试。");

        const counted = sql.insert.run(target, visitor, new Date(time).toISOString()).changes > 0;
        if (counted) breaker.record(target, time);
        return [200, { target, count: Number(sql.count.get(target).n), observed: true, counted }];
      },
    },

    "/reactions/challenge": {
      GET: (req, url) => {
        assertSameOrigin(req, config.siteOrigin);
        const visitor = visitorOf(req);
        limit(limits.challenge, visitor);
        const target = url.searchParams.get("target") ?? "";
        requireTarget(target, whitelist());
        // No point making the browser work for a write that would get 503.
        assertWritable(target);
        const expiresAt = now() + config.challengeTtlMs;
        const challenge = signChallenge({
          target,
          visitor,
          difficulty: config.powDifficulty,
          expiresAt,
          random: crypto.randomBytes(16).toString("base64url"),
        }, config.challengeSecret);
        return [200, { challenge, difficulty: config.powDifficulty, expiresAt }];
      },
    },
  }).map(([pathname, methods]) => [pathname, new Map(Object.entries(methods))]));

  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", "http://localhost");
      // Accept /api/... too, for direct calls that skip Caddy.
      const pathname = trimTrailingSlashes(url.pathname.replace(/^\/api(?=\/|$)/, "")) || "/";
      const route = routes.get(pathname);
      if (!route) throw new HttpError(404, "接口不存在。");
      const handler = route.get(req.method === "HEAD" ? "GET" : req.method);
      if (!handler) throw new HttpError(405, "请求方法不支持。", { allow: [...route.keys()].join(", ") });
      const [status, body] = await handler(req, url);
      send(res, status, body);
    } catch (error) {
      if (!(error instanceof HttpError)) console.error(error);
      const known = error instanceof HttpError;
      if (res.headersSent) return res.destroy();
      send(res, known ? error.status : 500, { error: known ? error.message : "服务器错误。" }, known ? error.headers : {});
    }
  });
  // Bound slow clients; Caddy on loopback is the only peer.
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;

  let closed;
  return {
    server,
    config,
    listen(port = config.port, host = config.host) {
      return new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, () => {
          server.off("error", reject);
          const address = server.address();
          resolve({ host: address.address, port: address.port });
        });
      });
    },
    close() {
      closed ||= new Promise((resolve) => {
        const finish = () => {
          db.close();
          resolve();
        };
        if (!server.listening) return finish();
        server.close(finish);
        server.closeAllConnections();
      });
      return closed;
    },
  };
}

// ---------------------------------------------------------------------------
// Entry point: listen only when run directly (node server.mjs), not on import.

function isMain() {
  if (!process.argv[1]) return false;
  try {
    return fs.realpathSync(process.argv[1]) === fs.realpathSync(import.meta.filename);
  } catch {
    return false;
  }
}

if (isMain()) {
  const api = createReactionsServer(loadConfig());
  const { host, port } = await api.listen();
  const { dbPath, powDifficulty } = api.config;
  console.log(`pnc-reactions listening on http://${host}:${port} (db: ${dbPath}, difficulty: ${powDifficulty} bits)`);
  for (const signal of ["SIGTERM", "SIGINT"]) {
    process.once(signal, () => api.close().then(() => process.exit(0)));
  }
}
