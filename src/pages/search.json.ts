import { getAllPosts, toPostSearchDocument } from "../lib/content/posts";

export async function GET() {
  const posts = await getAllPosts();
  return Response.json(
    posts.map(toPostSearchDocument),
  );
}
