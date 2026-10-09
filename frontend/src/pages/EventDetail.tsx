import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Clock3, Newspaper, TrendingUp, ArrowRight, PenSquare } from 'lucide-react';
import { apiMode, eventById, fetchEventById, type EventWithScore } from '../api/client';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle, useTick } from '../utils/hooks';
import { ArticleWithScore } from '../types';
import ArticleModal from '../components/ArticleModal';
import { EventStatusTag } from '../components/ArticleModal';
import { createDraft, type DraftTier } from '../api/drafts';
import { createSiteDraft } from '../api/siteDrafts';
import {
  AiBadge,
  ChannelTag,
  HeatBar,
  Loading,
  SourceBadge,
} from '../components/common';
import {
  clockTime,
  formatCount,
  formatScore,
  fullTime,
  timeAgo,
} from '../utils/format';

export default function EventDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<ArticleWithScore | null>(null);
  // AI 生成文章：tier 版本选择 + 提交状态
  const [genTier, setGenTier] = useState<DraftTier>('brief');
  const [pendingCreations, setPendingCreations] = useState<Record<string, 'wechat' | 'site'>>({});
  const pendingCreationsRef = useRef(new Map<string, number>());
  const mountedRef = useRef(true);
  const [genError, setGenError] = useState<{ id: string; message: string } | null>(null);
  const generationRequest = useRef(0);
  const bootReady = useBootstrapReady();
  const dataVer = useDataVersion();
  useTick(); // 时间线与“最近更新”相对时间自动跳动

  useLayoutEffect(() => {
    mountedRef.current = true;
    generationRequest.current += 1;
    return () => {
      mountedRef.current = false;
      generationRequest.current += 1;
    };
  }, [id]);

  const beginCreation = (eventId: string, channel: 'wechat' | 'site') => {
    if (pendingCreationsRef.current.has(eventId)) return null;
    const requestId = ++generationRequest.current;
    pendingCreationsRef.current.set(eventId, requestId);
    setPendingCreations((previous) => ({ ...previous, [eventId]: channel }));
    setGenError(null);
    return requestId;
  };
  const finishCreation = (eventId: string, requestId: number) => {
    if (pendingCreationsRef.current.get(eventId) !== requestId) return;
    pendingCreationsRef.current.delete(eventId);
    if (!mountedRef.current) return;
    setPendingCreations((previous) => {
      if (!(eventId in previous)) return previous;
      const next = { ...previous };
      delete next[eventId];
      return next;
    });
  };

  const generateArticle = async () => {
    if (!event) return;
    const requestedEventId = event.id;
    const requestId = beginCreation(requestedEventId, 'wechat');
    if (requestId === null) return;
    try {
      const d = await createDraft('event', requestedEventId, genTier);
      if (generationRequest.current === requestId) navigate(`/drafts/${d.id}`);
    } catch (e) {
      if (generationRequest.current === requestId) setGenError({ id: requestedEventId, message: (e as Error).message || String(e) });
    } finally {
      finishCreation(requestedEventId, requestId);
    }
  };

  const generateSiteArticle = async () => {
    if (!event) return;
    const requestedEventId = event.id;
    const requestId = beginCreation(requestedEventId, 'site');
    if (requestId === null) return;
    try {
      const d = await createSiteDraft('event', requestedEventId, genTier);
      if (generationRequest.current === requestId) navigate(`/site-drafts/${d.id}`);
    } catch (e) {
      if (generationRequest.current === requestId) setGenError({ id: requestedEventId, message: (e as Error).message || String(e) });
    } finally {
      finishCreation(requestedEventId, requestId);
    }
  };

  // dataVer：后台同步拉到新数据后重新查询（含首次引导完成的那次通知）
  // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer 变化触发重算
  const cachedEvent = useMemo(() => (id ? eventById(id) : undefined), [id, dataVer]);
  // 事件滚出列表窗口（/events?limit=100）时的按需兜底：直调 /events/{id}。
  // 兜底状态携带所属路由 id 并派生生效范围——路由参数变化时组件实例被复用
  // （key 是常量页名），派生条件自动失效回退"未拉取"，不会把上一个事件的
  // 兜底数据渲染在当前 URL 下（shouldFetch 也会重新放行补拉）
  const [fetchAttempt, setFetchAttempt] = useState(0);
  const [fetched, setFetched] = useState<{
    id: string; ev: EventWithScore | null; error: string; status?: number; attempt: number;
  } | null>(null);
  const fetchedForId = fetched && fetched.id === id ? fetched : null;
  const fetchedEvent = fetchedForId?.ev ?? null;
  const event = cachedEvent ?? fetchedEvent ?? undefined;
  const shouldFetch = bootReady && !!id && !cachedEvent &&
    (!fetchedForId || fetchedForId.attempt !== fetchAttempt);

  useEffect(() => {
    if (!shouldFetch || !apiMode || !id) return;
    let cancelled = false;
    const attempt = fetchAttempt;
    fetchEventById(id)
      .then((ev) => {
        if (!cancelled) setFetched({ id, ev, error: '', attempt });
      })
      .catch((e: Error & { status?: number }) => {
        if (!cancelled) setFetched({ id, ev: null, error: e.message || String(e), status: e.status, attempt });
      });
    return () => {
      cancelled = true;
    };
  }, [shouldFetch, id, fetched, fetchAttempt]);

  usePageTitle(event ? event.title : '事件详情');

  // 引导未完成或兜底拉取进行中时不急着判“未找到”，避免直接打开链接时误报
  // （后端未连接 apiMode=false 时无兜底可拉，直接落入未找到态）
  if (!bootReady) return <Loading />;

  if (!event) {
    if (shouldFetch && apiMode) return <Loading />;
    if (fetchedForId?.error && fetchedForId.status !== 404) {
      return (
        <div className="card py-20 text-center">
          <div className="text-sm text-red-300 mb-4">事件详情加载失败：{fetchedForId.error}</div>
          <button onClick={() => setFetchAttempt((attempt) => attempt + 1)} className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2">
            重试
          </button>
        </div>
      );
    }
    return (
      <div className="card py-20 text-center">
        <div className="text-sm text-slate-400 mb-4">未找到该热点事件</div>
        <button onClick={() => navigate('/events')} className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2">
          返回事件列表
        </button>
      </div>
    );
  }

  const sources = new Set(event.articles.map((a) => a.source));
  const sourceStats = [...sources].map((s) => ({
    source: s,
    count: event.articles.filter((a) => a.source === s).length,
  }));
  const timeline = [...event.timeline].sort((a, b) => a.time.getTime() - b.time.getTime());
  const articles = [...event.articles].sort((a, b) => b.hotScore - a.hotScore);
  // 后端全量口径优先：成员滚出「最近 400 篇」窗口时 articles 数组会缩水
  const articleCount = event.articlesCount ?? event.articles.length;
  const sourceCount = event.sourcesCount ?? sources.size;

  return (
    <div>
      {/* 顶部 */}
      <div className="mb-5">
        <Link
          to="/events"
          className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 mb-4 cursor-pointer"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> 返回热点事件
        </Link>

        <div className="card p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2.5 mb-2.5">
                <EventStatusTag status={event.status} />
                <span className="text-xs text-slate-500 inline-flex items-center gap-1">
                  <Newspaper className="h-3.5 w-3.5" /> {sourceCount} 家媒体 · {articleCount} 篇报道
                </span>
                <span className="text-xs text-slate-500 inline-flex items-center gap-1">
                  <Clock3 className="h-3.5 w-3.5" /> {event.articles.length > 0 ? `${fullTime(event.firstTime)} 起` : '—'}
                </span>
              </div>
              <h1 className="text-2xl font-semibold text-slate-100 leading-snug">{event.title}</h1>
            </div>
            <div className="text-right shrink-0">
              <div className="text-3xl font-bold text-orange-300 tabular-nums">
                {formatScore(event.hotScore)}
              </div>
              <div className="text-xs text-slate-500 mt-1">事件热度 HotScore</div>
              <div className="w-36 mt-2">
                <HeatBar value={event.hotScore} />
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* 左列：AI 总结 + 相关报道 */}
        <div className="lg:col-span-2 space-y-4">
          {/* AI 总结 */}
          <section className="rounded-xl border border-cyan-400/15 bg-cyan-400/[0.04] p-5">
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <AiBadge label="AI 事件总结" />
              <span className="text-[11px] text-slate-500">基于 {articleCount} 篇报道交叉聚合</span>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <select
                  value={genTier}
                  onChange={(e) => setGenTier(e.target.value as DraftTier)}
                  className="rounded-lg border border-radar-border bg-radar-panel px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-accent/60 cursor-pointer"
                  aria-label="选择文章版本"
                >
                  <option value="brief">快讯版</option>
                  <option value="depth">深度版</option>
                </select>
                <button
                  onClick={() => void generateSiteArticle()}
                  disabled={!!pendingCreations[event.id] || !apiMode}
                  className="inline-flex items-center gap-1.5 text-xs text-emerald-300 border border-emerald-400/40 hover:bg-emerald-400/10 rounded-lg px-3 py-1.5 transition-colors cursor-pointer disabled:opacity-50"
                  title="生成独立的网站资讯稿，包含页面关键字、标签和内容简介"
                >
                  <PenSquare className="h-3.5 w-3.5" /> {pendingCreations[event.id] === 'site' ? '创建中…' : '生成网站稿'}
                </button>
                <button
                  onClick={() => void generateArticle()}
                  disabled={!!pendingCreations[event.id] || !apiMode}
                  className="inline-flex items-center gap-1.5 text-xs text-accent border border-accent/40 hover:bg-accent/10 rounded-lg px-3 py-1.5 transition-colors cursor-pointer disabled:opacity-50"
                  title="基于该事件的多来源报道生成可编辑公众号草稿"
                >
                  <PenSquare className="h-3.5 w-3.5" /> {pendingCreations[event.id] === 'wechat' ? '创建中…' : '生成公众号稿'}
                </button>
              </div>
            </div>
            {genError?.id === event.id && (
              <div role="alert" className="mb-3 text-xs text-red-300 border border-red-400/25 bg-red-500/5 rounded-lg px-3 py-2">
                {genError.message}
              </div>
            )}
            <p className="text-sm text-slate-200 leading-relaxed">{event.summary}</p>
            <div className="mt-4 pt-4 border-t border-radar-border">
              <div className="text-[11px] font-medium text-slate-400 mb-2">核心影响</div>
              <ul className="space-y-2">
                {event.impacts.map((imp, i) => (
                  <li key={i} className="flex gap-2.5 text-[13px] text-slate-300 leading-relaxed">
                    <span className="mt-0.5 shrink-0 h-5 px-1.5 rounded bg-amber-400/10 text-amber-300 text-[10px] font-semibold flex items-center">
                      {i + 1}
                    </span>
                    {imp}
                  </li>
                ))}
              </ul>
            </div>
          </section>

          {/* 相关报道 */}
          <section className="card p-5">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold text-slate-200">相关报道</h2>
              <span className="text-xs text-slate-500">多来源关联 · 聚类阈值可在管理面板热调</span>
            </div>
            <div className="divide-y divide-radar-border">
              {articles.length === 0 && (
                <div className="py-6 text-center text-xs text-slate-500">
                  暂无关联报道 · 成员文章可能超出近期文章窗口
                </div>
              )}
              {articles.map((a) => (
                <div
                  key={a.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看「${a.title}」详情`}
                  onClick={() => setSelected(a)}
                  onKeyDown={activateOnKey(() => setSelected(a))}
                  className="py-3.5 first:pt-0 last:pb-0 cursor-pointer group"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <h3 className="text-[14px] text-slate-200 group-hover:text-accent transition-colors leading-snug mb-1.5">
                        {a.title}
                      </h3>
                      <div className="flex items-center gap-3 flex-wrap text-xs text-slate-500">
                        <SourceBadge source={a.source} size="sm" />
                        <ChannelTag channel={a.channel} />
                        <span>{fullTime(a.publishTime)}</span>
                        {a.views > 0 && <span>{formatCount(a.views)} 阅读</span>}
                        {a.comments > 0 && <span>{formatCount(a.comments)} 评论</span>}
                        {a.likes > 0 && <span>{formatCount(a.likes)} 点赞</span>}
                      </div>
                    </div>
                    <div className="text-right shrink-0 w-14">
                      <div className="text-sm font-semibold text-orange-300/90 tabular-nums">
                        {formatScore(a.hotScore)}
                      </div>
                      <div className="text-[10px] text-slate-500">热度</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        {/* 右列：时间线 + 来源分布 */}
        <div className="space-y-4">
          <section className="card p-5">
            <h2 className="text-sm font-semibold text-slate-200 mb-4">事件时间线</h2>
            {timeline.length === 0 ? (
              <div className="text-xs text-slate-500 py-2">暂无时间线记录</div>
            ) : (
              <div className="relative pl-4">
                <div className="absolute left-[5px] top-1.5 bottom-1.5 w-px bg-radar-borderStrong" />
                {timeline.map((t, i) => (
                  <div key={i} className="relative pb-5 last:pb-0">
                    <span
                      className={`absolute -left-4 top-1 h-[11px] w-[11px] rounded-full border-2 ${
                        i === timeline.length - 1
                          ? 'bg-amber-400 border-amber-400/30 shadow-[var(--glow-latest)]'
                          : 'bg-radar-card border-radar-borderStrong'
                      }`}
                    />
                    <div className="text-[11px] text-slate-500 mb-0.5 tabular-nums">
                      {clockTime(t.time)} · {timeAgo(t.time)}
                      {t.source && <span className="ml-1.5 text-slate-500">{t.source}</span>}
                    </div>
                    <div className="text-[13px] text-slate-300 leading-relaxed">{t.text}</div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card p-5">
            <h2 className="text-sm font-semibold text-slate-200 mb-4">传播来源</h2>
            {sourceStats.length === 0 ? (
              <div className="text-xs text-slate-500 py-2">暂无来源统计</div>
            ) : (
              <div className="space-y-3">
                {sourceStats
                  .sort((a, b) => b.count - a.count)
                  .map((s) => (
                    <div key={s.source} className="flex items-center gap-3">
                      <SourceBadge source={s.source} size="sm" />
                      <div className="flex-1 h-1.5 rounded-full bg-radar-border overflow-hidden">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-sky-500 to-cyan-400"
                          style={{ width: `${(s.count / event.articles.length) * 100}%` }}
                        />
                      </div>
                      <span className="text-xs text-slate-500 tabular-nums">{s.count} 篇</span>
                    </div>
                  ))}
              </div>
            )}
            <div className="mt-4 pt-4 border-t border-radar-border flex items-center justify-between text-[11px] text-slate-500">
              <span className="inline-flex items-center gap-1">
                <TrendingUp className="h-3.5 w-3.5" />{' '}
                {event.articles.length > 0
                  ? `最近更新 ${timeAgo(event.articles[event.articles.length - 1].publishTime)}`
                  : '暂无关联报道'}
              </span>
              <Link
                to="/latest"
                className="text-accent/80 hover:text-accent inline-flex items-center gap-1 cursor-pointer"
              >
                查看最新资讯 <ArrowRight className="h-3 w-3" />
              </Link>
            </div>
          </section>
        </div>
      </div>

      {selected && (
        <ArticleModal
          article={selected}
          onClose={() => setSelected(null)}
          onOpenEvent={(eventId) => {
            setSelected(null);
            navigate(`/events/${eventId}`);
          }}
          onOpenDraft={(draftId) => {
            setSelected(null);
            navigate(`/drafts/${draftId}`);
          }}
          onOpenSiteDraft={(draftId) => {
            setSelected(null);
            navigate(`/site-drafts/${draftId}`);
          }}
        />
      )}
    </div>
  );
}
