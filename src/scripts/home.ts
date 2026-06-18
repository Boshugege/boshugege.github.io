import { filterPosts, type PostIndexDocument, type PostSearchDocument } from "../lib/search";

const SEARCH_DELAY = 120;
let postsPromise: Promise<PostIndexDocument[]> | undefined;
let searchPromise: Promise<Map<string, string>> | undefined;
let timer: number | undefined;

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
    meta.textContent = [
      post.date,
      `约 ${post.wordCount.toLocaleString("zh-CN")} 字`,
      `约 ${post.readingMinutes} 分钟读完`,
      post.tags.join(" / "),
    ].filter(Boolean).join(" · ");
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
