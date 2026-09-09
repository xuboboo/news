import { useMemo, useState } from 'react';
import { Search, MessageSquare, Share2, Eye, ThumbsUp, Zap, X, ChevronDown } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  ARTICLES_WITH_SCORE,
  FRESH_ARTICLE_IDS,
  LAST_SYNC,
  aiSummaryText,
  eventsForArticle,
  loadMoreArticles,
} from '../api/client';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle, useTick } from '../utils/hooks';
import { ArticleWithScore } from '../types';
import ArticleModal from '../components/ArticleModal';
import { AiBadge, ChannelTag, Loading, PageHeader, TagChip } from '../components/common';
import { clockTime, dateLabel, formatCount } from '../utils/format';

export default function Latest() {
  // 频道与搜索词同步到 URL（#/latest?channel=...&q=...），刷新/分享不丢失
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
  const query = searchParams.get('q') ?? '';
  const setQuery = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === '') next.delete('q');
    else next.set('q', value);
    setSearchParams(next, { replace: true });
  };
  const setChannel = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === '全部') next.delete('channel');
    else next.set('channel', value);
    setSearchParams(next, { replace: true });
  };

  const [selected, setSelected] = useState<ArticleWithScore | null>(null);
  const bootReady = useBootstrapReady();
  const navigate = useNavigate();
  const tick = useTick(); // 相对时间自动跳动

  usePageTitle('最新资讯');

  // 「加载更多」状态：加载中 / 没有更多（后端 total 已全部加载）。
  // exhaustedAt 记录在哪一轮数据版本（dataVer）耗尽：新数据回流后自动视为可再加载
  const [loadingMore, setLoadingMore] = useState(false);
  const [exhaustedAt, setExhaustedAt] = useState<number | null>(null);
  const exhausted = exhaustedAt === dataVer;
  const onLoadMore = async () => {
    if (loadingMore || exhausted) return;
    setLoadingMore(true);
    try {
      const added = await loadMoreArticles();
      if (added === 0) setExhaustedAt(dataVer);
    } finally {
      setLoadingMore(false);
    }
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return ARTICLES_WITH_SCORE.filter((a) => {
      const inChannel = channel === '全部' || a.channel === channel;
      const inQuery =
        q === '' ||
        a.title.toLowerCase().includes(q) ||
        a.summary.tags.some((t) => t.toLowerCase().includes(q));
      return inChannel && inQuery;
    }).sort((a, b) => b.publishTime.getTime() - a.publishTime.getTime());
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer/tick 变化触发重算
  }, [channel, query, dataVer, tick]);

  // 按日期分组
  const groups = useMemo(() => {
    const map = new Map<string, ArticleWithScore[]>();
    filtered.forEach((a) => {
      const label = dateLabel(a.publishTime);
      if (!map.has(label)) map.set(label, []);
      map.get(label)!.push(a);
    });
    return [...map.entries()];
  }, [filtered]);

  if (!bootReady) return <Loading />;

  return (
    <div>
      <PageHeader
        title="最新资讯"
        description={`定时同步时间轴 · 按 publish_time 倒序 · AI 摘要与标签已自动生成${
          LAST_SYNC ? ` · 最近同步 ${new Date(LAST_SYNC).toLocaleTimeString('zh-CN', { hour12: false })}` : ''
        }`}
      >
        <div className="relative">
          <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题或标签…"
            aria-label="搜索标题或标签"
            className="w-64 pl-9 pr-8 py-2 text-sm rounded-lg bg-radar-card border border-radar-border text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-accent/50"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              aria-label="清空搜索"
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded text-slate-500 hover:text-slate-200 hover:bg-radar-card cursor-pointer"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </PageHeader>

      <div className="sticky top-14 z-10 flex items-center gap-2 mb-5 py-2 -mx-1 px-1 rounded-xl bg-radar-bg/70 backdrop-blur-xl backdrop-saturate-150 flex-wrap">
        {channels.map((c) => (
          <button
            key={c}
            onClick={() => setChannel(c)}
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
        <span className="ml-auto text-xs text-slate-500">共 {filtered.length} 条</span>
      </div>

      {groups.map(([label, items]) => (
        <div key={label} className="mb-7">
          <div className="flex items-center gap-3 mb-3">
            <span className="text-sm font-semibold text-slate-300">{label}</span>
            <span className="text-xs text-slate-500">{items.length} 条</span>
            <div className="flex-1 h-px bg-radar-border" />
          </div>

          <div className="space-y-2.5">
            {items.map((a) => {
              const evs = eventsForArticle(a);
              const fresh = FRESH_ARTICLE_IDS.has(a.id);
              return (
                <div
                  key={a.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看「${a.title}」详情`}
                  onClick={() => setSelected(a)}
                  onKeyDown={activateOnKey(() => setSelected(a))}
                  className={`card px-4 py-3.5 flex gap-4 cursor-pointer hover:border-accent/30 hover:bg-radar-cardHover/50 transition-all group ${
                    fresh ? 'border-l-2 border-l-emerald-400/70 bg-emerald-400/[0.03]' : ''
                  }`}
                >
                  <div className="w-14 shrink-0 pt-0.5 text-right">
                    <div className="text-sm text-slate-300 tabular-nums">{clockTime(a.publishTime)}</div>
                    <div className="text-[10px] text-slate-500 mt-0.5">{a.source}</div>
                  </div>
                  <div className="w-px bg-radar-border self-stretch" />
                  <div className="flex-1 min-w-0">
                    <h3 className="text-[15px] font-medium text-slate-100 group-hover:text-accent transition-colors leading-snug mb-1.5">
                      {a.title}
                    </h3>
                    <div className="flex items-center gap-2.5 flex-wrap text-xs text-slate-500 mb-2">
                      {fresh && (
                        <span className="chip text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-400/25">
                          NEW
                        </span>
                      )}
                      <ChannelTag channel={a.channel} />
                      {/* 互动芯片：有值才显示（阅读/分享暂无公开数据源恒为 0，避免"阅读 0"破相） */}
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
                      {evs.map((e) => (
                        <button
                          key={e.id}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            navigate(`/events/${e.id}`);
                          }}
                          // 事件芯片是卡片内的独立可点元素：阻止键盘/点击事件冒泡到卡片，
                          // 否则卡片激活逻辑会劫持 Enter 键（打开弹窗而非跳转事件页）
                          onKeyDown={(ev) => ev.stopPropagation()}
                          title={e.title}
                          className="chip max-w-[240px] bg-amber-500/10 text-amber-300 border border-amber-400/25 hover:bg-amber-500/20 cursor-pointer"
                        >
                          <Zap className="h-3 w-3 shrink-0" />
                          <span className="truncate min-w-0">{e.title}</span>
                        </button>
                      ))}
                    </div>
                    <div className="flex items-start gap-2">
                      <AiBadge />
                      <p
                        className={`text-[13px] leading-relaxed flex-1 min-w-0 ${
                          a.summary.summary_short ? 'text-slate-400' : 'text-slate-500'
                        }`}
                      >
                        {aiSummaryText(a)}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {a.summary.tags.map((t) => (
                        <TagChip key={t} tag={t} />
                      ))}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {/* 加载更多（offset 分页；后端 total 未全部加载时展示） */}
      {filtered.length > 0 && !exhausted && (
        <div className="flex justify-center mb-6 print:hidden">
          <button
            onClick={() => void onLoadMore()}
            disabled={loadingMore}
            className="chip bg-radar-card text-slate-400 border border-radar-border hover:text-accent hover:border-accent/40 px-5 py-2 cursor-pointer disabled:opacity-50 flex items-center gap-1.5"
          >
            <ChevronDown className={`h-4 w-4 ${loadingMore ? 'animate-bounce' : ''}`} />
            {loadingMore ? '加载中…' : '加载更早的资讯'}
          </button>
        </div>
      )}

      {filtered.length === 0 && (
        <div className="card py-16 text-center">
          <div className="text-sm text-slate-500 mb-4">
            {query.trim()
              ? `未找到与「${query.trim()}」相关的资讯`
              : '当前频道下暂无资讯'}
          </div>
          <button
            onClick={() => setSearchParams({}, { replace: true })}
            className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25"
          >
            {query.trim() ? '清除搜索与筛选' : '切换到全部频道'}
          </button>
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
