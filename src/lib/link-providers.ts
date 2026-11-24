// Which site a link in a note points at, worked out from the URL alone (no
// network). The Telegram bot has a Python twin of classifyLink
// (ops/telegram-bot/pnc_bot/linkpreview.py: classify); keep the two in step.

export type LinkKind = "link" | "youtube" | "bilibili" | "x" | "github";

export interface LinkTarget {
  kind: LinkKind;
  /** youtube: id · bilibili: bvid | aid | short · x: user, id · github: owner, repo */
  ids: Record<string, string>;
}

const PLAIN: LinkTarget = { kind: "link", ids: {} };

// github.com/<name>/<x> pages that are not repositories.
const GITHUB_RESERVED = new Set([
  "features", "topics", "orgs", "settings", "marketplace", "sponsors", "about", "pricing", "login", "join",
  "explore", "notifications", "search", "collections", "trending", "apps", "enterprise", "security", "site",
  "readme", "new", "organizations", "users", "issues", "pulls",
]);

function hostOf(url: URL, prefixes: string[]) {
  let host = url.hostname.toLowerCase();
  for (const prefix of prefixes) {
    if (host.startsWith(`${prefix}.`)) {
      host = host.slice(prefix.length + 1);
      break;
    }
  }
  return host;
}

export function classifyLink(href: string): LinkTarget {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return PLAIN;
  }
  if (url.protocol !== "https:") return PLAIN;
  const segments = url.pathname.split("/").filter(Boolean);

  const youtubeHost = hostOf(url, ["www", "m", "music"]);
  if (youtubeHost === "youtube.com" || youtubeHost === "youtu.be") {
    let id: string | null | undefined;
    if (youtubeHost === "youtu.be") id = segments.length === 1 ? segments[0] : undefined;
    else if (segments.length === 1 && segments[0] === "watch") id = url.searchParams.get("v");
    else if (segments.length === 2 && ["shorts", "live", "embed"].includes(segments[0])) id = segments[1];
    return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? { kind: "youtube", ids: { id } } : PLAIN;
  }

  const bilibiliHost = hostOf(url, ["www", "m"]);
  if (bilibiliHost === "bilibili.com") {
    if (segments.length === 2 && segments[0] === "video") {
      if (/^BV[0-9A-Za-z]{10}$/.test(segments[1])) return { kind: "bilibili", ids: { bvid: segments[1] } };
      const aid = /^av(\d{1,12})$/.exec(segments[1]);
      if (aid) return { kind: "bilibili", ids: { aid: aid[1] } };
    }
    return PLAIN;
  }
  if (url.hostname.toLowerCase() === "b23.tv") {
    return segments.length === 1 && /^[0-9A-Za-z]{1,16}$/.test(segments[0])
      ? { kind: "bilibili", ids: { short: segments[0] } }
      : PLAIN;
  }

  const xHost = hostOf(url, ["www", "mobile", "m"]);
  if (xHost === "x.com" || xHost === "twitter.com") {
    if (segments.length >= 3 && segments[1] === "status" && /^[A-Za-z0-9_]{1,15}$/.test(segments[0]) && /^\d{1,20}$/.test(segments[2])) {
      return { kind: "x", ids: { user: segments[0], id: segments[2] } };
    }
    return PLAIN;
  }

  if (hostOf(url, ["www"]) === "github.com") {
    if (segments.length === 2 && !GITHUB_RESERVED.has(segments[0].toLowerCase())) {
      const repo = segments[1].replace(/\.git$/, "");
      if (/^[A-Za-z0-9-]{1,39}$/.test(segments[0]) && /^[A-Za-z0-9._-]{1,100}$/.test(repo) && repo !== "." && repo !== "..") {
        return { kind: "github", ids: { owner: segments[0], repo } };
      }
    }
    return PLAIN;
  }
  return PLAIN;
}

/** Seconds from a start-time parameter: "90", "90s", "1m30s", "1h2m3s". */
export function parseStartTime(value: string | null | undefined): number {
  if (!value) return 0;
  if (/^\d{1,6}$/.test(value)) return Number(value);
  const match = /^(?:(\d{1,3})h)?(?:(\d{1,4})m)?(?:(\d{1,6})s)?$/.exec(value);
  if (!match || !match[0]) return 0;
  return Number(match[1] || 0) * 3600 + Number(match[2] || 0) * 60 + Number(match[3] || 0);
}

/**
 * The player a click loads for a video or post: the provider's own embed
 * page. Undefined when there is none (plain links, GitHub, unresolved b23.tv
 * short links). `href` supplies the start time and part number.
 */
export function embedSrc(target: LinkTarget, href: string): string | undefined {
  let params = new URLSearchParams();
  try {
    params = new URL(href).searchParams;
  } catch {
    // keep the empty parameters
  }
  if (target.kind === "youtube") {
    const start = parseStartTime(params.get("t") || params.get("start"));
    // The no-cookie host sets nothing until the reader presses play.
    return `https://www.youtube-nocookie.com/embed/${target.ids.id}?autoplay=1&rel=0${start ? `&start=${start}` : ""}`;
  }
  if (target.kind === "bilibili") {
    const video = target.ids.bvid ? `bvid=${target.ids.bvid}` : target.ids.aid ? `aid=${target.ids.aid}` : "";
    if (!video) return undefined;
    const part = /^\d{1,4}$/.test(params.get("p") || "") ? Number(params.get("p")) : 1;
    const start = parseStartTime(params.get("t"));
    return `https://player.bilibili.com/player.html?${video}&p=${part}&autoplay=1${start ? `&t=${start}` : ""}`;
  }
  if (target.kind === "x") {
    return `https://platform.twitter.com/embed/Tweet.html?id=${target.ids.id}&dnt=true`;
  }
  return undefined;
}
