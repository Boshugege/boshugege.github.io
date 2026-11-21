/**
 * Posts per year, newest first. Every year from the first year (the site's
 * founding year or the earliest post, whichever is older) to the latest post
 * is listed, with 0 for years without posts, so the timeline has no gaps.
 */
export function countPostsByYear(dates: string[], startYear?: number): [string, number][] {
  const counts = new Map<number, number>();
  for (const date of dates) {
    const year = Number(date.slice(0, 4));
    if (Number.isInteger(year)) counts.set(year, (counts.get(year) || 0) + 1);
  }
  if (counts.size === 0) return [];
  const years = [...counts.keys()];
  const last = Math.max(...years);
  const first = Math.min(...years, startYear ?? last);
  const result: [string, number][] = [];
  for (let year = last; year >= first; year--) result.push([String(year), counts.get(year) || 0]);
  return result;
}
