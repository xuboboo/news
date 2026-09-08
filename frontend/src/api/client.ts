import { ArticleWithScore, HotEvent } from '../types';
import { toDateKey } from '../utils/format';
import { getAuthStatus, onAuthChange, setAnon } from '../auth';

/**
 * API 层（对接后端 /api/v2，见 PRD 第 12 节）：
 * - API 模式：从真实后端拉取数据并水合内存结构（nginx 反代 /api）
 * - 兜底模式：后端不可达时保持空数据 + 15s 周期重连（2026-09 移除内置假数据：
 *   演示文章与真实数据源口径不符，宁可空态也不展示编造内容；页面自带「暂无数据」空状态）
 *
 * 对应后端接口：
 *   GET /api/v2/articles/latest?limit=      → 最新/全量文章
 *   GET /api/v2/events                      → 事件（含成员文章）
 *   GET /api/v2/report/dates + daily        → 日报
 *   GET /api/v2/article/{id}/summary        → AI 摘要（内嵌于 article.summary）
 */

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE ?? '';
export let apiMode = false;

/** 应用引导状态：pending = 首屏骨架；ready = 数据就绪（API 或空态兜底） */
export let bootstrapState: 'pending' | 'ready' = 'pending';

export interface EventWithScore extends HotEvent {
  rawScore: number;
  hotScore: number;
  articles: ArticleWithScore[];
  firstTime: Date;
  lastTime: Date;
}

// ———————————————————— 可变数据源（页面直接读取） ————————————————————

export let ARTICLES_WITH_SCORE: ArticleWithScore[] = [];
export let EVENTS_WITH_SCORE: EventWithScore[] = [];

const reportCache = new Map<string, any>();
export let REPORT_DATES: string[] = [];

// ———————————————————— API 水合 ————————————————————

async function fetchJSON(path: string, timeoutMs = 4000): Promise<any> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    // no-store：轮询接口必须拿实时数据，避免浏览器启发式缓存返回陈旧 JSON
    const resp = await fetch(`${API_BASE}/api/v2${path}`, { signal: ctrl.signal, cache: 'no-store' });
    if (!resp.ok) {
      const err = new Error(`HTTP ${resp.status}`) as Error & { status?: number };
      err.status = resp.status;
      // 401 = 会话失效/未登录：全局反映到登录态，由 RequireAuth 跳转登录页
      // （而不是静默降级演示数据——未登录用户绝不能看到"假数据糊脸"）
      if (resp.status === 401) setAnon();
      throw err;
    }
    // content-type 校验：无 /api 反代的静态预览站会把 SPA fallback 的 HTML 以 200 返回，
    // 若只查 resp.ok 会把 HTML 当 JSON 解析失败后静默落入演示模式
    const contentType = resp.headers.get('content-type') ?? '';
    if (!contentType.includes('application/json')) {
      throw new Error(`非 JSON 应答（content-type: ${contentType || '缺失'}）`);
    }
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

const EMPTY_SUMMARY = { summary_short: '', summary_long: '', key_points: [], tags: [], model: '' };

function mapApiArticle(a: any): ArticleWithScore {
  const publishTime = new Date(a.publish_time);
  return {
    id: String(a.id),
    title: a.title,
    channel: a.channel,
    source: a.source,
    url: a.url ?? '',
    publishOffsetMin: Math.max(0, (Date.now() - publishTime.getTime()) / 60000),
    views: a.views ?? 0,
    comments: a.comments ?? 0,
    shares: a.shares ?? 0,
    likes: a.likes ?? 0,
    summary: a.summary
      ? {
          summary_short: a.summary.summary_short ?? '',
          key_points: a.summary.key_points ?? [],
          tags: a.summary.tags ?? [],
          model: a.summary.model ?? '',
        }
      : { ...EMPTY_SUMMARY },
    publishTime,
    rawScore: a.hot_score ?? 0,
    hotScore: a.hot_score ?? 0,
  };
}

function mapApiTimeline(t: any): { time: Date; text: string; source?: string } {
  return {
    time: t.time ? new Date(t.time) : new Date(),
    text: t.text ?? '',
    source: t.source,
  };
}

/** 日报 brief → 展示结构：文章已滚出「最近400篇」窗口或被清理时，
 *  用日报快照里自带的书目信息兜底渲染，历史日报不再缺项。 */
