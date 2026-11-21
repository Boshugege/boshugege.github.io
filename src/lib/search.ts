export interface SearchablePost {
  id: string;
  title: string;
  date: string;
  tags: string[];
  excerpt: string;
}

export interface PostIndexDocument extends SearchablePost {
  cover: string;
  coverAlt: string;
  url: string;
  slug: string;
  wordCount: number;
  readingMinutes: number;
}

export interface PostSearchDocument extends SearchablePost {
  url: string;
  content: string;
}

// CJK punctuation (，。、“”《》…) carries nothing to search for. search.json
// stores it as spaces, and queries are normalized the same way, so 「你好，世界」
// still finds 「你好。世界」 while the index stays smaller.
const SEARCH_PUNCTUATION = /[\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65\u2018\u2019\u201c\u201d\u2014\u2026\u00b7]+/g;

export function stripSearchPunctuation(text: string) {
  return text.replace(SEARCH_PUNCTUATION, " ").replace(/\s+/g, " ").trim();
}

export function normalizeSearchText(text: string) {
  return stripSearchPunctuation(text).toLowerCase();
}

export function filterPosts<T extends SearchablePost>(
  posts: T[],
  tag: string,
  query: string,
  fullText?: ReadonlyMap<string, string>,
) {
  const normalized = query.trim().toLowerCase();
  const fullTextQuery = normalizeSearchText(normalized);
  return posts
    .filter((post) => !tag || post.tags.includes(tag))
    .map((post) => {
      if (!normalized) return { post, score: 1 };
      let score = 0;
      if (post.title.toLowerCase().includes(normalized)) score += 20;
      if (post.tags.join(" ").toLowerCase().includes(normalized)) score += 14;
      if (post.excerpt.toLowerCase().includes(normalized)) score += 10;
      if (fullTextQuery && fullText?.get(post.id)?.includes(fullTextQuery)) score += 4;
      return { post, score };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || b.post.date.localeCompare(a.post.date))
    .map(({ post }) => post);
}

export interface YearGroup<T> {
  year: string;
  posts: T[];
}

/** Groups posts by the year of their date, newest year first, keeping each
 *  group's posts in their given order. */
export function groupPostsByYear<T extends { date: string }>(posts: T[]): YearGroup<T>[] {
  const groups = new Map<string, T[]>();
  for (const post of posts) {
    const year = post.date.slice(0, 4);
    const group = groups.get(year);
    if (group) group.push(post);
    else groups.set(year, [post]);
  }
  return [...groups.entries()]
    .sort((a, b) => b[0].localeCompare(a[0]))
    .map(([year, items]) => ({ year, posts: items }));
}
