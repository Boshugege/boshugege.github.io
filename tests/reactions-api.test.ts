// Tests for the "观测" reactions API reference implementation in
// ops/reactions-api (deployed separately; never part of dist/).
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { after, before, describe, it, test } from "node:test";
import {
  CircuitBreaker,
  SlidingWindowLimiter,
  isTargetSyntax,
  leadingZeroBits,
  normalizeIp,
  signChallenge,
  verifyChallenge,
  verifyPow,
  visitorKey,
} from "../ops/reactions-api/lib.mjs";
import { createReactionsServer, loadConfig } from "../ops/reactions-api/server.mjs";

const SITE = "https://parityncsvt.top";
// Fresh per run, so no key material lives in the repository.
const CHALLENGE_SECRET = crypto.randomBytes(24).toString("hex");
const VISITOR_SALT = crypto.randomBytes(24).toString("hex");

// Independent of lib.mjs: walk the digest bit by bit.
function hasZeroBits(digest: Uint8Array, bits: number): boolean {
  for (let i = 0; i < bits; i++) if (digest[i >> 3] & (0x80 >> (i & 7))) return false;
  return true;
}
const digestOf = (challenge: string, nonce: string) => crypto.createHash("sha256").update(`${challenge}:${nonce}`, "utf8").digest();
function solve(challenge: string, bits: number): string {
  for (let n = 0; ; n++) if (hasZeroBits(digestOf(challenge, String(n)), bits)) return String(n);
}
function unsolved(challenge: string, bits: number): string {
  for (let n = 0; ; n++) if (!hasZeroBits(digestOf(challenge, String(n)), bits)) return String(n);
}

test("normalizeIp keeps IPv4, unwraps IPv4-mapped and folds IPv6 to its /64", () => {
  assert.equal(normalizeIp("203.0.113.7"), "203.0.113.7");
  assert.equal(normalizeIp(" 203.0.113.7 "), "203.0.113.7");
  assert.equal(normalizeIp("::ffff:203.0.113.7"), "203.0.113.7");
  assert.equal(normalizeIp("::FFFF:cb00:7107"), "203.0.113.7");

  const sameNetwork = [
    "2001:db8:0:1::1",
    "2001:0db8:0000:0001:0000:0000:0000:0001",
    "2001:DB8:0:1:abcd:ef01:2345:6789",
    "[2001:db8:0:1::42]",
    "2001:db8:0:1:0:0:1.2.3.4",
  ];
  for (const ip of sameNetwork) assert.equal(normalizeIp(ip), "2001:db8:0:1::/64", ip);
  const keys = new Set(sameNetwork.map((ip) => visitorKey(ip, VISITOR_SALT)));
  assert.equal(keys.size, 1);
  assert.notEqual(visitorKey("2001:db8:0:2::1", VISITOR_SALT), visitorKey("2001:db8:0:1::1", VISITOR_SALT));
  assert.equal(normalizeIp("2001:db8:0:2::1"), "2001:db8:0:2::/64");
  assert.equal(normalizeIp("::1"), "0:0:0:0::/64");
  assert.equal(normalizeIp("fe80::1%eth0"), "fe80:0:0:0::/64");
  assert.equal(normalizeIp("1:2:3:4:5:6:7::"), "1:2:3:4::/64");

  for (const garbage of ["", "unknown", "not-an-ip", "1.2.3", "256.1.1.1", "01.2.3.4", "2001:db8::1::2", "12345::",
    "gggg::1", "1:2:3:4:5:6:7:8:9", "1:2:3:4:5:6:7:8::", ":::", "::ffff:999.1.1.1", undefined as unknown as string]) {
    assert.equal(normalizeIp(garbage), "unknown", String(garbage));
  }
  assert.match(visitorKey("203.0.113.7", VISITOR_SALT), /^[0-9a-f]{64}$/);
  assert.notEqual(visitorKey("203.0.113.7", VISITOR_SALT), visitorKey("203.0.113.7", `${VISITOR_SALT}!`));
});

test("isTargetSyntax accepts post paths and note ids only", () => {
  for (const key of ["/posts/cg-final-path-tracing.html", "/posts/a_b.c-1.html", "note:2026-04-11", "note:2026-04-11-2"]) {
    assert.equal(isTargetSyntax(key), true, key);
  }
  for (const key of ["", "/posts/.html", "/posts/a/b.html", "posts/a.html", "/posts/a.html?x=1", "/posts/a b.html",
    "note:2026-4-11", "note:x", "note:2026-04-11-", "/notes.html#2026-04-11", `/posts/${"a".repeat(300)}.html`, 42]) {
    assert.equal(isTargetSyntax(key as string), false, String(key));
  }
});

