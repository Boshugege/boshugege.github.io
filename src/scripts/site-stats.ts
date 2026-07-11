interface PostViewResponse {
  views?: number;
}

interface SiteStatsResponse {
  totalViews?: number;
  commentCount?: number;
  mostViewed?: {
    slug?: string;
    title?: string;
    views?: number;
  } | null;
}

const numberFormatter = new Intl.NumberFormat("zh-CN");

function formatCount(value: unknown) {
  const count = Number(value);
  return Number.isFinite(count) ? numberFormatter.format(Math.max(0, Math.trunc(count))) : "—";
}

function dateParts(date: Date, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, Number(part.value)]));
  return { year: values.year, month: values.month, day: values.day };
}

export function formatSiteAge(startedAt: string, now = new Date(), timeZone = "Asia/Shanghai") {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(startedAt);
  if (!match) return "—";

  const start = { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
  const current = dateParts(now, timeZone);
  let totalMonths = (current.year - start.year) * 12 + current.month - start.month;
  if (current.day < start.day) totalMonths -= 1;
  if (totalMonths < 0) return "尚未建立";

  const years = Math.floor(totalMonths / 12);
  const months = totalMonths % 12;
  if (years > 0) return `${years} 年${months > 0 ? ` ${months} 个月` : ""}`;
  if (months > 0) return `${months} 个月`;
  return `${Math.max(0, current.day - start.day)} 天`;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json() as Promise<T>;
}

function updateStat(root: HTMLElement, key: string, value: string, detail?: string) {
  const item = root.querySelector<HTMLElement>(`[data-stat-key="${key}"]`);
  const valueNode = item?.querySelector<HTMLElement>("[data-stat-value]");
  const detailNode = item?.querySelector<HTMLElement>("[data-stat-detail]");
  if (valueNode) valueNode.textContent = value;
  if (detail !== undefined && detailNode) detailNode.textContent = detail;
}

async function setupPostView(root: HTMLElement) {
  if (root.dataset.postViewReady === "true") return;
  root.dataset.postViewReady = "true";
  const apiBase = root.dataset.apiBase;
  const slug = root.dataset.postSlug;
  const countNode = root.querySelector<HTMLElement>("[data-post-view-count]");
  if (!apiBase || !slug || !countNode) return;

  try {
    const data = await fetchJson<PostViewResponse>(`${apiBase}/views`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    countNode.textContent = formatCount(data.views);
  } catch {
    // Keep the static fallback without changing the article metadata layout.
  }
}

async function setupSiteStats(root: HTMLElement) {
  if (root.dataset.siteStatsReady === "true") return;
  root.dataset.siteStatsReady = "true";
  const apiBase = root.dataset.apiBase;
  const foundedAt = root.dataset.siteFoundedAt || "";
  const timeZone = root.dataset.siteTimeZone || "Asia/Shanghai";

  updateStat(root, "siteAge", formatSiteAge(foundedAt, new Date(), timeZone));
  if (!apiBase) return;

  try {
    const data = await fetchJson<SiteStatsResponse>(`${apiBase}/stats`);
    updateStat(root, "totalViews", formatCount(data.totalViews));
    updateStat(root, "commentCount", formatCount(data.commentCount), "公开评论");
    if (data.mostViewed) {
      updateStat(
        root,
        "mostViewed",
        formatCount(data.mostViewed.views),
        data.mostViewed.title || data.mostViewed.slug || "暂无阅读记录",
      );
    } else {
      updateStat(root, "mostViewed", "0", "暂无阅读记录");
    }
  } catch {
    // Static values remain readable when the local dynamic API is unavailable.
  }
}

function boot() {
  document.querySelectorAll<HTMLElement>("[data-post-view]").forEach((root) => void setupPostView(root));
  document.querySelectorAll<HTMLElement>("[data-site-stats]").forEach((root) => void setupSiteStats(root));
}

if (typeof document !== "undefined") {
  document.addEventListener("astro:page-load", boot);
  boot();
}
