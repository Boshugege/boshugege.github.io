// "回到顶部" for long posts: shows after the reader scrolls past about one and
// a half screens, and jumps instead of gliding when reduced motion is on.

let cleanupBackToTop: (() => void) | undefined;

function initializeBackToTop() {
  cleanupBackToTop?.();
  cleanupBackToTop = undefined;

  const button = document.querySelector<HTMLButtonElement>("[data-back-to-top]");
  if (!button) return;

  const abortController = new AbortController();
  const { signal } = abortController;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let frame = 0;

  function update() {
    frame = 0;
    const threshold = Math.max(600, window.innerHeight * 1.5);
    const visible = window.scrollY > threshold;
    button!.classList.toggle("is-visible", visible);
    button!.tabIndex = visible ? 0 : -1;
    button!.setAttribute("aria-hidden", String(!visible));
  }

  function schedule() {
    if (!frame) frame = window.requestAnimationFrame(update);
  }

  button.hidden = false;
  button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: reducedMotion.matches ? "auto" : "smooth" });
    document.querySelector<HTMLElement>(".post-topbar .site-brand")?.focus({ preventScroll: true });
  }, { signal });
  window.addEventListener("scroll", schedule, { passive: true, signal });
  window.addEventListener("resize", schedule, { passive: true, signal });
  update();

  cleanupBackToTop = () => {
    abortController.abort();
    if (frame) window.cancelAnimationFrame(frame);
  };
}

document.addEventListener("astro:page-load", initializeBackToTop);
document.addEventListener("astro:before-swap", () => cleanupBackToTop?.());
initializeBackToTop();