test("leadingZeroBits counts from the most significant bit", () => {
  assert.equal(leadingZeroBits(new Uint8Array([])), 0);
  assert.equal(leadingZeroBits(new Uint8Array([0xff])), 0);
  assert.equal(leadingZeroBits(new Uint8Array([0x0f, 0x00])), 4);
  assert.equal(leadingZeroBits(new Uint8Array([0x00, 0x01])), 15);
  assert.equal(leadingZeroBits(new Uint8Array([0x00, 0x00, 0x80])), 16);
  assert.equal(leadingZeroBits(new Uint8Array([0x00, 0x00, 0x00, 0x00])), 32);
});

test("verifyPow checks SHA-256(challenge:nonce) against the difficulty", () => {
  const challenge = "v1.test-challenge.signature";
  const nonce = solve(challenge, 8);
  assert.equal(verifyPow(challenge, nonce, 8), true);
  assert.equal(verifyPow(challenge, nonce, 0), true);
  const bits = leadingZeroBits(digestOf(challenge, nonce));
  assert.ok(hasZeroBits(digestOf(challenge, nonce), bits) && !hasZeroBits(digestOf(challenge, nonce), bits + 1));
  assert.equal(verifyPow(challenge, nonce, bits), true);
  assert.equal(verifyPow(challenge, nonce, bits + 1), false);
  assert.equal(verifyPow(challenge, unsolved(challenge, 8), 8), false);
  assert.equal(verifyPow(`${challenge}x`, nonce, 8), hasZeroBits(digestOf(`${challenge}x`, nonce), 8));

  for (const bad of ["", "01", "-1", "+1", "1.5", "1e3", " 1", "0x10", "12345678901234567", 7, null]) {
    assert.equal(verifyPow(challenge, bad as string, 0), false, String(bad));
  }
  assert.equal(verifyPow(challenge, "0", 0), true);
  assert.equal(verifyPow(challenge, "1234567890123456", 0), true);
  assert.equal(verifyPow(challenge, nonce, -1), false);
  assert.equal(verifyPow(challenge, nonce, 8.5), false);
});

test("signChallenge and verifyChallenge round-trip and reject tampering", () => {
  const visitor = visitorKey("203.0.113.7", VISITOR_SALT);
  const fields = { target: "/posts/a.html", visitor, difficulty: 14, expiresAt: 1_800_000_060_000, random: crypto.randomBytes(16).toString("base64url") };
  const challenge = signChallenge(fields, CHALLENGE_SECRET);
  assert.match(challenge, /^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$/);
  assert.deepEqual(verifyChallenge(challenge, CHALLENGE_SECRET), { ...fields, visitor: visitor.slice(0, 16) });

  const [version, payload, signature] = challenge.split(".");
  const forged = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  forged.d = 1;
  const forgedPayload = Buffer.from(JSON.stringify(forged)).toString("base64url");
  assert.equal(verifyChallenge(`${version}.${forgedPayload}.${signature}`, CHALLENGE_SECRET), null);

  const flipped = `${signature[0] === "A" ? "B" : "A"}${signature.slice(1)}`;
  assert.equal(verifyChallenge(`${version}.${payload}.${flipped}`, CHALLENGE_SECRET), null);
  // A second spelling of the same signature bytes (unused low bits of the
  // last base64url char) must not pass either.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const respelled = signature.slice(0, -1) + alphabet[alphabet.indexOf(signature.at(-1)!) ^ 1];
  assert.deepEqual(Buffer.from(respelled, "base64url"), Buffer.from(signature, "base64url"));
  assert.equal(verifyChallenge(`${version}.${payload}.${respelled}`, CHALLENGE_SECRET), null);

  assert.equal(verifyChallenge(challenge, `${CHALLENGE_SECRET}-other`), null);
  for (const bad of ["", "v1", "v1..", `v2.${payload}.${signature}`, `${challenge}.x`, `${challenge}=`, "x".repeat(600), 42]) {
    assert.equal(verifyChallenge(bad as string, CHALLENGE_SECRET), null, String(bad));
  }
  // Well signed but malformed payloads are refused too.
  const signed = (body: object) => {
    const text = `v1.${Buffer.from(JSON.stringify(body)).toString("base64url")}`;
    return `${text}.${crypto.createHmac("sha256", CHALLENGE_SECRET).update(text).digest("base64url")}`;
  };
  assert.equal(verifyChallenge(signed({ t: "/posts/a.html", v: "zz", d: 8, e: 1, r: "a".repeat(22) }), CHALLENGE_SECRET), null);
  assert.equal(verifyChallenge(signed({ t: "/posts/a.html", v: "0123456789abcdef", d: "8", e: 1, r: "a".repeat(22) }), CHALLENGE_SECRET), null);
  assert.ok(verifyChallenge(signed({ t: "/posts/a.html", v: "0123456789abcdef", d: 8, e: 1, r: "a".repeat(22) }), CHALLENGE_SECRET));
});

