/**
 * 公众号排版草稿箱：每日早报 → 一键排版 → 复制发布链路的入口页。
 * 与「公众号草稿」（选题生文）独立：数据走 /api/v2/wx-layouts，
 * 排版为后端确定性渲染（不经 LLM），同步完成、无需轮询生成状态。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, LayoutTemplate, Sparkles, Trash2 } from 'lucide-react';
import {
  deleteWxLayout,
  generateWxLayout,
  listWxLayouts,
  type WxLayoutItem,
} from '../api/wxLayouts';
import { availableDates, loadReports, reportsLoaded } from '../api/client';
import { Loading, PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { timeAgo } from '../utils/format';

const PAGE_SIZE = 50;

export default function WxLayouts() {
  usePageTitle('公众号排版');
  const navigate = useNavigate();

  // 可排版日期 = 已生成的早报日期（复用全局 client 的日报缓存）
  const [dateReady, setDateReady] = useState(reportsLoaded);
  useEffect(() => {
    if (reportsLoaded) return;
    let cancelled = false;
    void loadReports(false).finally(() => {
      if (!cancelled) setDateReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  const dates = useMemo(() => (dateReady ? availableDates() : []), [dateReady]);
  const [picked, setPicked] = useState('');
  const effectivePicked = picked && dates.some((d) => d.key === picked) ? picked : dates[0]?.key ?? '';

  const [layouts, setLayouts] = useState<WxLayoutItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState('');
  const [genError, setGenError] = useState('');
  const [generating, setGenerating] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const refresh = useCallback(() => {
    return listWxLayouts(PAGE_SIZE, 0)
      .then((page) => {
        setLayouts(page.items);
        setTotal(page.total);
        setError('');
      })
      .catch((e: Error) => {
        setLayouts([]);
        setError(e.message || String(e));
      });
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onGenerate = async () => {
    if (generating || !effectivePicked) return;
    setGenerating(true);
    setGenError('');
    try {
      const item = await generateWxLayout(effectivePicked);
      navigate(`/wx-layouts/${item.id}`);
    } catch (e) {
      setGenError((e as Error).message || String(e));
    } finally {
      setGenerating(false);
    }
  };

  const onDelete = async (item: WxLayoutItem) => {
    if (deletingId !== null) return;
    if (!window.confirm(`删除「${item.title}」排版草稿后不可恢复，确定删除吗？`)) return;
    setDeletingId(item.id);
    setError('');
    try {
      await deleteWxLayout(item.id, item.revision);
      await refresh();
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div>
      <PageHeader
        title="公众号排版"
        description="每日早报一键排版 · 固定开头结尾 · 复制后直接粘贴到公众号后台"
      />

      {/* 从早报生成 */}
      <div className="card p-5 mb-4">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-slate-200">从早报生成排版</h2>
          <span className="text-[11px] text-slate-500">选择早报日期，同一期重复生成会覆盖旧稿</span>
        </div>
        {!dateReady ? (
          <div className="text-xs text-slate-500 py-2">早报日期加载中…</div>
        ) : dates.length === 0 ? (
          <div className="text-xs text-slate-500 py-2">
            暂无可用早报 · 早报由系统每日 08:00 自动生成，也可在「运维监控」页手动触发
          </div>
        ) : (
          <>
            <div className="flex items-center gap-1.5 flex-wrap mb-3">
              {dates.map((d) => {
                const active = effectivePicked === d.key;
                return (
                  <button
                    key={d.key}
                    onClick={() => setPicked(d.key)}
                    aria-pressed={active}
                    className={`chip border px-3 py-1.5 cursor-pointer transition-colors ${
                      active
                        ? 'bg-accent/15 text-accent border-accent/40'
                        : 'bg-radar-card text-slate-400 border-radar-border hover:text-slate-200'
                    }`}
                  >
                    {d.label}
                  </button>
                );
              })}
            </div>
            <button
              onClick={() => void onGenerate()}
              disabled={generating || !effectivePicked}
              className="inline-flex items-center gap-1.5 text-sm text-white bg-accent hover:bg-accent/85 rounded-lg px-4 py-2 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <LayoutTemplate className="h-4 w-4" />
              {generating ? '排版中…' : `排版 ${effectivePicked} 早报`}
            </button>
          </>
        )}
        {genError && (
          <div role="alert" className="mt-3 text-sm text-red-300 border border-red-400/30 bg-red-500/5 rounded-lg px-3 py-2">
            {genError}
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="card p-4 mb-4 text-sm text-red-300 border border-red-400/30 bg-red-500/5">
          {error}
        </div>
      )}

      {layouts === null ? (
        <Loading />
      ) : layouts.length === 0 ? (
        <div className="card py-16 text-center">
          <FileText className="h-10 w-10 mx-auto text-slate-600 mb-4" />
          <div className="text-sm text-slate-400 mb-1.5">还没有排版草稿</div>
          <div className="text-xs text-slate-500">在上方选择早报日期点「排版」，生成后一键复制到公众号后台</div>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {layouts.map((item) => (
              <div key={item.id} className="card overflow-hidden hover:border-accent/35 transition-all group">
                <button
                  type="button"
                  aria-label={`查看排版草稿「${item.title}」`}
                  onClick={() => navigate(`/wx-layouts/${item.id}`)}
                  className="w-full p-5 text-left cursor-pointer hover:bg-radar-cardHover/50 transition-all focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                >
                  <span className="flex items-center justify-between gap-3 mb-2">
                    <span className="flex items-center gap-2 flex-wrap">
                      <span className="chip text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-400/30">
                        可复制
                      </span>
                      <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
                        第 {item.issue_no} 期
                      </span>
                    </span>
                    <span className="text-[11px] text-slate-500 shrink-0">
                      {item.updated_at ? timeAgo(new Date(item.updated_at)) : '—'}
                    </span>
                  </span>
                  <span className="block text-[15px] font-semibold text-slate-100 group-hover:text-accent transition-colors leading-snug mb-1.5 line-clamp-2">
                    {item.title || `${item.report_date} 早报排版`}
                  </span>
                  <span className="block text-[13px] text-slate-400 leading-relaxed line-clamp-2">
                    {item.digest || '（无摘要）'}
                  </span>
                  <span className="flex items-center gap-3 text-[11px] text-slate-500 mt-3 pt-3 border-t border-radar-border">
                    <span>{item.report_date}</span>
                    {item.gen_count > 1 && <span>已生成 {item.gen_count} 次</span>}
                  </span>
                </button>
                <div className="flex justify-end border-t border-radar-border px-5 py-2">
                  <button
                    type="button"
                    aria-label={`删除排版草稿「${item.title}」`}
                    onClick={() => void onDelete(item)}
                    disabled={deletingId !== null}
                    className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-red-300 hover:bg-red-500/10 border border-transparent hover:border-red-400/25 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> {deletingId === item.id ? '删除中…' : '删除草稿'}
                  </button>
                </div>
              </div>
            ))}
          </div>
          {layouts.length < total && (
            <div className="text-center mt-6 text-xs text-slate-500">
              仅显示最近 {PAGE_SIZE} 期（共 {total} 期排版稿）
            </div>
          )}
        </>
      )}
    </div>
  );
}
