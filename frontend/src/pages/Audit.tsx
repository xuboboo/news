/**
 * 审计日志页（仅平台超管）：登录全路径 + 管理写操作的时间线。
 * 支持动作/邮箱/IP/时间范围筛选、CSV 导出与分页。
 * 数据源：/api/v2/admin/audit-logs（见 backend/app/api/v2/audit.py）。
 */
import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Download, Loader2, RefreshCw, ScrollText, Search, ShieldX } from 'lucide-react';
import { PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { isSuperAdmin, useAuth } from '../auth';
import { getTenants, type AuditLogItem, exportAuditLogs, getAuditActions, getAuditLogs } from '../api/admin';

const PAGE_SIZE = 50;

/** 动作 → 中文标签 + 配色 */
const ACTION_STYLES: Record<string, { label: string; cls: string }> = {
  'auth.login': { label: '登录成功', cls: 'bg-emerald-500/10 text-emerald-300 border-emerald-400/20' },
  'auth.login_failed': { label: '登录失败', cls: 'bg-amber-500/10 text-amber-300 border-amber-400/30' },
  'auth.locked': { label: '账号锁定', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'auth.throttled': { label: '登录限流', cls: 'bg-amber-500/10 text-amber-300 border-amber-400/30' },
  'auth.logout': { label: '登出', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'auth.password_changed': { label: '修改密码', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'auth.session_revoked': { label: '吊销会话', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'auth.access_denied': { label: '越权访问', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'tenant.created': { label: '创建租户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'tenant.updated': { label: '更新租户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.created': { label: '创建用户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.updated': { label: '更新用户', cls: 'bg-violet-500/10 text-violet-300 border-violet-400/30' },
  'user.disabled': { label: '停用用户', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'user.password_reset': { label: '重置密码', cls: 'bg-amber-500/10 text-amber-300 border-amber-400/30' },
  'user.unlocked': { label: '解除锁定', cls: 'bg-emerald-500/10 text-emerald-300 border-emerald-400/20' },
  'admin.config_updated': { label: '配置修改', cls: 'bg-cyan-500/10 text-cyan-300 border-cyan-400/30' },
  'admin.price_updated': { label: '价目变更', cls: 'bg-cyan-500/10 text-cyan-300 border-cyan-400/30' },
  'admin.price_deleted': { label: '价目删除', cls: 'bg-red-500/10 text-red-300 border-red-400/30' },
  'admin.test_llm': { label: 'LLM 测试', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'admin.test_embedding': { label: 'Embedding 测试', cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' },
  'admin.trigger_crawl': { label: '手动采集', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.trigger_stats': { label: '手动互动轮询', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.trigger_events': { label: '手动聚合', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.trigger_report': { label: '手动日报', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
  'admin.test_report_push': { label: '日报测试推送', cls: 'bg-sky-500/10 text-sky-300 border-sky-400/30' },
};

function actionLabel(action: string): { label: string; cls: string } {
  return ACTION_STYLES[action] ?? { label: action, cls: 'bg-slate-500/10 text-slate-300 border-slate-400/20' };
}

const LOGIN_FAIL_REASONS: Record<string, string> = {
  unknown_email: '邮箱不存在',
  account_disabled: '账号已停用',
  tenant_disabled: '所属租户已停用',
  bad_credentials: '密码错误',
};

/** 详情 → 人话摘要（原始 JSON 在悬停提示里） */
function detailText(action: string, d: Record<string, unknown>): string {
  if (!d || Object.keys(d).length === 0) return '—';
  const list = (v: unknown) => (Array.isArray(v) ? v.join('、') : '');
  switch (action) {
    case 'auth.login':
    case 'auth.logout':
      return `会话 #${d.session_id ?? '—'}`;
    case 'auth.login_failed':
      return LOGIN_FAIL_REASONS[String(d.reason)] ?? JSON.stringify(d);
    case 'auth.session_revoked':
      return d.revoke_others != null ? `登出其他 ${d.revoke_others} 个会话` : `吊销会话 #${d.session_id ?? '—'}`;
    case 'auth.access_denied': {
      const reason = d.reason === 'invalid_admin_token' ? '（无效管理令牌）' : '';
      return `${d.method ?? 'GET'} ${d.path ?? ''}${reason}`;
    }
    case 'tenant.created':
      return `创建租户「${d.name}」(#${d.tenant_id})`;
    case 'tenant.updated':
      return `租户 #${d.tenant_id} 修改：${list(d.changes) || JSON.stringify(d)}`;
    case 'user.created':
      return `创建 ${d.email}（${d.role}，租户 #${d.tenant_id}）`;
    case 'user.updated':
      return `用户 #${d.target_user_id} 修改：${list(d.changes) || JSON.stringify(d)}`;
    case 'user.disabled':
      return `停用用户 #${d.target_user_id}：${list(d.changes) || JSON.stringify(d)}`;
    case 'user.password_reset':
      return `重置 ${d.email} 的密码（用户 #${d.target_user_id}）`;
    case 'admin.config_updated': {
      const changes = d.changes as Record<string, string> | undefined;
      if (changes && Object.keys(changes).length) {
        return Object.entries(changes).map(([k, v]) => `${k}: ${v}`).join('；');
      }
      return `修改配置 ${list(d.keys)}`;
    }
    case 'admin.price_updated': {
      const newP = d.new as Record<string, unknown> | undefined;
      const oldP = d.old as Record<string, unknown> | null | undefined;
      const fmt = (p: Record<string, unknown> | null | undefined) =>
        p ? `入 ${p.price_input} / 出 ${p.price_output} / 缓存 ${p.price_cache ?? '按输入价'}` : '无';
      return `${d.op === 'create' ? '新增' : '更新'} ${d.model}：${fmt(oldP)} → ${fmt(newP)}`;
    }
    case 'admin.price_deleted': {
      const del = d.deleted as Record<string, unknown> | undefined;
      return del
        ? `删除 ${d.model}：入 ${del.price_input} / 出 ${del.price_output} / 缓存 ${del.price_cache ?? '按输入价'}`
        : `删除 ${d.model}`;
    }
    case 'admin.test_llm':
    case 'admin.test_embedding': {
      const name = action === 'admin.test_llm' ? 'LLM' : 'Embedding';
      return d.ok
        ? `${name} 连通正常 · ${d.model} · ${d.latency_ms}ms${d.dim != null ? ` · 维度 ${d.dim}` : ''}`
        : `${name} 连通失败 · ${d.error ?? `HTTP ${d.http_status}`}`;
    }
    case 'admin.trigger_crawl':
    case 'admin.trigger_events':
    case 'admin.trigger_report':
    case 'admin.trigger_stats':
      return `任务 ${d.task_id ?? '—'}`;
    default:
      return JSON.stringify(d);
  }
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
  const [committedQ, setCommittedQ] = useState(''); // 回车提交的筛选值（避免逐键请求）
  const [ip, setIp] = useState('');
  const [committedIp, setCommittedIp] = useState('');
  const [fromLocal, setFromLocal] = useState(''); // datetime-local 原始值
  const [toLocal, setToLocal] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  const [tenantNames, setTenantNames] = useState<Map<number, string>>(new Map());
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  // datetime-local（本地时区）→ ISO+Z；空串返回 undefined（不筛）
  const isoOrUndefined = (v: string) => (v ? new Date(v).toISOString() : undefined);

  const load = useCallback(async () => {
    if (!allowed) return;
    setBusy(true);
    setLoadError(null);
    try {
      const r = await getAuditLogs({
        action: action || undefined,
        q: committedQ.trim() || undefined,
        ip: committedIp.trim() || undefined,
        time_from: isoOrUndefined(fromLocal),
        time_to: isoOrUndefined(toLocal),
        page,
        page_size: PAGE_SIZE,
      });
      setItems(r.items ?? []);
      setTotal(r.total);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '加载失败');
      setItems([]);
    } finally {
      setBusy(false);
    }
  }, [allowed, action, committedQ, committedIp, fromLocal, toLocal, page]);

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
      // 租户名称映射：审计表只存 tenant_id，展示层翻译成可读名称
      getTenants()
        .then((r) => setTenantNames(new Map((r.items ?? []).map((t) => [t.id, t.name]))))
        .catch(() => {});
    }
  }, [allowed]);

  const hasFilter = Boolean(action || committedQ || committedIp || fromLocal || toLocal);

  const clearFilters = () => {
    setPage(1);
    setAction('');
    setQ('');
    setCommittedQ('');
    setIp('');
    setCommittedIp('');
    setFromLocal('');
    setToLocal('');
  };

  const doExport = async () => {
    setExporting(true);
    try {
      await exportAuditLogs({
        action: action || undefined,
        q: committedQ.trim() || undefined,
        ip: committedIp.trim() || undefined,
        time_from: isoOrUndefined(fromLocal),
        time_to: isoOrUndefined(toLocal),
      });
    } catch {
      /* 导出失败静默：按钮恢复即可（浏览器不触发下载即无文件） */
    } finally {
      setExporting(false);
    }
  };

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
      <PageHeader title="审计日志" description={`登录与操作留痕 · 共 ${total} 条 · 保留期内可追溯 · 哈希链防篡改`}>
        <button
          onClick={() => void doExport()}
          disabled={exporting}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 cursor-pointer disabled:opacity-50"
        >
          {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />} 导出 CSV
        </button>
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
        <div className="relative min-w-[13rem]">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setCommittedQ(q);
                setPage(1);
              }
            }}
            placeholder="按操作者邮箱筛选，回车确认"
            className="w-full pl-8 pr-3 py-2 rounded-lg border border-radar-border bg-radar-panel text-xs text-slate-100 outline-none focus:border-accent/60"
          />
        </div>
        <div className="relative min-w-[11rem]">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
          <input
            value={ip}
            onChange={(e) => setIp(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setCommittedIp(ip);
                setPage(1);
              }
            }}
            placeholder="按 IP 前缀筛选，回车确认"
            className="w-full pl-8 pr-3 py-2 rounded-lg border border-radar-border bg-radar-panel text-xs text-slate-100 font-mono outline-none focus:border-accent/60"
          />
        </div>
        <input
          type="datetime-local"
          value={fromLocal}
          onChange={(e) => {
            setPage(1);
            setFromLocal(e.target.value);
          }}
          aria-label="起始时间"
          className="rounded-lg border border-radar-border bg-radar-panel px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-accent/60 [color-scheme:dark]"
        />
        <span className="text-xs text-slate-500">至</span>
        <input
          type="datetime-local"
          value={toLocal}
          onChange={(e) => {
            setPage(1);
            setToLocal(e.target.value);
          }}
          aria-label="截止时间"
          className="rounded-lg border border-radar-border bg-radar-panel px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-accent/60 [color-scheme:dark]"
        />
        {hasFilter && (
          <button
            onClick={clearFilters}
            className="chip border border-radar-border bg-radar-card text-slate-400 hover:text-slate-200 cursor-pointer"
          >
            清除筛选
          </button>
        )}
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
              ) : loadError ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center">
                    <div className="flex flex-col items-center gap-2 text-amber-300">
                      <ShieldX className="h-6 w-6" />
                      <span>加载失败：{loadError}</span>
                      <button
                        onClick={() => void load()}
                        className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent cursor-pointer"
                      >
                        <RefreshCw className="h-3.5 w-3.5" /> 重试
                      </button>
                    </div>
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
                  const tooltip = [it.user_agent, JSON.stringify(it.detail ?? {})].filter(Boolean).join('\n');
                  return (
                    <tr key={it.id} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                      <td className="px-4 py-2 text-slate-400 whitespace-nowrap tabular-nums">{fmtTime(it.created_at)}</td>
                      <td className="px-2 py-2">
                        <span className={`chip text-[10px] border ${a.cls}`}>{a.label}</span>
                      </td>
                      <td className="px-2 py-2 text-slate-200">{it.user_email || '—'}</td>
                      <td className="px-2 py-2 text-slate-400">{it.tenant_id != null ? (tenantNames.get(it.tenant_id) ?? `#${it.tenant_id}`) : '—'}</td>
                      <td className="px-2 py-2 text-slate-400 font-mono">{it.ip || '—'}</td>
                      <td className="px-4 py-2 text-slate-400 max-w-sm truncate" title={tooltip}>
                        {detailText(it.action, it.detail ?? {})}
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
