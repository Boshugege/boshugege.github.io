import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

const env = process.env;
async function envOrFile(name, fallback = "") {
  if (env[name]) return String(env[name]).trim();
  if (env[`${name}_FILE`]) return (await fs.readFile(env[`${name}_FILE`], "utf8")).trim();
  return fallback;
}

const host = env.HOST || "127.0.0.1";
const port = Number.parseInt(env.PORT || "46215", 10);
const siteOrigin = (env.SITE_ORIGIN || "https://parityncsvt.top").replace(/\/$/, "");
const directusUrl = (env.DIRECTUS_URL || "http://127.0.0.1:46214").replace(/\/$/, "");
const directusStaticToken = await envOrFile("DIRECTUS_TOKEN");
const directusEmail = env.DIRECTUS_EMAIL || "";
const directusPassword = env.DIRECTUS_PASSWORD || "";
const turnstileSiteKey = await envOrFile("TURNSTILE_SITE_KEY");
const turnstileSecretKey = await envOrFile("TURNSTILE_SECRET_KEY");
const rateLimitWindowMs = Number.parseInt(env.COMMENT_RATE_LIMIT_WINDOW_MS || "600000", 10);
const rateLimitMax = Number.parseInt(env.COMMENT_RATE_LIMIT_MAX || "3", 10);
const hashSecret = await envOrFile("COMMENT_HASH_SECRET", "dev-comment-hash-secret");

let directusToken = "";
let directusTokenExpiresAt = 0;
const rateLimitBuckets = new Map();

function sendJson(res, status, body, headers = {}) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...corsHeaders(),
    ...headers,
  });
  res.end(JSON.stringify(body));
}

function corsHeaders() {
  return {
    "access-control-allow-origin": siteOrigin,
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
    "vary": "Origin",
  };
}

function getClientIp(req) {
  return req.headers["cf-connecting-ip"] || req.headers["x-forwarded-for"]?.split(",")[0]?.trim() || req.socket.remoteAddress || "";
}

function hashValue(value) {
  return crypto.createHmac("sha256", hashSecret).update(String(value)).digest("hex");
}

export function parseUserAgent(userAgent = "") {
  const ua = String(userAgent);
  const osPatterns = [
    ["Windows", /Windows NT ([\d.]+)/],
    ["macOS", /Mac OS X ([\d_]+)/],
    ["iOS", /(?:iPhone|iPad).*OS ([\d_]+)/],
    ["Android", /Android ([\d.]+)/],
    ["Linux", /Linux/],
  ];
  const browserPatterns = [
    ["Edge", /Edg\/([\d.]+)/],
    ["Chrome", /Chrome\/([\d.]+)/],
    ["Firefox", /Firefox\/([\d.]+)/],
    ["Safari", /Version\/([\d.]+).*Safari/],
  ];

  const osMatch = osPatterns.find(([, pattern]) => pattern.test(ua));
  const browserMatch = browserPatterns.find(([, pattern]) => pattern.test(ua));
  const osVersion = osMatch?.[1].exec(ua)?.[1]?.replaceAll("_", ".") || "";
  const browserVersion = browserMatch?.[1].exec(ua)?.[1]?.split(".").slice(0, 2).join(".") || "";

  return {
    os_name: osMatch?.[0] || "Unknown",
    os_version: osVersion,
    browser_name: browserMatch?.[0] || "Unknown",
    browser_version: browserVersion,
  };
}

export function validateCommentInput(input) {
  const slug = String(input?.slug || "").trim();
  const authorName = String(input?.authorName || "").trim();
  const content = String(input?.content || "").trim();
  const turnstileToken = String(input?.turnstileToken || "").trim();

  if (!/^\/posts\/[a-zA-Z0-9._-]+\.html$/.test(slug)) throw new Error("文章地址无效。");
  if (authorName.length < 1 || authorName.length > 40) throw new Error("用户名长度需要在 1 到 40 个字符之间。");
  if (content.length < 1 || content.length > 1200) throw new Error("评论长度需要在 1 到 1200 个字符之间。");
  if (!turnstileToken) throw new Error("请先完成人机验证。");

  return { slug, authorName, content, turnstileToken };
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16 * 1024) throw new Error("请求体过大。");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function directusRequest(path, options = {}) {
  const token = await getDirectusToken();
  const response = await fetch(`${directusUrl}${path}`, {
    ...options,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body?.errors?.[0]?.message || body?.error || "Directus request failed");
  }
  return body;
}

