import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Newspaper, Clock3 } from 'lucide-react';
import { EVENTS_WITH_SCORE, FRESH_EVENT_IDS } from '../api/client';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle, useTick } from '../utils/hooks';
import { EventStatusTag } from '../components/ArticleModal';
import { HeatBar, Loading, PageHeader, RankBadge } from '../components/common';
import { SOURCE_COLORS, SOURCE_FALLBACK_COLOR } from '../constants/sourceColors';
import { fullTime, formatScore, timeAgo } from '../utils/format';

const STATUS_FILTERS = ['全部', '发酵中', '持续关注', '已平息', '多来源聚合'] as const;

export default function Events() {
  const bootReady = useBootstrapReady();
  const navigate = useNavigate();
  const dataVer = useDataVersion();
  const [statusFilter, setStatusFilter] = useState<string>('全部');
  useTick(); // 相对时间自动跳动（驱动 timeAgo 重渲染）

  usePageTitle('热点事件');

  // dataVer：后台同步后重新排序（依赖数组处的豁免注释）；
  // 「多来源聚合」滤掉单报道事件（本地降级模式下占大头，单独看聚合结果更聚焦）
  const events = useMemo(
    () =>
      EVENTS_WITH_SCORE.filter((e) => {
        if (statusFilter === '全部') return true;
        if (statusFilter === '多来源聚合') return e.articlesCount >= 2;
        return e.status === statusFilter;
      }).sort((a, b) => b.hotScore - a.hotScore),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer 变化触发重算
    [dataVer, statusFilter]
  );

  if (!bootReady) return <Loading />;

  return (
    <div>
      <PageHeader
        title="热点事件"
        description="Embedding 相似度 + 标题关键词 + 时间窗口三重门槛聚类 · 阈值可在管理面板热调"
      />

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {STATUS_FILTERS.map((s) => {
          const n =
            s === '全部'
              ? EVENTS_WITH_SCORE.length
              : s === '多来源聚合'
                ? EVENTS_WITH_SCORE.filter((e) => e.articlesCount >= 2).length
                : EVENTS_WITH_SCORE.filter((e) => e.status === s).length;
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
                  首发 {e.articles.length > 0 ? fullTime(e.firstTime) : '—'}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {events.length === 0 && (
        <div className="card py-16 text-center text-sm text-slate-500">
          暂无聚合的热点事件 · 事件聚合任务运行后自动生成
        </div>
      )}
    </div>
  );
}
