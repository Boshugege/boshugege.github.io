import { getAllPosts } from "../lib/posts";

export async function GET() {
  const posts = await getAllPosts();
  return Response.json(
    posts.map(({ id, title, date, tags, excerpt, cover, url }) => ({
      id,
      title,
      date,
      tags,
      excerpt,
      cover: cover.replace(/^\//, ""),
      url,
    })),
  );
}
