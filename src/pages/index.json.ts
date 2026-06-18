import { getAllPosts, toPostIndexDocument } from "../lib/content/posts";

export async function GET() {
  const posts = await getAllPosts();
  return Response.json(
    posts.map(toPostIndexDocument),
  );
}
