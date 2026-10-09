import { getAllPosts } from "../lib/content/posts";
import { site } from "../lib/site";

export async function GET() {
  const posts = await getAllPosts();
  const staticPaths = ["/index.html", "/about.html", "/notes.html", "/rss.xml"];
  const urls = [
    ...staticPaths.map((pathname) => ({ pathname })),
    ...posts.map((post) => ({
      pathname: `/${post.url}`,
      lastmod: post.updated || post.date,
    })),
  ];
  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls
  .map(({ pathname, lastmod }) => `  <url><loc>${new URL(pathname, site.url).toString()}</loc>${lastmod ? `<lastmod>${lastmod}</lastmod>` : ""}</url>`)
  .join("\n")}
</urlset>
`;
  return new Response(body, {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
    },
  });
}
