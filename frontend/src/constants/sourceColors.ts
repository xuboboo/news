/**
 * 新闻来源 → 品牌色（Tailwind 官方色值）。
 * 品牌色为主题无关色：中饱和度在深/浅两种面板底色上均可辨识，不随主题切换。
 * 此前在 common.tsx 与 pages/Events.tsx 各维护一份，现收敛于此。
 */
export const SOURCE_COLORS: Record<string, string> = {
  IT之家: '#EF4444',
  极客公园: '#F97316',
  少数派: '#10B981',
  '36氪': '#A78BFA',
  爱范儿: '#EC4899',
  InfoQ: '#14B8A6',
};

/** 未识别来源的兜底色（slate-400） */
export const SOURCE_FALLBACK_COLOR = '#94A3B8';
