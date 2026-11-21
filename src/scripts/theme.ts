import { site } from "../lib/site";

const STORAGE_KEY = "pnc-theme";
const THEME_COLORS = { light: site.themeColor, dark: site.themeColorDark } as const;

type Theme = "light" | "dark";

const systemDark = typeof window.matchMedia === "function"
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : undefined;

function getSavedTheme(): Theme | undefined {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {}
  return undefined;
}

// A theme picked with the toggle wins; otherwise follow the system setting.
function getTheme(): Theme {
  return getSavedTheme() || (systemDark?.matches ? "dark" : "light");
}

function applyTheme(doc: Document, theme: Theme) {
  doc.documentElement.dataset.theme = theme;
  doc.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLORS[theme]);
}

function updateControls(theme: Theme) {
  const label = theme === "dark" ? "切换到日间主题" : "切换到夜间主题";
  document.querySelectorAll<HTMLElement>("[data-theme-toggle-text]").forEach((element) => {
    element.textContent = label;
  });
  document.querySelectorAll<HTMLButtonElement>("[data-theme-toggle]").forEach((button) => {
    button.ariaLabel = label;
    button.title = label;
  });
}

function setTheme(theme: Theme) {
  applyTheme(document, theme);
  updateControls(theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {}
}

function initializeTheme() {
  const theme = getTheme();
  applyTheme(document, theme);
  updateControls(theme);
}

document.addEventListener("click", (event) => {
  const button = (event.target as Element | null)?.closest("[data-theme-toggle]");
  if (!button) return;
  setTheme(getTheme() === "dark" ? "light" : "dark");
});

document.addEventListener("astro:before-swap", (event) => {
  const nextDocument = (event as unknown as { newDocument?: Document }).newDocument;
  if (nextDocument) applyTheme(nextDocument, getTheme());
});

// Follow live system changes until the reader picks a theme themselves.
systemDark?.addEventListener("change", () => {
  if (!getSavedTheme()) initializeTheme();
});

document.addEventListener("astro:page-load", initializeTheme);
initializeTheme();
