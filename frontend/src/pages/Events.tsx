import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Newspaper, Clock3, ChevronDown, TrendingUp } from 'lucide-react';
import { EVENTS_WITH_SCORE, FRESH_EVENT_IDS, loadMoreEvents } from '../api/client';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle, useTick } from '../utils/hooks';
import { EventStatusTag } from '../components/ArticleModal';
import { HeatBar, Loading, PageHeader, RankBadge } from '../components/common';
import { SOURCE_COLORS, SOURCE_FALLBACK_COLOR } from '../constants/sourceColors';
import { fullTime, formatScore, timeAgo } from '../utils/format';

const STATUS_FILTERS = ['全部', '发酵中', '持续关注', '已平息', '多来源聚合'] as const;

// 时间窗筛选：按 last_time（事件全量成员的最新报道时间，后端 event_article 全量口径）。
// 与热榜的 24h/3d/7d 窗口同语义，补齐事件页缺失的「最近热点」视图。
const WINDOWS = [
  { key: 'all', label: '全部时间', ms: 0 },
  { key: '24h', label: '24小时内', ms: 24 * 3600_000 },
  { key: '3d', label: '3天内', ms: 3 * 24 * 3600_000 },
  { key: '7d', label: '7天内', ms: 7 * 24 * 3600_000 },
] as const;

