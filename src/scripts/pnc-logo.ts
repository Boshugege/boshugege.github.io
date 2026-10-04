const motion = matchMedia("(prefers-reduced-motion: reduce)");
let controller: AbortController;
let logos: HTMLElement[] = [];
let frame = 0;
function reset(logo: HTMLElement) {
  logo.classList.remove("pnc-hover", "pnc-breaking", "pnc-scanning");
  for (const key of ["rx", "ry", "px", "py"]) logo.style.removeProperty(`--pnc-${key}`);
}
function scan(logo: HTMLElement) {
  if (!motion.matches && !document.hidden) logo.classList.add("pnc-scanning");
}
const clamp = (v: number) => Math.max(-1, Math.min(1, v));
function tilt(logo: HTMLElement, dx: number, dy: number, angle: number) {
  logo.style.setProperty("--pnc-rx", `${-dy * angle}deg`);
  logo.style.setProperty("--pnc-ry", `${dx * angle}deg`);
}
function setup() {
  controller?.abort();
  cancelAnimationFrame(frame);
  controller = new AbortController();
  const options = { signal: controller.signal };
  logos = [...document.querySelectorAll<HTMLElement>("[data-pnc-logo]")];
  const near = logos.filter((logo) => logo.hasAttribute("data-pnc-proximity"));
  for (const logo of logos) {
    reset(logo);
    scan(logo);
    logo.addEventListener("pointerenter", () => scan(logo), options);
    if (!near.includes(logo)) {
      logo.addEventListener("pointermove", (event) => {
        if (motion.matches || event.pointerType === "touch") return;
        const box = logo.getBoundingClientRect();
        logo.classList.add("pnc-hover");
        tilt(logo, clamp((event.clientX - box.left) / box.width * 2 - 1), clamp((event.clientY - box.top) / box.height * 2 - 1), 12);
      }, options);
      logo.addEventListener("pointerleave", () => {
        logo.classList.remove("pnc-hover");
        tilt(logo, 0, 0, 12);
      }, options);
    }
    logo.addEventListener("click", () => {
      if (!motion.matches) logo.classList.add("pnc-breaking");
    }, options);
    logo.addEventListener("animationend", (event) => {
      if (event.animationName === "pnc-invert") logo.classList.remove("pnc-breaking");
      if (event.animationName === "pnc-scan") logo.classList.remove("pnc-scanning");
    }, options);
  }
  if (near.length) {
    document.addEventListener("pointermove", (event) => {
      if (motion.matches || event.pointerType === "touch") return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        for (const logo of near) {
          const box = logo.getBoundingClientRect();
          const x = event.clientX - box.left - box.width / 2;
          const y = event.clientY - box.top - box.height / 2;
          const weight = Math.max(0, 1 - Math.hypot(x, y) / 220);
          const dx = x / 110 * weight, dy = y / 110 * weight;
          tilt(logo, dx, dy, 4);
          logo.style.setProperty("--pnc-px", `${dx * 2}px`);
          logo.style.setProperty("--pnc-py", `${dy * 2}px`);
        }
      });
    }, options);
    for (const event of ["pointerleave", "scroll", "visibilitychange"]) {
      document.addEventListener(event, () => { cancelAnimationFrame(frame); near.forEach(reset); }, options);
    }
  }
}
setInterval(() => logos.forEach(scan), 8000);
motion.addEventListener("change", () => { if (motion.matches) { cancelAnimationFrame(frame); logos.forEach(reset); } });
document.addEventListener("astro:before-swap", () => {
  controller?.abort();
  cancelAnimationFrame(frame);
  logos.forEach(reset);
  logos = [];
});
document.addEventListener("astro:page-load", setup);
setup();
