import { ReactNode } from 'react';
import { Sparkles } from 'lucide-react';
import { SOURCE_COLORS, SOURCE_FALLBACK_COLOR } from '../constants/sourceColors';

const CHANNEL_STYLES: Record<string, string> = {
  软件之家: 'bg-violet-500/10 text-violet-300 border-violet-400/20',
  'Win11之家': 'bg-sky-500/10 text-sky-300 border-sky-400/20',
  'Win10之家': 'bg-cyan-500/10 text-cyan-300 border-cyan-400/20',
  智能时代: 'bg-emerald-500/10 text-emerald-300 border-emerald-400/20',
};

export function ChannelTag({ channel }: { channel: string }) {
  return (
    <span className={`chip border ${CHANNEL_STYLES[channel] ?? 'bg-slate-500/10 text-slate-300 border-slate-400/20'}`}>
      {channel}
    </span>
  );
}

export function SourceBadge({ source, size = 'md' }: { source: string; size?: 'sm' | 'md' }) {
  const color = SOURCE_COLORS[source] ?? SOURCE_FALLBACK_COLOR;
  const box = size === 'sm' ? 'h-4 w-4 text-[9px]' : 'h-5 w-5 text-[10px]';
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
      <span
        className={`${box} rounded flex items-center justify-center font-bold text-white/90`}
        style={{ background: color }}
      >
        {source.charAt(0)}
      </span>
      {source}
    </span>
  );
}

export function AiBadge({ label = 'AI 摘要' }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gradient-to-r from-cyan-500/20 to-blue-500/20 text-cyan-300 border border-cyan-400/25">
      <Sparkles className="h-3 w-3" />
      {label}
    </span>
  );
}

export function TagChip({ tag }: { tag: string }) {
  return (
    <span className="chip bg-radar-panel text-slate-400 border border-radar-border">
      {tag}
    </span>
  );
}

export function HeatBar({ value, className = '' }: { value: number; className?: string }) {
  const pct = Math.max(3, Math.min(100, value));
  return (
    <div className={`h-1.5 rounded-full bg-radar-border overflow-hidden ${className}`}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-amber-400 via-orange-500 to-red-500 transition-all duration-500"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export function StatCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="card px-4 py-3.5 flex items-start gap-3">
      <div className="mt-0.5 p-2 rounded-lg bg-accent/10 text-accent">{icon}</div>
      <div className="min-w-0">
        <div className="text-xs text-slate-400">{label}</div>
        <div className="text-xl font-semibold text-slate-100 mt-0.5 leading-6">{value}</div>
        {sub && <div className="text-[11px] text-slate-500 mt-0.5 truncate">{sub}</div>}
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 mb-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-100">{title}</h1>
        <p className="text-sm text-slate-400 mt-1.5">{description}</p>
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>
  );
}

export function RankBadge({ rank }: { rank: number }) {
  const styles =
    rank === 1
      ? 'bg-amber-400/15 text-amber-300 border-amber-400/30'
      : rank === 2
      ? 'bg-slate-300/10 text-slate-300 border-slate-300/25'
      : rank === 3
      ? 'bg-orange-500/10 text-orange-300 border-orange-400/25'
      : 'bg-transparent text-slate-500 border-transparent';
  return (
    <span className={`inline-flex items-center justify-center h-7 w-7 rounded-lg border text-sm font-bold ${styles}`}>
      {rank}
    </span>
  );
}

export function Loading() {
  return (
    <div className="flex items-center justify-center py-24 text-slate-500">
      <div className="flex items-center gap-2.5">
        <span className="h-4 w-4 rounded-full border-2 border-accent border-t-transparent animate-spin" />
        <span className="text-sm">数据加载中…</span>
      </div>
    </div>
  );
}
