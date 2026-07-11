import type { NoteEntry } from "../notes";
import type { PostSummary } from "./posts";
import { site } from "../site";

export interface StatItem {
  key?: "totalViews" | "siteAge" | "mostViewed" | "commentCount";
  label: string;
  value: string | number;
  detail?: string;
}

export interface SiteStatistics {
  stats: StatItem[];
  highlights: StatItem[];
  yearlyPosts: [string, number][];
}

export function buildSiteStatistics(posts: PostSummary[], _notes: NoteEntry[]): SiteStatistics {
  const yearCounts = new Map<string, number>();
  for (const post of posts) {
    const year = post.date.slice(0, 4);
    yearCounts.set(year, (yearCounts.get(year) || 0) + 1);
  }

  const yearlyPosts = [...yearCounts.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  const totalWords = posts.reduce((sum, post) => sum + post.reading.wordCount, 0);
  const averageWords = posts.length ? Math.round(totalWords / posts.length) : 0;
  const longestPost = posts.reduce<PostSummary | undefined>(
    (longest, post) => !longest || post.reading.wordCount > longest.reading.wordCount ? post : longest,
    undefined,
  );
  const peakYear = [...yearCounts.entries()].sort((a, b) => b[1] - a[1] || b[0].localeCompare(a[0]))[0];

  return {
    yearlyPosts,
    stats: [
      { key: "totalViews", label: "总阅读", value: "—", detail: `${posts.length} 篇文章累计` },
      { key: "siteAge", label: "建站时间", value: "—", detail: `始于 ${site.foundedAt}` },
      { key: "mostViewed", label: "最多阅读", value: "—", detail: "暂无阅读记录" },
      { key: "commentCount", label: "评论", value: "—", detail: "公开评论" },
    ],
    highlights: [
      { label: "代码块", value: posts.reduce((sum, post) => sum + post.features.codeBlocks, 0), detail: "技术笔记密度" },
      { label: "公式文章", value: posts.filter((post) => post.features.hasMath).length, detail: "含 TeX 数学" },
      { label: "图片", value: posts.reduce((sum, post) => sum + post.features.images, 0), detail: "文章配图" },
      { label: "平均篇幅", value: averageWords.toLocaleString("zh-CN"), detail: "字/篇" },
      { label: "最长文章", value: longestPost?.reading.wordCount.toLocaleString("zh-CN") || 0, detail: longestPost?.title || "暂无" },
      { label: "高峰年份", value: peakYear?.[0] || "暂无", detail: peakYear ? `${peakYear[1]} 篇文章` : "" },
    ],
  };
}