function briefToArticle(brief: any): ArticleWithScore {
  return {
    id: String(brief.id),
    title: brief.title ?? '',
    channel: brief.channel ?? '智能时代',
    source: brief.source ?? 'IT之家',
    url: brief.url ?? '',
    publishOffsetMin: 0,
    views: 0,
    comments: 0,
    shares: 0,
    likes: 0,
    summary: {
      summary_short: brief.summary_short ?? '',
      key_points: [],
      tags: brief.tags ?? [],
      model: '',
    },
    publishTime: brief.publish_time ? new Date(brief.publish_time) : new Date(0),
    rawScore: brief.hot_score ?? 0,
    hotScore: brief.hot_score ?? 0,
  };
}

function mapApiEvent(ev: any, articleMap: Map<string, ArticleWithScore>): EventWithScore {
  const articles = (ev.articles ?? [])
    .map((a: any) => articleMap.get(String(a.id)))
    .filter(Boolean) as ArticleWithScore[];
  const sorted = [...articles].sort((x, y) => x.publishTime.getTime() - y.publishTime.getTime());
  return {
    id: `e${ev.id}`,
    title: ev.title,
    summary: ev.summary ?? '',
    impacts: ev.impacts ?? [],
    status: ev.status ?? '持续关注',
    articleIds: (ev.articles ?? []).map((a: any) => String(a.id)),
    timeline: (ev.timeline ?? []).map(mapApiTimeline).sort(
      (x: { time: Date }, y: { time: Date }) => y.time.getTime() - x.time.getTime()
    ),
    articles: sorted,
    rawScore: ev.hot_score ?? 0,
    hotScore: ev.hot_score ?? 0,
    firstTime: sorted[0]?.publishTime ?? new Date(),
    lastTime: sorted[sorted.length - 1]?.publishTime ?? new Date(),
  };
}

export interface DailyReportView {
  date: string;
  issueNo: number;
  topEvents: EventWithScore[];
  quickNews: ArticleWithScore[];
  industryChanges: string[];
  sourceStats: { source: string; count: number }[];
  topArticles: ArticleWithScore[];
  /** 今日 AI 成本（后端日报 content.ai_cost；本地兜底模式无此字段） */
  aiCost?: { calls: number; tokens: number; cost: number };
  /** 当日窗口文章总数（content.articles_total；历史快照缺省时回退收录条数） */
  articlesTotal?: number;
  /** 频道维度分布（content.channel_stats；历史快照缺省时回退 sourceStats） */
  channelStats?: { channel: string; count: number }[];
  /** 快照生成时刻（ISO；历史快照缺省） */
  generatedAt?: string;
  /** 今日综述（content.overview；历史快照缺省时页面回退模板句） */
  overview?: string;
}

function mapApiReport(date: string, data: any, articleMap: Map<string, ArticleWithScore>): DailyReportView {
  return {
    date,
    issueNo: data.issue_no ?? 1,
    topEvents: (data.top_events ?? []).map((e: any) => ({
      id: `e${e.id}`,
      title: e.title,
      summary: e.summary ?? '',
      impacts: [],
      status: e.status ?? '持续关注',
      articleIds: [],
      timeline: [],
      hotScore: e.hot_score ?? 0,
      rawScore: e.hot_score ?? 0,
      articles: [],
      firstTime: new Date(),
      lastTime: new Date(),
    })) as unknown as EventWithScore[],
    quickNews: (data.quick_news ?? [])
      .map((n: any) => articleMap.get(String(n.id)) ?? briefToArticle(n)) as ArticleWithScore[],
    industryChanges: data.industry_changes ?? [],
    sourceStats: data.source_stats ?? [],
    topArticles: (data.top_articles ?? [])
      .map((n: any) => articleMap.get(String(n.id)) ?? briefToArticle(n)) as ArticleWithScore[],
    aiCost: data.ai_cost ?? undefined,
    articlesTotal: typeof data.articles_total === 'number' ? data.articles_total : undefined,
    channelStats: Array.isArray(data.channel_stats) ? data.channel_stats : undefined,
    generatedAt: typeof data.generated_at === 'string' ? data.generated_at : undefined,
    overview: typeof data.overview === 'string' && data.overview ? data.overview : undefined,
  };
}

