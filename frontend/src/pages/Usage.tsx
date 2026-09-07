/**
 * 成本看板页：Token 用量与成本计费总览。
 * 数据源：/api/v2/admin/usage/*（见 backend/app/api/v2/admin.py 的 usage 端点组）。
 * 鉴权复用运维监控页保存的 Token（localStorage）；未登录时提示先去 /admin 登录。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  CheckCircle2,
  Coins,
  Download,
  Loader2,
  Plus,
  RefreshCw,
  ShieldAlert,
  Tag,
  XCircle,
} from 'lucide-react';
import { Loading, PageHeader, StatCard } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import {
  ModelPriceItem,
  UsageGroup,
  UsageLogItem,
  UsageSummary,
  exportUsageCsv,
  getUsageLogs,
  getUsagePrices,
  getUsageSummary,
  upsertUsagePrice,
} from '../api/admin';

const TASK_LABELS: Record<string, string> = {
  summary: '文章摘要',
  event_summary: '事件总结',
  embedding: '向量化',
  report: '日报',
};

const PAGE_SIZE = 50;

function fmtMoney(cost: number | undefined): string {
  return `¥${(cost ?? 0).toFixed(2)}`;
}

function fmtTokens(n: number | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return String(v);
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(ms: number | null): string {
  if (ms == null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function StatusChip({ status }: { status: string }) {
  if (status === 'success') {
    return (
      <span className="text-emerald-300 flex items-center gap-1 text-xs">
        <CheckCircle2 className="h-3.5 w-3.5" />成功
      </span>
    );
  }
  if (status === 'parse_fail') {
    return (
      <span className="text-amber-300 flex items-center gap-1 text-xs">
        <AlertTriangle className="h-3.5 w-3.5" />解析失败
      </span>
    );
  }
  return (
    <span className="text-red-400 flex items-center gap-1 text-xs">
      <XCircle className="h-3.5 w-3.5" />API 失败
    </span>
  );
}

export default function Usage() {
  usePageTitle('成本看板');
  const [summaryToday, setSummaryToday] = useState<UsageSummary | null>(null);
  const [summaryMonth, setSummaryMonth] = useState<UsageSummary | null>(null);
  const [summary30d, setSummary30d] = useState<UsageSummary | null>(null);
  const [logs, setLogs] = useState<UsageLogItem[]>([]);
  const [logTotal, setLogTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [prices, setPrices] = useState<ModelPriceItem[]>([]);
  const [priceEdits, setPriceEdits] = useState<Record<string, { in: string; out: string }>>({});
  const [newPrice, setNewPrice] = useState({ model: '', in: '', out: '0' });
  const [loading, setLoading] = useState(true);
  const [needAuth, setNeedAuth] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);

  // 组件卸载时清理 toast 自动消失的定时器
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const later = useCallback((fn: () => void, ms: number) => {
    const id = setTimeout(fn, ms);
    timersRef.current.push(id);
  }, []);
  useEffect(
    () => () => {
      timersRef.current.forEach(clearTimeout);
    },
    []
  );

  const flash = (ok: boolean, text: string) => {
    setToast({ ok, text });
    later(() => setToast(null), 4000);
  };

  const refresh = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    else setBusy('refresh');
    try {
      const [today, month, d30, logPage, priceList] = await Promise.all([
        getUsageSummary('today'),
        getUsageSummary('month'),
        getUsageSummary('30d'),
        getUsageLogs(1, PAGE_SIZE),
        getUsagePrices(),
      ]);
      setSummaryToday(today);
      setSummaryMonth(month);
      setSummary30d(d30);
      setLogs(logPage.items ?? []);
      setLogTotal(logPage.total ?? 0);
      setPage(1);
      setPrices(priceList.items ?? []);
      setPriceEdits({}); // 服务端价目已是最新，丢弃本地残留的行内编辑态
      setNeedAuth(false);
      setError('');
    } catch (e: any) {
      // 仅 401 视为未登录；503/网络错误属于后端不可达，展示错误而不是误导性登录提示
      if (e?.status === 401) setNeedAuth(true);
      else setError(e?.message ?? '网络错误');
    } finally {
      setLoading(false);
      setBusy('');
    }
  }, []);

  useEffect(() => {
    // 延迟到定时器回调执行，避免在 effect 体内同步触发 setState 级联
    const id = setTimeout(() => void refresh(true), 0);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadLogs = async (p: number) => {
    setBusy('logs');
    try {
      const r = await getUsageLogs(p, PAGE_SIZE);
      setLogs(r.items ?? []);
      setLogTotal(r.total ?? 0);
      setPage(p);
    } catch (e: any) {
      flash(false, `加载明细失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  const doExport = async () => {
    setBusy('export');
    try {
      await exportUsageCsv();
      flash(true, 'CSV 已开始下载（含对账口径注释行）');
    } catch (e: any) {
      flash(false, `导出失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  const savePrice = async (model: string, pin: string, pout: string): Promise<boolean> => {
    if (!model.trim()) return false;
    // 前端校验：单价必须是有限非负数值，避免静默无操作或发出非法请求
    const pinN = Number(pin);
    const poutN = Number(pout);
    if (pin === '' || pout === '' || !Number.isFinite(pinN) || !Number.isFinite(poutN) || pinN < 0 || poutN < 0) {
      flash(false, `价目无效：「${model.trim()}」的单价需为非负数值`);
      return false;
    }
    setBusy(`price-${model}`);
    try {
      await upsertUsagePrice({
        model: model.trim(),
        price_input: pinN,
        price_output: poutN,
      });
      const r = await getUsagePrices();
      setPrices(r.items ?? []);
      setPriceEdits((m) => {
        if (!(model in m)) return m;
        const rest = { ...m };
        delete rest[model];
        return rest;
      });
      flash(true, `价目已保存：${model.trim()}`);
      return true;
    } catch (e: any) {
      flash(false, `保存失败：${e?.message ?? e}`);
      return false;
    } finally {
      setBusy('');
    }
  };

  if (loading) {
    return (
      <div>
        <PageHeader title="成本看板" description="Token 用量 · 成本计费 · 正在加载…" />
        <Loading />
      </div>
    );
  }

  if (needAuth) {
    return (
      <div>
        <PageHeader title="成本看板" description="Token 用量 · 成本计费" />
        <div className="card px-5 py-6 max-w-md mx-auto mt-16 text-sm text-slate-300 flex items-start gap-3">
          <ShieldAlert className="h-5 w-5 text-amber-400 shrink-0 mt-0.5" />
          <div>
            需要管理员 Token。请先在
            <Link to="/admin" className="text-accent hover:underline mx-1">运维监控页</Link>
            登录（Token 保存在本地，登录后本页自动可用）。
          </div>
        </div>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(logTotal / PAGE_SIZE));
  const daily = summary30d?.daily ?? [];
  const dailyMax = Math.max(1, ...daily.map((d) => d.tokens));

  return (
    <div className="space-y-6">
      <PageHeader
        title="成本看板"
        description="AI 调用计量：Token 用量与成本（入库时按当时价目锁定）· 今日按 UTC 0 点起算，与日报口径一致"
      >
        <button
          onClick={() => void refresh()}
          disabled={busy === 'refresh'}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${busy === 'refresh' ? 'animate-spin' : ''}`} /> 刷新
        </button>
      </PageHeader>

      {toast && (
        <div className={`card px-4 py-2.5 text-sm flex items-center gap-2 ${toast.ok ? 'text-emerald-300' : 'text-red-300'}`}>
          {toast.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {toast.text}
        </div>
      )}
      {error && !summaryToday && (
        <div className="card px-4 py-3 text-sm text-amber-200 flex items-center gap-2">
          <AlertTriangle className="h-4 w-4" /> 暂时无法获取数据：{error}
        </div>
      )}
      {error && summaryToday && (
        <div className="card px-4 py-2.5 text-xs text-amber-200 flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          刷新失败（{error}），以下为上次获取的数据 · 可点右上角「刷新」重试
        </div>
      )}

      {/* ———— 顶部卡 ———— */}
      <section className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard icon={<Coins className="h-4 w-4" />} label="今日成本" value={fmtMoney(summaryToday?.total_cost)} sub={`${summaryToday?.total_calls ?? 0} 次调用 · ${fmtTokens(summaryToday?.total_tokens)} tokens`} />
        <StatCard icon={<Coins className="h-4 w-4" />} label="本月累计成本" value={fmtMoney(summaryMonth?.total_cost)} sub={`${summaryMonth?.total_calls ?? 0} 次调用 · ${fmtTokens(summaryMonth?.total_tokens)} tokens`} />
        <StatCard icon={<Tag className="h-4 w-4" />} label="今日调用数" value={String(summaryToday?.total_calls ?? 0)} sub={`摘要 ${summaryToday?.summary_total ?? 0} 次 · 覆盖率 ${summaryToday?.llm_coverage ?? 0}%`} />
        <StatCard icon={<Tag className="h-4 w-4" />} label="本月调用数" value={String(summaryMonth?.total_calls ?? 0)} sub={`Embedding 失败 ${summaryMonth?.embedding_api_fail ?? 0} 次`} />
      </section>

      {/* ———— 健康指标（今日口径） ———— */}
      <section className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="card px-4 py-3.5">
          <div className="text-xs text-slate-400">LLM 摘要覆盖率</div>
          <div className="text-xl font-semibold text-slate-100 mt-1 tabular-nums">{summaryToday?.llm_coverage ?? 0}%</div>
          <div className="text-[11px] text-slate-500 mt-0.5">LLM 成功 {summaryToday?.summary_total ?? 0} 次摘要中的占比（降级/失败拉低覆盖率）</div>
        </div>
        <div className="card px-4 py-3.5">
          <div className="text-xs text-slate-400">Embedding API 失败</div>
          <div className={`text-xl font-semibold mt-1 tabular-nums ${(summaryToday?.embedding_api_fail ?? 0) > 0 ? 'text-red-300' : 'text-slate-100'}`}>{summaryToday?.embedding_api_fail ?? 0}</div>
          <div className="text-[11px] text-slate-500 mt-0.5">大于 0 说明向量 API 在降级本地哈希</div>
        </div>
        <div className="card px-4 py-3.5">
          <div className="text-xs text-slate-400">近 24h 事件总结调用</div>
          <div className="text-xl font-semibold text-slate-100 mt-1 tabular-nums">{summaryToday?.event_summary_calls_24h ?? 0}</div>
          <div className="text-[11px] text-slate-500 mt-0.5">异常暴涨时注意检查事件聚合频率</div>
        </div>
      </section>

      {/* ———— 近 30 天趋势 + 分组 ———— */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="card p-4 lg:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-slate-300">近 30 天 Token / 成本趋势</h2>
            <span className="text-[11px] text-slate-500">条形按 token 归一化</span>
          </div>
          {daily.length === 0 ? (
            <p className="text-xs text-slate-500 py-6 text-center">暂无调用记录（流水线跑起来后自动积累）</p>
          ) : (
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {daily.map((d) => (
                <div key={d.date} className="flex items-center gap-2.5 text-[11px]">
                  <span className="text-slate-500 w-20 shrink-0 tabular-nums">{d.date.slice(5)}</span>
                  <div className="flex-1 h-2.5 rounded bg-radar-border/60 overflow-hidden">
                    <div
                      className="h-full rounded bg-gradient-to-r from-accent/70 to-cyan-300"
                      style={{ width: `${Math.max(2, (d.tokens / dailyMax) * 100)}%` }}
                    />
                  </div>
                  <span className="text-slate-400 w-16 text-right tabular-nums">{fmtTokens(d.tokens)}</span>
                  <span className="text-slate-500 w-12 text-right tabular-nums">{d.calls} 次</span>
                  <span className="text-amber-300/80 w-16 text-right tabular-nums">{fmtMoney(d.cost)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="space-y-3">
          <GroupTable title="按任务类型（30 天）" rows={summary30d?.by_task_type ?? []} />
          <GroupTable title="按模型（30 天）" rows={summary30d?.by_model ?? []} />
        </div>
      </section>

      {/* ———— 价目表 ———— */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
          <Coins className="h-4 w-4 text-accent" /> 模型价目（每百万 token，元）
          <span className="text-[11px] font-normal text-slate-500">调价只影响之后的新记录，历史账单锁定</span>
        </h2>
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-radar-border">
                  <th className="text-left font-medium px-4 py-2.5">模型</th>
                  <th className="text-left font-medium px-2 py-2.5">输入单价</th>
                  <th className="text-left font-medium px-2 py-2.5">输出单价</th>
                  <th className="text-left font-medium px-2 py-2.5">更新时间</th>
                  <th className="text-right font-medium px-4 py-2.5">操作</th>
                </tr>
              </thead>
              <tbody>
                {prices.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-6 text-center text-slate-500">
                      暂无价目记录（未配置价的模型照记 token、cost=0）
                    </td>
                  </tr>
                )}
                {prices.map((p) => {
                  const edit = priceEdits[p.model];
                  const changed = edit !== undefined;
                  return (
                    <tr key={p.model} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                      <td className="px-4 py-2 text-slate-200 font-mono">{p.model}</td>
                      <td className="px-2 py-2">
                        <PriceInput value={edit ? edit.in : String(p.price_input)} onChange={(v) => setPriceEdits((m) => ({ ...m, [p.model]: { in: v, out: m[p.model]?.out ?? String(p.price_output) } }))} ariaLabel={`编辑 ${p.model} 输入单价`} />
                      </td>
                      <td className="px-2 py-2">
                        <PriceInput value={edit ? edit.out : String(p.price_output)} onChange={(v) => setPriceEdits((m) => ({ ...m, [p.model]: { in: m[p.model]?.in ?? String(p.price_input), out: v } }))} ariaLabel={`编辑 ${p.model} 输出单价`} />
                      </td>
                      <td className="px-2 py-2 text-slate-500 whitespace-nowrap">{fmtTime(p.updated_at)}</td>
                      <td className="px-4 py-2 text-right">
                        <button
                          onClick={() => savePrice(p.model, edit?.in ?? String(p.price_input), edit?.out ?? String(p.price_output))}
                          disabled={!changed || busy === `price-${p.model}`}
                          className="chip bg-accent/90 hover:bg-accent text-white border border-accent/50 disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          {busy === `price-${p.model}` ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                          保存
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {/* 新增一行 */}
                <tr className="hover:bg-radar-panel/50">
                  <td className="px-4 py-2">
                    <input
                      value={newPrice.model}
                      onChange={(e) => setNewPrice((s) => ({ ...s, model: e.target.value }))}
                      placeholder="模型名，如 glm-4-flash"
                      aria-label="新增模型名"
                      className="w-44 px-2 py-1 rounded bg-radar-panel border border-radar-border text-slate-200 outline-none focus:border-accent font-mono"
                    />
                  </td>
                  <td className="px-2 py-2">
                    <PriceInput
                      value={newPrice.in}
                      onChange={(v) => setNewPrice((s) => ({ ...s, in: v }))}
                      ariaLabel="新增模型输入单价"
                    />
                  </td>
                  <td className="px-2 py-2">
                    <PriceInput value={newPrice.out} onChange={(v) => setNewPrice((s) => ({ ...s, out: v }))} ariaLabel="新增模型输出单价" />
                  </td>
                  <td className="px-2 py-2 text-slate-500">—</td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={async () => {
                        const ok = await savePrice(newPrice.model.trim(), newPrice.in, newPrice.out);
                        // 仅成功后清空表单，失败保留输入供修正
                        if (ok) setNewPrice({ model: '', in: '', out: '0' });
                      }}
                      disabled={!newPrice.model.trim() || newPrice.in === '' || busy === `price-${newPrice.model.trim()}`}
                      className="chip bg-accent/90 hover:bg-accent text-white border border-accent/50 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {busy === `price-${newPrice.model.trim()}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />} 添加
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* ———— 明细分页表 ———— */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">调用流水明细（共 {logTotal} 条）</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={doExport}
              disabled={busy === 'export'}
              className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40"
            >
              {busy === 'export' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
              导出 CSV
            </button>
          </div>
        </div>
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-radar-border">
                  <th className="text-left font-medium px-4 py-2.5">时间</th>
                  <th className="text-left font-medium px-2 py-2.5">任务</th>
                  <th className="text-left font-medium px-2 py-2.5">模型</th>
                  <th className="text-left font-medium px-2 py-2.5">状态</th>
                  <th className="text-left font-medium px-2 py-2.5">文章 / 事件</th>
                  <th className="text-right font-medium px-2 py-2.5">tokens (入/出)</th>
                  <th className="text-right font-medium px-2 py-2.5">成本</th>
                  <th className="text-right font-medium px-2 py-2.5">耗时</th>
                  <th className="text-left font-medium px-2 py-2.5">触发</th>
                  <th className="text-left font-medium px-4 py-2.5">错误</th>
                </tr>
              </thead>
              <tbody>
                {logs.length === 0 && (
                  <tr>
                    <td colSpan={10} className="px-4 py-8 text-center text-slate-500">
                      暂无流水记录
                    </td>
                  </tr>
                )}
                {logs.map((r) => (
                  <tr key={r.id} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                    <td className="px-4 py-2 text-slate-400 whitespace-nowrap">{fmtTime(r.created_at)}</td>
                    <td className="px-2 py-2 text-slate-200 whitespace-nowrap">{TASK_LABELS[r.task_type] ?? r.task_type}</td>
                    <td className="px-2 py-2 text-slate-300 font-mono max-w-[160px] truncate" title={r.model}>{r.model}</td>
                    <td className="px-2 py-2 whitespace-nowrap"><StatusChip status={r.status} /></td>
                    <td className="px-2 py-2 text-slate-400 max-w-[260px] truncate" title={r.article_title ?? (r.event_id != null ? `事件 #${r.event_id}` : '')}>
                      {r.article_title ?? (r.event_id != null ? `事件 #${r.event_id}` : '—')}
                    </td>
                    <td className="px-2 py-2 text-right text-slate-300 tabular-nums whitespace-nowrap">
                      {r.prompt_tokens}/{r.completion_tokens}
                      <span className="text-slate-500"> ({fmtTokens(r.total_tokens)})</span>
                    </td>
                    <td className="px-2 py-2 text-right text-amber-300/90 tabular-nums whitespace-nowrap">{fmtMoney(r.cost)}</td>
                    <td className="px-2 py-2 text-right text-slate-400 whitespace-nowrap">{fmtDuration(r.latency_ms)}</td>
                    <td className="px-2 py-2 whitespace-nowrap">
                      <span className={`chip text-[10px] ${r.trigger === 'manual_repair' ? 'bg-sky-500/10 text-sky-300' : 'bg-slate-500/10 text-slate-400'}`}>
                        {r.trigger === 'manual_repair' ? '补数据' : '自动'}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-red-300/80 max-w-[200px] truncate" title={r.error ?? ''}>{r.error ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-3 text-xs text-slate-400">
            <button
              onClick={() => loadLogs(page - 1)}
              disabled={page <= 1 || busy === 'logs'}
              className="chip border border-radar-border bg-radar-card hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed"
            >
              上一页
            </button>
            <span className="tabular-nums">第 {page} / {totalPages} 页</span>
            <button
              onClick={() => loadLogs(page + 1)}
              disabled={page >= totalPages || busy === 'logs'}
              className="chip border border-radar-border bg-radar-card hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed"
            >
              下一页
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

function GroupTable({ title, rows }: { title: string; rows: UsageGroup[] }) {
  return (
    <div className="card p-4">
      <h3 className="text-xs font-semibold text-slate-300 mb-2.5">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-[11px] text-slate-500 py-2">暂无数据</p>
      ) : (
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-slate-500 border-b border-radar-border">
              <th className="text-left font-medium py-1.5">名称</th>
              <th className="text-right font-medium py-1.5">调用</th>
              <th className="text-right font-medium py-1.5">tokens</th>
              <th className="text-right font-medium py-1.5">成本</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((g) => (
              <tr key={g.key} className="border-b border-radar-border/40 last:border-0">
                <td className="py-1.5 text-slate-300 font-mono truncate max-w-[120px]" title={g.key}>{TASK_LABELS[g.key] ?? g.key}</td>
                <td className="py-1.5 text-right text-slate-400 tabular-nums">{g.calls}</td>
                <td className="py-1.5 text-right text-slate-400 tabular-nums">{fmtTokens(g.tokens)}</td>
                <td className="py-1.5 text-right text-amber-300/80 tabular-nums">{fmtMoney(g.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function PriceInput({
  value,
  onChange,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  ariaLabel: string;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      inputMode="decimal"
      aria-label={ariaLabel}
      className="w-20 px-2 py-1 rounded bg-radar-panel border border-radar-border text-slate-200 outline-none focus:border-accent tabular-nums"
    />
  );
}
