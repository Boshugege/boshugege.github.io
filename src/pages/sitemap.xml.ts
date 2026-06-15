import { getAllPosts } from "../lib/posts";
import { site } from "../lib/site";

export async function GET() {
  const posts = await getAllPosts();
  const paths = ["/index.html", "/about.html", "/notes.html", "/rss.xml", ...posts.map((post) => `/${post.url}`)];
  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${paths
  .map((pathname) => `  <url><loc>${new URL(pathname, site.url).toString()}</loc></url>`)
  .join("\n")}
</urlset>
`;
  return new Response(body, {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
    },
  });
}
