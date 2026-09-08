import { useMemo, useState } from 'react';
import { Flame, Radar, Bot, TrendingUp, ChevronDown, MessageSquare, Share2, Eye, ThumbsUp } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ARTICLES_WITH_SCORE, FRESH_ARTICLE_IDS } from '../api/client';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle, useTick } from '../utils/hooks';
import { ArticleWithScore } from '../types';
import ArticleModal from '../components/ArticleModal';
import {
  AiBadge,
  ChannelTag,
  HeatBar,
  Loading,
  PageHeader,
  RankBadge,
  SourceBadge,
  StatCard,
  TagChip,
} from '../components/common';
import { formatCount, formatScore, minutesAgo, timeAgo } from '../utils/format';

/** 热榜时间窗口：与后端 /hot/board 的 120h 语义对齐并暴露给用户切换 */
const WINDOWS = [
  { key: '24h', label: '近24小时', mins: 24 * 60 },
  { key: '3d', label: '近3天', mins: 3 * 24 * 60 },
  { key: '7d', label: '近7天', mins: 7 * 24 * 60 },
  { key: 'all', label: '全部', mins: Infinity },
] as const;

/** 默认时间窗口：近 24 小时（URL 无 win 参数或参数非法时生效） */
const DEFAULT_WIN = '24h';