/** 后端不可达时保持本地引擎；401（未登录）不算后端不可达——登录后由下面的
 *  onAuthChange 监听器重启引导。 */
export async function bootstrapFromApi(): Promise<boolean> {
  try {
    await fetchJSON('/health', 3500);

    const latest = await fetchJSON('/articles/latest?limit=400', 10000);
    const hydrated: ArticleWithScore[] = (latest.items ?? []).map(mapApiArticle);
    const articleMap = new Map<string, ArticleWithScore>(
      hydrated.map((a) => [a.id, a] as [string, ArticleWithScore])
    );

    const events = await fetchJSON('/events?limit=100', 8000);
    const mappedEvents: EventWithScore[] = (events.items ?? []).map((e: any) =>
      mapApiEvent(e, articleMap)
    );

    // ———— 全部请求成功，一次性提交 ————
    apiMode = true;
    ARTICLES_WITH_SCORE = hydrated;
    EVENTS_WITH_SCORE = mappedEvents;
    prevArticleIds = hydrated.map((a) => a.id);
    prevEventIds = mappedEvents.map((e) => e.id);
    LAST_SYNC = Date.now();
    return true;
  } catch {
    apiMode = false;
    return false;
  }
}

// 登录成功（anon→authed）后重启数据引导：登录前 401 失败的首次引导在此补偿
let authWatchInstalled = false;
function installAuthWatch(): void {
  if (authWatchInstalled) return;
  authWatchInstalled = true;
  let prev = getAuthStatus();
  onAuthChange((next) => {
    if (prev === 'anon' && next === 'authed') {
      void (async () => {
        const ok = await bootstrapFromApi();
        if (ok) {
          startAutoRefresh(refreshIntervalMs);
          setTimeout(() => {
            void loadReports(true);
          }, 0);
        }
        bootstrapState = 'ready';
        notify();
      })();
    }
    prev = next;
  });
}

/**
 * 引导入口：先拉必要数据（health/文章/事件），就绪后立即通知首帧；
 * 日报数据非首屏必需，推迟到下一帧再拉，不阻塞页面展示。
 * 后端不可达时保持本地引擎并启动周期性重连，恢复后无缝切换 API 模式。
 */
export async function startBootstrap(): Promise<void> {
  installAuthWatch();
  await bootstrapFromApi();
  bootstrapState = 'ready';
  notify();
  if (apiMode) {
    setTimeout(() => {
      void loadReports();
    }, 0);
  } else {
    ensureReconnect();
  }
}

// ———————————————————— 后端重连（本地兜底模式专用） ————————————————————

// 15s：后端重启/部署窗口通常只有几十秒，快速重连让演示横幅尽快消失
const RECONNECT_INTERVAL_MS = 15000;
let reconnectTimer: ReturnType<typeof setInterval> | null = null;
let reconnecting = false;

function ensureReconnect(): void {
  if (reconnectTimer) return;
  reconnectTimer = setInterval(() => {
    // 未登录时 401 必然失败，不空转（登录成功后由 auth 监听器重启引导）
    if (reconnecting || getAuthStatus() === 'anon') return;
    reconnecting = true;
    void bootstrapFromApi()
      .then((ok) => {
        if (ok) {
          if (reconnectTimer) {
            clearInterval(reconnectTimer);
            reconnectTimer = null;
          }
          startAutoRefresh(refreshIntervalMs);
          void loadReports(true);
          notify();
        }
      })
      .finally(() => {
        reconnecting = false;
      });
  }, RECONNECT_INTERVAL_MS);
}

/** 拉取日报日期 + 最近 7 天日报缓存；失败静默（DailyReport 页可按需重试）。
 *  force=false 时 minIntervalMs 内已成功加载过或在途中则跳过（启动与日报页挂载都会调用，
 *  避免重复全量拉取；日报页传更长间隔，让 08:00 新生成的日报在常驻页面上低频自动出现）。 */
let lastReportsLoad = 0;
let loadingReports = false;
/** 是否至少完整尝试过一次日报加载（无论成败）；日报页据此区分「加载中」与「确实无日报」 */
export let reportsLoaded = false;

