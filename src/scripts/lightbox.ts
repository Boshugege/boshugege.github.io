// Click (or Enter on) an article image to see it full size. Uses a modal
// <dialog>, so Esc, focus containment and the top layer come from the
// browser. Set up per page and torn down before Astro swaps the page.

const SELECTOR = "[data-post-body] img";
const MIN_SIZE = 48;

let cleanupLightbox: (() => void) | undefined;

function isZoomable(image: HTMLImageElement) {
  if (image.closest("a, button, [data-comments]")) return false;
  const width = image.naturalWidth || image.width;
  const height = image.naturalHeight || image.height;
  return width >= MIN_SIZE && height >= MIN_SIZE;
}

function captionFor(image: HTMLImageElement) {
  const figure = image.closest("figure");
  const caption = figure?.querySelector(":scope > figcaption")?.textContent?.trim();
  return caption || image.alt.trim();
}

function createDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "lightbox";
  dialog.setAttribute("aria-label", "查看图片");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "lightbox-close";
  close.ariaLabel = "关闭";
  close.title = "关闭（Esc）";
  close.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6l12 12M18 6 6 18"></path></svg>';
  const figure = document.createElement("figure");
  figure.className = "lightbox-figure";
  const image = document.createElement("img");
  image.className = "lightbox-image";
  image.decoding = "async";
  const caption = document.createElement("figcaption");
  caption.className = "lightbox-caption";
  figure.append(image, caption);
  dialog.append(close, figure);
  return { dialog, image, caption };
}

function initializeLightbox() {
  cleanupLightbox?.();
  cleanupLightbox = undefined;

  const body = document.querySelector<HTMLElement>("[data-post-body]");
  if (!body || typeof HTMLDialogElement === "undefined") return;

  const abortController = new AbortController();
  const { signal } = abortController;
  let lightbox: ReturnType<typeof createDialog> | undefined;
  let opener: HTMLImageElement | undefined;

  const images = [...document.querySelectorAll<HTMLImageElement>(SELECTOR)];
  for (const image of images) {
    if (image.closest("a, button, [data-comments]")) continue;
    image.dataset.lightbox = "";
    image.tabIndex = 0;
    image.setAttribute("role", "button");
    image.setAttribute("aria-haspopup", "dialog");
    if (!image.getAttribute("aria-label")) image.setAttribute("aria-label", `查看大图${image.alt ? `：${image.alt}` : ""}`);
  }

  function close() {
    if (lightbox?.dialog.open) lightbox.dialog.close();
  }

  function open(image: HTMLImageElement) {
    if (!isZoomable(image)) return;
    lightbox ||= createDialog();
    const { dialog, image: preview, caption } = lightbox;
    if (!dialog.isConnected) {
      document.body.append(dialog);
      // Any click closes it: on the image, the backdrop or the button.
      dialog.addEventListener("click", close, { signal });
      dialog.addEventListener("close", () => {
        document.documentElement.classList.remove("lightbox-open");
        preview.removeAttribute("src");
        opener?.focus({ preventScroll: true });
        opener = undefined;
      }, { signal });
    }
    opener = image;
    preview.src = image.currentSrc || image.src;
    preview.alt = image.alt;
    const text = captionFor(image);
    caption.textContent = text;
    caption.hidden = !text;
    document.documentElement.classList.add("lightbox-open");
    dialog.showModal();
  }

  body.addEventListener("click", (event) => {
    const image = (event.target as Element | null)?.closest<HTMLImageElement>("img[data-lightbox]");
    if (image) open(image);
  }, { signal });
  body.addEventListener("keydown", (event) => {
    const image = (event.target as Element | null)?.closest<HTMLImageElement>("img[data-lightbox]");
    if (!image || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    open(image);
  }, { signal });

  cleanupLightbox = () => {
    close();
    abortController.abort();
    lightbox?.dialog.remove();
    document.documentElement.classList.remove("lightbox-open");
  };
}

document.addEventListener("astro:page-load", initializeLightbox);
document.addEventListener("astro:before-swap", () => cleanupLightbox?.());
initializeLightbox();
