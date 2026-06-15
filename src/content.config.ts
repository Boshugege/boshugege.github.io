import { defineCollection, z } from "astro:content";

const posts = defineCollection({
  type: "content",
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    tags: z.union([z.array(z.string()), z.string()]).default([]),
    excerpt: z.string().optional(),
    cover: z.string().optional(),
  }),
});

const about = defineCollection({
  type: "content",
  schema: z.object({
    title: z.string().default("关于我"),
    description: z.string().default("个人介绍与简历"),
    updated: z.coerce.date().optional(),
  }),
});

export const collections = { posts, about };