export async function loadReports(force = false, minIntervalMs = 60000): Promise<void> {
  if (!apiMode) return;
  if (!force && (loadingReports || Date.now() - lastReportsLoad < minIntervalMs)) return;
  loadingReports = true;
  try {
    const dates = await fetchJSON('/report/dates', 6000);
    REPORT_DATES = (dates.items ?? []).map((d: any) => d.date);
    const articleMap = new Map<string, ArticleWithScore>(
      ARTICLES_WITH_SCORE.map((a) => [a.id, a] as [string, ArticleWithScore])
    );
    await Promise.all(
      REPORT_DATES.slice(0, 7).map(async (date) => {
        try {
          const r = await fetchJSON(`/report/daily?date=${date}`, 8000);
          reportCache.set(date, mapApiReport(date, r, articleMap));
        } catch {
          /* 单日失败不影响整体 */
        }
      })
    );
    lastReportsLoad = Date.now();
    notify();
  } catch {
    /* 日报加载失败不影响主流程 */
  } finally {
    loadingReports = false;
    if (!reportsLoaded) {
      reportsLoaded = true;
      notify();
    }
  }
}

// ———————————————————— 增量刷新（让页面活起来） ————————————————————

let prevArticleIds: string[] = [];
let prevEventIds: string[] = [];

/** 连续同步失败次数；≥2 时置 lastSyncError，成功后清零 */
let syncFailures = 0;
/** 最近一轮同步失败提示（null = 正常）；供头部状态条展示 */
export let lastSyncError: string | null = null;

/** 最近一次成功同步时间戳（ms）；null = 尚未同步过 */
export let LAST_SYNC: number | null = null;
/** 最近一轮刷新新到的文章/事件 ID（展示 NEW 标记，下一轮刷新时替换） */
export let FRESH_ARTICLE_IDS = new Set<string>();
export let FRESH_EVENT_IDS = new Set<string>();

const updateListeners = new Set<() => void>();

/** 订阅数据更新（auto-refresh 拉到新数据时触发）；返回取消订阅函数 */
export function onDataUpdate(cb: () => void): () => void {
  updateListeners.add(cb);
  return () => {
    updateListeners.delete(cb);
  };
}

function notify() {
  updateListeners.forEach((cb) => {
    try {
      cb();
    } catch {
      /* 单个页面渲染失败不影响其他订阅者 */
    }
  });
}

/** 拉取一次最新文章 + 事件并水合；标记本轮新增的 ID。失败静默（下轮再试）。
 *  同一时刻只允许一轮在途（可见性恢复补拉与 60s 轮询可能撞车，防止状态交叉覆盖）。 */
let refreshing = false;

export async function refreshFromApi(): Promise<boolean> {
  if (!apiMode || refreshing) return false;
  refreshing = true;
  try {
    // 两个请求并行且全部成功后才提交（原子化：避免文章已换、事件未换的混血一轮）
    const [latest, events] = await Promise.all([
      fetchJSON('/articles/latest?limit=400', 10000),
      fetchJSON('/events?limit=100', 8000),
    ]);
    const hydrated: ArticleWithScore[] = (latest.items ?? []).map(mapApiArticle);
    const articleMap = new Map<string, ArticleWithScore>(
      hydrated.map((a) => [a.id, a] as [string, ArticleWithScore])
    );
    const mapped: EventWithScore[] = (events.items ?? []).map((e: any) => mapApiEvent(e, articleMap));

    FRESH_ARTICLE_IDS = new Set(
      hydrated
        .map((a) => a.id)
        .filter((id) => prevArticleIds.length > 0 && !prevArticleIds.includes(id))
    );
    FRESH_EVENT_IDS = new Set(
      mapped
        .map((e) => e.id)
        .filter((id) => prevEventIds.length > 0 && !prevEventIds.includes(id))
    );
    ARTICLES_WITH_SCORE = hydrated;
    prevArticleIds = hydrated.map((a) => a.id);
    EVENTS_WITH_SCORE = mapped;
    prevEventIds = mapped.map((e) => e.id);

    LAST_SYNC = Date.now();
    syncFailures = 0;
    if (lastSyncError !== null) {
      lastSyncError = null;
    }
    notify();
    return true;
  } catch {
    syncFailures += 1;
    if (syncFailures >= 2 && lastSyncError === null) {
      lastSyncError = '同步失败 · 将自动重试';
      notify();
    }
    return false;
  } finally {
    refreshing = false;
  }
}

let refreshTimer: ReturnType<typeof setInterval> | null = null;
let refreshIntervalMs = 60000;

