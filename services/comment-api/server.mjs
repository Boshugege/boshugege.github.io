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
const blogIndexFile = env.BLOG_INDEX_FILE || "/home/lyy/services/pnc-blog/current/index.json";
const viewTimeZone = env.VIEW_TIME_ZONE || "Asia/Shanghai";
const postCatalogTtlMs = Number.parseInt(env.POST_CATALOG_TTL_MS || "60000", 10);

let directusToken = "";
let directusTokenExpiresAt = 0;
const rateLimitBuckets = new Map();
let postCatalogCache = { expiresAt: 0, posts: new Map() };

const postSlugPattern = /^\/posts\/[a-zA-Z0-9._-]+\.html$/;

class DirectusRequestError extends Error {
  constructor(message, status, code = "") {
    super(message);
    this.name = "DirectusRequestError";
    this.status = status;
    this.code = code;
  }
}

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

export function validatePostSlug(value) {
  const slug = String(value || "").trim();
  if (!postSlugPattern.test(slug)) throw new Error("文章地址无效。");
  return slug;
}

export function validateViewInput(input) {
  return { slug: validatePostSlug(input?.slug) };
}

export function getViewDate(date = new Date(), timeZone = viewTimeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export function parseAggregateCount(row) {
  const raw = row?.count;
  if (typeof raw === "number" || typeof raw === "string") {
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  }
  if (raw && typeof raw === "object") {
    const value = Number(raw["*"] ?? Object.values(raw)[0]);
    return Number.isFinite(value) ? value : 0;
  }
  return 0;
}

export function summarizeViewGroups(rows, postCatalog = new Map()) {
  const groups = (Array.isArray(rows) ? rows : []).map((row) => ({
    slug: String(row?.post_slug || row?.group?.post_slug || ""),
    views: parseAggregateCount(row),
  })).filter((row) => postSlugPattern.test(row.slug) && row.views > 0);
  const visibleGroups = postCatalog.size > 0
    ? groups.filter((row) => postCatalog.has(row.slug))
    : groups;
  const totalViews = visibleGroups.reduce((sum, row) => sum + row.views, 0);
  const mostViewedRow = [...visibleGroups].sort((a, b) => b.views - a.views || a.slug.localeCompare(b.slug))[0];
  const fallbackTitle = mostViewedRow?.slug.split("/").pop()?.replace(/\.html$/, "") || "";

  return {
    totalViews,
    mostViewed: mostViewedRow ? {
      slug: mostViewedRow.slug,
      title: postCatalog.get(mostViewedRow.slug) || fallbackTitle,
      views: mostViewedRow.views,
    } : null,
  };
}

export function buildViewCounts(rows, postCatalog = new Map()) {
  const counts = {};
  for (const row of Array.isArray(rows) ? rows : []) {
    const slug = String(row?.post_slug || row?.group?.post_slug || "");
    if (!postSlugPattern.test(slug) || (postCatalog.size > 0 && !postCatalog.has(slug))) continue;
    counts[slug] = parseAggregateCount(row);
  }
  return counts;
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
  const slug = validatePostSlug(input?.slug);
  const authorName = String(input?.authorName || "").trim();
  const content = String(input?.content || "").trim();
  const turnstileToken = String(input?.turnstileToken || "").trim();

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
    throw new DirectusRequestError(
      body?.errors?.[0]?.message || body?.error || "Directus request failed",
      response.status,
      body?.errors?.[0]?.extensions?.code || "",
    );
  }
  return body;
}

async function loadPostCatalog() {
  const now = Date.now();
  if (now < postCatalogCache.expiresAt) return postCatalogCache.posts;

  const posts = new Map();
  try {
    const rows = JSON.parse(await fs.readFile(blogIndexFile, "utf8"));
    for (const row of Array.isArray(rows) ? rows : []) {
      const slug = `/${String(row?.url || "").replace(/^\/+/, "")}`;
      if (postSlugPattern.test(slug)) posts.set(slug, String(row?.title || slug));
    }
  } catch (error) {
    if (error?.code !== "ENOENT") console.warn(`Unable to read blog index: ${error.message}`);
  }

  postCatalogCache = { expiresAt: now + Math.max(1000, postCatalogTtlMs), posts };
  return posts;
}

