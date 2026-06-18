import type { NoteEntry } from "../notes";
import type { PostSummary } from "./posts";

export interface StatItem {
  label: string;
  value: string | number;
  detail?: string;
}

export interface SiteStatistics {
  stats: StatItem[];
  highlights: StatItem[];
  yearlyPosts: [string, number][];
}

export function buildSiteStatistics(posts: PostSummary[], notes: NoteEntry[]): SiteStatistics {
  const allTags = new Set(posts.flatMap((post) => post.tags));
  const yearCounts = new Map<string, number>();
  for (const post of posts) {
    const year = post.date.slice(0, 4);
    yearCounts.set(year, (yearCounts.get(year) || 0) + 1);
  }

  const yearlyPosts = [...yearCounts.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  const years = yearlyPosts.map(([year]) => Number(year)).filter(Number.isFinite);
  const writingYears = years.length ? `${Math.min(...years)}-${Math.max(...years)}` : "暂无";
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
      { label: "文章", value: posts.length, detail: writingYears },
      { label: "标签", value: allTags.size, detail: "主题跨度" },
      { label: "随想", value: notes.length, detail: notes[0] ? `最近 ${notes[0].date}` : "暂无" },
      { label: "字数", value: `${Math.round(totalWords / 1000)}k`, detail: "粗略估算" },
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