function handleVisibilityChange(): void {
  // 页面隐藏时暂停轮询，回到前台立即补拉一次并恢复计时
  if (typeof document === 'undefined') return;
  if (document.hidden) {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  } else if (apiMode) {
    void refreshFromApi();
    if (!refreshTimer) {
      refreshTimer = setInterval(() => {
        void refreshFromApi();
      }, refreshIntervalMs);
    }
  }
}

/** 启动周期性增量刷新（仅 apiMode 生效）；页面隐藏时自动暂停 */
export function startAutoRefresh(intervalMs = 60000): void {
  refreshIntervalMs = intervalMs;
  if (refreshTimer || !apiMode) return;
  refreshTimer = setInterval(() => {
    void refreshFromApi();
  }, intervalMs);
  document.addEventListener('visibilitychange', handleVisibilityChange);
}

// ———————————————————— 页面使用的查询函数 ————————————————————

export function eventById(id: string): EventWithScore | undefined {
  // 兼容 "31" 与 "e31" 两种路由参数（API 模式下事件 ID 带 e 前缀）
  const key = id.startsWith('e') ? id : `e${id}`;
  return EVENTS_WITH_SCORE.find((e) => e.id === id || e.id === key);
}

export function eventsForArticle(articleId: string): EventWithScore[] {
  return EVENTS_WITH_SCORE.filter((e) => e.articleIds.includes(articleId));
}

/** 按需拉取单个事件（/events/{id}）：列表窗口（limit=100）外的事件详情兜底。
 *  成员文章用响应内嵌数据水合（_article_dict 自带 summary），不经全局文章缓存。 */
export async function fetchEventById(id: string): Promise<EventWithScore | null> {
  if (!apiMode) return null;
  const rawId = id.startsWith('e') ? id.slice(1) : id;
  try {
    const raw = await fetchJSON(`/events/${rawId}`, 8000);
    const articleMap = new Map<string, ArticleWithScore>(
      (raw.articles ?? []).map(
        (a: any) => [String(a.id), mapApiArticle(a)] as [string, ArticleWithScore]
      )
    );
    return mapApiEvent(raw, articleMap);
  } catch {
    return null;
  }
}

export interface DailyIssue {
  key: string;
  label: string;
  offsetMin: number;
}

function friendlyDateLabel(key: string): string {
  const todayKey = toDateKey(Date.now());
  const yesterdayKey = toDateKey(Date.now() - 86400000);
  return key === todayKey ? '今天' : key === yesterdayKey ? '昨天' : key.slice(5).replace('-', '/');
}

/** 可选日报日期：来自后端返回（loadReports 完成前为空） */
export function availableDates(): DailyIssue[] {
  return REPORT_DATES.map((key) => ({ key, label: friendlyDateLabel(key), offsetMin: 0 }));
}

/** 空日报视图：后端未连接时的兜底（页面渲染空态，不再展示本地推导的假数据） */
function emptyReport(dateKey: string): DailyReportView {
  return {
    date: dateKey,
    issueNo: 1,
    topEvents: [],
    quickNews: [],
    industryChanges: [],
    sourceStats: [],
    topArticles: [],
  };
}

/** 读取日报：API 模式仅命中缓存；未连接时返回空视图。未命中返回 null（配合 fetchDailyReport 异步拉取）。 */
export function peekDailyReport(dateKey: string): DailyReportView | null {
  if (apiMode) return reportCache.get(dateKey) ?? null;
  return emptyReport(dateKey);
}

/** 按需拉取日报：API 模式实时请求并写入缓存。拉取失败返回 null。 */
export async function fetchDailyReport(dateKey: string): Promise<DailyReportView | null> {
  if (apiMode) {
    try {
      const r = await fetchJSON(`/report/daily?date=${dateKey}`, 8000);
      const articleMap = new Map<string, ArticleWithScore>(
        ARTICLES_WITH_SCORE.map((a) => [a.id, a] as [string, ArticleWithScore])
      );
      const view = mapApiReport(dateKey, r, articleMap);
      reportCache.set(dateKey, view);
      if (!REPORT_DATES.includes(dateKey)) REPORT_DATES = [...REPORT_DATES, dateKey];
      return view;
    } catch {
      return null;
    }
  }
  return emptyReport(dateKey);
}
