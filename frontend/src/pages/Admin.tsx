/**
 * 运维监控页：运行状态总览（含任务节奏心跳）/ 运行时配置（含连通性测试）/ 任务执行记录 / 手动触发。
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
  RotateCcw,
  Save,
  Send,
  Server,
  ShieldCheck,
  ShieldX,
  XCircle,
} from 'lucide-react';
import { PageHeader, StatCard } from '../components/common';
import { loadReports } from '../api/client';
import { usePageTitle } from '../utils/hooks';
import { toDateKey } from '../utils/format';
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
  resetConfig,
  testEmbedding,
  testLlm,
  testReportPush,
  triggerCrawl,
  triggerEvents,
  triggerReport,
  triggerStats,
} from '../api/admin';

const FIELD_LABELS: Record<string, string> = {
  llm_base_url: 'LLM Base URL',
  llm_api_key: 'LLM API Key',
  llm_model: 'LLM 模型',
  llm_protocol: 'LLM 协议',
  embedding_base_url: 'Embedding Base URL',
  embedding_api_key: 'Embedding API Key',
  embedding_model: 'Embedding 模型',
  crawler_interval_minutes: '采集间隔（分钟）',
  sim_threshold: '事件聚合相似度阈值',
  keyword_jaccard_threshold: '标题关键词 Jaccard 阈值',
  high_band_kw_floor: '高位带关键词重合下限（跨来源佐证）',
  cluster_lookback_hours: '聚合回看窗口（小时）',
  cluster_window_hours: '聚类时间窗（小时）',
  admin_token: '管理后门 Token',
  session_ttl_hours: '会话有效期（小时）',
  session_idle_hours: '会话空闲超时（小时，0 = 不限制）',
  login_max_attempts: '登录失败锁定阈值（次）',
  login_lock_minutes: '登录锁定时长（分钟）',
  login_ip_throttle_max: '单 IP 登录尝试上限（窗口内）',
  login_ip_throttle_window_seconds: 'IP 限速窗口（秒）',
  cookie_secure: 'Cookie Secure（HTTPS 必开）',
  audit_retention_days: '审计日志保留（天）',
  stats_poll_enabled: '互动轮询开关',
  stats_poll_batch: '单轮轮询批量（篇）',
  stats_heal_batch: '自愈重排批次（篇，0 = 关闭）',
  alert_webhook_url: '告警 Webhook URL',
  alert_webhook_style: '告警消息格式',
  report_push_enabled: '早报推送总开关',
  report_push_wecom_webhook_url: '企业微信机器人 URL',
  report_push_feishu_webhook_url: '飞书机器人 URL',
  report_push_feishu_secret: '飞书签名密钥（未开启校验留空）',
  ai_daily_cost_budget: 'AI 日成本预算（元，0 = 不告警）',
  ai_usage_retention_days: 'AI 流水保留（天，0 = 永久）',
};

const SENSITIVE = new Set([
  'llm_api_key',
  'embedding_api_key',
  'admin_token',
  'alert_webhook_url',
  'report_push_wecom_webhook_url',
  'report_push_feishu_webhook_url',
  'report_push_feishu_secret',
]);

/** 数值字段前端校验规则（留空 = 恢复默认，允许）；后端 config_service 有同规则兜底 */
const NUMERIC_RULES: Record<string, { min: number; max: number }> = {
  crawler_interval_minutes: { min: 1, max: 1440 },
  sim_threshold: { min: 0, max: 1 },
  keyword_jaccard_threshold: { min: 0, max: 1 },
  high_band_kw_floor: { min: 0, max: 1 },
  cluster_lookback_hours: { min: 1, max: 720 },
  cluster_window_hours: { min: 1, max: 720 },
  session_ttl_hours: { min: 1, max: 8760 },
  session_idle_hours: { min: 0, max: 8760 },
  login_max_attempts: { min: 1, max: 100 },
  login_lock_minutes: { min: 1, max: 1440 },
  login_ip_throttle_max: { min: 5, max: 10000 },
  login_ip_throttle_window_seconds: { min: 60, max: 86400 },
  audit_retention_days: { min: 30, max: 3650 },
  stats_poll_batch: { min: 1, max: 50 },
  stats_heal_batch: { min: 0, max: 50 },
  ai_daily_cost_budget: { min: 0, max: 100000 },
  ai_usage_retention_days: { min: 0, max: 3650 },
};

const WEBHOOK_STYLES: { value: string; label: string }[] = [
  { value: 'wecom', label: '企业微信机器人' },
  { value: 'dingtalk', label: '钉钉机器人' },
  { value: 'feishu', label: '飞书机器人' },
  { value: 'raw', label: '通用 JSON（text 字段）' },
];

