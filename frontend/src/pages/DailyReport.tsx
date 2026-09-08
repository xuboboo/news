import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CalendarDays,
  Bot,
  Coins,
  Flame,
  Newspaper,
  TrendingUp,
  FileBarChart,
} from 'lucide-react';
import {
  availableDates,
  fetchDailyReport,
  loadReports,
  peekDailyReport,
  reportsLoaded,
  DailyReportView,
} from '../api/client';
import ArticleModal from '../components/ArticleModal';
import { EventStatusTag } from '../components/ArticleModal';
import { ArticleWithScore } from '../types';
import {
  AiBadge,
  ChannelTag,
  Loading,
  PageHeader,
  RankBadge,
  SourceBadge,
} from '../components/common';
import { activateOnKey, useBootstrapReady, useDataVersion, usePageTitle } from '../utils/hooks';
import { clockTime, formatScore } from '../utils/format';

/** 日期切换 chips：只渲染最近 14 期，更早的收敛为计数（避免期号无限增长挤爆页头） */
const MAX_DATE_CHIPS = 14;

export default function DailyReport() {
  const bootReady = useBootstrapReady();
  const dataVer = useDataVersion();

  usePageTitle('科技日报');

  // dataVer 变化（loadReports 完成 / 周期刷新）时重新读取可用日期
  // eslint-disable-next-line react-hooks/exhaustive-deps -- dataVer 变化触发重算
  const dates = useMemo(() => availableDates(), [dataVer]);
  const [dateKey, setDateKey] = useState('');
  const [selected, setSelected] = useState<ArticleWithScore | null>(null);
  // 未命中缓存的日期按需拉取的结果（记录所属日期，防止切日期串数据）
  const [fetched, setFetched] = useState<{ date: string; view: DailyReportView | null } | null>(
    null
  );
  // 空态手动重试进行中（避免重复点击、给加载反馈）
  const [retrying, setRetrying] = useState(false);
  const navigate = useNavigate();

  // API 模式下日报日期晚于首帧到达：用派生值兜底选中最新一天，无需 effect 同步
  const effectiveDateKey = dateKey || dates[0]?.key || '';
  // 加载已尝试过但仍无任何日期：确为「无日报」而非加载中
  const noDates = dates.length === 0 && reportsLoaded;

  const retryLoadReports = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      await loadReports(true);
    } finally {
      setRetrying(false);
    }
  };

  // 挂载与每轮数据回流时低频刷新日报列表（client 内部 5 分钟节流）：
  // 让 08:00 report_task 新生成的日报在常驻页面上自动出现，无需手动刷新
  useEffect(() => {
    if (!bootReady) return;
    void loadReports(false, 300000);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- bootReady/dataVer 变化触发
  }, [bootReady, dataVer]);

  // 渲染期直接读缓存（纯读取）；未命中时异步拉取，setState 仅出现在回调中
  const cachedReport = effectiveDateKey ? peekDailyReport(effectiveDateKey) : null;
  const fetchedView =
    fetched && fetched.date === effectiveDateKey ? fetched.view : null;
  const report = cachedReport ?? fetchedView;
  const loadFailed = !cachedReport && fetched !== null && fetched.date === effectiveDateKey && fetched.view === null;

  useEffect(() => {
    if (!effectiveDateKey || cachedReport) return;
    let cancelled = false;
    fetchDailyReport(effectiveDateKey)
      .then((view) => {
        if (!cancelled) setFetched({ date: effectiveDateKey, view });
      })
      .catch(() => {
        if (!cancelled) setFetched({ date: effectiveDateKey, view: null });
      });
    return () => {
      cancelled = true;
    };
  }, [effectiveDateKey, cachedReport]);

  const visibleDates = dates.slice(0, MAX_DATE_CHIPS);
  const dateChips = (
    <div className="flex items-center gap-1.5 flex-wrap">
      {visibleDates.map((d) => (
        <button
          key={d.key}
          onClick={() => setDateKey(d.key)}
          aria-pressed={effectiveDateKey === d.key}
          className={`chip border px-3 py-1.5 cursor-pointer transition-colors ${
            effectiveDateKey === d.key
              ? 'bg-accent/15 text-accent border-accent/40'
              : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
          }`}
        >
          {d.label}
        </button>
      ))}
      {dates.length > visibleDates.length && (
        <span className="text-[11px] text-slate-500">共 {dates.length} 期</span>
      )}
    </div>
  );

  if (!bootReady) return <Loading />;

  if (!report) {
    return (
      <div>
        <PageHeader
          title="科技日报"
          description="系统每日 08:00 自动生成 · TOP 热点 / 事件摘要 / 行业变化 / 重点来源"
        >
          {dateChips}
        </PageHeader>
        {noDates ? (
          <div className="card py-16 text-center">
            <div className="text-sm text-slate-500 mb-4">
              暂无可用日报 · 日报由系统每日 08:00 自动生成，也可在「运维监控」页手动触发
            </div>
            <button
              onClick={() => void retryLoadReports()}
              disabled={retrying}
              className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25 disabled:opacity-40"
            >
              {retrying ? '重新加载中…' : '重新加载'}
            </button>
          </div>
        ) : loadFailed ? (
          <div className="card py-16 text-center text-sm text-slate-500">
            该日期暂无日报（{effectiveDateKey}）· 可在「运维监控」页手动触发生成
          </div>
        ) : (
          <Loading />
        )}
      </div>
    );
  }

  // 收录总数：优先用快照里的当日窗口文章总数（历史快照缺省时回退收录条数）
  const collectedTotal = report.articlesTotal ?? report.quickNews.length;

  // 统计窗口口径：日报日期为 UTC 自然日，对应北京时间当日 08:00 → 次日 08:00
  const winLabel = (() => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(report.date);
    if (!m) return null;
    const end = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) + 86400000);
    const mm = String(end.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(end.getUTCDate()).padStart(2, '0');
    return `统计窗口（北京时间）${report.date.slice(5).replace('-', '/')} 08:00 – ${mm}/${dd} 08:00`;
  })();

  // 环比：与上一期对比（新进热点 / 收录篇数变化），上一期快照在缓存中才有
  const prevKey = dates.find((d) => d.key < report.date)?.key ?? null;
  const prevReport = prevKey ? peekDailyReport(prevKey) : null;
  const newEventCount = prevReport
    ? report.topEvents.filter((e) => !prevReport.topEvents.some((p) => p.id === e.id)).length
    : 0;
  const articleDelta =
    prevReport?.articlesTotal != null && report.articlesTotal != null
      ? report.articlesTotal - prevReport.articlesTotal
      : null;
  const deltaParts: string[] = [];
  if (prevReport && newEventCount > 0) deltaParts.push(`新进热点 ${newEventCount} 个`);
  if (articleDelta !== null && articleDelta !== 0) {
    deltaParts.push(`收录 ${articleDelta > 0 ? '+' : ''}${articleDelta} 篇`);
  }

  // 重点来源：新快照按频道统计（source 粒度只有 IT之家/快科技，四频道共用 source）；
  // 历史快照无 channel_stats 时回退 source_stats
  const originStats: { key: string; count: number }[] = report.channelStats?.length
    ? report.channelStats.map((c) => ({ key: c.channel, count: c.count }))
    : report.sourceStats.map((s) => ({ key: s.source, count: s.count }));

  // 本地降级模式下 tokens/cost 恒为 0：这些"调用"是本地算法运行，不是 API 计费调用
  const localAi =
    !!report.aiCost &&
    report.aiCost.tokens === 0 &&
    report.aiCost.cost === 0 &&
    report.aiCost.calls > 0;

  const generatedAtLabel = report.generatedAt
    ? new Date(report.generatedAt).toLocaleString('zh-CN', { hour12: false })
    : null;

  return (
    <div>
      <PageHeader
        title="科技日报"
        description="系统每日 08:00 自动生成 · TOP 热点 / 事件摘要 / 行业变化 / 重点来源"
      >
        {dateChips}
      </PageHeader>

      {/* 日报刊头 */}
      <div className="card p-6 mb-4 relative overflow-hidden">
        <div className="absolute top-0 right-0 w-64 h-64 bg-accent/5 rounded-full blur-3xl -translate-y-1/2 translate-x-1/3 pointer-events-none" />
        <div className="relative flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5 mb-2 flex-wrap">
              <span className="chip bg-accent/10 text-accent border border-accent/30">
                <Bot className="h-3 w-3" /> 自动生成
              </span>
              <span className="chip bg-radar-panel text-slate-400 border border-radar-border">
                第 {report.issueNo} 期
              </span>
              {winLabel && (
                <span className="chip bg-radar-panel text-slate-400 border border-radar-border">
                  {winLabel}
                </span>
              )}
              {deltaParts.length > 0 && (
                <span className="chip bg-emerald-500/10 text-emerald-300 border border-emerald-400/20">
                  <TrendingUp className="h-3 w-3" /> 较上期 {deltaParts.join(' · ')}
                </span>
              )}
            </div>
            <h2 className="text-2xl font-semibold text-slate-100">
              华军软件园 · 科技日报 {report.date}
            </h2>
            <p className="text-sm text-slate-400 mt-1.5">
              {report.topEvents.length > 0
                ? `当日 ${report.topEvents.length} 个热点事件持续发酵，共聚合 ${collectedTotal} 篇报道。`
                : `当日暂无聚合事件，共收录 ${collectedTotal} 篇重点报道。`}
            </p>
          </div>
          <div className="flex gap-6 text-right">
            <div>
              <div className="text-2xl font-bold text-slate-100 tabular-nums">{collectedTotal}</div>
              <div className="text-[11px] text-slate-500 mt-0.5">收录报道</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-amber-300 tabular-nums">{report.topEvents.length}</div>
              <div className="text-[11px] text-slate-500 mt-0.5">热点事件</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-cyan-300 tabular-nums">{originStats.length}</div>
              <div className="text-[11px] text-slate-500 mt-0.5">来源栏目</div>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* 左列 */}
        <div className="lg:col-span-2 space-y-4">
          {/* 当日 TOP 热点 */}
          <section className="card p-5">
            <div className="flex items-center gap-2 mb-4">
              <Flame className="h-4 w-4 text-orange-400" />
              <h2 className="text-sm font-semibold text-slate-200">当日 TOP 热点</h2>
            </div>
            {report.topEvents.length === 0 ? (
              <p className="text-xs text-slate-500 py-4">当日暂无聚合热点事件，请查看下方「热点速览」。</p>
            ) : (
              <div className="space-y-3">
                {report.topEvents.map((e, i) => (
                  <div
                    key={e.id}
                    role="button"
                    tabIndex={0}
                    aria-label={`查看事件「${e.title}」详情`}
                    onClick={() => navigate(`/events/${e.id}`)}
                    onKeyDown={activateOnKey(() => navigate(`/events/${e.id}`))}
                    className="flex gap-3.5 p-3 rounded-lg border border-radar-border bg-radar-panel/60 hover:border-accent/30 cursor-pointer transition-colors"
                  >
                    <RankBadge rank={i + 1} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <h3 className="text-sm font-medium text-slate-100">{e.title}</h3>
                        <EventStatusTag status={e.status} />
                      </div>
                      <p className="text-xs text-slate-400 leading-relaxed line-clamp-2">{e.summary}</p>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="text-sm font-bold text-orange-300 tabular-nums">
                        {formatScore(e.hotScore)}
                      </div>
                      <div className="text-[10px] text-slate-500">热度</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* 行业变化 */}
          {report.industryChanges.length > 0 && (
            <section className="card p-5">
              <div className="flex items-center gap-2 mb-4">
                <TrendingUp className="h-4 w-4 text-emerald-400" />
                <h2 className="text-sm font-semibold text-slate-200">行业变化</h2>
              </div>
              <ul className="space-y-2.5">
                {report.industryChanges.map((c, i) => (
                  <li key={i} className="flex gap-2.5 text-[13px] text-slate-300 leading-relaxed">
                    <span className="mt-[7px] h-1.5 w-1.5 rounded-full bg-emerald-400/70 shrink-0" />
                    {c}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* 热点速览 */}
          <section className="card p-5">
            <div className="flex items-center gap-2 mb-4">
              <Newspaper className="h-4 w-4 text-sky-400" />
              <h2 className="text-sm font-semibold text-slate-200">热点速览</h2>
              <span className="text-[11px] text-slate-500">点击查看 AI 摘要</span>
            </div>
            <div className="divide-y divide-radar-border">
              {report.quickNews.map((a) => (
                <div
                  key={a.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看「${a.title}」的 AI 摘要`}
                  onClick={() => setSelected(a)}
                  onKeyDown={activateOnKey(() => setSelected(a))}
                  className="py-3 first:pt-0 last:pb-0 cursor-pointer group"
                >
                  <h3 className="text-[14px] text-slate-200 group-hover:text-accent transition-colors leading-snug mb-1">
                    {a.title}
                  </h3>
                  <div className="flex items-center gap-2.5 flex-wrap text-xs text-slate-500">
                    <SourceBadge source={a.source} size="sm" />
                    <span>{clockTime(a.publishTime)}</span>
                    {a.summary.summary_short && (
                      <span className="inline-flex items-center gap-1">
                        <AiBadge label="" />
                        {a.summary.summary_short.length > 42
                          ? `${a.summary.summary_short.slice(0, 42)}…`
                          : a.summary.summary_short}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        {/* 右列 */}
        <div className="space-y-4">
          {/* 当日热榜 TOP5 */}
          <section className="card p-5">
            <div className="flex items-center gap-2 mb-4">
              <FileBarChart className="h-4 w-4 text-amber-400" />
              <h2 className="text-sm font-semibold text-slate-200">当日热榜 TOP 5</h2>
            </div>
            <div className="space-y-3">
              {report.topArticles.map((a, i) => (
                <div
                  key={a.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看「${a.title}」详情`}
                  onClick={() => setSelected(a)}
                  onKeyDown={activateOnKey(() => setSelected(a))}
                  className="flex gap-2.5 cursor-pointer group"
                >
                  <span
                    className={`text-xs font-bold w-4 shrink-0 ${
                      i === 0 ? 'text-amber-300' : i === 1 ? 'text-slate-300' : i === 2 ? 'text-orange-300' : 'text-slate-500'
                    }`}
                  >
                    {i + 1}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[13px] text-slate-300 group-hover:text-accent transition-colors leading-snug line-clamp-2">
                      {a.title}
                    </div>
                    <div className="text-[10px] text-slate-500 mt-0.5">
                      {a.source} · 热度 {formatScore(a.hotScore)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* 重点来源（按频道统计） */}
          {originStats.length > 0 && (
            <section className="card p-5">
              <div className="flex items-center gap-2 mb-4">
                <Newspaper className="h-4 w-4 text-violet-400" />
                <h2 className="text-sm font-semibold text-slate-200">重点来源</h2>
                <span className="text-[11px] text-slate-500">按频道统计</span>
              </div>
              <div className="space-y-3">
                {originStats.map((s) => {
                  const max = originStats[0].count || 1;
                  return (
                    <div key={s.key} className="flex items-center gap-3">
                      <ChannelTag channel={s.key} />
                      <div className="flex-1 h-1.5 rounded-full bg-radar-border overflow-hidden">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-violet-500 to-fuchsia-400"
                          style={{ width: `${(s.count / max) * 100}%` }}
                        />
                      </div>
                      <span className="text-xs text-slate-500 tabular-nums">{s.count} 篇</span>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {/* 当日 AI 成本（日报 content.ai_cost，有则显示；历史日报无此字段自动隐藏） */}
          {report.aiCost && (
            <section className="card p-5">
              <div className="flex items-center gap-2 mb-3">
                <Coins className="h-4 w-4 text-amber-400" />
                <h2 className="text-sm font-semibold text-slate-200">当日 AI 成本</h2>
              </div>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div>
                  <div className="text-lg font-bold text-slate-100 tabular-nums">{report.aiCost.calls}</div>
                  <div className="text-[10px] text-slate-500 mt-0.5">调用次数</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-slate-100 tabular-nums">{report.aiCost.tokens}</div>
                  <div className="text-[10px] text-slate-500 mt-0.5">Tokens</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-amber-300 tabular-nums">¥{report.aiCost.cost.toFixed(2)}</div>
                  <div className="text-[10px] text-slate-500 mt-0.5">成本（元）</div>
                </div>
              </div>
              {localAi && (
                <p className="text-[10px] text-slate-500 mt-2.5 text-center">
                  本地降级算法运行中 · 无 API 成本
                </p>
              )}
            </section>
          )}

          {/* 日报说明 */}
          <section className="card p-5">
            <div className="flex items-center gap-2 mb-3">
              <CalendarDays className="h-4 w-4 text-slate-400" />
              <h2 className="text-sm font-semibold text-slate-200">生成说明</h2>
            </div>
            <ul className="space-y-2 text-xs text-slate-500 leading-relaxed">
              <li>· 本报告由系统于每日 08:00 自动生成</li>
              <li>· 事件摘要基于 event 聚合结果与 AI 总结交叉生成</li>
              <li>· 热度算法：HotScore = Engagement × TimeDecay，页面内为当日归一化相对值（满分 100）</li>
              {originStats.length > 0 && (
                <li>· 数据来源栏目：{originStats.map((s) => s.key).join(' / ')}</li>
              )}
              {generatedAtLabel && <li>· 本期生成于 {generatedAtLabel}</li>}
            </ul>
          </section>
        </div>
      </div>

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
