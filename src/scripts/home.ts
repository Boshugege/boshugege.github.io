import { filterPosts, type PostIndexDocument, type PostSearchDocument } from "../lib/search";

const SEARCH_DELAY = 120;
let postsPromise: Promise<PostIndexDocument[]> | undefined;
let searchPromise: Promise<Map<string, string>> | undefined;
let viewCountsPromise: Promise<Map<string, number>> | undefined;
let viewCounts = new Map<string, number>();
let viewCountsLoaded = false;
let timer: number | undefined;

interface ViewCountsResponse {
  views?: Record<string, number>;
}

function loadPosts() {
  return postsPromise ||= fetch("/index.json").then((response) => {
    if (!response.ok) throw new Error(`index.json: ${response.status}`);
    return response.json();
  });
}

function loadSearchIndex() {
  return searchPromise ||= fetch("/search.json")
    .then((response) => {
      if (!response.ok) throw new Error(`search.json: ${response.status}`);
      return response.json();
    })
    .then((rows: PostSearchDocument[]) => new Map(rows.map((row) => [
      row.id,
      [row.title, row.excerpt, row.tags.join(" "), row.content].join("\n").toLowerCase(),
    ])));
}

function loadViewCounts(apiBase: string) {
  return viewCountsPromise ||= fetch(`${apiBase}/views`)
    .then((response) => {
      if (!response.ok) throw new Error(`views: ${response.status}`);
      return response.json() as Promise<ViewCountsResponse>;
    })
    .then((body) => {
      viewCounts = new Map(Object.entries(body.views || {}).map(([slug, count]) => [slug, Number(count) || 0]));
      viewCountsLoaded = true;
      return viewCounts;
    })
    .catch((error) => {
      viewCountsPromise = undefined;
      throw error;
    });
}

function formatViewCount(slug: string) {
  const value = viewCounts.get(slug);
  if (value === undefined && !viewCountsLoaded) return "—";
  return (value || 0).toLocaleString("zh-CN");
}

function updateViewCountNodes() {
  document.querySelectorAll<HTMLElement>("[data-post-list-view]").forEach((node) => {
    node.textContent = formatViewCount(node.dataset.postSlug || "");
  });
}

async function hydrateViewCounts() {
  const region = document.querySelector<HTMLElement>("[data-post-list]");
  const apiBase = region?.dataset.viewsApi;
  if (!apiBase) return;
  try {
    await loadViewCounts(apiBase);
    updateViewCountNodes();
  } catch {
    // Keep the static fallback when the local API is unavailable.
  }
}

function renderPosts(posts: PostIndexDocument[]) {
  const region = document.querySelector<HTMLElement>("[data-post-list]");
  if (!region) return;
  if (!posts.length) {
    const empty = document.createElement("p");
    empty.className = "loading";
    empty.textContent = "没有找到相关文章。";
    region.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const post of posts) {
    const item = document.createElement("div");
    item.className = "dir-item";
    const title = document.createElement("a");
    title.className = "dir-item-title";
    title.href = `/${post.url}`;
    title.dataset.astroPrefetch = "";
    title.textContent = post.title;
    const meta = document.createElement("span");
    meta.className = "meta dir-item-meta";
    const slug = `/${post.url}`;
    const metaText = [
      post.date,
      `约 ${post.wordCount.toLocaleString("zh-CN")} 字`,
      `约 ${post.readingMinutes} 分钟读完`,
      post.tags.join(" / "),
    ].filter(Boolean).join(" · ");
    const viewCount = document.createElement("span");
    viewCount.dataset.postListView = "";
    viewCount.dataset.postSlug = slug;
    viewCount.textContent = formatViewCount(slug);
    meta.append(document.createTextNode(`${metaText} · 阅读：`), viewCount, document.createTextNode(" 次"));
    item.append(title, meta);
    fragment.append(item);
  }
  region.replaceChildren(fragment);
}

function updateTagState(activeTag: string) {
  document.querySelectorAll<HTMLAnchorElement>("[data-tag]").forEach((link) => {
    const active = (link.dataset.tag || "") === activeTag;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const activeLink = document.querySelector<HTMLElement>("[data-tag].active");
  if (activeTag && activeLink && !activeLink.classList.contains("visible")) {
    document.querySelector("[data-tag-list]")?.classList.remove("collapsed");
    const button = document.querySelector<HTMLButtonElement>("[data-tag-toggle]");
    if (button) {
      button.ariaExpanded = "true";
      button.textContent = "收起标签";
    }
  }
}

async function applyFilters(tag: string, query: string) {
  const posts = await loadPosts();
  renderPosts(filterPosts(posts, tag, query));
  if (query.trim().length >= 2) {
    renderPosts(filterPosts(posts, tag, query, await loadSearchIndex()));
  }
}

function initializeHome() {
  const input = document.querySelector<HTMLInputElement>("[data-post-search]");
  if (!input || input.dataset.bound === "true") return;
  input.dataset.bound = "true";
  const params = new URLSearchParams(location.search);
  const tag = params.get("tag") || "";
  const query = params.get("q") || "";
  input.value = query;
  updateTagState(tag);
  void hydrateViewCounts();
  if (tag || query) void applyFilters(tag, query);

  input.addEventListener("input", () => {
    const next = new URLSearchParams(location.search);
    const value = input.value.trim();
    if (value) next.set("q", value);
    else next.delete("q");
    history.replaceState(null, "", `${location.pathname}${next.size ? `?${next}` : ""}`);
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void applyFilters(tag, value), SEARCH_DELAY);
  });

  document.querySelector<HTMLButtonElement>("[data-tag-toggle]")?.addEventListener("click", (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    const list = document.querySelector("[data-tag-list]");
    const expanded = list?.classList.toggle("collapsed") === false;
    button.ariaExpanded = String(expanded);
    button.textContent = expanded ? "收起标签" : button.dataset.collapsedLabel || "展开全部标签";
  });
}

document.addEventListener("astro:page-load", initializeHome);
initializeHome();
