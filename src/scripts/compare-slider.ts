// Before/after slider for <Compare mode="slider">. Drag with a mouse, pen or
// one finger (vertical swipes still scroll the page), click or tap to jump,
// or focus the handle and use the arrow keys, Home and End. Without this
// script the figure stays side by side.

export {};

const STEP = 2;
const BIG_STEP = 10;
// A corner label fades once the divider comes this close (px), so its text
// is never cut in half by the divider.
const LABEL_GAP = 12;
// A touch has to move this far sideways before it counts as a drag.
const DRAG_THRESHOLD = 6;
const JUMP_MS = 220;

const KEYS: Record<string, (split: number, step: number) => number> = {
  ArrowLeft: (split, step) => split - step,
  ArrowDown: (split, step) => split - step,
  ArrowRight: (split, step) => split + step,
  ArrowUp: (split, step) => split + step,
  PageDown: (split) => split - BIG_STEP,
  PageUp: (split) => split + BIG_STEP,
  Home: () => 0,
  End: () => 100,
};

let controller: AbortController | undefined;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const clamp = (value: number) => Math.min(100, Math.max(0, value));

function setup(root: HTMLElement, signal: AbortSignal) {
  const stage = root.querySelector<HTMLElement>("[data-compare-stage]");
  const ui = root.querySelector<HTMLElement>("[data-compare-ui]");
  const handle = root.querySelector<HTMLElement>("[data-compare-handle]");
  const items = stage ? [...stage.querySelectorAll<HTMLElement>(":scope > .mdx-compare-item")] : [];
  if (!stage || !ui || !handle || items.length !== 2) return;
  root.dataset.compareReady = "";

  const [beforeLabel, afterLabel] = items.map((item) => item.querySelector<HTMLElement>(".mdx-compare-label"));
  const names = [beforeLabel?.textContent?.trim() || "左图", afterLabel?.textContent?.trim() || "右图"];
  const initial = Number(root.dataset.initial);
  let split = Number.isFinite(initial) ? clamp(initial) : 50;
  let jumpTimer = 0;

  function fadeLabels() {
    const box = stage!.getBoundingClientRect();
    if (!box.width) return;
    const x = box.left + (box.width * split) / 100;
    beforeLabel?.classList.toggle("is-faded", x < beforeLabel.getBoundingClientRect().right + LABEL_GAP);
    afterLabel?.classList.toggle("is-faded", x > afterLabel.getBoundingClientRect().left - LABEL_GAP);
  }

  function render() {
    root.style.setProperty("--split", `${split}%`);
    const value = Math.round(split);
    handle!.setAttribute("aria-valuenow", String(value));
    handle!.setAttribute("aria-valuetext", `${names[0]} ${value}%，${names[1]} ${100 - value}%`);
    fadeLabels();
  }

  function moveTo(clientX: number, animate = false) {
    const box = stage!.getBoundingClientRect();
    if (!box.width) return;
    window.clearTimeout(jumpTimer);
    root.classList.toggle("is-animating", animate && !reducedMotion.matches);
    if (animate) jumpTimer = window.setTimeout(() => root.classList.remove("is-animating"), JUMP_MS);
    split = clamp(((clientX - box.left) / box.width) * 100);
    render();
  }

  // A mouse drags at once (a click jumps). Touch and pen wait until the move
  // is clearly sideways, so scrolling across the image never moves the divider.
  let pointer: { id: number; x: number; y: number; dragging: boolean } | undefined;

  stage.addEventListener("pointerdown", (event) => {
    if (pointer || !event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
    const deferred = event.pointerType !== "mouse";
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, dragging: !deferred };
    if (deferred) return;
    event.preventDefault();
    stage.setPointerCapture(event.pointerId);
    root.classList.add("is-dragging");
    handle.focus({ preventScroll: true });
    moveTo(event.clientX, true);
  }, { signal });

  stage.addEventListener("pointermove", (event) => {
    if (!pointer || event.pointerId !== pointer.id) return;
    if (!pointer.dragging) {
      const dx = Math.abs(event.clientX - pointer.x);
      if (dx < DRAG_THRESHOLD || dx < Math.abs(event.clientY - pointer.y)) return;
      pointer.dragging = true;
      stage.setPointerCapture(event.pointerId);
      root.classList.add("is-dragging");
    }
    moveTo(event.clientX);
  }, { signal });

  function release(event: PointerEvent) {
    if (!pointer || event.pointerId !== pointer.id) return;
    const tap = event.type === "pointerup" && !pointer.dragging
      && Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) < 10;
    pointer = undefined;
    root.classList.remove("is-dragging");
    if (tap) moveTo(event.clientX, true);
  }
  stage.addEventListener("pointerup", release, { signal });
  stage.addEventListener("pointercancel", release, { signal });
  // Taking capture for a touch releases the browser's implicit capture on the
  // touched child, whose lostpointercapture bubbles here: only the stage's
  // own loss ends a drag.
  stage.addEventListener("lostpointercapture", (event) => {
    if (event.target === stage) release(event);
  }, { signal });

  handle.addEventListener("keydown", (event) => {
    const next = KEYS[event.key];
    if (!next || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    root.classList.remove("is-animating");
    split = clamp(next(split, event.shiftKey ? BIG_STEP : STEP));
    render();
  }, { signal });

  for (const image of stage.querySelectorAll("img")) image.draggable = false;

  // Label positions depend on the rendered width.
  const resize = new ResizeObserver(fadeLabels);
  resize.observe(stage);
  signal.addEventListener("abort", () => {
    resize.disconnect();
    window.clearTimeout(jumpTimer);
  }, { once: true });

  ui.hidden = false;
  root.classList.add("is-ready");
  render();
}

function initializeSliders() {
  controller ||= new AbortController();
  const { signal } = controller;
  // Skip sliders already set up: astro:page-load fires again at window load
  // on a first visit.
  document.querySelectorAll<HTMLElement>("[data-compare-slider]:not([data-compare-ready])")
    .forEach((root) => setup(root, signal));
}

document.addEventListener("astro:before-swap", () => {
  controller?.abort();
  controller = undefined;
});
document.addEventListener("astro:page-load", initializeSliders);
initializeSliders();