export default function Events() {
  const bootReady = useBootstrapReady();
  const navigate = useNavigate();
  const dataVer = useDataVersion();
  const [statusFilter, setStatusFilter] = useState<string>('全部');
  const [windowKey, setWindowKey] = useState<string>('all');
  // 「加载更多」状态：exhaustedAt 记录耗尽时的数据版本，新数据回流后自动可再加载
  const [loadingMore, setLoadingMore] = useState(false);
  const [exhaustedAt, setExhaustedAt] = useState<number | null>(null);
  const exhausted = exhaustedAt === dataVer;
  useTick(); // 相对时间自动跳动（驱动 timeAgo 重渲染）

  usePageTitle('热点事件');

  // 窗口先行、状态叠加：状态 chip 的计数基于窗口内事件，两行数字口径一致。
  // lastTime 为后端全量口径；缓存外事件（超出首页 100 个）不参与窗口计数。
  const inWindow = useMemo(() => {
    const win = WINDOWS.find((w) => w.key === windowKey) ?? WINDOWS[0];
    if (win.ms === 0) return EVENTS_WITH_SCORE;
    // 刻意的不纯：时间窗按当前时刻计算，重渲染由 useTick/dataVer 驱动刷新
    // eslint-disable-next-line react-hooks/purity -- 时间窗过滤依赖「现在」
    const since = Date.now() - win.ms;
    return EVENTS_WITH_SCORE.filter((e) => e.lastTime.getTime() >= since);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer/windowKey 变化触发重算
  }, [dataVer, windowKey]);

  const events = useMemo(
    () =>
      inWindow
        .filter((e) => {
          if (statusFilter === '全部') return true;
          if (statusFilter === '多来源聚合') return e.articlesCount >= 2;
          return e.status === statusFilter;
        })
        .sort((a, b) => b.hotScore - a.hotScore),
    [inWindow, statusFilter]
  );

  if (!bootReady) return <Loading />;

  return (
    <div>
      <PageHeader
        title="热点事件"
        description="Embedding 相似度 + 标题关键词 + 时间窗口三重门槛聚类 · 阈值可在管理面板热调"
      />

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {WINDOWS.map((w) => {
          const n =
            w.ms === 0
              ? EVENTS_WITH_SCORE.length
              : EVENTS_WITH_SCORE.filter(
                  (e) => e.lastTime.getTime() >= Date.now() - w.ms
                ).length;
          const active = windowKey === w.key;
          return (
            <button
              key={w.key}
              onClick={() => setWindowKey(w.key)}
              className={`chip px-3 py-1.5 text-xs border transition-colors cursor-pointer ${
                active
                  ? 'bg-accent/15 text-accent border-accent/40'
                  : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
              }`}
            >
              {w.label} · {n}
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {STATUS_FILTERS.map((s) => {
          const n =
            s === '全部'
              ? inWindow.length
              : s === '多来源聚合'
                ? inWindow.filter((e) => e.articlesCount >= 2).length
                : inWindow.filter((e) => e.status === s).length;
          const active = statusFilter === s;
          return (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`chip px-3 py-1.5 text-xs border transition-colors cursor-pointer ${
                active
                  ? 'bg-accent/15 text-accent border-accent/40'
                  : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
              }`}
            >
              {s} · {n}
            </button>
          );
        })}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {events.map((e, idx) => {
          const sources = new Set(e.articles.map((a) => a.source));
          const lastArticle = e.articles[e.articles.length - 1];
          // 后端全量口径优先：成员滚出「最近 400 篇」窗口时 articles 数组会缩水
          const articleCount = e.articlesCount ?? e.articles.length;
          const sourceCount = e.sourcesCount ?? sources.size;
          return (
            <div
              key={e.id}
              role="button"
              tabIndex={0}
              aria-label={`查看事件「${e.title}」详情`}
              onClick={() => navigate(`/events/${e.id}`)}
              onKeyDown={activateOnKey(() => navigate(`/events/${e.id}`))}
              className="card p-5 cursor-pointer hover:border-accent/35 hover:bg-radar-cardHover/50 transition-all group"
            >
              <div className="flex items-start gap-3 mb-3">
                <RankBadge rank={idx + 1} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <h3 className="text-[15px] font-semibold text-slate-100 group-hover:text-accent transition-colors leading-snug">
                      {e.title}
                    </h3>
                    <EventStatusTag status={e.status} />
                    {e.isRising && (
                      <span className="chip inline-flex items-center gap-0.5 text-[10px] bg-orange-500/15 text-orange-300 border border-orange-400/25">
                        <TrendingUp className="h-3 w-3" /> 上升
                      </span>
                    )}
                    {FRESH_EVENT_IDS.has(e.id) && (
                      <span className="chip text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-400/25">
                        NEW
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-xs text-slate-500">
                    <span className="inline-flex items-center gap-1">
                      <Newspaper className="h-3 w-3" /> {sourceCount} 家来源 · {articleCount} 篇报道
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <Clock3 className="h-3 w-3" />{' '}
                      {lastArticle ? `${timeAgo(lastArticle.publishTime)}更新` : '暂无关联报道'}
                    </span>
                  </div>
                </div>
                <div className="text-right shrink-0 w-16">
                  <div className="text-lg font-bold text-orange-300 tabular-nums">
                    {formatScore(e.hotScore)}
                  </div>
                  <div className="text-[10px] text-slate-500">事件热度</div>
                </div>
              </div>

              <p className="text-[13px] text-slate-400 leading-relaxed line-clamp-3 mb-3">
                {e.summary}
              </p>

              <HeatBar value={e.hotScore} className="mb-3" />

              <div className="flex items-center justify-between">
                <div className="flex -space-x-1.5 items-center">
                  {[...sources].map((s) => (
                    <span
                      key={s}
                      title={s}
                      className="h-5 w-5 rounded-full ring-2 ring-radar-card flex items-center justify-center text-[9px] font-bold text-white/90"
                      style={{ background: SOURCE_COLORS[s] ?? SOURCE_FALLBACK_COLOR }}
                    >
                      {s.charAt(0)}
                    </span>
                  ))}
                  <span className="ml-2.5 text-[11px] text-slate-500">
                    {[...sources].join(' / ')}
                  </span>
                </div>
                <span className="text-[11px] text-slate-500">
                  首发 {fullTime(e.firstTime)}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* 加载更多（offset 分页；事件总数超出首屏 100 个后可继续拉取） */}
      {events.length > 0 && !exhausted && (
        <div className="flex justify-center mt-5 print:hidden">
          <button
            onClick={() => {
              if (loadingMore || exhausted) return;
              setLoadingMore(true);
              loadMoreEvents()
                .then((added) => {
                  if (added === 0) setExhaustedAt(dataVer);
                })
                .finally(() => setLoadingMore(false));
            }}
            disabled={loadingMore}
            className="chip bg-radar-card text-slate-400 border border-radar-border hover:text-accent hover:border-accent/40 px-5 py-2 cursor-pointer disabled:opacity-50 flex items-center gap-1.5"
          >
            <ChevronDown className={`h-4 w-4 ${loadingMore ? 'animate-bounce' : ''}`} />
            {loadingMore ? '加载中…' : '加载更早的事件'}
          </button>
        </div>
      )}

      {events.length === 0 && (
        <div className="card py-16 text-center text-sm text-slate-500">
          暂无聚合的热点事件 · 事件聚合任务运行后自动生成
        </div>
      )}
    </div>
  );
}
