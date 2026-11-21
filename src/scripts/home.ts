import { filterPosts, groupPostsByYear, normalizeSearchText, type PostIndexDocument, type PostSearchDocument } from "../lib/search";

const SEARCH_DELAY = 120;
let postsPromise: Promise<PostIndexDocument[]> | undefined;
let searchPromise: Promise<Map<string, string>> | undefined;
let viewCountsPromise: Promise<Map<string, number>> | undefined;
let viewCounts = new Map<string, number>();
let viewCountsLoaded = false;
let timer: number | undefined;
// Each filter pass gets a number; a pass that finishes after a newer one
// started (slow search.json, fast typing) is dropped.
let filterRun = 0;
const filters = { tag: "", query: "" };

interface ViewCountsResponse {
  views?: Record<string, number>;
}

function loadPosts() {
  return postsPromise ||= fetch("/index.json")
    .then((response) => {
      if (!response.ok) throw new Error(`index.json: ${response.status}`);
      return response.json() as Promise<PostIndexDocument[]>;
    })
    .catch((error) => {
      postsPromise = undefined;
      throw error;
    });
}

function loadSearchIndex() {
  return searchPromise ||= fetch("/search.json")
    .then((response) => {
      if (!response.ok) throw new Error(`search.json: ${response.status}`);
      return response.json() as Promise<PostSearchDocument[]>;
    })
    .then((rows) => new Map(rows.map((row) => [
      row.id,
      normalizeSearchText([row.title, row.excerpt, row.tags.join(" "), row.content].join(" ")),
    ])))
    .catch((error) => {
      searchPromise = undefined;
      throw error;
    });
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
    if (node.parentElement) node.parentElement.hidden = !viewCountsLoaded;
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

// Same markup as the server-rendered list in index.astro.
function createPostItem(post: PostIndexDocument) {
  const item = document.createElement("div");
  item.className = "dir-item";
  const title = document.createElement("a");
  title.className = "dir-item-title";
  title.href = `/${post.url}`;
  title.dataset.astroPrefetch = "";
  title.textContent = post.title;
  const meta = document.createElement("p");
  meta.className = "meta-facts dir-item-meta";
  const slug = `/${post.url}`;
  const date = document.createElement("time");
  date.dateTime = post.date;
  date.textContent = post.date;
  const words = document.createElement("span");
  words.textContent = `${post.wordCount.toLocaleString("zh-CN")} 字`;
  const minutes = document.createElement("span");
  minutes.textContent = `${post.readingMinutes} 分钟`;
  const viewCount = document.createElement("span");
  viewCount.dataset.postListView = "";
  viewCount.dataset.postSlug = slug;
  viewCount.textContent = formatViewCount(slug);
  const viewWrap = document.createElement("span");
  viewWrap.className = "view-count";
  viewWrap.hidden = !viewCountsLoaded;
  viewWrap.append(viewCount, " 次阅读");
  meta.append(date, words, minutes, viewWrap);
  item.append(title, meta);
  return item;
}

/** Date-ordered lists get year headings; relevance-ordered search results
 *  stay one flat list, since years would interrupt the ranking. */
function renderPosts(posts: PostIndexDocument[], byYear: boolean) {
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
  if (byYear) {
    for (const group of groupPostsByYear(posts)) {
      const section = document.createElement("section");
      section.className = "dir-year-group";
      const heading = document.createElement("h3");
      heading.className = "dir-year";
      const count = document.createElement("small");
      count.textContent = `${group.posts.length} 篇`;
      heading.append(group.year, count);
      section.append(heading, ...group.posts.map(createPostItem));
      fragment.append(section);
    }
  } else {
    const list = document.createElement("div");
    list.className = "dir-search-results";
    list.append(...posts.map(createPostItem));
    fragment.append(list);
  }
  region.replaceChildren(fragment);
}

function appendTerm(parent: HTMLElement, label: string, value: string) {
  if (parent.childNodes.length) parent.append(" · ");
  const term = document.createElement("strong");
  term.textContent = value;
  parent.append(`${label} `, term);
}

/** Shows what the list is filtered by, how many posts match (or why that is
 *  unknown), and a reset. */
function updateFilterStatus(count?: number, note?: string) {
  const bar = document.querySelector<HTMLElement>("[data-filter-status]");
  const text = bar?.querySelector<HTMLElement>("[data-filter-status-text]");
  if (!bar || !text) return;
  const query = filters.query.trim();
  bar.hidden = !filters.tag && !query;
  if (bar.hidden) return;
  text.replaceChildren();
  if (filters.tag) appendTerm(text, "标签", filters.tag);
  if (query) appendTerm(text, "搜索", `“${query}”`);
  const result = count === undefined ? "筛选中…" : count ? `${count} 篇文章` : "没有匹配的文章";
  text.append(" · ", note ? (count === undefined ? note : `${result}（${note}）`) : result);
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
  // On phones the tags are one scrolling row: bring the active one into view.
  const strip = activeLink?.closest<HTMLElement>("[data-tag-strip]");
  if (activeLink && strip && strip.scrollWidth > strip.clientWidth) {
    strip.scrollLeft += activeLink.getBoundingClientRect().left - strip.getBoundingClientRect().left - 16;
  }
}

/** Renders the list for the current filters. Resolves false when the post
 *  list could not be loaded (the page keeps what it showed before). */
async function applyFilters() {
  const run = ++filterRun;
  const tag = filters.tag;
  const query = filters.query.trim();
  updateFilterStatus();
  let posts: PostIndexDocument[];
  try {
    posts = await loadPosts();
  } catch {
    if (run === filterRun) updateFilterStatus(undefined, "文章列表加载失败，请刷新重试");
    return false;
  }
  if (run !== filterRun) return true;
  let results = filterPosts(posts, tag, query);
  renderPosts(results, !query);
  updateFilterStatus(results.length);
  if (query.length < 2) return true;
  try {
    const fullText = await loadSearchIndex();
    if (run !== filterRun) return true;
    results = filterPosts(posts, tag, query, fullText);
    renderPosts(results, false);
    updateFilterStatus(results.length);
  } catch {
    if (run === filterRun) updateFilterStatus(results.length, "全文索引加载失败，只搜索了标题、标签和摘要");
  }
  return true;
}

function replaceQueryString() {
  const next = new URLSearchParams(location.search);
  const query = filters.query.trim();
  if (query) next.set("q", query);
  else next.delete("q");
  if (filters.tag) next.set("tag", filters.tag);
  else next.delete("tag");
  const search = next.toString();
  history.replaceState(history.state, "", `${location.pathname}${search ? `?${search}` : ""}${location.hash}`);
}

function initializeHome() {
  const input = document.querySelector<HTMLInputElement>("[data-post-search]");
  if (!input || input.dataset.bound === "true") return;
  input.dataset.bound = "true";
  const params = new URLSearchParams(location.search);
  filters.tag = params.get("tag") || "";
  filters.query = params.get("q") || "";
  input.value = filters.query;
  updateTagState(filters.tag);
  void hydrateViewCounts();
  if (filters.tag || filters.query.trim()) void applyFilters();

  input.addEventListener("input", () => {
    filters.query = input.value;
    replaceQueryString();
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void applyFilters(), SEARCH_DELAY);
  });

  document.querySelector<HTMLButtonElement>("[data-filter-clear]")?.addEventListener("click", () => {
    window.clearTimeout(timer);
    filters.tag = "";
    filters.query = "";
    input.value = "";
    replaceQueryString();
    updateTagState("");
    // The button hides with the bar; keep keyboard focus on the list.
    document.querySelector<HTMLElement>("[data-post-list-title]")?.focus({ preventScroll: true });
    // Without the post list the reset cannot render; reload the full page.
    void applyFilters().then((loaded) => {
      if (!loaded) location.assign("/index.html");
    });
  });

  document.querySelector<HTMLButtonElement>("[data-tag-toggle]")?.addEventListener("click", (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    const list = document.querySelector("[data-tag-list]");
    const expanded = list?.classList.toggle("collapsed") === false;
    button.ariaExpanded = String(expanded);
    button.textContent = expanded ? "收起标签" : button.dataset.collapsedLabel || "展开全部标签";
  });
}

// A pass still waiting for index.json / search.json must not render into
// the next page after a navigation.
document.addEventListener("astro:before-swap", () => {
  filterRun++;
  window.clearTimeout(timer);
});
document.addEventListener("astro:page-load", initializeHome);
initializeHome();