async function getDirectusToken() {
  if (directusStaticToken) return directusStaticToken;
  if (directusToken && Date.now() < directusTokenExpiresAt) return directusToken;
  if (!directusEmail || !directusPassword) throw new Error("Directus token is not configured");

  const response = await fetch(`${directusUrl}/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: directusEmail, password: directusPassword }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.errors?.[0]?.message || "Directus login failed");

  directusToken = body.data?.access_token || "";
  const expires = Number(body.data?.expires || 900000);
  directusTokenExpiresAt = Date.now() + Math.max(60000, expires - 60000);
  return directusToken;
}

async function verifyTurnstile(token, ip) {
  if (!turnstileSecretKey || turnstileSecretKey === "change-me") throw new Error("Turnstile secret is not configured");
  const form = new FormData();
  form.set("secret", turnstileSecretKey);
  form.set("response", token);
  if (ip) form.set("remoteip", ip);
  const response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body: form,
  });
  const body = await response.json().catch(() => ({}));
  if (!body.success) throw new Error("人机验证失败。");
}

function checkRateLimit(ip, slug) {
  const now = Date.now();
  const key = `${hashValue(ip)}:${slug}`;
  const bucket = rateLimitBuckets.get(key)?.filter((time) => now - time < rateLimitWindowMs) || [];
  if (bucket.length >= rateLimitMax) throw new Error("评论发送太频繁，请稍后再试。");
  bucket.push(now);
  rateLimitBuckets.set(key, bucket);
}

async function listComments(req, res, url) {
  const slug = url.searchParams.get("slug") || "";
  if (!/^\/posts\/[a-zA-Z0-9._-]+\.html$/.test(slug)) {
    sendJson(res, 400, { error: "文章地址无效。" });
    return;
  }
  const filter = encodeURIComponent(JSON.stringify({
    post_slug: { _eq: slug },
    status: { _eq: "visible" },
  }));
  const fields = "id,author_name,content,browser_name,browser_version,os_name,os_version,country_code,created_at";
  const body = await directusRequest(`/items/comments?filter=${filter}&fields=${fields}&sort=created_at&limit=100`);
  sendJson(res, 200, { comments: body.data || [] });
}

async function createComment(req, res) {
  const bodyText = await readBody(req);
  const input = validateCommentInput(JSON.parse(bodyText || "{}"));
  const ip = String(getClientIp(req));
  checkRateLimit(ip, input.slug);
  await verifyTurnstile(input.turnstileToken, ip);

  const userAgent = String(req.headers["user-agent"] || "");
  const runtime = parseUserAgent(userAgent);
  const country = String(req.headers["cf-ipcountry"] || "").slice(0, 2).toUpperCase();
  const referer = String(req.headers.referer || "").slice(0, 500);

  const item = {
    post_slug: input.slug,
    author_name: input.authorName,
    content: input.content,
    status: "visible",
    ...runtime,
    country_code: country || null,
    referer: referer || null,
    ip_hash: hashValue(ip),
    user_agent_hash: hashValue(userAgent),
    created_at: new Date().toISOString(),
  };

  await directusRequest("/items/comments", {
    method: "POST",
    body: JSON.stringify(item),
  });
  sendJson(res, 201, { ok: true });
}

async function handle(req, res) {
  const url = new URL(req.url || "/", `http://${req.headers.host || `${host}:${port}`}`);
  if (req.method === "OPTIONS") {
    res.writeHead(204, corsHeaders());
    res.end();
    return;
  }
  try {
    if (req.method === "GET" && url.pathname === "/healthz") {
      sendJson(res, 200, { ok: true });
    } else if (req.method === "GET" && url.pathname === "/config") {
      sendJson(res, 200, { turnstileSiteKey: turnstileSiteKey === "change-me" ? "" : turnstileSiteKey });
    } else if (req.method === "GET" && url.pathname === "/comments") {
      await listComments(req, res, url);
    } else if (req.method === "POST" && url.pathname === "/comments") {
      await createComment(req, res);
    } else {
      sendJson(res, 404, { error: "Not found" });
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : "服务器错误";
    const status = /无效|长度|验证|频繁|configured/.test(message) ? 400 : 500;
    sendJson(res, status, { error: message });
  }
}

export function createServer() {
  return http.createServer(handle);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  createServer().listen(port, host, () => {
    console.log(`comment-api listening on http://${host}:${port}`);
  });
}