test("SlidingWindowLimiter allows `limit` hits per window and reports when to retry", () => {
  const limiter = new SlidingWindowLimiter({ limit: 3, windowMs: 1000 });
  assert.deepEqual(limiter.hit("a", 0), { ok: true, retryAfterMs: 0 });
  assert.equal(limiter.hit("a", 100).ok, true);
  assert.equal(limiter.hit("a", 200).ok, true);
  assert.deepEqual(limiter.hit("a", 300), { ok: false, retryAfterMs: 700 });
  assert.deepEqual(limiter.hit("a", 999), { ok: false, retryAfterMs: 1 });
  assert.equal(limiter.hit("b", 999).ok, true, "keys are independent");
  assert.equal(limiter.hit("a", 1000).ok, true, "the oldest hit left the window");
  assert.deepEqual(limiter.hit("a", 1001), { ok: false, retryAfterMs: 99 });

  const small = new SlidingWindowLimiter({ limit: 1, windowMs: 1000, maxKeys: 10 });
  for (let i = 0; i < 50; i++) small.hit(`k${i}`, i);
  assert.ok(small.size <= 10, `size ${small.size}`);
  assert.equal(small.hit("k49", 60).ok, false, "recent keys survive pruning");
});

test("CircuitBreaker opens per target and globally, once per opening", () => {
  const events: Array<{ scope: string; target: string; count: number }> = [];
  const breaker = new CircuitBreaker({
    targetLimit: 3, globalLimit: 5, windowMs: 1000, cooldownMs: 5000, onOpen: (event: { scope: string; target: string; count: number }) => { events.push(event); },
  });
  for (const time of [0, 1, 2]) breaker.record("a", time);
  assert.equal(breaker.isOpen("a", 2), false);
  assert.equal(breaker.record("a", 3), true);
  assert.equal(events.length, 1);
  assert.deepEqual([events[0].scope, events[0].target, events[0].count], ["target", "a", 4]);
  assert.equal(breaker.isOpen("a", 4), true);
  assert.equal(breaker.isOpen("b", 4), false);
  assert.equal(breaker.openUntil("a", 4), 5003);

  breaker.record("a", 5); // still open: no second alert
  assert.equal(events.length, 1);
  breaker.record("b", 6); // sixth reaction in the window: global
  assert.equal(events.length, 2);
  assert.equal(events[1].scope, "global");
  assert.equal(breaker.isOpen("c", 7), true);
  assert.equal(breaker.isOpen("c", 5006), false);
  assert.equal(breaker.isOpen("a", 5006), false);

  // After the cooldown it counts afresh and can open (and alert) again.
  for (const time of [6000, 6001, 6002]) breaker.record("a", time);
  assert.equal(events.length, 2);
  breaker.record("a", 6003);
  assert.equal(events.length, 3);
  assert.equal(events[2].target, "a");

  // A throwing or rejecting alert never breaks recording.
  const quiet = console.error;
  console.error = () => {};
  try {
    const failing = new CircuitBreaker({ targetLimit: 0, globalLimit: 100, windowMs: 1000, cooldownMs: 1000, onOpen: () => { throw new Error("boom"); } });
    assert.equal(failing.record("x", 0), true);
    const rejecting = new CircuitBreaker({ targetLimit: 0, globalLimit: 100, windowMs: 1000, cooldownMs: 1000, onOpen: () => Promise.reject(new Error("boom")) });
    assert.equal(rejecting.record("x", 0), true);
  } finally {
    setImmediate(() => { console.error = quiet; });
  }
});

