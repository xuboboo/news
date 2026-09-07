/** 时间工具：统一接收绝对时间（Date 或 epoch 毫秒），显示时按当前时刻实时计算，
 *  避免"5 分钟前"随页面停留时间失真。 */

type TimeLike = Date | number;

function toMs(t: TimeLike): number {
  return t instanceof Date ? t.getTime() : t;
}

/** 距今多少分钟（非负） */
export function minutesAgo(t: TimeLike): number {
  return Math.max(0, (Date.now() - toMs(t)) / 60000);
}

export function timeAgo(t: TimeLike): string {
  const offsetMin = minutesAgo(t);
  if (offsetMin < 3) return '刚刚';
  if (offsetMin < 60) return `${Math.round(offsetMin)} 分钟前`;
  const hours = offsetMin / 60;
  if (hours < 24) return `${Math.round(hours)} 小时前`;
  const days = Math.round(hours / 24);
  if (days === 1) return '昨天';
  if (days < 7) return `${days} 天前`;
  return new Date(toMs(t)).toLocaleDateString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
  });
}

export function clockTime(t: TimeLike): string {
  return new Date(toMs(t)).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function fullTime(t: TimeLike): string {
  return new Date(toMs(t)).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

export function formatScore(score: number): string {
  if (!Number.isFinite(score)) return '—';
  return score >= 100 ? score.toFixed(0) : score.toFixed(1);
}

export function formatCount(n: number): string {
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)} 亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`;
  return String(n);
}

export function dateLabel(t: TimeLike): string {
  const d = new Date(toMs(t));
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  const same = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, today)) return '今天';
  if (same(d, yesterday)) return '昨天';
  return d.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' });
}

export function toDateKey(t: TimeLike): string {
  const d = new Date(toMs(t));
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
