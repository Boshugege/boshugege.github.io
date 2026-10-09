interface CommentItem {
  id: string | number;
  author_name: string;
  content: string;
  browser_name?: string;
  browser_version?: string;
  os_name?: string;
  os_version?: string;
  country_code?: string;
  created_at?: string;
}

export {};

interface CommentsConfig {
  turnstileSiteKey?: string;
}

declare global {
  interface Window {
    turnstile?: {
      render: (container: Element, options: { sitekey: string; callback: (token: string) => void; "expired-callback": () => void }) => string;
      reset: (widgetId: string) => void;
    };
  }
}

const scriptSrc = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
let turnstileScriptPromise: Promise<void> | undefined;

function loadTurnstileScript() {
  if (window.turnstile) return Promise.resolve();
  if (turnstileScriptPromise) return turnstileScriptPromise;
  turnstileScriptPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${scriptSrc}"]`);
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("Turnstile script failed to load")), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = scriptSrc;
    script.async = true;
    script.defer = true;
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => reject(new Error("Turnstile script failed to load")), { once: true });
    document.head.append(script);
  });
  return turnstileScriptPromise;
}

function setStatus(root: Element, message: string, state: "info" | "error" = "info") {
  const status = root.querySelector<HTMLElement>("[data-comments-status]");
  if (!status) return;
  status.textContent = message;
  status.dataset.state = state;
}

function formatDate(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function appendText(parent: Element, className: string, text: string) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text;
  parent.append(node);
}

function renderComments(root: Element, comments: CommentItem[]) {
  const list = root.querySelector<HTMLElement>("[data-comments-list]");
  if (!list) return;
  list.replaceChildren();

  if (comments.length === 0) {
    setStatus(root, "还没有评论。");
    return;
  }

  setStatus(root, `${comments.length} 条评论`);
  for (const comment of comments) {
    const item = document.createElement("article");
    item.className = "comment-item";

    const header = document.createElement("header");
    header.className = "comment-meta";
    appendText(header, "comment-author", comment.author_name || "匿名");

    const osText = [comment.os_name, comment.os_version].filter(Boolean).join(" ");
    const browserText = [comment.browser_name, comment.browser_version].filter(Boolean).join(" ");
    const runtime = [osText, browserText].filter(Boolean).join(" / ");
    const details = [runtime, comment.country_code, formatDate(comment.created_at)].filter(Boolean).join(" · ");
    if (details) appendText(header, "comment-detail", ` · ${details}`);

    const body = document.createElement("p");
    body.className = "comment-body";
    body.textContent = comment.content;

    item.append(header, body);
    list.append(item);
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof body?.error === "string" ? body.error : "请求失败";
    throw new Error(message);
  }
  return body as T;
}

async function setupComments(root: HTMLElement) {
  const api = root.dataset.commentsApi;
  const postSlug = root.dataset.postSlug;
  const form = root.querySelector<HTMLFormElement>("[data-comments-form]");
  const submit = root.querySelector<HTMLButtonElement>("[data-comments-submit]");
  const turnstileContainer = root.querySelector<HTMLElement>("[data-comments-turnstile]");
  if (!api || !postSlug || !form || !submit || !turnstileContainer) return;
  const commentsApi = api;
  const currentPostSlug = postSlug;

  let turnstileToken = "";
  let turnstileWidgetId = "";

  async function loadComments() {
    const data = await fetchJson<{ comments: CommentItem[] }>(`${commentsApi}/comments?slug=${encodeURIComponent(currentPostSlug)}`);
    renderComments(root, data.comments || []);
  }

  try {
    const config = await fetchJson<CommentsConfig>(`${commentsApi}/config`);
    const siteKey = root.dataset.turnstileSiteKey || config.turnstileSiteKey || "";
    if (!siteKey) {
      submit.disabled = true;
      setStatus(root, "评论服务未配置 Turnstile。", "error");
    } else {
      await loadTurnstileScript();
      turnstileWidgetId = window.turnstile?.render(turnstileContainer, {
        sitekey: siteKey,
        callback: (token: string) => {
          turnstileToken = token;
        },
        "expired-callback": () => {
          turnstileToken = "";
        },
      }) || "";
    }
    await loadComments();
  } catch (error) {
    setStatus(root, "评论暂时无法加载，请稍后再试。", "error");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    const authorName = String(formData.get("authorName") || "").trim();
    const content = String(formData.get("content") || "").trim();
    if (!authorName || !content) return;
    if (!turnstileToken) {
      setStatus(root, "请先完成人机验证。", "error");
      return;
    }

    submit.disabled = true;
    setStatus(root, "正在发送...");
    try {
      await fetchJson(`${commentsApi}/comments`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ slug: currentPostSlug, authorName, content, turnstileToken }),
      });
      form.reset();
      turnstileToken = "";
      if (turnstileWidgetId) window.turnstile?.reset(turnstileWidgetId);
      await loadComments();
    } catch (error) {
      setStatus(root, error instanceof Error ? error.message : "评论发送失败", "error");
    } finally {
      submit.disabled = false;
    }
  });
}

function boot() {
  document.querySelectorAll<HTMLElement>("[data-comments]").forEach((root) => {
    if (root.dataset.commentsReady) return;
    root.dataset.commentsReady = "true";
    void setupComments(root);
  });
}

document.addEventListener("astro:page-load", boot);
boot();
