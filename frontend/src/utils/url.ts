/**
 * 外链 scheme 白名单（2026-09-18 审计整改 F-6）：仅放行 http(s)。
 * 文章 URL 来自爬虫抓取，属外部可控数据——javascript:/data:/vbscript: 等
 * 危险 scheme 一律判为不可点击，不依赖 React 对 href 的隐式防线。
 */
export function safeExternalUrl(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null;
}