test("loadConfig reads env and *_FILE secrets; the server refuses weak secrets", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pnc-reactions-config-"));
  try {
    const secretFile = path.join(dir, "secret");
    fs.writeFileSync(secretFile, `${CHALLENGE_SECRET}\n`);
    const config = loadConfig({ CHALLENGE_SECRET_FILE: secretFile, VISITOR_SALT, PORT: "46299", POW_DIFFICULTY: "12", BURST_TARGET_LIMIT: "" });
    assert.equal(config.challengeSecret, CHALLENGE_SECRET);
    assert.equal(config.visitorSalt, VISITOR_SALT);
    assert.equal(config.port, 46299);
    assert.equal(config.powDifficulty, 12);
    assert.equal(config.burstTargetLimit, 30);
    assert.equal(config.host, "127.0.0.1");
    assert.equal(config.siteOrigin, SITE);
    assert.equal(config.notesIndexFile, "/home/lyy/services/pnc-blog/current/notes.json");
    assert.throws(() => loadConfig({ PORT: "abc" }), /PORT/);

    const dbPath = path.join(dir, "never.db");
    assert.throws(() => createReactionsServer({ ...loadConfig({}), dbPath }), /challengeSecret/);
    assert.throws(() => createReactionsServer({ dbPath, challengeSecret: "short", visitorSalt: VISITOR_SALT }), /challengeSecret/);
    assert.throws(() => createReactionsServer({ dbPath, challengeSecret: VISITOR_SALT, visitorSalt: VISITOR_SALT }), /differ/);
    assert.equal(fs.existsSync(dbPath), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test(".env.example lists every setting with its default and no secrets", () => {
  const example = fs.readFileSync(new URL("../ops/reactions-api/.env.example", import.meta.url), "utf8");
  const env: Record<string, string> = {};
  for (const line of example.split("\n")) {
    const match = /^([A-Z_]+)=(.*)$/.exec(line);
    if (match) env[match[1]] = match[2];
  }
  for (const name of ["CHALLENGE_SECRET", "VISITOR_SALT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]) assert.equal(env[name], "", name);
  const fromExample = loadConfig(env);
  const fromNothing = loadConfig({});
  assert.deepEqual(fromExample, fromNothing);
  // Every key loadConfig knows is documented (secrets and Telegram ids included).
  const documented = new Set(Object.keys(env));
  for (const name of ["HOST", "PORT", "DB_PATH", "SITE_ORIGIN", "BLOG_INDEX_FILE", "NOTES_INDEX_FILE", "INDEX_CHECK_MS",
    "POW_DIFFICULTY", "CHALLENGE_TTL_MS", "RATE_GET_PER_MINUTE", "RATE_CHALLENGE_PER_MINUTE", "RATE_POST_PER_MINUTE",
    "RATE_POST_PER_HOUR", "BURST_TARGET_LIMIT", "BURST_GLOBAL_LIMIT", "BURST_WINDOW_MS", "BREAKER_COOLDOWN_MS"]) {
    assert.ok(documented.has(name) && env[name] !== "", name);
  }
  assert.equal(Object.keys(fromNothing).length, documented.size);
});

// ---------------------------------------------------------------------------
// End to end against a real server on a random port.

type Reply = { status: number; headers: http.IncomingHttpHeaders; body: any };
type Headers = Record<string, string | undefined>;

function request(port: number, method: string, pathname: string, options: { headers?: Headers; body?: unknown } = {}): Promise<Reply> {
  const payload = options.body === undefined ? undefined : typeof options.body === "string" ? options.body : JSON.stringify(options.body);
  const headers: Record<string, string | number> = {};
  if (payload !== undefined) Object.assign(headers, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
  for (const [name, value] of Object.entries(options.headers || {})) if (value !== undefined) headers[name] = value;
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, method, path: pathname, headers, agent: false }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (chunk: Buffer) => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve({ status: res.statusCode || 0, headers: res.headers, body: text ? JSON.parse(text) : undefined });
      });
    });
    req.on("error", reject);
    req.end(payload);
  });
}

function writeIndexes(dir: string, extraNotes: string[] = []) {
  const posts = ["alpha", "beta", "burst"].map((slug) => ({ url: `posts/${slug}.html`, title: slug }));
  fs.writeFileSync(path.join(dir, "index.json"), JSON.stringify(posts));
  const ids = ["2026-04-11", "2026-04-11-2", ...extraNotes];
  fs.writeFileSync(path.join(dir, "notes.json"), JSON.stringify({ version: 1, entries: ids.map((id) => ({ date: id.slice(0, 10), url: `/notes.html#${id}` })) }));
}

