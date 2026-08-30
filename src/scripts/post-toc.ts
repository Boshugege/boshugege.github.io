export {};

let cleanupToc: (() => void) | undefined;

async function copyText(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function initializePostToc() {
  cleanupToc?.();
  cleanupToc = undefined;

  const body = document.querySelector<HTMLElement>("[data-post-body]");
  const tocLinks = [...document.querySelectorAll<HTMLAnchorElement>("[data-toc-link]")];
  if (!body) return;

  const headings = [...body.querySelectorAll<HTMLElement>("h2[id], h3[id], h4[id]")];
  if (headings.length === 0) return;

  const abortController = new AbortController();
  let frame = 0;
  let copiedTimer = 0;
  let activeSlug = "";

  function setActive(slug: string) {
    if (slug === activeSlug) return;
    activeSlug = slug;
    for (const link of tocLinks) {
      const active = link.dataset.tocLink === slug;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
  }

  function updateActiveHeading() {
    frame = 0;
    const readingLine = Math.max(96, window.innerHeight * 0.2);
    let current = headings[0];
    for (const heading of headings) {
      if (heading.getBoundingClientRect().top > readingLine) break;
      current = heading;
    }
    setActive(current.id);
  }

  function scheduleUpdate() {
    if (!frame) frame = window.requestAnimationFrame(updateActiveHeading);
  }

  for (const heading of headings) {
    if (heading.querySelector("[data-heading-anchor]")) continue;
    const anchor = document.createElement("a");
    anchor.className = "heading-anchor";
    anchor.href = `#${heading.id}`;
    anchor.dataset.headingAnchor = "";
    anchor.textContent = "#";
    anchor.title = "复制本节链接";
    anchor.ariaLabel = `复制“${heading.textContent?.trim() || "本节"}”的链接`;
    anchor.addEventListener("click", async () => {
      history.replaceState(null, "", anchor.hash);
      setActive(heading.id);
      try {
        await copyText(window.location.href);
        anchor.classList.add("copied");
        anchor.title = "已复制";
        window.clearTimeout(copiedTimer);
        copiedTimer = window.setTimeout(() => {
          anchor.classList.remove("copied");
          anchor.title = "复制本节链接";
        }, 1400);
      } catch {
        window.location.hash = heading.id;
      }
    }, { signal: abortController.signal });
    heading.append(anchor);
  }

  if (tocLinks.length > 0) {
    window.addEventListener("scroll", scheduleUpdate, { passive: true, signal: abortController.signal });
    window.addEventListener("resize", scheduleUpdate, { passive: true, signal: abortController.signal });
    updateActiveHeading();
  }

  cleanupToc = () => {
    abortController.abort();
    if (frame) window.cancelAnimationFrame(frame);
    window.clearTimeout(copiedTimer);
  };
}

document.addEventListener("astro:page-load", initializePostToc);
document.addEventListener("astro:before-swap", () => cleanupToc?.());
initializePostToc();
