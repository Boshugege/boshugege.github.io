export interface PostFeatures {
  codeBlocks: number;
  images: number;
  hasMath: boolean;
}

export function getPostFeatures(content: string): PostFeatures {
  const prose = content.replace(/^```[^\n]*\n[\s\S]*?^```\s*$/gm, "");
  return {
    codeBlocks: Math.floor((content.match(/^```/gm)?.length || 0) / 2),
    images: content.match(/!\[[^\]]*\]\([^)]+\)/g)?.length || 0,
    hasMath: /\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$/m.test(prose),
  };
}