async function assertKnownPostSlug(slug) {
  const posts = await loadPostCatalog();
  if (posts.size > 0 && !posts.has(slug)) throw new Error("文章地址无效。");
}

function aggregateQuery(filter, groupBy) {
  const params = new URLSearchParams();
  params.set("aggregate[count]", "*");
  if (filter) params.set("filter", JSON.stringify(filter));
  if (groupBy) params.append("groupBy[]", groupBy);
  return params.toString();
}

async function countItems(collection, filter) {
  const body = await directusRequest(`/items/${collection}?${aggregateQuery(filter)}`);
  return parseAggregateCount(body.data?.[0]);
}

async function getPostViewCount(slug) {
  return countItems("post_views", { post_slug: { _eq: slug } });
}

function isUniqueConstraintError(error) {
  return error instanceof DirectusRequestError
    && (error.status === 409 || /unique|duplicate|constraint/i.test(`${error.code} ${error.message}`));
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
  if (!postSlugPattern.test(slug)) {
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

async function getViews(req, res, url) {
  const slugValue = url.searchParams.get("slug");
  if (slugValue) {
    const slug = validatePostSlug(slugValue);
    await assertKnownPostSlug(slug);
    sendJson(res, 200, { views: await getPostViewCount(slug) });
    return;
  }

  const [viewGroupsBody, postCatalog] = await Promise.all([
    directusRequest(`/items/post_views?${aggregateQuery(undefined, "post_slug")}`),
    loadPostCatalog(),
  ]);
  sendJson(res, 200, { views: buildViewCounts(viewGroupsBody.data, postCatalog) });
}

async function recordView(req, res) {
  const bodyText = await readBody(req);
  const { slug } = validateViewInput(JSON.parse(bodyText || "{}"));
  await assertKnownPostSlug(slug);

  const ip = String(getClientIp(req));
  const userAgent = String(req.headers["user-agent"] || "");
  const visitorHash = hashValue(`${ip}\n${userAgent}`);
  const viewedOn = getViewDate();
  const viewKey = hashValue(`${slug}\n${visitorHash}\n${viewedOn}`);
  const filter = encodeURIComponent(JSON.stringify({ view_key: { _eq: viewKey } }));
  const existing = await directusRequest(`/items/post_views?filter=${filter}&fields=id&limit=1`);
  let counted = false;

  if (!existing.data?.length) {
    try {
      await directusRequest("/items/post_views", {
        method: "POST",
        body: JSON.stringify({
          post_slug: slug,
          view_key: viewKey,
          visitor_hash: visitorHash,
          viewed_on: viewedOn,
          created_at: new Date().toISOString(),
        }),
      });
      counted = true;
    } catch (error) {
      if (!isUniqueConstraintError(error)) throw error;
    }
  }

  sendJson(res, 200, { views: await getPostViewCount(slug), counted });
}

async function getSiteStats(res) {
  const [viewGroupsBody, commentCount, postCatalog] = await Promise.all([
    directusRequest(`/items/post_views?${aggregateQuery(undefined, "post_slug")}`),
    countItems("comments", { status: { _eq: "visible" } }),
    loadPostCatalog(),
  ]);
  const viewStats = summarizeViewGroups(viewGroupsBody.data, postCatalog);
  sendJson(res, 200, { ...viewStats, commentCount });
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
    } else if (req.method === "GET" && url.pathname === "/views") {
      await getViews(req, res, url);
    } else if (req.method === "POST" && url.pathname === "/views") {
      await recordView(req, res);
    } else if (req.method === "GET" && url.pathname === "/stats") {
      await getSiteStats(res);
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
