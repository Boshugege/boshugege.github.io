import rss from "@astrojs/rss";
import { getAllPosts } from "../lib/content/posts";
import { site } from "../lib/site";

export async function GET(context: { site: URL }) {
  const posts = await getAllPosts();
  return rss({
    title: site.name,
    description: "静态 HTML 技术博客 RSS",
    site: context.site,
    items: posts.map((post) => ({
      title: post.title,
      pubDate: new Date(post.date),
      description: post.excerpt,
      link: `/${post.url}`,
      categories: post.tags,
    })),
  });
}
