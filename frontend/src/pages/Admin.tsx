/**
 * 运维监控页：运行状态总览 / 运行时配置（含连通性测试）/ 任务执行记录 / 手动触发。
 * 数据源：/api/v2/admin/*（见 backend/app/api/v2/admin.py）。
 * 鉴权：走全局登录会话（RequireAuth 已保证已登录），本页仅平台超管可见。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Loader2,
  LogOut,
  Play,
  RefreshCw,
  Save,
  Server,
  ShieldCheck,
  ShieldX,
  XCircle,
} from 'lucide-react';
import { PageHeader, StatCard } from '../components/common';
import { loadReports } from '../api/client';
import { usePageTitle } from '../utils/hooks';
import { isSuperAdmin, setAnon, useAuth } from '../auth';
import { logout as authLogout } from '../api/auth';
import {
  AdminStatus,
  ConfigView,
  TaskRunItem,
  getConfig,
  getStatus,
  getTaskRuns,
  putConfig,
  testEmbedding,
  testLlm,
  triggerCrawl,
  triggerEvents,
  triggerReport,
} from '../api/admin';

const FIELD_LABELS: Record<string, string> = {
  llm_base_url: 'LLM Base URL',
  llm_api_key: 'LLM API Key',
  llm_model: 'LLM 模型',
  embedding_base_url: 'Embedding Base URL',
  embedding_api_key: 'Embedding API Key',
  embedding_model: 'Embedding 模型',
  crawler_interval_minutes: '采集间隔（分钟）',
  sim_threshold: '事件聚合相似度阈值',
  cluster_lookback_hours: '聚合回看窗口（小时）',
  admin_token: '管理后门 Token',
  session_ttl_hours: '会话有效期（小时）',
  login_max_attempts: '登录失败锁定阈值（次）',
  login_lock_minutes: '登录锁定时长（分钟）',
  cookie_secure: 'Cookie Secure（HTTPS 必开）',
  audit_retention_days: '审计日志保留（天）',
};

const SENSITIVE = new Set(['llm_api_key', 'embedding_api_key', 'admin_token']);

/** 数值字段前端校验规则（留空 = 恢复默认，允许） */
const NUMERIC_RULES: Record<string, { min: number; max: number }> = {
  crawler_interval_minutes: { min: 1, max: 1440 },
  sim_threshold: { min: 0, max: 1 },
  cluster_lookback_hours: { min: 1, max: 720 },
  session_ttl_hours: { min: 1, max: 8760 },
  login_max_attempts: { min: 1, max: 100 },
  login_lock_minutes: { min: 1, max: 1440 },
  audit_retention_days: { min: 7, max: 3650 },
};

const GROUPS: { title: string; hint: string; keys: string[] }[] = [
  {
    title: 'LLM 服务（AI 摘要）',
    hint: 'OpenAI 兼容协议；留空时系统自动使用内置抽取式摘要',
    keys: ['llm_base_url', 'llm_api_key', 'llm_model'],
  },
  {
    title: 'Embedding 服务（事件聚合）',
    hint: '用于文章向量化与相似度聚类；向量维度需与 EMBED_DIM 一致',
    keys: ['embedding_base_url', 'embedding_api_key', 'embedding_model'],
  },
  {
    title: '爬虫与聚合参数',
    hint: '采集间隔、聚合阈值修改后立即生效，无需重启服务',
    keys: ['crawler_interval_minutes', 'sim_threshold', 'cluster_lookback_hours'],
  },
  {
    title: '安全与会话',
    hint: '登录锁定 / 会话有效期 / 审计保留期；admin_token 仅作脚本应急后门',
    keys: [
      'admin_token',
      'session_ttl_hours',
      'login_max_attempts',
      'login_lock_minutes',
      'cookie_secure',
      'audit_retention_days',
    ],
  },
];

