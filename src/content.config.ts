import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const tags = z.union([z.array(z.string()), z.string()])
  .default([])
  .transform((value) => {
    const items = Array.isArray(value) ? value : value.split(",");
    return items.map((tag) => tag.trim()).filter(Boolean);
  });

const posts = defineCollection({
  loader: glob({ base: "./src/content/posts", pattern: "**/*.{md,mdx}" }),
  schema: ({ image }) => z.object({
    title: z.string(),
    date: z.coerce.date(),
    updated: z.coerce.date().optional(),
    tags,
    excerpt: z.string().optional(),
    cover: image().optional(),
    coverAlt: z.string().optional(),
    canonical: z.string().url().optional(),
    draft: z.boolean().default(false),
  }),
});

const about = defineCollection({
  loader: glob({ base: "./src/content/about", pattern: "*.mdx" }),
  schema: z.object({
    title: z.string().default("关于我"),
    description: z.string().default("个人介绍与简历"),
    updated: z.coerce.date().optional(),
    headline: z.string().optional(),
    location: z.string().optional(),
    availability: z.string().optional(),
    links: z.array(z.object({
      label: z.string(),
      href: z.string(),
      detail: z.string().optional(),
    })).default([]),
    showSiteStats: z.boolean().default(true),
    showWritingHistory: z.boolean().default(true),
  }),
});

export const collections = { posts, about };
