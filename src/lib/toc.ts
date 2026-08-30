import type { MarkdownHeading } from "astro";

export interface TocItem {
  depth: number;
  slug: string;
  text: string;
  children: TocItem[];
}

export interface TableOfContents {
  count: number;
  items: TocItem[];
}

export function buildTableOfContents(
  headings: MarkdownHeading[],
  enabled = true,
  minimumHeadings = 3,
): TableOfContents | undefined {
  if (!enabled) return undefined;

  const included = headings.filter((heading) => heading.depth >= 2 && heading.depth <= 4);
  if (included.length < minimumHeadings) return undefined;

  const items: TocItem[] = [];
  const stack: TocItem[] = [];

  for (const heading of included) {
    const item: TocItem = { ...heading, children: [] };
    while (stack.length > 0 && stack.at(-1)!.depth >= item.depth) stack.pop();

    const parent = stack.at(-1);
    if (parent) parent.children.push(item);
    else items.push(item);

    stack.push(item);
  }

  return { count: included.length, items };
}
