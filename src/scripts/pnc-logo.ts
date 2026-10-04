const motion = matchMedia("(prefers-reduced-motion: reduce)");
let controller: AbortController;
let logos: HTMLElement[] = [];
function reset(logo: HTMLElement) {
  logo.classList.remove("pnc-hover", "pnc-breaking", "pnc-scanning");
  logo.style.removeProperty("--pnc-rx");
  logo.style.removeProperty("--pnc-ry");
}
function scan(logo: HTMLElement) {
  if (!motion.matches && !document.hidden) logo.classList.add("pnc-scanning");
}
function setup() {
  controller?.abort();
  controller = new AbortController();
  const options = { signal: controller.signal };
  logos = [...document.querySelectorAll<HTMLElement>("[data-pnc-logo]")];
  for (const logo of logos) {
    reset(logo);
    scan(logo);
    logo.addEventListener("pointerenter", () => scan(logo), options);
    logo.addEventListener("pointermove", (event) => {
      if (motion.matches || event.pointerType === "touch") return;
      const box = logo.getBoundingClientRect();
      const dx = Math.max(-1, Math.min(1, (event.clientX - box.left) / box.width * 2 - 1));
      const dy = Math.max(-1, Math.min(1, (event.clientY - box.top) / box.height * 2 - 1));
      logo.classList.add("pnc-hover");
      logo.style.setProperty("--pnc-rx", `${-dy * 12}deg`);
      logo.style.setProperty("--pnc-ry", `${dx * 12}deg`);
    }, options);
    logo.addEventListener("pointerleave", () => {
      logo.classList.remove("pnc-hover");
      logo.style.removeProperty("--pnc-rx");
      logo.style.removeProperty("--pnc-ry");
    }, options);
    logo.addEventListener("click", () => {
      if (!motion.matches) logo.classList.add("pnc-breaking");
    }, options);
    logo.addEventListener("animationend", (event) => {
      if (event.animationName === "pnc-invert") logo.classList.remove("pnc-breaking");
      if (event.animationName === "pnc-scan") logo.classList.remove("pnc-scanning");
    }, options);
  }
}
setInterval(() => logos.forEach(scan), 8000);
motion.addEventListener("change", () => logos.forEach(reset));
document.addEventListener("astro:before-swap", () => {
  controller?.abort();
  logos.forEach(reset);
  logos = [];
});
document.addEventListener("astro:page-load", setup);
setup();
