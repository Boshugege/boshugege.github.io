// PNC logo: faces pop apart on hover, a shine sweeps across every few
// seconds, and a click plays the parity-mirror easter egg.
const motion = matchMedia("(prefers-reduced-motion: reduce)");
let controller: AbortController;
let logos: HTMLElement[] = [];

function reset(logo: HTMLElement) {
  logo.classList.remove("pnc-hover", "pnc-breaking", "pnc-scanning");
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
    logo.addEventListener("pointerenter", (event) => {
      scan(logo);
      if (!motion.matches && event.pointerType !== "touch") logo.classList.add("pnc-hover");
    }, options);
    logo.addEventListener("pointerleave", () => logo.classList.remove("pnc-hover"), options);
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
motion.addEventListener("change", () => { if (motion.matches) logos.forEach(reset); });
document.addEventListener("astro:before-swap", () => {
  controller?.abort();
  logos.forEach(reset);
  logos = [];
});
document.addEventListener("astro:page-load", setup);
setup();
