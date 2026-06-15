export const site = {
  name: "PNC's Blog",
  homeDescription: "一个随意写写的简单博客",
  headerDescription: "ParityNonconservation's personal blog, a simple static site.",
  themeColor: "#f2efe7",
  language: "zh-CN",
  footerOwner: "ParityNonconservation",
  url: "https://parityncsvt.top",
  defaultCover: "/assets/img/icon.jpg",
  aboutTitle: "About Me",
  aboutLines: [
    "一个正在学习的小朋友。",
    "Tsinghua University",
    "Beijing, China",
    "Tech and something else",
  ],
  email: "admin@parityncsvt.top",
  github: "https://github.com/Boshugege",
};

export function absoluteUrl(path = "/") {
  return new URL(path, site.url).toString();
}

export function normalizeTags(tags: string[] | string | undefined) {
  if (!tags) return [];
  if (Array.isArray(tags)) return tags.map((tag) => tag.trim()).filter(Boolean);
  return tags.split(",").map((tag) => tag.trim()).filter(Boolean);
}

export function formatDate(date: Date | string) {
  const value = typeof date === "string" ? new Date(date) : date;
  return value.toISOString().slice(0, 10);
}