describe("reactions API end to end", () => {
  const start = Date.UTC(2026, 10, 21, 12, 0, 0);
  let clock = start;
  const notices: string[] = [];
  let dir = "";
  let dbPath = "";
  let api: ReturnType<typeof createReactionsServer>;
  let port = 0;

  const call = (method: string, pathname: string, options?: { headers?: Headers; body?: unknown }) => request(port, method, pathname, options);
  const counts = (ip: string, query: string) => call("GET", `/api/reactions?${query}`, { headers: { "cf-connecting-ip": ip } });
  const challengeFor = (ip: string, target: string, headers: Headers = {}) => call("GET", `/api/reactions/challenge?target=${encodeURIComponent(target)}`, {
    headers: { "cf-connecting-ip": ip, referer: `${SITE}/posts/alpha.html`, "sec-fetch-site": "same-origin", ...headers },
  });
  const post = (ip: string, body: unknown, headers: Headers = {}) => call("POST", "/api/reactions", {
    headers: { "cf-connecting-ip": ip, origin: SITE, "sec-fetch-site": "same-origin", ...headers }, body,
  });
  async function observe(ip: string, target: string) {
    const issued = await challengeFor(ip, target);
    assert.equal(issued.status, 200, JSON.stringify(issued.body));
    return post(ip, { target, challenge: issued.body.challenge, nonce: solve(issued.body.challenge, issued.body.difficulty) });
  }

  before(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "pnc-reactions-"));
    writeIndexes(dir);
    dbPath = path.join(dir, "data", "reactions.db");
    api = createReactionsServer({
      dbPath,
      blogIndexFile: path.join(dir, "index.json"),
      notesIndexFile: path.join(dir, "notes.json"),
      siteOrigin: `${SITE}/`,
      challengeSecret: CHALLENGE_SECRET,
      visitorSalt: VISITOR_SALT,
      powDifficulty: 8,
      burstTargetLimit: 3,
      now: () => clock,
      notify: (text: string) => { notices.push(text); },
    });
    ({ port } = await api.listen(0, "127.0.0.1"));
  });

  after(async () => {
    await api?.close();
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it("serves health and JSON headers, with or without the /api prefix", async () => {
    for (const pathname of ["/api/reactions/healthz", "/reactions/healthz"]) {
      const reply = await call("GET", pathname);
      assert.equal(reply.status, 200);
      assert.deepEqual(reply.body, { ok: true });
      assert.equal(reply.headers["content-type"], "application/json; charset=utf-8");
      assert.equal(reply.headers["cache-control"], "no-store");
      assert.equal(reply.headers["x-content-type-options"], "nosniff");
      assert.equal(reply.headers["access-control-allow-origin"], undefined);
    }
    assert.equal((await call("GET", "/api/reactions/nope")).status, 404);
    assert.equal((await call("GET", "/api/comments")).status, 404);
    const wrong = await call("DELETE", "/api/reactions");
    assert.equal(wrong.status, 405);
    assert.equal(wrong.headers.allow, "GET, POST");
    assert.equal((await call("POST", "/api/reactions/challenge")).status, 405);
    assert.equal((await call("POST", "/api/reactions/healthz")).status, 405);
  });

  it("reads zero counts for a target and an empty prefix listing", async () => {
    const one = await counts("203.0.113.10", "target=%2Fposts%2Falpha.html");
    assert.equal(one.status, 200);
    assert.deepEqual(one.body, { counts: { "/posts/alpha.html": 0 }, observed: [] });
    const notes = await counts("203.0.113.10", "prefix=note%3A");
    assert.deepEqual(notes.body, { counts: {}, observed: [] });
    for (const query of ["prefix=foo", "prefix=", "prefix=note", "", "target=%2Fposts%2Falpha.html&prefix=note%3A",
      "target=%2Fetc%2Fpasswd", "target=%2Fposts%2Fmissing.html"]) {
      const reply = await counts("203.0.113.10", query);
      assert.equal(reply.status, 400, query);
      assert.equal(typeof reply.body.error, "string");
    }
  });

  let firstChallenge = "";
  let firstNonce = "";
  it("issues a challenge to the site's own pages and counts the reaction once", async () => {
    const issued = await challengeFor("203.0.113.10", "/posts/alpha.html");
    assert.equal(issued.status, 200);
    assert.equal(issued.body.difficulty, 8);
    assert.equal(issued.body.expiresAt, clock + 60_000);
    assert.match(issued.body.challenge, /^v1\./);
    firstChallenge = issued.body.challenge;
    firstNonce = solve(firstChallenge, 8);

    const reply = await post("203.0.113.10", { target: "/posts/alpha.html", challenge: firstChallenge, nonce: firstNonce });
    assert.equal(reply.status, 200, JSON.stringify(reply.body));
    assert.deepEqual(reply.body, { target: "/posts/alpha.html", count: 1, observed: true, counted: true });

    const after = await counts("203.0.113.10", "target=%2Fposts%2Falpha.html");
    assert.deepEqual(after.body, { counts: { "/posts/alpha.html": 1 }, observed: ["/posts/alpha.html"] });
    const stranger = await counts("198.51.100.1", "target=%2Fposts%2Falpha.html");
    assert.deepEqual(stranger.body, { counts: { "/posts/alpha.html": 1 }, observed: [] });
  });

  it("is idempotent for the same visitor and stores only the salted hash", async () => {
    const again = await observe("203.0.113.10", "/posts/alpha.html");
    assert.equal(again.status, 200);
    assert.deepEqual(again.body, { target: "/posts/alpha.html", count: 1, observed: true, counted: false });

    const db = new DatabaseSync(dbPath);
    try {
      const rows = db.prepare("SELECT target, visitor_hash, created_at FROM reactions").all() as Array<Record<string, string>>;
      assert.equal(rows.length, 1);
      assert.equal(rows[0].visitor_hash, visitorKey("203.0.113.10", VISITOR_SALT));
      assert.equal(rows[0].created_at, new Date(start).toISOString());
    } finally {
      db.close();
    }
  });

  it("rejects replayed, expired, foreign, forged and unsolved challenges", async () => {
    const ip = "203.0.113.20";
    const replay = await post("203.0.113.10", { target: "/posts/alpha.html", challenge: firstChallenge, nonce: firstNonce });
    assert.equal(replay.status, 403);

    const stale = await challengeFor(ip, "/posts/beta.html");
    clock += 60_001;
    const expired = await post(ip, { target: "/posts/beta.html", challenge: stale.body.challenge, nonce: solve(stale.body.challenge, 8) });
    assert.equal(expired.status, 403);

    // Bound to its target...
    const forAlpha = await challengeFor(ip, "/posts/alpha.html");
    const otherTarget = await post(ip, { target: "/posts/beta.html", challenge: forAlpha.body.challenge, nonce: solve(forAlpha.body.challenge, 8) });
    assert.equal(otherTarget.status, 403);
    // ...and to its visitor.
    const forOther = await challengeFor(ip, "/posts/beta.html");
    const otherVisitor = await post("203.0.113.21", { target: "/posts/beta.html", challenge: forOther.body.challenge, nonce: solve(forOther.body.challenge, 8) });
    assert.equal(otherVisitor.status, 403);

    // Too little work spends the challenge: no second guess with it.
    const weak = await challengeFor(ip, "/posts/beta.html");
    const wrong = await post(ip, { target: "/posts/beta.html", challenge: weak.body.challenge, nonce: unsolved(weak.body.challenge, 8) });
    assert.equal(wrong.status, 403);
    const retry = await post(ip, { target: "/posts/beta.html", challenge: weak.body.challenge, nonce: solve(weak.body.challenge, 8) });
    assert.equal(retry.status, 403);

    // Correctly signed, but easier than the server's current difficulty.
    const easy = signChallenge({
      target: "/posts/beta.html", visitor: visitorKey(ip, VISITOR_SALT), difficulty: 1, expiresAt: clock + 60_000,
      random: crypto.randomBytes(16).toString("base64url"),
    }, CHALLENGE_SECRET);
    assert.equal((await post(ip, { target: "/posts/beta.html", challenge: easy, nonce: solve(easy, 1) })).status, 403);

    const genuine = await challengeFor(ip, "/posts/beta.html");
    const tampered = genuine.body.challenge.replace(/.$/, (c: string) => (c === "A" ? "B" : "A"));
    assert.equal((await post(ip, { target: "/posts/beta.html", challenge: tampered, nonce: solve(tampered, 8) })).status, 403);

    // Malformed nonces never reach the proof-of-work check (another visitor,
    // so this visitor's 10 writes a minute are not used up).
    const careful = "203.0.113.22";
    const fresh = await challengeFor(careful, "/posts/beta.html");
    for (const nonce of ["01", "abc", "-1", "1.5", "", "12345678901234567"]) {
      const reply = await post(careful, { target: "/posts/beta.html", challenge: fresh.body.challenge, nonce });
      assert.equal(reply.status, 400, nonce);
    }
    // A plain integer nonce is the same decimal text.
    const numeric = await post(careful, { target: "/posts/beta.html", challenge: fresh.body.challenge, nonce: Number(solve(fresh.body.challenge, 8)) });
    assert.equal(numeric.status, 200, JSON.stringify(numeric.body));
    assert.equal(numeric.body.counted, true);
  });

  it("accepts only same-origin requests for challenges and writes", async () => {
    const ip = "203.0.113.30";
    const target = "/posts/beta.html";
    const evil = "https://evil.example";
    assert.equal((await challengeFor(ip, target, { origin: evil })).status, 403);
    assert.equal((await challengeFor(ip, target, { referer: `${evil}/page` })).status, 403);
    assert.equal((await challengeFor(ip, target, { referer: `${SITE}.evil.example/` })).status, 403);
    assert.equal((await challengeFor(ip, target, { referer: undefined })).status, 403);
    assert.equal((await challengeFor(ip, target, { "sec-fetch-site": "cross-site" })).status, 403);
    assert.equal((await challengeFor(ip, target, { "sec-fetch-site": "same-site" })).status, 403);
    // Older browsers send no Sec-Fetch-Site; Origin or Referer then decides.
    assert.equal((await challengeFor(ip, target, { "sec-fetch-site": undefined })).status, 200);
    assert.equal((await challengeFor(ip, target, { origin: SITE })).status, 200);

    const issued = await challengeFor(ip, target);
    const body = { target, challenge: issued.body.challenge, nonce: solve(issued.body.challenge, 8) };
    assert.equal((await post(ip, body, { origin: evil })).status, 403);
    assert.equal((await post(ip, body, { origin: "null" })).status, 403);
    assert.equal((await post(ip, body, { "sec-fetch-site": "cross-site" })).status, 403);
    assert.equal((await post(ip, body, { origin: undefined })).status, 403);
    // Rejected by origin before the challenge was looked at, so it still works.
    const ok = await post(ip, body, { origin: undefined, referer: `${SITE}/posts/beta.html` });
    assert.equal(ok.status, 200, JSON.stringify(ok.body));
  });

  it("refuses unknown targets and malformed bodies", async () => {
    const ip = "203.0.113.40";
    assert.equal((await challengeFor(ip, "/posts/missing.html")).status, 400);
    assert.equal((await challengeFor(ip, "/etc/passwd")).status, 400);
    assert.equal((await challengeFor(ip, "note:2030-01-01")).status, 400);
    const issued = await challengeFor(ip, "/posts/alpha.html");
    const nonce = solve(issued.body.challenge, 8);
    assert.equal((await post(ip, { target: "/posts/missing.html", challenge: issued.body.challenge, nonce })).status, 400);
    assert.equal((await post(ip, { target: "note:2030-01-01", challenge: issued.body.challenge, nonce })).status, 400);
    assert.equal((await post(ip, { target: "/posts/alpha.html", challenge: "", nonce })).status, 400);
    assert.equal((await post(ip, "{not json")).status, 400);
    assert.equal((await post(ip, "null")).status, 400);
    const huge = await post(ip, { target: "/posts/alpha.html", challenge: "x".repeat(5000), nonce });
    assert.equal(huge.status, 413);
  });

  it("folds an IPv6 /64 into one visitor and reads X-Forwarded-For", async () => {
    const first = await observe("2001:db8:1:2::1", "/posts/beta.html");
    assert.equal(first.body.counted, true);
    const sibling = await observe("2001:db8:1:2:ffff::9", "/posts/beta.html");
    assert.equal(sibling.status, 200);
    assert.equal(sibling.body.counted, false);
    const viaProxy = await call("GET", "/api/reactions?target=%2Fposts%2Fbeta.html", { headers: { "x-forwarded-for": "2001:db8:1:2::7, 10.0.0.1" } });
    assert.deepEqual(viaProxy.body.observed, ["/posts/beta.html"]);
  });

  it("lists counts and the visitor's observed keys by prefix", async () => {
    const ip = "203.0.113.10";
    assert.equal((await observe(ip, "note:2026-04-11")).body.counted, true);
    assert.equal((await observe("198.51.100.2", "note:2026-04-11")).body.count, 2);

    const notes = await counts(ip, "prefix=note%3A");
    assert.equal(notes.status, 200);
    assert.deepEqual(notes.body, { counts: { "note:2026-04-11": 2 }, observed: ["note:2026-04-11"] });
    const other = await counts("198.51.100.99", "prefix=note%3A");
    assert.deepEqual(other.body, { counts: { "note:2026-04-11": 2 }, observed: [] });

    const posts = await counts(ip, "prefix=%2Fposts%2F");
    assert.ok(Object.keys(posts.body.counts).length > 0);
    assert.ok(Object.keys(posts.body.counts).every((key) => key.startsWith("/posts/")));
    assert.equal(posts.body.counts["/posts/alpha.html"], 1);
    assert.deepEqual(posts.body.observed, ["/posts/alpha.html"]);
  });

  it("picks up a new release's notes after the index changes", async () => {
    writeIndexes(dir, ["2026-05-01"]);
    const later = new Date(start + 3_600_000);
    fs.utimesSync(path.join(dir, "notes.json"), later, later);
    assert.equal((await counts("203.0.113.50", "target=note%3A2026-05-01")).status, 400, "cached until the next check");
    clock += 10_001;
    const fresh = await counts("203.0.113.50", "target=note%3A2026-05-01");
    assert.equal(fresh.status, 200);
    assert.deepEqual(fresh.body, { counts: { "note:2026-05-01": 0 }, observed: [] });
  });

  it("rate-limits writes and challenges per visitor with Retry-After", async () => {
    const ip = "2001:db8:99::1";
    for (let i = 0; i < 10; i++) assert.equal((await post(ip, {})).status, 400);
    const limited = await post(ip, {});
    assert.equal(limited.status, 429);
    const seconds = Number(limited.headers["retry-after"]);
    assert.ok(Number.isInteger(seconds) && seconds >= 1 && seconds <= 60, `Retry-After ${limited.headers["retry-after"]}`);
    // Same /64, other interface id: still the same visitor.
    assert.equal((await post("2001:db8:99::abcd", {})).status, 429);
    assert.equal((await post("2001:db8:98::1", {})).status, 400, "other visitors are unaffected");

    for (let i = 0; i < 20; i++) assert.equal((await challengeFor(ip, "/posts/alpha.html")).status, 200);
    assert.equal((await challengeFor(ip, "/posts/alpha.html")).status, 429);
  });

  it("opens the breaker on a burst, alerts once, keeps reads working and recovers", async () => {
    const target = "/posts/burst.html";
    const waiting = await challengeFor("192.0.2.5", target);
    assert.equal(waiting.status, 200);
    for (let i = 1; i <= 4; i++) {
      const reply = await observe(`192.0.2.${i}`, target);
      assert.equal(reply.status, 200);
      assert.equal(reply.body.counted, true);
    }
    assert.equal(notices.length, 1);
    assert.match(notices[0], /\/posts\/burst\.html/);

    const blocked = await post("192.0.2.5", { target, challenge: waiting.body.challenge, nonce: solve(waiting.body.challenge, 8) });
    assert.equal(blocked.status, 503);
    assert.ok(Number(blocked.headers["retry-after"]) > 0);
    assert.equal((await challengeFor("192.0.2.6", target)).status, 503);
    assert.deepEqual((await counts("192.0.2.1", "target=%2Fposts%2Fburst.html")).body, { counts: { [target]: 4 }, observed: [target] });
    assert.equal((await observe("192.0.2.1", "/posts/alpha.html")).status, 200, "other targets stay writable");
    assert.equal(notices.length, 1);

    clock += 900_001;
    const resumed = await observe("192.0.2.5", target);
    assert.equal(resumed.status, 200);
    assert.deepEqual(resumed.body, { target, count: 5, observed: true, counted: true });
    assert.equal(notices.length, 1);
  });
});

