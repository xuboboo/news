/**
 * 审计日志页（仅平台超管）：登录全路径 + 管理写操作的时间线，支持动作/邮箱筛选与分页。
 * 数据源：/api/v2/admin/audit-logs（见 backend/app/api/v2/audit.py）。
 */
import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Loader2, RefreshCw, ScrollText, Search, ShieldX } from 'lucide-react';
import { PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { isSuperAdmin, useAuth } from '../auth';
import { AuditLogItem, getAuditActions, getAuditLogs } from '../api/admin';

const PAGE_SIZE = 50;

/** 动作 → 中文标签 + 配色 */
const ACTION_STYLES: Record<string, { label: string; cls: string }> = {
  'auth.login': { label: '登录成功', cls: 'bg-emerald-500/10 text-emerald-300 border-emerald-400/20' },
  'auth.login_failed': { label: '登录失败', cls: 'bg-amber-500/10 text-amber-300 border-amber-400/30' },
  'auth.locked': { label: '账号锁定', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'auth.logout': { label: '登出', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'auth.password_changed': { label: '修改密码', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'auth.session_revoked': { label: '吊销会话', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'tenant.created': { label: '创建租户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'tenant.updated': { label: '更新租户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.created': { label: '创建用户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.updated': { label: '更新用户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.disabled': { label: '停用用户', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'user.password_reset': { label: '重置密码', cls: 'bg-amber-500/10 text-amber-300 border-amber-400/30' },
  'admin.config_updated': { label: '配置修改', cls: 'bg-cyan-500/10 text-cyan-300 border-cyan-400/30' },
  'admin.test_llm': { label: 'LLM 测试', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'admin.test_embedding': { label: 'Embedding 测试', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'admin.trigger_crawl': { label: '手动采集', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.trigger_events': { label: '手动聚合', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.trigger_report': { label: '手动日报', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
};

function actionLabel(action: string): { label: string; cls: string } {
  return ACTION_STYLES[action] ?? { label: action, cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' };
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

export default function Audit() {
  usePageTitle('审计日志');
  const { user } = useAuth();
  const allowed = isSuperAdmin(user);

  const [items, setItems] = useState<AuditLogItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [action, setAction] = useState('');
  const [q, setQ] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!allowed) return;
    setBusy(true);
    try {
      const r = await getAuditLogs({
        action: action || undefined,
        q: q || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      setItems(r.items ?? []);
      setTotal(r.total);
    } catch {
      setItems([]);
    } finally {
      setBusy(false);
    }
  }, [allowed, action, q, page]);

  useEffect(() => {
    if (allowed) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载拉取审计数据（异步数据加载）
      void load();
    }
  }, [allowed, load]);

  useEffect(() => {
    if (allowed) {
      getAuditActions()
        .then((r) => setActions(r.items ?? []))
        .catch(() => {});
    }
  }, [allowed]);

  if (!allowed) {
    return (
      <div>
        <PageHeader title="审计日志" description="操作留痕 · 可追溯" />
        <div className="max-w-md card p-8 mx-auto mt-16 text-center">
          <div className="mx-auto mb-4 w-fit p-3 rounded-xl bg-red-500/10 text-red-400">
            <ShieldX className="h-7 w-7" />
          </div>
          <div className="text-sm font-semibold text-slate-100 mb-1.5">无访问权限</div>
          <p className="text-xs text-slate-500 leading-5">审计日志仅对平台超管开放。</p>
        </div>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-5">
      <PageHeader title="审计日志" description={`登录与操作留痕 · 共 ${total} 条 · 保留期内可追溯`}>
        <button
          onClick={() => void load()}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 cursor-pointer"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} /> 刷新
        </button>
      </PageHeader>

      {/* 筛选条 */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={action}
          onChange={(e) => {
            setPage(1);
            setAction(e.target.value);
          }}
          aria-label="动作筛选"
          className="rounded-lg border border-radar-border bg-radar-panel px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-accent/60 cursor-pointer"
        >
          <option value="">全部动作</option>
          {actions.map((a) => (
            <option key={a} value={a}>
              {actionLabel(a).label}（{a}）
            </option>
          ))}
        </select>
        <div className="relative min-w-[14rem]">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setPage(1);
                void load();
              }
            }}
            placeholder="按操作者邮箱筛选，回车确认"
            className="w-full pl-8 pr-3 py-2 rounded-lg border border-radar-border bg-radar-panel text-xs text-slate-100 outline-none focus:border-accent/60"
          />
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 border-b border-radar-border">
                <th className="text-left font-medium px-4 py-2.5">时间</th>
                <th className="text-left font-medium px-2 py-2.5">动作</th>
                <th className="text-left font-medium px-2 py-2.5">操作者</th>
                <th className="text-left font-medium px-2 py-2.5">租户</th>
                <th className="text-left font-medium px-2 py-2.5">IP</th>
                <th className="text-left font-medium px-4 py-2.5">详情</th>
              </tr>
            </thead>
            <tbody>
              {items === null || (busy && items.length === 0) ? (
                <tr>
                  <td colSpan={6} className="px-4 py-10 text-center">
                    <Loader2 className="h-5 w-5 animate-spin text-slate-500 mx-auto" />
                  </td>
                </tr>
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                    <div className="flex flex-col items-center gap-2">
                      <ScrollText className="h-6 w-6 text-slate-600" />
                      暂无审计记录
                    </div>
                  </td>
                </tr>
              ) : (
                items.map((it) => {
                  const a = actionLabel(it.action);
                  return (
                    <tr key={it.id} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                      <td className="px-4 py-2 text-slate-400 whitespace-nowrap tabular-nums">{fmtTime(it.created_at)}</td>
                      <td className="px-2 py-2">
                        <span className={`chip text-[10px] border ${a.cls}`}>{a.label}</span>
                      </td>
                      <td className="px-2 py-2 text-slate-200">{it.user_email || '—'}</td>
                      <td className="px-2 py-2 text-slate-400">{it.tenant_id ?? '—'}</td>
                      <td className="px-2 py-2 text-slate-400 font-mono">{it.ip || '—'}</td>
                      <td className="px-4 py-2 text-slate-400 max-w-sm truncate" title={JSON.stringify(it.detail)}>
                        {Object.keys(it.detail ?? {}).length ? JSON.stringify(it.detail) : '—'}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 分页 */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-end gap-3 text-xs text-slate-400">
          <span>
            第 {page} / {totalPages} 页
          </span>
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="chip border border-radar-border bg-radar-card disabled:opacity-30 cursor-pointer"
            aria-label="上一页"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="chip border border-radar-border bg-radar-card disabled:opacity-30 cursor-pointer"
            aria-label="下一页"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
