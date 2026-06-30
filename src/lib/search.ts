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

export function filterPosts<T extends SearchablePost>(
  posts: T[],
  tag: string,
  query: string,
  fullText?: ReadonlyMap<string, string>,
) {
  const normalized = query.trim().toLowerCase();
  return posts
    .filter((post) => !tag || post.tags.includes(tag))
    .map((post) => {
      if (!normalized) return { post, score: 1 };
      let score = 0;
      if (post.title.toLowerCase().includes(normalized)) score += 20;
      if (post.tags.join(" ").toLowerCase().includes(normalized)) score += 14;
      if (post.excerpt.toLowerCase().includes(normalized)) score += 10;
      if (fullText?.get(post.id)?.includes(normalized)) score += 4;
      return { post, score };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || b.post.date.localeCompare(a.post.date))
    .map(({ post }) => post);
}
