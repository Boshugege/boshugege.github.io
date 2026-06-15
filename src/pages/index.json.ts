import { getAllPosts } from "../lib/posts";

export async function GET() {
  const posts = await getAllPosts();
  return Response.json(
    posts.map(({ id, title, date, tags, excerpt, cover, url, slug, reading }) => ({
      id,
      title,
      date,
      tags,
      excerpt,
      cover: cover.replace(/^\//, ""),
      url,
      slug,
      wordCount: reading.wordCount,
      readingMinutes: reading.readingMinutes,
    })),
  );
}
