(function () {
  const SEARCH_DEBOUNCE_MS = 120;
  const FULLTEXT_MIN_QUERY_LEN = 2;

  let fullTextMap = null;
  let fullTextPromise = null;
  let searchDebounceTimer = null;

  function getContainer(id) {
    return document.getElementById(id);
  }

  function normalizeQuery(value) {
    return String(value || "").trim().toLowerCase();
  }

  function getTags(post) {
    if (!post) return [];
    if (Array.isArray(post.tags)) return post.tags;
    if (typeof post.tags === "string") {
      return post.tags
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }
    return [];
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (c) => {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  async function loadPosts() {
    const res = await fetch("/index.json", { cache: "no-store" });
    if (!res.ok) {
      throw new Error("无法加载 index.json");
    }
    return res.json();
  }

  function getForcedTagFromDom() {
    const nav = getContainer("tag-nav");
    if (!nav) return "";
    return String(nav.dataset.forcedTag || "");
  }

  async function loadFullTextIndex() {
    if (fullTextMap) return fullTextMap;
    if (fullTextPromise) return fullTextPromise;

    fullTextPromise = fetch("/search.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) {
          throw new Error("无法加载 search.json");
        }
        return res.json();
      })
      .then((rows) => {
        const map = new Map();
        for (const row of rows) {
          const tags = Array.isArray(row.tags)
            ? row.tags.join(" ")
            : String(row.tags || "");
          const text = [row.title, row.excerpt, tags, row.content]
            .filter(Boolean)
            .join("\n")
            .toLowerCase();
          map.set(String(row.id || ""), text);
        }
        fullTextMap = map;
        return map;
      })
      .catch((err) => {
        fullTextPromise = null;
        throw err;
      });

    return fullTextPromise;
  }

  function ensureFullTextIndex(posts, activeTag, query) {
    const q = normalizeQuery(query);
    if (q.length < FULLTEXT_MIN_QUERY_LEN || fullTextMap || fullTextPromise) {
      return;
    }

    loadFullTextIndex()
      .then(() => {
        const input = document.getElementById("post-search");
        const latestQuery = input ? input.value : query;
        renderPostListRegion(posts, activeTag, latestQuery);
      })
      .catch((err) => {
        // Keep metadata search usable even if fulltext index fails.
        console.warn(err);
      });
  }

  function buildTagMap(posts) {
    const map = new Map();
    for (const post of posts) {
      for (const tag of getTags(post)) {
        map.set(tag, (map.get(tag) || 0) + 1);
      }
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }

  function renderTags(posts, activeTag) {
    const nav = getContainer("tag-nav");
    if (!nav) return;

    const sorted = buildTagMap(posts);
    nav.innerHTML = "";

    const allLink = document.createElement("a");
    allLink.href = "/index.html";
    allLink.textContent = "全部";
    allLink.className = "tag top";
    if (!activeTag) allLink.classList.add("active");
    nav.appendChild(allLink);

    const VISIBLE_COUNT = 8;
    const list = document.createElement("div");
    list.className = "tags-list";

    sorted.forEach(([tag, count], idx) => {
      const a = document.createElement("a");
      a.href = "?tag=" + encodeURIComponent(tag);
      a.textContent = `${tag} (${count})`;
      a.className = "tag";
      if (idx < VISIBLE_COUNT) a.classList.add("visible");
      if (tag === activeTag) a.classList.add("active");
      list.appendChild(a);
    });

    let expanded = false;
    if (activeTag) {
      const foundIndex = sorted.findIndex(([t]) => t === activeTag);
      if (foundIndex >= VISIBLE_COUNT) expanded = true;
    }

    if (!expanded) list.classList.add("collapsed");
    nav.appendChild(list);

    if (sorted.length > VISIBLE_COUNT) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "tag-toggle";
      toggle.textContent = expanded ? "收起标签" : `展开全部标签 (${sorted.length})`;
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.addEventListener("click", () => {
        expanded = !expanded;
        list.classList.toggle("collapsed", !expanded);
        toggle.textContent = expanded ? "收起标签" : `展开全部标签 (${sorted.length})`;
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
      nav.appendChild(toggle);
    }
  }

  function getFilteredPosts(posts, activeTag, query) {
    let shown = posts.slice();

    if (activeTag) {
      shown = shown.filter((p) => getTags(p).includes(activeTag));
    }

    const q = normalizeQuery(query);
    if (q) {
      shown = shown
        .map((p) => {
          const title = String(p.title || "").toLowerCase();
          const excerpt = String(p.excerpt || "").toLowerCase();
          const tags = getTags(p).join(" ").toLowerCase();
          const full = fullTextMap ? fullTextMap.get(String(p.id || "")) || "" : "";

          let score = 0;
          if (title.includes(q)) score += 20;
          if (tags.includes(q)) score += 14;
          if (excerpt.includes(q)) score += 10;
          if (fullTextMap && full.includes(q)) score += 4;

          return { post: p, score };
        })
        .filter((entry) => entry.score > 0)
        .sort((a, b) => {
          if (b.score !== a.score) return b.score - a.score;
          return String(b.post.date || "").localeCompare(String(a.post.date || ""));
        })
        .map((entry) => entry.post);
    } else {
      shown.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
    }

    return shown;
  }

  function renderPostListRegion(posts, activeTag, query) {
    const region = document.getElementById("post-list-region");
    if (!region) return;

    const shown = getFilteredPosts(posts, activeTag, query);
    if (shown.length === 0) {
      region.innerHTML = `<div class="dir-list loading">没有找到相关文章。</div>`;
      return;
    }

    const items = shown
      .map(
        (p) => `
          <div class="dir-item">
            <a href="/${escapeHtml(p.url)}">${escapeHtml(p.title)}</a>
            <span class="meta">${escapeHtml(p.date || "")}</span>
          </div>
        `,
      )
      .join("");

    region.innerHTML = `<div class="dir-list">${items}</div>`;
  }

  function renderPosts(posts, activeTag, query) {
    const container = getContainer("posts");
    if (!container) return;

    const searchValue = escapeHtml(query || "");
    const filterMeta = activeTag
      ? `<span class="meta">当前标签：${escapeHtml(activeTag)}</span>`
      : "";

    container.innerHTML = `
      <h2 class="dir-title">文章目录</h2>
      <div class="dir-tools">
        <input id="post-search" class="search-input" type="search" placeholder="搜索全文（标题 / 摘要 / 正文）" value="${searchValue}" />
        ${filterMeta}
      </div>
      <div id="post-list-region"></div>
    `;

    renderPostListRegion(posts, activeTag, query);
    bindSearchInput(posts, activeTag);
    ensureFullTextIndex(posts, activeTag, query);
  }

  function bindSearchInput(posts, activeTag) {
    const input = document.getElementById("post-search");
    if (!input || input.dataset.bound === "true") return;

    input.dataset.bound = "true";
    input.addEventListener("input", () => {
      const params = new URLSearchParams(window.location.search);
      const value = input.value;
      if (value.trim()) {
        params.set("q", value);
      } else {
        params.delete("q");
      }

      const url = `${window.location.pathname}${params.toString() ? "?" + params.toString() : ""}`;
      history.replaceState(null, "", url);

      if (searchDebounceTimer) {
        clearTimeout(searchDebounceTimer);
      }

      searchDebounceTimer = setTimeout(() => {
        renderPostListRegion(posts, activeTag, value);
        ensureFullTextIndex(posts, activeTag, value);
      }, SEARCH_DEBOUNCE_MS);
    });
  }

  function updateNowStatus(text, isError) {
    const statusTextEl = document.getElementById("now-status-text");
    if (!statusTextEl) return;

    statusTextEl.textContent = text;
    statusTextEl.classList.toggle("is-error", Boolean(isError));
  }

  function buildNowText(payload) {
    if (!payload || typeof payload !== "object") {
      return "空闲";
    }

    if (Array.isArray(payload.events)) {
      const now = new Date();
      const current = payload.events.find((event) => {
        const start = new Date(String(event.start || ""));
        const end = new Date(String(event.end || ""));
        if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
          return false;
        }
        return now >= start && now < end;
      });

      if (!current) {
        return "空闲";
      }

      const summary = String(current.summary || "").trim() || "忙碌中";
      const location = String(current.location || "").replace(/\s+/g, " ").trim();
      return location ? `${summary}（${location}）` : summary;
    }

    if (payload.statusText) {
      return String(payload.statusText);
    }

    const summary = String(payload.summary || "").trim();
    const location = String(payload.location || "").replace(/\s+/g, " ").trim();
    if (!summary) {
      return "空闲";
    }
    return location ? `${summary}（${location}）` : summary;
  }

  async function initNowStatus() {
    const statusTextEl = document.getElementById("now-status-text");
    if (!statusTextEl) return;

    try {
      const res = await fetch("/now.json", { cache: "no-store" });
      if (!res.ok) {
        throw new Error(`now.json 请求失败 (${res.status})`);
      }

      const payload = await res.json();
      updateNowStatus(buildNowText(payload), false);
    } catch (error) {
      console.warn(error);
      updateNowStatus("暂时无法读取状态", true);
    }
  }

  async function init() {
    const nav = getContainer("tag-nav");
    const postsContainer = getContainer("posts");
    if (!nav || !postsContainer) {
      return;
    }

    initNowStatus();

    try {
      const posts = await loadPosts();
      const params = new URLSearchParams(window.location.search);
      const forcedTag = getForcedTagFromDom();
      const activeTag = params.get("tag") || forcedTag || "";
      const query = params.get("q") || "";

      renderTags(posts, activeTag);
      renderPosts(posts, activeTag, query);
    } catch (error) {
      postsContainer.innerHTML = `<div class="loading">加载失败：${escapeHtml(error.message || String(error))}</div>`;
    }
  }

  init();
})();
