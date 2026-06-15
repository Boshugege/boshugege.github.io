export interface ReadingStats {
  wordCount: number;
  readingMinutes: number;
}

export function getReadingStats(content: string): ReadingStats {
  const text = content
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!\[[^\]]*\]\([^)]+\)/g, " ")
    .replace(/\[[^\]]+\]\([^)]+\)/g, " ")
    .replace(/<[^>]+>/g, " ");
  const cjkCount = text.match(/[\u3400-\u9fff]/g)?.length || 0;
  const latinWordCount = text.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g)?.length || 0;
  const wordCount = cjkCount + latinWordCount;
  return {
    wordCount,
    readingMinutes: Math.max(1, Math.ceil(wordCount / 450)),
  };
}
