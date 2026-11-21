export interface PostFeatures {
  codeBlocks: number;
  images: number;
  hasMath: boolean;
}

// Fenced blocks, then inline code spans of any backtick length (`a`, ``a`b``).
// remark-math never parses either, so `$HOME` and `$PATH` on one line must not
// count as a formula.
const FENCED_CODE = /^```[^\n]*\n[\s\S]*?^```\s*$/gm;
const INLINE_CODE = /(`+)(?:(?!\1)[^\n])+?\1/g;
const MATH = /\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$/m;

export function getPostFeatures(content: string): PostFeatures {
  const prose = content.replace(FENCED_CODE, "").replace(INLINE_CODE, " ");
  return {
    codeBlocks: Math.floor((content.match(/^```/gm)?.length || 0) / 2),
    images: content.match(/!\[[^\]]*\]\([^)]+\)/g)?.length || 0,
    hasMath: MATH.test(prose),
  };
}