function fmtTime(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(ms: number | null): string {
  if (ms == null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

export default function Admin() {
  usePageTitle('运维监控');
  const { user } = useAuth();
  const allowed = isSuperAdmin(user);
  const [loadError, setLoadError] = useState('');   // 状态获取失败（非鉴权问题）

  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<TaskRunItem[]>([]);

  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [testResult, setTestResult] = useState<{ llm?: string; embedding?: string }>({});
  const [revealKeys, setRevealKeys] = useState<Set<string>>(new Set());

  // 组件卸载时清理所有延时任务（toast 自动消失、触发后补拉任务列表）
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

  const toggleReveal = (key: string) =>
    setRevealKeys((s) => {
      const next = new Set(s);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const refreshAll = useCallback(async () => {
    try {
      const [s, c, r] = await Promise.all([getStatus(), getConfig(), getTaskRuns(50)]);
      setStatus(s);
      setConfig(c);
      setRuns(r.items ?? []);
      setLoadError('');
    } catch (e: any) {
      // 401 已由请求层全局处理（setAnon → 跳登录页）；这里只管网络/后端错误：
      // 保留旧数据，显示明确错误并等待 30s 自动重试
      setLoadError(e?.status === 401 ? '登录已失效' : e?.message ?? '网络错误');
    }
  }, []);

  // 进入面板即拉全量数据 + 每 30s 刷新状态（会话失效由全局登录态接管）
  useEffect(() => {
    if (allowed) {
      refreshAll();
      const timer = setInterval(() => {
        getStatus()
          .then((s) => {
            setStatus(s);
            setLoadError('');
          })
          .catch(() => {
            /* 轮询错误静默等待下一轮 */
          });
      }, 30000);
      return () => clearInterval(timer);
    }
  }, [allowed, refreshAll]);

  // ———— 配置编辑 ————
  // 敏感键（api_key/admin_token）后端不回传明文：留空 = 不修改，输入了内容才算待保存的修改
  const dirtyKeys = useMemo(() => {
    if (!config) return [];
    return Object.keys(edits).filter((k) => {
      const v = edits[k] ?? '';
      if (SENSITIVE.has(k)) return v !== '';
      return v !== (config.config[k]?.raw ?? '');
    });
  }, [edits, config]);

  // 数值字段前端校验：非法值标红并阻止保存
  const invalidKeys = useMemo(() => {
    const out: Record<string, string> = {};
    Object.entries(edits).forEach(([k, v]) => {
      const rule = NUMERIC_RULES[k];
      if (!rule || v.trim() === '') return;
      const n = Number(v);
      if (!Number.isFinite(n) || n < rule.min || n > rule.max) {
        out[k] = `需为 ${rule.min} ~ ${rule.max} 之间的数值`;
      }
    });
    return out;
  }, [edits]);

  /** 退出登录：吊销当前会话并回到登录页（全局登录态接管跳转） */
  const logout = async () => {
    try {
      await authLogout();
    } finally {
      setRevealKeys(new Set());
      setAnon();
    }
  };

  const saveConfig = async () => {
    if (dirtyKeys.length === 0) return;
    if (Object.keys(invalidKeys).length > 0) {
      flash(false, `存在未通过校验的字段：${Object.keys(invalidKeys).map((k) => FIELD_LABELS[k] ?? k).join('、')}`);
      return;
    }
    setBusy('save');
    try {
      const data: Record<string, string> = {};
      dirtyKeys.forEach((k) => (data[k] = edits[k]));
      const next = await putConfig(data);
      setConfig(next);
      setEdits({});
      flash(true, `已保存并生效：${dirtyKeys.map((k) => FIELD_LABELS[k] ?? k).join('、')}`);
    } catch (e: any) {
      flash(false, `保存失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  /** 清除敏感键的 DB 覆盖：发送空串（后端语义 = 删除覆盖、回落 .env 默认值） */
  const clearOverride = async (key: string) => {
    if (!window.confirm(`确定清除「${FIELD_LABELS[key] ?? key}」的覆盖，回落 .env 默认值？`)) return;
    setBusy(`clear-${key}`);
    try {
      const next = await putConfig({ [key]: '' });
      setConfig(next);
      setEdits((m) => {
        const copy = { ...m };
        delete copy[key];
        return copy;
      });
      flash(true, `已清除覆盖：${FIELD_LABELS[key] ?? key}（回落 .env 默认）`);
    } catch (e: any) {
      flash(false, `清除失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  const runTest = async (which: 'llm' | 'embedding') => {    setBusy(`test-${which}`);
    setTestResult((r) => ({ ...r, [which]: '' }));
    try {
      const payload: Record<string, string> = {};
      const baseKey = which === 'llm' ? 'llm' : 'embedding';
      (['base_url', 'api_key', 'model'] as const).forEach((suffix) => {
        const v = edits[`${baseKey}_${suffix}`];
        if (v !== undefined && v !== '') payload[suffix] = v;
      });
      const text =
        which === 'llm'
          ? await (async () => {
              const r = await testLlm(payload);
              return `连通 ✓ 模型 ${r.model} · ${fmtDuration(r.latency_ms)} · 回复「${r.reply}」`;
            })()
          : await (async () => {
              const r = await testEmbedding(payload);
              return `连通 ✓ 模型 ${r.model} · ${fmtDuration(r.latency_ms)} · 维度 ${r.dim}${r.warning ? ' ⚠ ' + r.warning : '（与 EMBED_DIM 一致）'}`;
            })();
      setTestResult((res) => ({ ...res, [which]: text }));
    } catch (e: any) {
      setTestResult((res) => ({ ...res, [which]: `失败：${e?.message ?? e}` }));
    } finally {
      setBusy('');
    }
  };

  const trigger = async (name: 'crawl' | 'events' | 'report') => {
    setBusy(`trigger-${name}`);
    try {
      const fn = name === 'crawl' ? triggerCrawl : name === 'events' ? triggerEvents : triggerReport;
      await fn();
      flash(true, '任务已入队，稍后可在「任务执行记录」查看结果');
      later(() => {
        getTaskRuns(50).then((r) => setRuns(r.items ?? [])).catch(() => {});
        // 日报生成入队后同步强制刷新日报缓存（绕过 60s 节流），科技日报页即可见
        if (name === 'report') void loadReports(true);
      }, 1500);
    } catch (e: any) {
      flash(false, `触发失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  // ———— 访问控制（会话有效性由 RequireAuth 保证；这里只拦角色） ————
  if (!allowed) {
    return (
      <div>
        <PageHeader title="运维监控" description="服务管理面板 · 仅平台超管可见" />
        <div className="max-w-md card p-8 mx-auto mt-16 text-center">
          <div className="mx-auto mb-4 w-fit p-3 rounded-xl bg-red-500/10 text-red-400">
            <ShieldX className="h-7 w-7" />
          </div>
          <div className="text-sm font-semibold text-slate-100 mb-1.5">无访问权限</div>
          <p className="text-xs text-slate-500 leading-5">
            运维监控仅对「平台超管」开放。
            <br />
            当前账号：{user?.email}（{user?.role === 'tenant_admin' ? '租户管理员' : '成员'}）
          </p>
        </div>
      </div>
    );
  }

  const infra = status?.infra;
  const stats = status?.stats;

  return (
    <div className="space-y-6">
      <PageHeader title="运维监控" description={`服务状态 · ${status?.app ?? ''} v${status?.version ?? ''} · 每 30s 自动刷新`}>
        <button
          onClick={refreshAll}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40"
        >
          <RefreshCw className="h-3.5 w-3.5" /> 刷新
        </button>
        <button
          onClick={() => void logout()}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-red-300 hover:border-red-400/40"
        >
          <LogOut className="h-3.5 w-3.5" /> 退出登录
        </button>
      </PageHeader>

      {toast && (
        <div
          className={`card px-4 py-2.5 text-sm flex items-center gap-2 ${
            toast.ok ? 'text-emerald-300' : 'text-red-300'
          }`}
        >
          {toast.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {toast.text}
        </div>
      )}

      {status?.last_failed_task && (
        <div className="card px-4 py-3 border-amber-500/30 bg-amber-500/5 text-sm text-amber-200 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            最近失败任务：<b>{status.last_failed_task.name}</b>（{fmtTime(status.last_failed_task.started_at)}）
            <div className="text-xs text-amber-300/80 mt-0.5 break-all">{status.last_failed_task.detail}</div>
          </div>
        </div>
      )}

      {/* ———— 运行状态（状态未取到时显示重试提示，避免误报红灯） ———— */}
      {!status && (
        <div className="card px-4 py-5 text-sm text-amber-200 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            暂时无法获取运行状态{loadError ? `：${loadError}` : ''}。后端可能正在重启，页面会在 30 秒内自动重试，也可点右上角「刷新」立即重试。
          </div>
        </div>
      )}
      {status && loadError && (
        <div className="card px-4 py-2.5 text-xs text-amber-200 flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          刷新失败（{loadError}），以下为上次获取的数据 · 30 秒内自动重试
        </div>
      )}
      {status && (
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
          <Server className="h-4 w-4 text-accent" /> 基础设施
        </h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <HealthCard
            icon={<Database className="h-4 w-4" />}
            label="PostgreSQL"
            ok={!!infra?.db}
            sub={infra?.db ? '连接正常' : '连接失败'}
          />
          <HealthCard
            icon={<Server className="h-4 w-4" />}
            label="Redis"
            ok={!!infra?.redis}
            sub={infra?.redis ? `连通 · ${infra.redis_latency_ms != null ? `${infra.redis_latency_ms}ms` : '—'}` : '连接失败'}
          />
          <HealthCard
            icon={<Bot className="h-4 w-4" />}
            label="Celery Workers"
            ok={(infra?.celery_workers?.length ?? 0) > 0}
            sub={
              infra?.celery_workers?.length
                ? `${infra.celery_workers.length} 个在线：${infra.celery_workers.join('、')}`
                : infra?.celery_workers_error
                ? '无响应'
                : '未检测到'
            }
          />
          <HealthCard
            icon={<ShieldCheck className="h-4 w-4" />}
            label="AI 服务配置"
            ok={
              !!(status?.ai.llm_configured || status?.ai.embedding_configured) &&
              !status?.ai.embedding_stale
            }
            sub={`LLM ${status?.ai.llm_configured ? '已配置' : '未配置（内置抽取）'} · Embedding ${
              status?.ai.embedding_configured ? '已配置' : '未配置'
            }${status?.ai.embedding_stale ? ' · ⚠ 模型已变更，存量向量需重嵌' : ''}`}
          />
        </div>

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard icon={<Database className="h-4 w-4" />} label="文章总量" value={String(stats?.articles_total ?? '—')} sub={`近 24h 新增 ${stats?.articles_24h ?? '—'} 篇`} />
          <StatCard icon={<Bot className="h-4 w-4" />} label="AI 摘要覆盖" value={`${stats?.ai_coverage ?? '—'}%`} sub={`已摘要 ${stats?.articles_summarized ?? '—'} · 已向量化 ${stats?.articles_embedded ?? '—'}`} />
          <StatCard icon={<Play className="h-4 w-4" />} label="活跃事件" value={String(stats?.events_active ?? '—')} sub="状态非「已平息」" />
          <StatCard icon={<RefreshCw className="h-4 w-4" />} label="最近采集" value={fmtTime(stats?.last_crawl ?? null)} sub={`采集间隔 ${status?.crawler.interval_minutes ?? '—'} 分钟`} />
        </div>
      </section>
      )}

      {/* ———— 运行时配置 ———— */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-accent" /> 运行时配置
            <span className="text-[11px] font-normal text-slate-500">DB 覆盖优先于 .env，保存立即生效</span>
          </h2>
          <button
            onClick={saveConfig}
            disabled={busy === 'save' || dirtyKeys.length === 0}
            className="chip bg-accent/90 hover:bg-accent text-white border border-accent/50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy === 'save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            保存{dirtyKeys.length > 0 ? `（${dirtyKeys.length} 项修改）` : ''}
          </button>
        </div>

        <div className="space-y-3">
          {GROUPS.map((g) => (
            <div key={g.title} className="card p-4">
              <div className="flex items-baseline justify-between mb-3">
                <div className="text-sm font-medium text-slate-200">{g.title}</div>
                <div className="text-[11px] text-slate-500">{g.hint}</div>
              </div>
              <div className="grid md:grid-cols-3 gap-3">
                {g.keys.map((key) => {
                  const item = config?.config[key];
                  if (!item) return null;
                  const isSensitive = SENSITIVE.has(key);
                  const editing = edits[key];
                  // 敏感键不回填明文：只要输入框有内容即视为修改；非敏感键与 raw 对比
                  const changed = editing !== undefined && (isSensitive ? editing !== '' : editing !== item.raw);
                  const revealed = revealKeys.has(key);
                  const invalid = invalidKeys[key];
                  return (
                    <label key={key} className="block">
                      <div className="flex items-center gap-1.5 mb-1">
                        <span className="text-xs text-slate-400">{FIELD_LABELS[key] ?? key}</span>
                        <span
                          className={`text-[10px] px-1 rounded ${
                            item.source === 'db'
                              ? 'bg-accent/10 text-accent'
                              : 'bg-slate-500/10 text-slate-500'
                          }`}
                        >
                          {item.source === 'db' ? 'DB 覆盖' : '.env 默认'}
                        </span>
                        {changed && <span className="text-[10px] text-amber-300">已修改</span>}
                        {isSensitive && item.overridden && (
                          <button
                            type="button"
                            onClick={() => clearOverride(key)}
                            disabled={busy === `clear-${key}`}
                            title="删除该配置的 DB 覆盖，回落 .env 默认值"
                            className="text-[10px] text-slate-500 hover:text-red-400 disabled:opacity-40 cursor-pointer"
                          >
                            清除覆盖
                          </button>
                        )}
                      </div>
                      <div className="relative">
                        <input
                          type={isSensitive && !revealed ? 'password' : 'text'}
                          value={editing ?? (item.raw ?? '')}
                          onChange={(e) => setEdits((m) => ({ ...m, [key]: e.target.value }))}
                          placeholder={
                            isSensitive
                              ? item.value
                                ? '已设置 · 留空不修改，输入新值即覆盖'
                                : '未设置，输入后保存'
                              : ''
                          }
                          autoComplete="off"
                          inputMode={NUMERIC_RULES[key] ? 'decimal' : undefined}
                          className={`w-full px-2.5 py-1.5 ${isSensitive ? 'pr-8' : ''} rounded-lg bg-radar-panel border text-xs text-slate-200 outline-none focus:border-accent font-mono ${
                            invalid ? 'border-red-400/60' : 'border-radar-border'
                          }`}
                        />
                        {isSensitive && (
                          <button
                            type="button"
                            onClick={() => toggleReveal(key)}
                            title={revealed ? '隐藏' : '显示'}
                            aria-label={revealed ? `隐藏${FIELD_LABELS[key] ?? key}` : `显示${FIELD_LABELS[key] ?? key}`}
                            className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-slate-500 hover:text-slate-300 cursor-pointer"
                          >
                            {revealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                          </button>
                        )}
                      </div>
                      {invalid && <div className="text-[10px] text-red-400 mt-1">{invalid}</div>}
                    </label>
                  );
                })}
              </div>
              {(g.title.startsWith('LLM') || g.title.startsWith('Embedding')) && (
                <div className="mt-3 flex items-center gap-2">
                  <button
                    onClick={() => runTest(g.title.startsWith('LLM') ? 'llm' : 'embedding')}
                    disabled={busy.startsWith('test-')}
                    className="chip border border-radar-border bg-radar-panel text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40"
                  >
                    {busy.startsWith('test-') ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                    测试连通（先用未保存的输入，再回退已保存值）
                  </button>
                  {testResult[g.title.startsWith('LLM') ? 'llm' : 'embedding'] && (
                    <span
                      className={`text-xs ${
                        testResult[g.title.startsWith('LLM') ? 'llm' : 'embedding']!.startsWith('失败')
                          ? 'text-red-400'
                          : 'text-emerald-300'
                      }`}
                    >
                      {testResult[g.title.startsWith('LLM') ? 'llm' : 'embedding']}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* ———— 手动触发 + 任务记录 ———— */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
            <Play className="h-4 w-4 text-accent" /> 任务执行记录
          </h2>
          <div className="flex items-center gap-2">
            <button onClick={() => trigger('crawl')} disabled={busy.startsWith('trigger-')} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 立即采集
            </button>
            <button onClick={() => trigger('events')} disabled={busy.startsWith('trigger-')} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 立即聚合
            </button>
            <button onClick={() => trigger('report')} disabled={busy.startsWith('trigger-')} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 生成昨日日报
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
                  <th className="text-left font-medium px-2 py-2.5">触发</th>
                  <th className="text-left font-medium px-2 py-2.5">状态</th>
                  <th className="text-left font-medium px-2 py-2.5">耗时</th>
                  <th className="text-left font-medium px-4 py-2.5">结果 / 错误</th>
                </tr>
              </thead>
              <tbody>
                {runs.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                      暂无记录（任务执行后自动生成）
                    </td>
                  </tr>
                )}
                {runs.map((r) => (
                  <tr key={r.id} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                    <td className="px-4 py-2 text-slate-400 whitespace-nowrap">{fmtTime(r.started_at)}</td>
                    <td className="px-2 py-2 text-slate-200">{r.name}</td>
                    <td className="px-2 py-2">
                      <span
                        className={`chip text-[10px] ${
                          r.trigger === 'manual'
                            ? 'bg-sky-500/10 text-sky-300'
                            : 'bg-slate-500/10 text-slate-400'
                        }`}
                      >
                        {r.trigger === 'manual' ? '手动' : '定时'}
                      </span>
                    </td>
                    <td className="px-2 py-2">
                      {r.status === 'success' ? (
                        <span className="text-emerald-300 flex items-center gap-1"><CheckCircle2 className="h-3.5 w-3.5" />成功</span>
                      ) : r.status === 'failed' ? (
                        <span className="text-red-400 flex items-center gap-1"><XCircle className="h-3.5 w-3.5" />失败</span>
                      ) : (
                        <span className="text-amber-300 flex items-center gap-1"><Loader2 className="h-3.5 w-3.5 animate-spin" />运行中</span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-slate-400 whitespace-nowrap">{fmtDuration(r.duration_ms)}</td>
                    <td className="px-4 py-2 text-slate-400 max-w-md truncate" title={r.detail ?? ''}>
                      {r.detail ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}

function HealthCard({
  icon,
  label,
  ok,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  ok: boolean;
  sub: string;
}) {
  return (
    <div className="card px-4 py-3.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <span className={ok ? 'text-accent' : 'text-slate-500'}>{icon}</span>
          {label}
        </div>
        <span
          className={`h-2 w-2 rounded-full ${
            ok ? 'bg-emerald-400 shadow-[0_0_6px] shadow-emerald-400/60' : 'bg-red-500'
          }`}
        />
      </div>
      <div className={`text-xs mt-2 ${ok ? 'text-slate-300' : 'text-red-300'} leading-4 break-all`}>{sub}</div>
    </div>
  );
}