export default function HotBoard() {
  // 筛选状态同步到 URL（#/hot?channel=...&win=...），刷新/分享不丢失
  const [searchParams, setSearchParams] = useSearchParams();
  const dataVer = useDataVersion();
  // 频道从当前数据动态派生（含「全部」），后端新增频道无需改前端
  const channels = useMemo(
    () => ['全部', ...Array.from(new Set(ARTICLES_WITH_SCORE.map((a) => a.channel)))],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer 变化触发重算
    [dataVer]
  );
  const channelParam = searchParams.get('channel') ?? '全部';
  const channel = channels.includes(channelParam) ? channelParam : '全部';
  const winParam = searchParams.get('win') ?? DEFAULT_WIN;
  const winKey = WINDOWS.some((w) => w.key === winParam) ? winParam : DEFAULT_WIN;

  const setFilter = (key: 'channel' | 'win', value: string) => {
    const next = new URLSearchParams(searchParams);
    const fallback = key === 'channel' ? '全部' : DEFAULT_WIN;
    if (value === fallback) next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: true });
  };

  const [expanded, setExpanded] = useState<string | null>(null);
  const [selected, setSelected] = useState<ArticleWithScore | null>(null);
  const bootReady = useBootstrapReady();
  const navigate = useNavigate();
  const tick = useTick(); // 相对时间自动跳动

  usePageTitle('热榜中心');

  const win = WINDOWS.find((w) => w.key === winKey) ?? WINDOWS[0];

  const filtered = useMemo(() => {
    const list = ARTICLES_WITH_SCORE.filter(
      (a) =>
        (channel === '全部' || a.channel === channel) &&
        minutesAgo(a.publishTime) <= win.mins
    );
    return [...list].sort((a, b) => b.hotScore - a.hotScore);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer/tick 变化触发重算
  }, [channel, win, dataVer, tick]);

  const summaryCoverage =
    filtered.length > 0
      ? Math.round(
          (filtered.filter((a) => a.summary.summary_short.trim() !== '').length /
            filtered.length) *
            100
        )
      : 0;

  const top = filtered[0];
  const avgScore =
    filtered.length > 0
      ? filtered.reduce((s, a) => s + a.hotScore, 0) / filtered.length
      : 0;

  if (!bootReady) return <Loading />;

  return (
    <div>
      <PageHeader
        title="热榜中心"
        description="实时热度榜 · HotScore = Engagement × TimeDecay · 可按时间窗口与频道切换"
      />

      {/* 概览 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <StatCard
          icon={<Flame className="h-5 w-5" />}
          label="在榜文章"
          value={`${filtered.length} 篇`}
          sub={`${win.label}窗口 · 覆盖 ${new Set(filtered.map((a) => a.channel)).size} 个频道`}
        />
        <StatCard
          icon={<TrendingUp className="h-5 w-5" />}
          label="平均热度"
          value={formatScore(avgScore)}
          sub="TimeDecay 已计入"
        />
        <StatCard
          icon={<Bot className="h-5 w-5" />}
          label="AI 摘要覆盖"
          value={`${summaryCoverage}%`}
          sub={top?.summary.model || 'AI Worker'}
        />
        <StatCard
          icon={<Radar className="h-5 w-5" />}
          label="当前最热"
          value={formatScore(top?.hotScore ?? 0)}
          sub={top ? top.title.slice(0, 18) + (top.title.length > 18 ? '…' : '') : '—'}
        />
      </div>

      {/* 频道筛选（吸顶：滚动时不再被顶部导航遮挡） */}
      <div className="sticky top-14 z-10 flex items-center gap-2 mb-4 py-2 -mx-1 px-1 rounded-xl bg-radar-bg/70 backdrop-blur-xl backdrop-saturate-150 flex-wrap">
        {channels.map((c) => (
          <button
            key={c}
            onClick={() => setFilter('channel', c)}
            aria-pressed={channel === c}
            className={`chip border px-3 py-1.5 cursor-pointer transition-colors ${
              channel === c
                ? 'bg-accent/15 text-accent border-accent/40'
                : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
            }`}
          >
            {c}
          </button>
        ))}
        <span className="w-px h-4 bg-radar-border mx-1" />
        {WINDOWS.map((w) => (
          <button
            key={w.key}
            onClick={() => setFilter('win', w.key)}
            aria-pressed={winKey === w.key}
            className={`chip border px-3 py-1.5 cursor-pointer transition-colors ${
              winKey === w.key
                ? 'bg-orange-500/15 text-orange-300 border-orange-400/40'
                : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
            }`}
          >
            {w.label}
          </button>
        ))}
        <span className="ml-auto text-xs text-slate-500">共 {filtered.length} 篇在榜</span>
      </div>

      {/* 热榜列表 */}
      {filtered.length === 0 ? (
        <div className="card py-16 text-center">
          <div className="text-sm text-slate-500 mb-4">当前筛选条件下暂无在榜文章</div>
          <button
            onClick={() => setSearchParams({}, { replace: true })}
            className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25"
          >
            清除筛选
          </button>
        </div>
      ) : (
        <div className="card divide-y divide-radar-border">
        {filtered.map((a, idx) => {
          const open = expanded === a.id;
          return (
            <div
              key={a.id}
              className={`px-4 py-3.5 hover:bg-radar-cardHover/60 transition-colors ${
                FRESH_ARTICLE_IDS.has(a.id) ? 'bg-emerald-400/[0.04]' : ''
              }`}
            >
              <div className="flex items-center gap-3.5">
                <RankBadge rank={idx + 1} />

                <div
                  className="flex-1 min-w-0 cursor-pointer"
                  role="button"
                  tabIndex={0}
                  aria-label={`查看「${a.title}」的 AI 摘要详情`}
                  onClick={() => setSelected(a)}
                  onKeyDown={activateOnKey(() => setSelected(a))}
                >
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <h3 className="text-[15px] font-medium text-slate-100 hover:text-accent transition-colors leading-snug">
                      {a.title}
                    </h3>
                    {FRESH_ARTICLE_IDS.has(a.id) && (
                      <span className="chip text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-400/25">
                        NEW
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 flex-wrap text-xs text-slate-500">
                    <ChannelTag channel={a.channel} />
                    <SourceBadge source={a.source} size="sm" />
                    <span>{timeAgo(a.publishTime)}</span>
                    {/* 互动芯片：有值才显示（阅读/分享暂无公开数据源恒为 0） */}
                    {a.views > 0 && (
                      <span className="inline-flex items-center gap-1">
                        <Eye className="h-3 w-3" /> {formatCount(a.views)}
                      </span>
                    )}
                    {a.comments > 0 && (
                      <span className="inline-flex items-center gap-1">
                        <MessageSquare className="h-3 w-3" /> {formatCount(a.comments)}
                      </span>
                    )}
                    {a.likes > 0 && (
                      <span className="inline-flex items-center gap-1">
                        <ThumbsUp className="h-3 w-3" /> {formatCount(a.likes)}
                      </span>
                    )}
                    {a.shares > 0 && (
                      <span className="inline-flex items-center gap-1">
                        <Share2 className="h-3 w-3" /> {formatCount(a.shares)}
                      </span>
                    )}
                  </div>
                </div>

                {/* 热度 */}
                <div className="w-36 shrink-0 hidden sm:block">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[10px] text-slate-500">HotScore</span>
                    <span className="text-sm font-semibold text-orange-300 tabular-nums">
                      {formatScore(a.hotScore)}
                    </span>
                  </div>
                  <HeatBar value={a.hotScore} />
                </div>

                <button
                  onClick={() => setExpanded(open ? null : a.id)}
                  className="shrink-0 p-2 rounded-lg text-slate-500 hover:text-accent hover:bg-accent/10 transition-colors"
                  aria-expanded={open}
                  aria-label={open ? '收起 AI 摘要' : '展开 AI 摘要'}
                  title="AI 摘要"
                >
                  <ChevronDown className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
                </button>
              </div>

              {/* 展开 AI 摘要 */}
              {open && (
                <div className="mt-3 ml-11 rounded-lg border border-cyan-400/15 bg-cyan-400/[0.04] p-3.5">
                  <div className="flex items-center gap-2 mb-2">
                    <AiBadge />
                    <span className="text-[11px] text-slate-500">一句话摘要</span>
                  </div>
                  <p className="text-sm text-slate-300 leading-relaxed mb-2.5">
                    {a.summary.summary_short || 'AI 摘要生成中…'}
                  </p>
                  {a.summary.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {a.summary.tags.map((t) => (
                        <TagChip key={t} tag={t} />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
        </div>
      )}

      {selected && (
        <ArticleModal
          article={selected}
          onClose={() => setSelected(null)}
          onOpenEvent={(id) => {
            setSelected(null);
            navigate(`/events/${id}`);
          }}
        />
      )}
    </div>
  );
}
