// Link previews in notes (markup from src/lib/link-cards.ts). A video cover
// or an X post card loads the provider's player only when the reader asks for
// it: until then the page contacts no third party. Without this script the
// cover and the card are ordinary links to the original page.

const ALLOWED_PLAYERS = new Set(["www.youtube-nocookie.com", "player.bilibili.com", "platform.twitter.com"]);
const POST_ORIGIN = "https://platform.twitter.com";
const POST_TIMEOUT = 12_000;
const POST_MAX_HEIGHT = 4000;

/** The player address from the markup, if it is one of the players this site embeds. */
function playerUrl(holder: HTMLElement): URL | undefined {
  try {
    const url = new URL(holder.dataset.embedSrc || "");
    return url.protocol === "https:" && ALLOWED_PLAYERS.has(url.hostname) ? url : undefined;
  } catch {
    return undefined;
  }
}

function createFrame(src: URL, title: string) {
  const frame = document.createElement("iframe");
  frame.className = "link-embed-frame";
  frame.title = title;
  frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  frame.allowFullscreen = true;
  // YouTube refuses to play without knowing which site embeds it.
  frame.referrerPolicy = "strict-origin-when-cross-origin";
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-presentation");
  frame.src = src.href;
  return frame;
}

function playVideo(holder: HTMLElement, cover: HTMLElement) {
  const src = playerUrl(holder);
  if (!src) return false;
  const frame = createFrame(src, holder.dataset.embedTitle || "视频");
  cover.replaceWith(frame);
  frame.focus();
  return true;
}

// X's embed page reports its height in a message; the card stays until the
// first one arrives, so a blocked or failed embed leaves the preview intact.
const waitingPosts = new Map<Window, { frame: HTMLIFrameElement; holder: HTMLElement; timer: number }>();
const shownPosts = new Map<Window, HTMLIFrameElement>();

function postHeight(data: unknown): number | undefined {
  let message = data;
  if (typeof message === "string") {
    try {
      message = JSON.parse(message);
    } catch {
      return undefined;
    }
  }
  const embed = (message as Record<string, any> | null)?.["twttr.embed"];
  if (embed?.method !== "twttr.private.resize") return undefined;
  const height = Number(embed.params?.[0]?.height);
  return Number.isFinite(height) && height > 0 ? Math.min(Math.ceil(height), POST_MAX_HEIGHT) : undefined;
}

window.addEventListener("message", (event) => {
  if (event.origin !== POST_ORIGIN || !event.source) return;
  const source = event.source as Window;
  const height = postHeight(event.data);
  if (!height) return;
  const waiting = waitingPosts.get(source);
  if (waiting) {
    window.clearTimeout(waiting.timer);
    waitingPosts.delete(source);
    shownPosts.set(source, waiting.frame);
    waiting.holder.classList.add("is-loaded");
    waiting.holder.querySelector(".link-embed-load")?.remove();
  }
  const frame = shownPosts.get(source);
  if (frame?.isConnected) frame.style.height = `${height}px`;
  else shownPosts.delete(source);
});

function loadPost(holder: HTMLElement, button: HTMLButtonElement) {
  const src = playerUrl(holder);
  if (!src) return;
  src.searchParams.set("theme", document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  src.searchParams.set("lang", "zh-cn");
  const frame = createFrame(src, holder.dataset.embedTitle || "X 帖子");
  frame.classList.add("link-embed-post-frame");
  button.disabled = true;
  button.textContent = "加载中…";
  holder.append(frame);
  const source = frame.contentWindow;
  if (!source) return;
  const timer = window.setTimeout(() => {
    waitingPosts.delete(source);
    frame.remove();
    button.disabled = false;
    button.textContent = "没能加载，点上面的卡片去 X 看";
  }, POST_TIMEOUT);
  waitingPosts.set(source, { frame, holder, timer });
}

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest<HTMLButtonElement>(".link-embed-load");
  if (button) {
    const holder = button.closest<HTMLElement>("[data-embed-src]");
    if (holder) loadPost(holder, button);
    return;
  }
  const cover = event.target.closest<HTMLElement>(".link-embed-cover");
  // A modified click (new tab, new window) keeps its meaning.
  if (!cover || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const holder = cover.closest<HTMLElement>("[data-embed-src]");
  if (holder && playVideo(holder, cover)) event.preventDefault();
});

function revealPostButtons() {
  for (const button of document.querySelectorAll<HTMLButtonElement>(".link-embed-load[hidden]")) button.hidden = false;
}

document.addEventListener("astro:page-load", revealPostButtons);
revealPostButtons();
