/**
 * 文章草稿列表：「选题 → AI 生成 → 编辑 → 复制发布」链路的入口页。
 * 数据走 /api/v2/drafts（与全局 client.ts 水合体系独立，按需拉取）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, PenSquare, Sparkles, Trash2, Zap } from 'lucide-react';
import {
  listDrafts,
  deleteDraft,
  type DraftItem,
  type DraftListPage,
  type DraftStatus,
} from '../api/drafts';
import { Loading, PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { timeAgo } from '../utils/format';
import { listSiteDrafts, deleteSiteDraft } from '../api/siteDrafts';

const PAGE_SIZE = 50;

const STATUS_FILTERS: { key: DraftStatus | ''; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'generating', label: '生成中' },
  { key: 'ready', label: '就绪' },
  { key: 'failed', label: '失败' },
];

export const TIER_LABELS: Record<string, string> = {
  brief: '快讯版',
  depth: '深度版',
};

export function DraftStatusChip({ status }: { status: DraftStatus }) {
  const style =
    status === 'generating'
      ? 'bg-cyan-500/15 text-cyan-300 border border-cyan-400/30 animate-pulse'
      : status === 'ready'
        ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-400/30'
        : 'bg-red-500/15 text-red-300 border border-red-400/30';
  const label = status === 'generating' ? '生成中' : status === 'ready' ? '就绪' : '失败';
  return <span className={`chip text-[10px] ${style}`}>{label}</span>;
}

export default function Drafts({ channel = 'wechat' }: { channel?: 'wechat' | 'site' }) {
  const isSite = channel === 'site';
  const list = isSite ? listSiteDrafts : listDrafts;
  const deleteItem = isSite ? deleteSiteDraft : deleteDraft;
  const basePath = isSite ? '/site-drafts' : '/drafts';
  const navigate = useNavigate();
  usePageTitle(isSite ? '网站资讯稿' : '公众号草稿');
  const [statusFilter, setStatusFilter] = useState<DraftStatus | ''>('');
  // loaded 与筛选键绑定：loading 派生（status 不匹配即加载中），effect 内零同步 setState
  const [loaded, setLoaded] = useState<{ status: DraftStatus | ''; data: DraftListPage } | null>(null);
  const [loadMoreRun, setLoadMoreRun] = useState<{ id: number; status: DraftStatus | '' } | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const loadMoreSequence = useRef(0);
  const [error, setError] = useState('');
  const requestSequence = useRef(0);
  const loadedRef = useRef(loaded);
  const statusFilterRef = useRef(statusFilter);
  const mountedRef = useRef(false);
  useEffect(() => { loadedRef.current = loaded; }, [loaded]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestSequence.current += 1;
      loadMoreSequence.current += 1;
    };
  }, []);
  const loading = loaded === null || loaded.status !== statusFilter;
  const page = loaded && loaded.status === statusFilter ? loaded.data : { items: [], total: 0 };

  const fetchFirst = useCallback((status: DraftStatus | '') => {
    const requestId = ++requestSequence.current;
    const previousPage = loadedRef.current;
    const oldestLoadedId = previousPage?.status === status
      ? previousPage.data.items[previousPage.data.items.length - 1]?.id
      : undefined;
    return list(status, PAGE_SIZE, 0)
      .then(async (data) => {
        if (requestId !== requestSequence.current) return;
        // 刷新覆盖已加载到的游标范围；不把旧分页对象当作最新状态继续保留。
        const refreshed = [...data.items];
        let lastPage = data.items;
        let cursor = lastPage[lastPage.length - 1]?.id;
        while (oldestLoadedId !== undefined && cursor !== undefined &&
          cursor > oldestLoadedId && lastPage.length === PAGE_SIZE) {
          const next = await list(status, PAGE_SIZE, 0, cursor);
          if (requestId !== requestSequence.current) return;
          const seen = new Set(refreshed.map((item) => item.id));
          refreshed.push(...next.items.filter((item) => !seen.has(item.id)));
          data.total = next.total;
          lastPage = next.items;
          const nextCursor = lastPage[lastPage.length - 1]?.id;
          if (nextCursor === undefined || nextCursor >= cursor) break;
          cursor = nextCursor;
        }
        setLoaded(() => {
          const previous = refreshed;
          const firstIds = new Set(data.items.map((item) => item.id));
          const older = previous
            .filter((item) => !firstIds.has(item.id) && (!status || item.status === status))
            .sort((a, b) => b.id - a.id);
          return { status, data: { ...data, items: [...data.items, ...older] } };
        });
        setError('');
      })
      .catch((e: Error) => {
        if (requestId !== requestSequence.current) return;
        setLoaded({ status, data: { items: [], total: 0 } });
        setError(e.message || String(e));
      });
  }, [list]);

  // 首屏与筛选切换（setState 仅出现在回调中）
  useEffect(() => {
    void fetchFirst(statusFilter);
  }, [statusFilter, fetchFirst]);

  // 有生成中的草稿时轻量轮询（5s），完成后自动停
  useEffect(() => {
    if (!page.items.some((d) => d.status === 'generating')) return;
    const timer = window.setInterval(() => {
      void fetchFirst(statusFilter);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [page.items, statusFilter, fetchFirst]);

  const onLoadMore = () => {
    const requestedStatus = statusFilter;
    const beforeId = page.items[page.items.length - 1]?.id;
    if (loadMoreRun?.status === requestedStatus || beforeId === undefined) return;
    const runId = ++loadMoreSequence.current;
    const pageRequestId = requestSequence.current;
    setLoadMoreRun({ id: runId, status: requestedStatus });
    list(requestedStatus, PAGE_SIZE, 0, beforeId)
      .then((p) => {
        if (runId !== loadMoreSequence.current || pageRequestId !== requestSequence.current) return;
        setLoaded((prev) => {
          if (!prev || prev.status !== requestedStatus) return prev;
          const seen = new Set(prev.data.items.map((item) => item.id));
          const additions = p.items.filter((item) => !seen.has(item.id));
          const items = [...prev.data.items, ...additions].sort((a, b) => b.id - a.id);
          return { status: requestedStatus, data: { items, total: p.total } };
        });
      })
      .catch((e: Error) => {
        if (runId === loadMoreSequence.current && pageRequestId === requestSequence.current)
          setError(e.message || String(e));
      })
      .finally(() => {
        if (runId === loadMoreSequence.current) setLoadMoreRun(null);
      });
  };

  const onDeleteDraft = async (draft: DraftItem) => {
    if (deletingId !== null) return;
    if (!window.confirm(`删除草稿「${draft.title || draft.source_title}」后不可恢复，确定删除吗？`)) return;
    const filterAtRequest = statusFilter;
    setDeletingId(draft.id);
    setError('');
    try {
      await deleteItem(draft.id, draft.revision);
      if (!mountedRef.current) return;
      requestSequence.current += 1;
      loadMoreSequence.current += 1;
      setLoadMoreRun(null);
      setLoaded((prev) => {
        if (!prev || !prev.data.items.some((item) => item.id === draft.id)) return prev;
        return {
          status: prev.status,
          data: {
            items: prev.data.items.filter((item) => item.id !== draft.id),
            total: Math.max(0, prev.data.total - 1),
          },
        };
      });
      // 删除后废弃的是旧快照，必须为当前筛选发起替代请求，避免永久 loading。
      void fetchFirst(statusFilterRef.current);
    } catch (e) {
      if (mountedRef.current && statusFilterRef.current === filterAtRequest)
        setError((e as Error).message || String(e));
    } finally {
      if (mountedRef.current) setDeletingId(null);
    }
  };

  const changeStatusFilter = (next: DraftStatus | '') => {
    if (next === statusFilter) return;
    statusFilterRef.current = next;
    requestSequence.current += 1;
    loadMoreSequence.current += 1;
    setLoadMoreRun(null);
    setError('');
    setStatusFilter(next);
  };

  const loadingMore = loadMoreRun?.status === statusFilter;


  return (
    <div>
      <PageHeader
        title={isSite ? '网站资讯稿' : '文章草稿'}
        description={isSite
          ? '独立的网站新闻稿 · SEO字段与后台逐项复制 · 人工审核发布'
          : '公众号编辑草稿 · AI 生成 → 编辑 → 复制富文本发布'}
      >
        <button
          onClick={() => navigate('/events')}
          className="inline-flex items-center gap-1.5 text-xs text-accent border border-accent/40 rounded-lg px-3 py-2 hover:bg-accent/10 transition-colors cursor-pointer"
        >
          <PenSquare className="h-3.5 w-3.5" /> 去选选题
        </button>
      </PageHeader>

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {STATUS_FILTERS.map((s) => {
          const active = statusFilter === s.key;
          return (
            <button
              key={s.key}
              aria-pressed={active}
              onClick={() => changeStatusFilter(s.key)}
              className={`chip px-3 py-1.5 text-xs border transition-colors cursor-pointer ${
                active
                  ? 'bg-accent/15 text-accent border-accent/40'
                  : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
              }`}
            >
              {s.label}
            </button>
          );
        })}
      </div>

      {error && (
        <div role="alert" className="card p-4 mb-4 text-sm text-red-300 border border-red-400/30 bg-red-500/5">
          {error}
        </div>
      )}

      {loading ? (
        <Loading />
      ) : page.items.length === 0 ? (
        <div className="card py-16 text-center">
          <FileText className="h-10 w-10 mx-auto text-slate-600 mb-4" />
          <div className="text-sm text-slate-400 mb-1.5">
            {statusFilter
              ? `暂无${STATUS_FILTERS.find((filter) => filter.key === statusFilter)?.label}的草稿`
              : isSite ? '还没有网站资讯稿' : '还没有文章草稿'}
          </div>
          <div className="text-xs text-slate-500">
            {statusFilter
              ? '可切换到“全部”查看其他状态的草稿'
              : isSite ? '在事件详情或资讯详情选择“生成网站稿”'
                : '去热点事件详情或资讯详情点「AI 生成文章」，第一份草稿会出现在这里'}
          </div>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {page.items.map((d) => (
              <DraftCard
                key={d.id}
                draft={d}
                onOpen={() => navigate(`${basePath}/${d.id}`)}
                onDelete={() => void onDeleteDraft(d)}
                deleting={deletingId === d.id}
                deleteDisabled={deletingId !== null}
              />
            ))}
          </div>
          {page.items.length < page.total && (
            <div className="text-center mt-6">
              <button
                onClick={onLoadMore}
                disabled={loadingMore}
                className="chip bg-radar-card text-slate-300 border border-radar-border px-5 py-2.5 hover:border-accent/40 transition-colors cursor-pointer disabled:opacity-50"
              >
                {loadingMore ? '加载中…' : `加载更早（共 ${page.total} 篇）`}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function DraftCard({
  draft,
  onOpen,
  onDelete,
  deleting,
  deleteDisabled,
}: {
  draft: DraftItem;
  onOpen: () => void;
  onDelete: () => void;
  deleting: boolean;
  deleteDisabled: boolean;
}) {
  const tokens = draft.prompt_tokens + draft.completion_tokens;
  const label = draft.title || draft.source_title || '（无标题）';
  return (
    <div className="card overflow-hidden hover:border-accent/35 transition-all group">
      <button
        type="button"
        aria-label={`查看草稿「${label}」`}
        aria-describedby={`draft-status-${draft.id} draft-summary-${draft.id}`}
        onClick={onOpen}
        className="w-full p-5 text-left cursor-pointer hover:bg-radar-cardHover/50 transition-all focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      >
        <span className="flex items-start justify-between gap-3 mb-2">
          <span id={`draft-status-${draft.id}`} className="flex items-center gap-2 flex-wrap">
            <DraftStatusChip status={draft.status} />
            <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
              {TIER_LABELS[draft.tier] ?? draft.tier}
            </span>
            <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border inline-flex items-center gap-1">
              {draft.source_type === 'event' ? (
                <><Zap className="h-3 w-3 text-amber-400" /> 事件选题</>
              ) : (
                <><Sparkles className="h-3 w-3 text-cyan-400" /> 单篇选题</>
              )}
            </span>
          </span>
          <span className="text-[11px] text-slate-500 shrink-0">
            {draft.updated_at ? timeAgo(new Date(draft.updated_at)) : '—'}
          </span>
        </span>

        <span className="block text-[15px] font-semibold text-slate-100 group-hover:text-accent transition-colors leading-snug mb-1.5 line-clamp-2">
          {label}
        </span>

        {draft.status === 'failed' ? (
          <span id={`draft-summary-${draft.id}`} className="block text-[13px] text-red-300/90 leading-relaxed line-clamp-2">{draft.error}</span>
        ) : draft.status === 'generating' ? (
          <span id={`draft-summary-${draft.id}`} className="block text-[13px] text-slate-500 leading-relaxed">
            AI 正在基于「{draft.source_title}」撰写{draft.gen_count > 1 ? `（第 ${draft.gen_count} 次）` : ''}……
          </span>
        ) : (
          <span id={`draft-summary-${draft.id}`} className="block text-[13px] text-slate-400 leading-relaxed line-clamp-2">
            {draft.digest || draft.source_title}
          </span>
        )}

        <span className="flex items-center gap-3 text-[11px] text-slate-500 mt-3 pt-3 border-t border-radar-border">
          {draft.model && <span className="truncate max-w-40">{draft.model}</span>}
          {tokens > 0 && <span className="tabular-nums">{tokens.toLocaleString()} tokens</span>}
          {draft.duration_ms > 0 && <span className="tabular-nums">{(draft.duration_ms / 1000).toFixed(1)}s</span>}
          {draft.gen_count > 1 && <span>已生成 {draft.gen_count} 次</span>}
        </span>
      </button>
      <div className="flex justify-end border-t border-radar-border px-5 py-2">
        <button
          type="button"
          aria-label={`删除草稿「${label}」`}
          onClick={onDelete}
          disabled={deleteDisabled}
          className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-red-300 hover:bg-red-500/10 border border-transparent hover:border-red-400/25 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Trash2 className="h-3.5 w-3.5" /> {deleting ? '删除中…' : '删除草稿'}
        </button>
      </div>
    </div>
  );
}
