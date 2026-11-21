// Rendered post HTML → feed-safe HTML for RSS <content:encoded>. Feed readers
// have none of the site's CSS or scripts, so this keeps plain semantic markup:
// absolute URLs, no <script>/<style>, no Astro scoping attributes, no
// theme-variable-only code colors, and formulas reduced to KaTeX's MathML
// (its HTML half only lines up with the KaTeX stylesheet).

const ABSOLUTE = /^(?:[a-z][a-z\d+.-]*:|\/\/)/i;

function absolutize(value: string, pageUrl: string) {
  if (!value || ABSOLUTE.test(value)) return value;
  try {
    return new URL(value, pageUrl).toString();
  } catch {
    return value;
  }
}

/** Removes every element whose start tag matches `opening`, with its whole
 *  subtree, by counting nested tags of the same name. */
export function removeElements(html: string, opening: RegExp, tag: string) {
  const pattern = new RegExp(opening.source, opening.flags.replace("g", ""));
  const nested = new RegExp(`<${tag}\\b[^>]*>|</${tag}\\s*>`, "gi");
  let result = "";
  let rest = html;
  for (let match = pattern.exec(rest); match; match = pattern.exec(rest)) {
    result += rest.slice(0, match.index);
    let depth = 1;
    let end = match.index + match[0].length;
    nested.lastIndex = end;
    for (let token = nested.exec(rest); token && depth > 0; token = depth > 0 ? nested.exec(rest) : null) {
      depth += token[0][1] === "/" ? -1 : 1;
      end = nested.lastIndex;
    }
    rest = rest.slice(depth > 0 ? rest.length : end);
  }
  return result + rest;
}

// Removes every match, repeating until none is left, so the pieces around
// one removal cannot join into a new element.
function removeRepeatedly(text: string, pattern: RegExp) {
  let previous;
  do {
    previous = text;
    text = text.replace(pattern, "");
  } while (text !== previous);
  return text;
}

export function toFeedHtml(html: string, pageUrl: string) {
  // End tags may carry whitespace or junk attributes (</script foo>).
  let out = removeRepeatedly(removeRepeatedly(html, /<script\b[\s\S]*?<\/script\b[^>]*>/gi), /<style\b[\s\S]*?<\/style\b[^>]*>/gi)
    .replace(/<link\b[^>]*>/gi, "")
    .replace(/\sdata-astro-[\w-]+(?:="[^"]*")?/g, "")
    // Shiki tokens carry only --shiki-light/--shiki-dark variables, which
    // mean nothing without the site CSS.
    .replace(/\sstyle="--shiki-[^"]*"/g, "")
    .replace(/<span>([^<]*)<\/span>/g, "$1");
  out = removeElements(out, /<span class="katex-html"[^>]*>/, "span");
  // A <Compare mode="slider"> reads as two labelled images; its drag handle
  // and divider only work on the site.
  out = removeElements(out, /<div\b[^>]*\bdata-compare-ui\b[^>]*>/, "div");
  out = out
    .replace(/\s(href|src|poster)="([^"]*)"/g, (_, attribute: string, value: string) => ` ${attribute}="${absolutize(value, pageUrl)}"`)
    .replace(/\ssrcset="([^"]*)"/g, (_, value: string) => ` srcset="${value
      .split(",")
      .map((candidate) => {
        const [url, ...descriptor] = candidate.trim().split(/\s+/);
        return [absolutize(url, pageUrl), ...descriptor].join(" ");
      })
      .join(", ")}"`);
  return out.trim();
}