// LLM 双协议：anthropic = 原生 /v1/messages（DeepSeek /anthropic、中转网关等）
const LLM_PROTOCOLS: { value: string; label: string }[] = [
  { value: 'openai', label: 'OpenAI 兼容（/chat/completions）' },
  { value: 'anthropic', label: 'Anthropic 原生（/v1/messages）' },
];

// resettable：组头展示「恢复默认」按钮（整组删除 DB 覆盖、回落 .env 默认值）
const GROUPS: { title: string; hint: string; keys: string[]; resettable?: boolean }[] = [
  {
    title: 'LLM 服务（AI 摘要）',
    hint: '支持 OpenAI 兼容 / Anthropic 原生双协议；留空时系统自动使用内置抽取式摘要',
    keys: ['llm_base_url', 'llm_api_key', 'llm_model', 'llm_protocol'],
    resettable: true,
  },
  {
    title: 'Embedding 服务（事件聚合）',
    hint: '用于文章向量化与相似度聚类；向量维度需与 EMBED_DIM 一致',
    keys: ['embedding_base_url', 'embedding_api_key', 'embedding_model'],
    resettable: true,
  },
  {
    title: '爬虫与聚合参数',
    hint: '采集间隔、聚合阈值/窗口修改后立即生效，无需重启服务',
    keys: [
      'crawler_interval_minutes',
      'sim_threshold',
      'keyword_jaccard_threshold',
      'high_band_kw_floor',
      'cluster_window_hours',
      'cluster_lookback_hours',
    ],
  },
  {
    title: '互动数据轮询',
    hint: '评论/点赞数回采接入热度算法；关闭后热度回退纯时间衰减，任务表不再产生 stats 记录',
    keys: ['stats_poll_enabled', 'stats_poll_batch', 'stats_heal_batch'],
  },
  {
    title: '安全与会话',
    hint: '登录锁定 / IP 限速 / 空闲超时 / 审计保留期；admin_token 仅作脚本应急后门',
    keys: [
      'admin_token',
      'session_ttl_hours',
      'session_idle_hours',
      'login_max_attempts',
      'login_lock_minutes',
      'login_ip_throttle_max',
      'login_ip_throttle_window_seconds',
      'cookie_secure',
      'audit_retention_days',
    ],
  },
  {
    title: '告警通知',
    hint: '巡检任务每 5 分钟检查 Redis/Worker/任务停摆/失败未恢复，异常经 webhook 推送（状态转坏/恢复才发）；URL 留空 = 关闭',
    keys: ['alert_webhook_url', 'alert_webhook_style'],
  },
  {
    title: '早报推送（企业微信 / 飞书）',
    hint: '每日 08:00 生成当期「今日早报」后自动推送（不传日期的手动触发同样推送；历史补算/空报告不推）；总开关打开 + 任一通道填 URL 即启用，双通道可并存',
    keys: [
      'report_push_enabled',
      'report_push_wecom_webhook_url',
      'report_push_feishu_webhook_url',
      'report_push_feishu_secret',
    ],
  },
  {
    title: 'AI 成本治理',
    hint: '日预算超限经告警 webhook 推送（0 = 关闭，北京时间日界）；成本看板流水保留天数超期由周清理删除（0 = 永久保留，归档请先用看板导出 CSV）',
    keys: ['ai_daily_cost_budget', 'ai_usage_retention_days'],
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
  const [pushTestResult, setPushTestResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [revealKeys, setRevealKeys] = useState<Set<string>>(new Set());
  const [reportDate, setReportDate] = useState('');

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
      // 手动刷新绕过后端 10s 缓存，确保点「刷新」看到的是即时状态
      const [s, c, r] = await Promise.all([getStatus(true), getConfig(), getTaskRuns(50)]);
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

  // 进入面板即拉全量数据 + 每 30s 刷新状态与任务表（会话失效由全局登录态接管）。
  // 任务表必须随轮询刷新：手动触发后 TaskRun 行在 worker 消费时才落库，
  // 只靠触发后的固定补拉会在队列拥堵时永远看不到新记录
  useEffect(() => {
    if (allowed) {
      refreshAll();
      const timer = setInterval(() => {
        Promise.all([getStatus(), getTaskRuns(50)])
          .then(([s, r]) => {
            setStatus(s);
            setRuns(r.items ?? []);
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
  // 敏感键（api_key/admin_token/webhook url）后端不回传明文：留空 = 不修改，输入了内容才算待保存的修改
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
      setTestResult({});  // 旧「连通 ✓」对应的是改前配置，不清空会误导
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

  /** 整组恢复默认：删除该组全部 DB 覆盖，回落 .env 默认值（LLM 默认空 = 内置抽取摘要） */
  const resetGroup = async (g: (typeof GROUPS)[number]) => {
    const label = g.keys.map((k) => FIELD_LABELS[k] ?? k).join('、');
    const extraHint = g.title.startsWith('LLM') ? '\n（LLM 组默认未配置 = 回退内置抽取式摘要）' : '';
    if (
      !window.confirm(
        `确定把「${g.title}」整组恢复为 .env 默认值？\n将清除覆盖：${label}${extraHint}`,
      )
    )
      return;
    setBusy(`reset-${g.keys[0]}`);
    try {
      const next = await resetConfig(g.keys);
      setConfig(next);
      setEdits((m) => {
        const copy = { ...m };
        g.keys.forEach((k) => delete copy[k]);
        return copy;
      });
      // 旧「连通 ✓」对应的是改前配置，不清空会误导；只清本组的，别组的仍然有效
      setTestResult((r) => ({ ...r, [g.title.startsWith('LLM') ? 'llm' : 'embedding']: undefined }));
      flash(true, `已恢复 .env 默认：${label}（清除 ${next.reset.length} 项覆盖）`);
    } catch (e: any) {
      flash(false, `恢复默认失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  const runTest = async (which: 'llm' | 'embedding') => {
    setBusy(`test-${which}`);
    setTestResult((r) => ({ ...r, [which]: '' }));
    try {
      const payload: Record<string, string> = {};
      const baseKey = which === 'llm' ? 'llm' : 'embedding';
      (['base_url', 'api_key', 'model'] as const).forEach((suffix) => {
        const v = edits[`${baseKey}_${suffix}`];
        if (v !== undefined && v !== '') payload[suffix] = v;
      });
      // 协议下拉有未保存修改时随测试请求带上（与 base_url 等的「先测后存」同语义）
      if (which === 'llm' && edits.llm_protocol) payload.protocol = edits.llm_protocol;
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

  // 早报推送自检：立即推最新一期到已配置通道，配置对不对当场见分晓（不必等次日 08:00）
  const runPushTest = async () => {
    setBusy('test-push');
    setPushTestResult(null);
    try {
      const { results } = await testReportPush();
      const allOk = results.split('；').every((r) => r.endsWith(':ok'));
      setPushTestResult({ ok: allOk, text: allOk ? `推送成功：${results}` : `部分失败：${results}` });
      flash(allOk, `早报测试推送：${results}`);
    } catch (e: any) {
      setPushTestResult({ ok: false, text: `失败：${e?.message ?? e}` });
      flash(false, `早报测试推送失败：${e?.message ?? e}`);
    } finally {
      setBusy('');
    }
  };

  // 各触发按钮的 busy 键互相独立，避免共享前缀导致并发操作误解锁
  const trigger = async (name: 'crawl' | 'events' | 'stats' | 'report') => {
    setBusy(`trigger-${name}`);
    try {
      const fn =
        name === 'crawl'
          ? triggerCrawl
          : name === 'events'
          ? triggerEvents
          : name === 'stats'
          ? triggerStats
          : () => triggerReport(reportDate || undefined);
      await fn();
      flash(true, '任务已入队，任务表会在数秒内自动刷新');
      // TaskRun 行在 worker 消费时才落库：间隔补拉 3 次，避免固定 1.5s 拉空后看不到记录
      [2000, 6000, 12000].forEach((ms) =>
        later(() => {
          getTaskRuns(50)
            .then((r) => setRuns(r.items ?? []))
            .catch(() => {});
          // 早报生成入队后同步强制刷新早报缓存（绕过 60s 节流），今日早报页即可见
          if (name === 'report' && ms === 12000) void loadReports(true);
        }, ms)
      );
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
  const statsPoll = status?.stats_poll;

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

      {/* ———— 任务节奏心跳：beat 是单点且 worker ping 看不见它，停摆只能靠节奏异常感知 ———— */}
      {status && (status.heartbeat ?? []).length > 0 && (
        <div className="card px-4 py-3 border-amber-500/30 bg-amber-500/5 text-sm text-amber-200 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            任务节奏异常（超出预期时长未运行，疑似调度器/服务停摆）：
            <ul className="list-disc ml-4 mt-1 space-y-0.5 text-xs text-amber-300/90">
              {status.heartbeat.map((h) => (
                <li key={h.name}>
                  <b>{h.label}</b>（{h.name}）最近运行 {fmtTime(h.last_run_at)}，距今 {h.age_minutes} 分钟（阈值 {h.threshold_minutes} 分钟）
                </li>
              ))}
            </ul>
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
          <StatCard
            icon={<RefreshCw className="h-4 w-4" />}
            label="最近采集"
            value={fmtTime(status.crawler.last_run?.started_at ?? stats?.last_crawl ?? null)}
            sub={`采集间隔 ${status?.crawler.interval_minutes ?? '—'} 分钟 · 最近入库 ${fmtTime(stats?.last_ingest ?? null)}`}
          />
        </div>

        {/* ———— 互动轮询水位（此前后端已计算但面板从未展示） ———— */}
        {statsPoll && (
          <>
            <div className="flex items-baseline justify-between pt-1">
              <h3 className="text-xs font-semibold text-slate-400">互动数据轮询水位</h3>
              <span className={`text-[11px] ${statsPoll.enabled === false ? 'text-slate-500' : 'text-emerald-300'}`}>
                {statsPoll.enabled === false
                  ? '已热停：热度回退纯时间衰减，不再产生轮询记录'
                  : statsPoll.last_run
                  ? `最近一轮 ${fmtTime(statsPoll.last_run.started_at)} · ${statsPoll.last_run.status === 'success' ? '成功' : statsPoll.last_run.status === 'failed' ? '失败' : statsPoll.last_run.status === 'degraded' ? '降级' : '运行中'}`
                  : '尚无轮询记录'}
              </span>
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <StatCard icon={<RefreshCw className="h-4 w-4" />} label="到期待轮询" value={String(statsPoll.due_now)} sub="已排期且已到期的文章" />
              <StatCard icon={<Database className="h-4 w-4" />} label="待回填" value={String(statsPoll.pending_backfill)} sub="未排期未放弃的历史文章" />
              <StatCard icon={<AlertTriangle className="h-4 w-4" />} label="已放弃" value={String(statsPoll.gave_up)} sub="连续失败达上限，不再排期" />
              <StatCard icon={<CheckCircle2 className="h-4 w-4" />} label="24h 已轮询" value={String(statsPoll.polled_24h)} sub="近 24 小时完成过回采的文章" />
            </div>
          </>
        )}
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
                <div className="flex items-center gap-2">
                  <div className="text-sm font-medium text-slate-200">{g.title}</div>
                  {g.resettable && (
                    <button
                      type="button"
                      onClick={() => void resetGroup(g)}
                      disabled={busy === `reset-${g.keys[0]}`}
                      title="清除该组全部 DB 覆盖，回落 .env 默认值（LLM 组默认未配置 = 内置抽取式摘要）"
                      className="chip border border-radar-border bg-radar-panel text-slate-400 hover:text-amber-300 hover:border-amber-300/40 disabled:opacity-40 cursor-pointer"
                    >
                      {busy === `reset-${g.keys[0]}` ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <RotateCcw className="h-3 w-3" />
                      )}
                      恢复默认
                    </button>
                  )}
                </div>
                <div className="text-[11px] text-slate-500">{g.hint}</div>
              </div>
              <div className="grid md:grid-cols-3 gap-3">
                {g.keys.map((key) => {
                  const item = config?.config[key];
                  if (!item) return null;
                  const isSensitive = SENSITIVE.has(key);
                  const isBool = item.type === 'bool';
                  // 下拉键 → 选项集（告警格式 / LLM 协议）；新增下拉配置在此挂选项
                  const selectOptions =
                    key === 'alert_webhook_style'
                      ? WEBHOOK_STYLES
                      : key === 'llm_protocol'
                        ? LLM_PROTOCOLS
                        : null;
                  const editing = edits[key];
                  // 敏感键不回填明文：只要输入框有内容即视为修改；非敏感键与 raw 对比
                  const changed = editing !== undefined && (isSensitive ? editing !== '' : editing !== item.raw);
                  const revealed = revealKeys.has(key);
                  const invalid = invalidKeys[key];
                  // 开关/下拉的当前值：优先未保存编辑，否则生效值
                  const current = editing ?? (item.raw || '');
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
                      {isBool ? (
                        // 布尔配置用开关：此前手填 true/false，拼写错误（如 flase）会被后端
                        // 静默当作 false，可能悄悄关闭 Secure Cookie / 互动轮询
                        <button
                          type="button"
                          role="switch"
                          aria-checked={current === 'true'}
                          aria-label={FIELD_LABELS[key] ?? key}
                          onClick={() =>
                            setEdits((m) => ({ ...m, [key]: current === 'true' ? 'false' : 'true' }))
                          }
                          className={`relative inline-flex h-5 w-9 mt-0.5 items-center rounded-full transition-colors cursor-pointer focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                            current === 'true' ? 'bg-emerald-500/80' : 'bg-slate-600/60'
                          }`}
                        >
                          <span
                            className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                              current === 'true' ? 'translate-x-[19px]' : 'translate-x-[3px]'
                            }`}
                          />
                        </button>
                      ) : selectOptions ? (
                        <select
                          value={current || selectOptions[0].value}
                          onChange={(e) => setEdits((m) => ({ ...m, [key]: e.target.value }))}
                          className="w-full px-2.5 py-1.5 rounded-lg bg-radar-panel border border-radar-border text-xs text-slate-200 outline-none focus:border-accent cursor-pointer"
                        >
                          {selectOptions.map((s) => (
                            <option key={s.value} value={s.value}>
                              {s.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="relative">
                          <input
                            type={isSensitive && !revealed ? 'password' : 'text'}
                            value={current}
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
                      )}
                      {invalid && <div className="text-[10px] text-red-400 mt-1">{invalid}</div>}
                    </label>
                  );
                })}
              </div>
              {(g.title.startsWith('LLM') || g.title.startsWith('Embedding')) && (
                <div className="mt-3 flex items-center gap-2">
                  <button
                    onClick={() => runTest(g.title.startsWith('LLM') ? 'llm' : 'embedding')}
                    disabled={busy === 'test-llm' || busy === 'test-embedding'}
                    className="chip border border-radar-border bg-radar-panel text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40"
                  >
                    {busy === 'test-llm' || busy === 'test-embedding' ? (
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
              {g.title.startsWith('早报推送') && (
                <div className="mt-3 flex items-center gap-2 flex-wrap">
                  <button
                    onClick={() => void runPushTest()}
                    disabled={busy === 'test-push'}
                    title="立即推送最新一期早报到已配置通道，验证 webhook 配置"
                    className="chip border border-radar-border bg-radar-panel text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40"
                  >
                    {busy === 'test-push' ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Send className="h-3.5 w-3.5" />
                    )}
                    测试推送（发送最新一期）
                  </button>
                  {pushTestResult && (
                    <span className={`text-xs ${pushTestResult.ok ? 'text-emerald-300' : 'text-red-400'}`}>
                      {pushTestResult.text}
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
            <input
              type="date"
              value={reportDate}
              onChange={(e) => setReportDate(e.target.value)}
              max={toDateKey(Date.now())}
              title="留空 = 生成今天的发布期刊（统计北京时间昨日全天）；选择日期可补生成任意一天（未来日期会被拒绝）"
              className="chip bg-radar-card border border-radar-border text-slate-400 [color-scheme:dark]"
            />
            <button onClick={() => trigger('crawl')} disabled={busy === 'trigger-crawl'} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 立即采集
            </button>
            <button onClick={() => trigger('events')} disabled={busy === 'trigger-events'} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 立即聚合
            </button>
            <button onClick={() => trigger('stats')} disabled={busy === 'trigger-stats'} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 立即轮询互动
            </button>
            <button onClick={() => trigger('report')} disabled={busy === 'trigger-report'} className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" /> 生成早报
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
                {runs.map((r) => {
                  // 双入口抢锁时另一入口的轮次记为 success + "skipped: ..."，显示为「跳过」而非绿色「成功」
                  const skipped = r.status === 'success' && (r.detail ?? '').startsWith('skipped');
                  return (
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
                        {r.status === 'success' && !skipped ? (
                          <span className="text-emerald-300 flex items-center gap-1"><CheckCircle2 className="h-3.5 w-3.5" />成功</span>
                        ) : r.status === 'failed' ? (
                          <span className="text-red-400 flex items-center gap-1"><XCircle className="h-3.5 w-3.5" />失败</span>
                        ) : r.status === 'degraded' ? (
                          <span className="text-amber-300 flex items-center gap-1" title={r.detail ?? ''}><AlertTriangle className="h-3.5 w-3.5" />降级</span>
                        ) : skipped ? (
                          <span className="text-amber-300 flex items-center gap-1" title={r.detail ?? ''}><AlertTriangle className="h-3.5 w-3.5" />跳过</span>
                        ) : (
                          <span className="text-amber-300 flex items-center gap-1"><Loader2 className="h-3.5 w-3.5 animate-spin" />运行中</span>
                        )}
                      </td>
                      <td className="px-2 py-2 text-slate-400 whitespace-nowrap">{fmtDuration(r.duration_ms)}</td>
                      <td className="px-4 py-2 text-slate-400 max-w-md truncate" title={r.detail ?? ''}>
                        {r.detail ?? '—'}
                      </td>
                    </tr>
                  );
                })}
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