describe("reactions API without readable indexes", () => {
  let dir = "";
  let api: ReturnType<typeof createReactionsServer>;
  let port = 0;

  before(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "pnc-reactions-noindex-"));
    api = createReactionsServer({
      dbPath: path.join(dir, "reactions.db"),
      blogIndexFile: path.join(dir, "missing-index.json"),
      notesIndexFile: path.join(dir, "missing-notes.json"),
      challengeSecret: CHALLENGE_SECRET,
      visitorSalt: VISITOR_SALT,
      powDifficulty: 8,
      notify: () => {},
    });
    ({ port } = await api.listen(0, "127.0.0.1"));
  });

  after(async () => {
    await api?.close();
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it("reads empty counts and fails writes closed with 503", async () => {
    const headers = { origin: SITE, referer: `${SITE}/`, "sec-fetch-site": "same-origin" };
    const one = await request(port, "GET", "/reactions?target=%2Fposts%2Falpha.html");
    assert.deepEqual([one.status, one.body], [200, { counts: {}, observed: [] }]);
    // A prefix listing without its index would read as "all zero": 503 instead.
    assert.equal((await request(port, "GET", "/reactions?prefix=note%3A")).status, 503);
    assert.equal((await request(port, "GET", "/reactions?prefix=%2Fposts%2F")).status, 503);
    assert.equal((await request(port, "GET", "/reactions/challenge?target=%2Fposts%2Falpha.html", { headers })).status, 503);
    const challenge = signChallenge({ target: "/posts/alpha.html", visitor: visitorKey("127.0.0.1", VISITOR_SALT), difficulty: 8,
      expiresAt: Date.now() + 60_000, random: crypto.randomBytes(16).toString("base64url") }, CHALLENGE_SECRET);
    const write = await request(port, "POST", "/reactions", { headers, body: { target: "/posts/alpha.html", challenge, nonce: solve(challenge, 8) } });
    assert.equal(write.status, 503);
  });
});

