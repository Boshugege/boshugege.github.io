// pnc-reactions: pure helpers (no I/O besides node:crypto) shared by
// server.mjs and the tests. Nothing here reads the environment or the clock:
// every time-dependent function takes `now` from the caller.
import crypto from "node:crypto";

// Reaction keys. Posts use their site path, notes their anchor id
// (YYYY-MM-DD, then -2, -3... for later notes on the same day).
export const POST_TARGET = /^\/posts\/[A-Za-z0-9._-]+\.html$/;
export const NOTE_TARGET = /^note:\d{4}-\d{2}-\d{2}(?:-\d+)?$/;
export const TARGET_PREFIXES = Object.freeze(["note:", "/posts/"]);
const MAX_TARGET_LENGTH = 200;

export function isTargetSyntax(key) {
  return typeof key === "string" && key.length <= MAX_TARGET_LENGTH && (POST_TARGET.test(key) || NOTE_TARGET.test(key));
}

// ---------------------------------------------------------------------------
// Visitors

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const HEXTET = /^[0-9a-f]{1,4}$/;

// Eight 16-bit groups of an IPv6 address, or null. Accepts "::" compression
// and a trailing dotted IPv4 (::ffff:1.2.3.4, 64:ff9b::1.2.3.4).
function ipv6Groups(text) {
  let address = text;
  const head = text.slice(0, text.lastIndexOf(":") + 1);
  const dotted = text.slice(head.length);
  if (dotted.includes(".")) {
    if (!IPV4.test(dotted)) return null;
    const [a, b, c, d] = dotted.split(".").map(Number);
    address = `${head}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = address.split("::");
  if (halves.length > 2) return null;
  const parse = (part) => (part === "" ? [] : part.split(":"));
  const left = parse(halves[0]);
  const right = halves.length === 2 ? parse(halves[1]) : [];
  if (![...left, ...right].every((group) => HEXTET.test(group))) return null;
  const missing = 8 - left.length - right.length;
  if (halves.length === 2 ? missing < 1 : missing !== 0) return null;
  return [...left, ...Array(missing).fill("0"), ...right].map((group) => Number.parseInt(group, 16));
}

// One key per IPv4 address or IPv6 /64: a home or phone connection usually
// owns a whole /64, so rotating the interface id must not mint new visitors.
export function normalizeIp(ip) {
  let text = String(ip ?? "").trim().toLowerCase();
  if (text.startsWith("[") && text.endsWith("]")) text = text.slice(1, -1);
  const zone = text.indexOf("%"); // zone id, as in fe80::1%eth0
  if (zone !== -1) text = text.slice(0, zone);
  if (IPV4.test(text)) return text;
  if (!text.includes(":") || text.length > 45) return "unknown";
  const groups = ipv6Groups(text);
  if (!groups) return "unknown";
  // IPv4-mapped (::ffff:a.b.c.d): the visitor really came over IPv4.
  if (groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff) {
    return [groups[6] >> 8, groups[6] & 0xff, groups[7] >> 8, groups[7] & 0xff].join(".");
  }
  return `${groups.slice(0, 4).map((group) => group.toString(16)).join(":")}::/64`;
}

// The raw address is never stored; only this salted HMAC of its normal form.
export function visitorKey(ip, salt) {
  return crypto.createHmac("sha256", String(salt)).update(normalizeIp(ip)).digest("hex");
}

// ---------------------------------------------------------------------------
// Proof of work: SHA-256(challenge + ":" + nonce) must start with d zero bits.
// src/lib/pow.ts solves the same puzzle in the browser with Web Crypto.

const NONCE = /^(0|[1-9]\d{0,15})$/;
const MAX_DIFFICULTY = 64;

export function isNonceSyntax(nonce) {
  return typeof nonce === "string" && NONCE.test(nonce);
}

export function leadingZeroBits(bytes) {
  let bits = 0;
  for (const byte of bytes) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    return bits + Math.clz32(byte) - 24;
  }
  return bits;
}

export function verifyPow(challenge, nonce, difficulty) {
  if (typeof challenge !== "string" || !isNonceSyntax(nonce)) return false;
  if (!Number.isInteger(difficulty) || difficulty < 0 || difficulty > MAX_DIFFICULTY) return false;
  const digest = crypto.createHash("sha256").update(`${challenge}:${nonce}`, "utf8").digest();
  return leadingZeroBits(digest) >= difficulty;
}

// ---------------------------------------------------------------------------
// Challenges: "v1." + base64url(payload JSON) + "." + base64url(HMAC). They
// are stateless; server.mjs keeps only the used random ids until expiry.

const VERSION = "v1";
const MAX_CHALLENGE_LENGTH = 512;
const BASE64URL = /^[A-Za-z0-9_-]+$/;
const VISITOR_PREFIX = /^[0-9a-f]{16}$/;
const RANDOM = /^[A-Za-z0-9_-]{16,64}$/;

const signature = (signed, secret) => crypto.createHmac("sha256", String(secret)).update(signed).digest("base64url");

// `visitor` may be the full visitor key; only its first 16 hex chars are kept.
export function signChallenge({ target, visitor, difficulty, expiresAt, random }, secret) {
  const payload = { t: target, v: String(visitor).slice(0, 16), d: difficulty, e: expiresAt, r: random };
  const signed = `${VERSION}.${Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")}`;
  return `${signed}.${signature(signed, secret)}`;
}

// Checks format and signature only (not expiry, target or reuse). Returns the
// payload in the shape signChallenge takes, or null.
export function verifyChallenge(challenge, secret) {
  if (typeof challenge !== "string" || challenge.length > MAX_CHALLENGE_LENGTH) return null;
  const parts = challenge.split(".");
  if (parts.length !== 3 || parts[0] !== VERSION || !BASE64URL.test(parts[1]) || !BASE64URL.test(parts[2])) return null;
  // Compare the canonical text, so no second spelling of a signature passes.
  const expected = Buffer.from(signature(`${parts[0]}.${parts[1]}`, secret));
  const given = Buffer.from(parts[2]);
  if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) return null;
  let payload;
  try {
    payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
  } catch {
    return null;
  }
  const { t, v, d, e, r } = payload ?? {};
  if (typeof t !== "string" || typeof v !== "string" || !VISITOR_PREFIX.test(v)) return null;
  if (!Number.isInteger(d) || d < 0 || d > MAX_DIFFICULTY || !Number.isSafeInteger(e)) return null;
  if (typeof r !== "string" || !RANDOM.test(r)) return null;
  return { target: t, visitor: v, difficulty: d, expiresAt: e, random: r };
}

// ---------------------------------------------------------------------------
// Abuse limits. Both classes keep bounded, self-pruning maps.

// Exact sliding window (a timestamp log per key); limits here are small, so
// the log stays short. Rejected hits are not recorded, so a client that keeps
// hammering is not locked out longer than the window.
export class SlidingWindowLimiter {
  constructor({ limit, windowMs, maxKeys = 10_000 }) {
    if (!(limit > 0) || !(windowMs > 0)) throw new Error("SlidingWindowLimiter needs a positive limit and windowMs");
    this.limit = limit;
    this.windowMs = windowMs;
    this.maxKeys = maxKeys;
    this.hits = new Map();
  }

  hit(key, now) {
    const since = now - this.windowMs;
    const times = (this.hits.get(key) || []).filter((time) => time > since);
    // Re-insert so the map iterates least recently used first.
    this.hits.delete(key);
    if (times.length >= this.limit) {
      this.hits.set(key, times);
      return { ok: false, retryAfterMs: Math.max(1, times[0] + this.windowMs - now) };
    }
    times.push(now);
    this.hits.set(key, times);
    if (this.hits.size > this.maxKeys) this.prune(now);
    return { ok: true, retryAfterMs: 0 };
  }

  // Drop idle keys, then the least recently used ones down to 90% of the cap
  // (the headroom keeps this scan rare under a flood of new keys).
  prune(now) {
    const since = now - this.windowMs;
    for (const [key, times] of this.hits) if (!times.some((time) => time > since)) this.hits.delete(key);
    for (const key of this.hits.keys()) {
      if (this.hits.size <= Math.floor(this.maxKeys * 0.9)) break;
      this.hits.delete(key);
    }
  }

  get size() {
    return this.hits.size;
  }
}

// Opens a target when more than targetLimit new reactions land on it within
// windowMs, and every target when more than globalLimit land overall. An open
// breaker stays open for cooldownMs; onOpen runs once per opening.
export class CircuitBreaker {
  constructor({ targetLimit, globalLimit, windowMs, cooldownMs, onOpen = () => {}, maxTargets = 10_000 }) {
    this.targetLimit = targetLimit;
    this.globalLimit = globalLimit;
    this.windowMs = windowMs;
    this.cooldownMs = cooldownMs;
    this.onOpen = onOpen;
    this.maxTargets = maxTargets;
    this.targets = new Map(); // target -> timestamps within the window
    this.global = [];
    this.openTargets = new Map(); // target -> open until (epoch ms)
    this.globalOpenUntil = 0;
  }

  record(target, now) {
    const since = now - this.windowMs;
    const times = (this.targets.get(target) || []).filter((time) => time > since);
    times.push(now);
    this.targets.delete(target);
    this.targets.set(target, times);
    this.global = this.global.filter((time) => time > since);
    this.global.push(now);
    if (this.targets.size > this.maxTargets) this.prune(now);

    let opened = false;
    if (times.length > this.targetLimit && !this.targetOpen(target, now)) {
      const until = now + this.cooldownMs;
      this.openTargets.set(target, until);
      // Count afresh after the cooldown instead of reopening on old hits.
      this.targets.delete(target);
      this.notify({ scope: "target", target, count: times.length, windowMs: this.windowMs, until });
      opened = true;
    }
    if (this.global.length > this.globalLimit && !this.globalOpen(now)) {
      this.globalOpenUntil = now + this.cooldownMs;
      const count = this.global.length;
      this.global = [];
      this.notify({ scope: "global", target, count, windowMs: this.windowMs, until: this.globalOpenUntil });
      opened = true;
    }
    return opened;
  }

  isOpen(target, now) {
    return this.globalOpen(now) || this.targetOpen(target, now);
  }

  // Epoch ms when writes to `target` may resume (0 when not open).
  openUntil(target, now) {
    if (!this.isOpen(target, now)) return 0;
    return Math.max(this.globalOpen(now) ? this.globalOpenUntil : 0, this.openTargets.get(target) || 0);
  }

  globalOpen(now) {
    return this.globalOpenUntil > now;
  }

  targetOpen(target, now) {
    const until = this.openTargets.get(target);
    if (until === undefined) return false;
    if (until > now) return true;
    this.openTargets.delete(target);
    return false;
  }

  prune(now) {
    const since = now - this.windowMs;
    for (const [target, times] of this.targets) if (!times.some((time) => time > since)) this.targets.delete(target);
    for (const target of this.targets.keys()) {
      if (this.targets.size <= Math.floor(this.maxTargets * 0.9)) break;
      this.targets.delete(target);
    }
    for (const [target, until] of this.openTargets) if (until <= now) this.openTargets.delete(target);
  }

  // A failing alert must never break the request that tripped the breaker.
  notify(event) {
    try {
      const result = this.onOpen(event);
      if (result && typeof result.catch === "function") result.catch((error) => console.error("breaker onOpen failed:", error));
    } catch (error) {
      console.error("breaker onOpen failed:", error);
    }
  }
}
