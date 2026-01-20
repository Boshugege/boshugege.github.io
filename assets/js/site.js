// 简单的客户端渲染：加载 index.json，生成文章列表与标签过滤
async function loadIndex() {
  try {
    const res = await fetch("/index.json");
    if (!res.ok) throw new Error("无法加载 index.json");
    const posts = await res.json();

    // 兼容 tags 可能为字符串或数组的情况
    function getTags(post) {
      if (!post) return [];
      if (Array.isArray(post.tags)) return post.tags;
      if (typeof post.tags === "string")
        return post.tags
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      return [];
    }

    // 将 getTags 暴露到模块作用域供其他函数使用
    window.__getTags = getTags;

    renderTags(posts);
    renderPosts(posts);
  } catch (e) {
    document.getElementById("posts").innerHTML =
      '<p class="loading">加载失败：' + e.message + "</p>";
  }
}

function renderTags(posts) {
  const nav = document.getElementById("tag-nav");
  nav.innerHTML = "";

  // 统计标签并按数量降序排列
  const tagMap = new Map();
  posts.forEach((p) => {
    const tags = (window.__getTags && window.__getTags(p)) || [];
    tags.forEach((t) => tagMap.set(t, (tagMap.get(t) || 0) + 1));
  });
  const sorted = [...tagMap.entries()].sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );

  const params = new URLSearchParams(location.search);
  const activeTag = params.get("tag");

  // 全部链接
  const allLink = document.createElement("a");
  allLink.href = "./";
  allLink.textContent = "全部";
  if (!activeTag) allLink.classList.add("active");
  nav.appendChild(allLink);

  if (sorted.length === 0) return;

  const VISIBLE_COUNT = 5; // 保留至少多少个标签可见
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

  // 计算初始展开状态：当 activeTag 在可见范围外则展开
  let expanded = false;
  if (activeTag) {
    const foundIndex = sorted.findIndex(([t]) => t === activeTag);
    if (foundIndex >= VISIBLE_COUNT) expanded = true;
  }

  if (!expanded) list.classList.add("collapsed");
  nav.appendChild(list);

  // 切换按钮，仅当总数超过 VISIBLE_COUNT 时显示
  const total = sorted.length;
  if (total > VISIBLE_COUNT) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tag-toggle";
    toggle.textContent = expanded ? "收起标签" : `展开全部标签 (${total})`;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    toggle.addEventListener("click", (e) => {
      e.preventDefault();
      expanded = !expanded;
      list.classList.toggle("collapsed", !expanded);
      toggle.textContent = expanded ? "收起标签" : `展开全部标签 (${total})`;
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
    nav.appendChild(toggle);
  }
}

function renderPosts(posts) {
  const container = document.getElementById("posts");
  const params = new URLSearchParams(location.search);
  const filter = params.get("tag");

  let shown = posts.filter(
    (p) =>
      !filter ||
      ((window.__getTags && window.__getTags(p)) || []).includes(filter),
  );
  if (shown.length === 0) {
    container.innerHTML =
      '<div class="directory"><h2 class="dir-title">文章目录</h2><div class="dir-list loading">没有找到相关文章。</div></div>';
    return;
  }

  container.innerHTML = "";
  shown.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  const list = document.createElement("div");
  list.className = "dir-list";

  for (const p of shown) {
    const item = document.createElement("div");
    item.className = "dir-item";
    item.innerHTML = `
      <a href="${p.url}">${escapeHtml(p.title)}</a>
      <span class="meta">${escapeHtml(p.date || "")}</span>
    `;
    list.appendChild(item);
  }

  const wrapper = document.createElement("div");
  wrapper.className = "directory";
  wrapper.innerHTML = '<h2 class="dir-title">文章目录</h2>';
  wrapper.appendChild(list);
  container.appendChild(wrapper);
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>\"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

// 启动
loadIndex();
