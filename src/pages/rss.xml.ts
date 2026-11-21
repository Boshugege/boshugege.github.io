import rss from "@astrojs/rss";
import { experimental_AstroContainer as AstroContainer } from "astro/container";
import { getContainerRenderer } from "@astrojs/mdx/container-renderer";
import { loadRenderers } from "astro:container";
import { render } from "astro:content";
import { mdxComponents } from "../components/mdx";
import { getAllPosts } from "../lib/content/posts";
import { toFeedHtml } from "../lib/feed";
import { absoluteUrl, site } from "../lib/site";

export async function GET(context: { site?: URL }) {
  const posts = await getAllPosts();
  // Full text: render each post's MDX (with the site's MDX components) to
  // HTML, then make it self-contained for feed readers.
  const container = await AstroContainer.create({
    renderers: await loadRenderers([getContainerRenderer()]),
  });
  const items = await Promise.all(posts.map(async (post) => {
    const link = absoluteUrl(`/${post.url}`);
    const { Content } = await render(post.entry);
    const html = await container.renderToString(Content, { props: { components: mdxComponents } });
    return {
      title: post.title,
      link,
      pubDate: new Date(post.date),
      description: post.excerpt,
      content: toFeedHtml(html, link),
      categories: post.tags,
      author: `${site.email} (${site.footerOwner})`,
    };
  }));
  const lastBuildDate = posts
    .map((post) => post.updated || post.date)
    .sort()
    .at(-1);
  const feedUrl = absoluteUrl("/rss.xml");

  return rss({
    title: site.name,
    description: site.homeDescription,
    site: context.site || site.url,
    xmlns: { atom: "http://www.w3.org/2005/Atom" },
    customData: [
      `<language>${site.language}</language>`,
      lastBuildDate ? `<lastBuildDate>${new Date(lastBuildDate).toUTCString()}</lastBuildDate>` : "",
      `<atom:link href="${feedUrl}" rel="self" type="application/rss+xml"/>`,
    ].join(""),
    items,
  });
}
