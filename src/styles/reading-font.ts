// 正文字体 (宋体 + Times New Roman) for pages with body text. Imported here,
// not via CSS @import, so the readingFont plugin in astro.config.mjs can make
// these faces try the system fonts first. See reading-font.css.
import "@fontsource/tinos/latin-400.css";
import "@fontsource/tinos/latin-400-italic.css";
import "@fontsource/tinos/latin-700.css";
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource/noto-serif-sc/700.css";
import "./reading-font.css";
