import { getAllPosts } from "../lib/posts";

export async function GET() {
  const posts = await getAllPosts();
  return Response.json(
    posts.map(({ id, title, date, tags, excerpt, url, content }) => ({
      id,
      title,
      date,
      tags,
      excerpt,
      url,
      content: content || "",
    })),
  );
}