describe("reactions API with only the post index readable", () => {
  let dir = "";
  let api: ReturnType<typeof createReactionsServer>;
  let port = 0;

  before(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "pnc-reactions-postsonly-"));
    writeIndexes(dir);
    fs.writeFileSync(path.join(dir, "notes.json"), "{ not json");
    api = createReactionsServer({
      dbPath: path.join(dir, "reactions.db"),
      blogIndexFile: path.join(dir, "index.json"),
      notesIndexFile: path.join(dir, "notes.json"),
      challengeSecret: CHALLENGE_SECRET,
      visitorSalt: VISITOR_SALT,
      powDifficulty: 8,
      notify: () => {},
    });
    ({ port } = await api.listen(0, "127.0.0.1"));
  });

  after(async () => {
    await api?.close();
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it("serves posts and refuses the note listing instead of showing zeros", async () => {
    assert.equal((await request(port, "GET", "/reactions?prefix=note%3A")).status, 503);
    const posts = await request(port, "GET", "/reactions?prefix=%2Fposts%2F");
    assert.deepEqual([posts.status, posts.body], [200, { counts: {}, observed: [] }]);
    const one = await request(port, "GET", "/reactions?target=%2Fposts%2Falpha.html");
    assert.deepEqual([one.status, one.body], [200, { counts: { "/posts/alpha.html": 0 }, observed: [] }]);
  });
});
