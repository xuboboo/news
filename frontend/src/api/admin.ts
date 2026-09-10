/**
 * 管理面 API 客户端（对接后端 /api/v2/admin/*）。
 * 鉴权：HttpOnly 会话 Cookie（同源自动携带），前端不接触令牌；
 * X-Admin-Token 后门仅供服务端脚本/回归使用，前端一律不发送。
 * 全部变更请求带 X-Requested-With 头（后端 CSRF 防线）。
 */
import { setAnon } from '../auth';

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE ?? '';

function httpError(status: number, message: string): Error & { status?: number } {
  const err = new Error(message) as Error & { status?: number };
  err.status = status;
  return err;
}

async function request<T = any>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/api/v2/admin${path}`, {
      ...init,
      cache: 'no-store',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw httpError(0, '网络错误 · 后端服务不可达');
  }
  if (!resp.ok) {
    // 会话失效：全局反映到登录态（RequireAuth 跳登录页），同时保留原错误语义
    if (resp.status === 401) setAnon();
    let message = `HTTP ${resp.status}`;
    try {
      const text = await resp.text();
      try {
        const body = JSON.parse(text);
        if (typeof body?.detail === 'string') message = body.detail;
      } catch {
        if (text.trimStart().startsWith('<')) {
          message = `后端不可达（HTTP ${resp.status}，返回了页面而非接口数据）`;
        }
      }
    } catch {
      /* 读取错误体失败，保留 HTTP 状态提示 */
    }
    throw httpError(resp.status, message);
  }
  // 200 响应也可能是 SPA fallback 的 HTML，防御性解析
  const text = await resp.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw httpError(resp.status, '后端不可达：接口返回了非 JSON 内容');
  }
}

// ———————————————————— 运行状态 ————————————————————

export interface AdminStatus {
  app: string;
  version: string;
  time: string;
  infra: {
    db: boolean;
    redis: boolean;
    redis_latency_ms: number | null;
    celery_workers: string[];
    celery_workers_error: string | null;
  };
  ai: {
    llm_configured: boolean;
    embedding_configured: boolean;
    /** Embedding 模型指纹（model|dim），无记录为 null */
    embedding_fingerprint?: string | null;
    /** 生效 Embedding 配置与上次成功写入向量时不一致（提示需要重嵌） */
    embedding_stale?: boolean;
  };
  crawler: {
    last_run: {
      status: string;
      trigger: string;
      started_at: string | null;
      duration_ms: number | null;
      detail: string | null;
    } | null;
    interval_minutes: number;
  };
  /** 互动数据轮询水位（到期/待回填/已放弃/近 24h 轮询量） */
  stats_poll: {
    enabled: boolean | null;
    due_now: number;
    pending_backfill: number;
    gave_up: number;
    polled_24h: number;
    last_run: {
      status: string;
      trigger: string;
      started_at: string | null;
      duration_ms: number | null;
      detail: string | null;
    } | null;
  };
  /** 任务节奏心跳：超出预期节奏仍未运行的任务（beat 停摆时这里会亮） */
  heartbeat: {
    name: string;
    label: string;
    last_run_at: string;
    age_minutes: number;
    threshold_minutes: number;
  }[];
  stats: {
    articles_total: number;
    articles_summarized: number;
    articles_embedded: number;
    events_active: number;
    articles_24h: number;
    ai_coverage: number;
    /** 最近一轮采集（TaskRun 口径：采集正常但 0 新文章时不会变旧） */
    last_crawl: string | null;
    /** 最近一篇文章入库时间 */
    last_ingest: string | null;
  };
  last_failed_task: {
    name: string;
    status: string;
    started_at: string | null;
    detail: string | null;
  } | null;
}

/** fresh=true 跳过后端 10s 缓存强制重算（手动刷新按钮用） */
export const getStatus = (fresh = false) =>
  request<AdminStatus>(`/status${fresh ? '?fresh=true' : ''}`);

// ———————————————————— 运行时配置 ————————————————————

export interface ConfigItem {
  value: string;
  /** 生效值原文；敏感键（api_key / admin_token / alert_webhook_url）后端不再回传明文，恒为 null */
  raw: string | null;
  source: 'env' | 'db';
  overridden: boolean;
  /** 配置类型：bool 渲染开关，int/float 渲染数字框，string 渲染文本框 */
  type: 'str' | 'int' | 'float' | 'bool';
}

export interface ConfigView {
  config: Record<string, ConfigItem>;
  allowlist: string[];
  sensitive: string[];
}

export const getConfig = () => request<ConfigView>('/config');

export const putConfig = (data: Record<string, string>) =>
  request<ConfigView & { updated: string[] }>('/config', {
    method: 'PUT',
    body: JSON.stringify(data),
  });

export const testLlm = (payload: { base_url?: string; api_key?: string; model?: string }) =>
  request<{ ok: true; latency_ms: number; model: string; reply: string }>('/test/llm', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

export const testEmbedding = (payload: { base_url?: string; api_key?: string; model?: string }) =>
  request<{
    ok: true;
    latency_ms: number;
    model: string;
    dim: number;
    dim_expected: number;
    dim_match: boolean;
    warning: string | null;
  }>('/test/embedding', { method: 'POST', body: JSON.stringify(payload) });

// ———————————————————— 任务记录与手动触发 ————————————————————

export interface TaskRunItem {
  id: number;
  name: string;
  trigger: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  detail: string | null;
}

export const getTaskRuns = (limit = 50) =>
  request<{ items: TaskRunItem[] }>(`/task-runs?limit=${limit}`);

// 触发接口挂在 /api/v2/admin/crawl|events|stats|report（routes.py），与上面同一前缀
export const triggerCrawl = () => request<{ queued: boolean; task_id: string }>('/crawl', { method: 'POST' });
export const triggerEvents = () => request<{ queued: boolean; task_id: string }>('/events', { method: 'POST' });
export const triggerStats = () => request<{ queued: boolean; task_id: string }>('/stats', { method: 'POST' });
export const triggerReport = (date?: string) =>
  request<{ queued: boolean; task_id: string }>(`/report${date ? `?date=${date}` : ''}`, { method: 'POST' });

// ———————————————————— Token 用量与成本 ————————————————————

export type UsagePeriod = 'today' | '7d' | '30d' | 'month';

export interface UsageGroup {
  key: string;
  calls: number;
  tokens: number;
  cost: number;
}

export interface UsageDaily {
  date: string;
  calls: number;
  tokens: number;
  cost: number;
}

export interface UsageSummary {
  period: UsagePeriod;
  start: string;
  total_calls: number;
  total_tokens: number;
  total_cost: number;
  /** 缓存命中的输入 token 合计（按缓存价/输入价计，见价目表 price_cache） */
  total_cached: number;
  /** 输入 token 合计（缓存命中率的分母） */
  total_prompt: number;
  by_task_type: UsageGroup[];
  by_model: UsageGroup[];
  /** 近窗口有调用但未配价、且非本地降级模型的模型（成本按 0 计的盲区告警） */
  unpriced_models: UsageGroup[];
  summary_total: number;
  llm_coverage: number;
  embedding_api_fail: number;
  event_summary_calls_24h: number;
  daily: UsageDaily[];
}

export interface UsageSummaryOpts {
  /** 近 30 天按日趋势（仅 30d 视图消费；短周期请求关闭可省一次全窗口扫描） */
  daily?: boolean;
  /** 按任务/模型分组（同上） */
  groups?: boolean;
}

export const getUsageSummary = (period: UsagePeriod, opts?: UsageSummaryOpts) => {
  const qs = new URLSearchParams({ period });
  if (opts?.daily === false) qs.set('daily', '0');
  if (opts?.groups === false) qs.set('groups', '0');
  return request<UsageSummary>(`/usage/summary?${qs.toString()}`);
};

export interface UsageLogFilters {
  task_type?: string;
  status?: string;
}

export interface UsageLogItem {
  id: number;
  created_at: string | null;
  task_type: string;
  trigger: string;
  model: string;
  status: string;
  article_id: number | null;
  article_title: string | null;
  event_id: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  latency_ms: number;
  cost: number;
  error: string | null;
}

export interface UsageLogsPage {
  items: UsageLogItem[];
  total: number;
  page: number;
  page_size: number;
}

export const getUsageLogs = (page = 1, pageSize = 50, filters?: UsageLogFilters) => {
  const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (filters?.task_type) qs.set('task_type', filters.task_type);
  if (filters?.status) qs.set('status', filters.status);
  return request<UsageLogsPage>(`/usage/logs?${qs.toString()}`);
};

export interface ModelPriceItem {
  model: string;
  price_input: number;
  price_output: number;
  /** 缓存命中输入单价；null = 按输入价全价计 */
  price_cache: number | null;
  updated_at: string | null;
}

export const getUsagePrices = () => request<{ items: ModelPriceItem[] }>('/usage/prices');

export const upsertUsagePrice = (item: {
  model: string;
  price_input: number;
  price_output: number;
  /** null = 清除缓存价（回落输入价全价口径） */
  price_cache: number | null;
}) =>
  request<{ ok: boolean; created: boolean; item: ModelPriceItem }>('/usage/prices', {
    method: 'POST',
    body: JSON.stringify(item),
  });

/** 导出流水 CSV：会话 Cookie 同源自动携带，fetch + blob 触发浏览器下载。
 * range 可选（含端点、UTC 日界），不传 = 全量导出。 */
export async function exportUsageCsv(range?: { start?: string; end?: string }): Promise<void> {
  const qs = new URLSearchParams();
  if (range?.start) qs.set('start', range.start);
  if (range?.end) qs.set('end', range.end);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  const resp = await fetch(`${API_BASE}/api/v2/admin/usage/export${suffix}`, {
    cache: 'no-store',
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    // 尽量透出后端 detail，而不是干巴巴的 HTTP 状态码
    let message = `HTTP ${resp.status}`;
    try {
      const body = JSON.parse(await resp.text());
      if (typeof body?.detail === 'string') message = body.detail;
    } catch {
      /* 保留状态码提示 */
    }
    throw new Error(message);
  }
  // SPA fallback 会以 200 返回 index.html，不能把 HTML 当 CSV 下载
  if ((resp.headers.get('content-type') ?? '').includes('text/html')) {
    throw new Error('后端不可达：接口返回了页面而非 CSV');
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const scope = range?.start || range?.end ? `-${range.start ?? 'begin'}-${range.end ?? 'now'}` : '';
  a.download = `ai-usage${scope}-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ———————————————————— 租户与用户管理 ————————————————————

export interface TenantItem {
  id: number;
  name: string;
  status: string;
  remark: string;
  user_count: number;
  created_at: string | null;
}

export const getTenants = () => request<{ items: TenantItem[] }>('/tenants');

export const createTenant = (data: { name: string; remark?: string }) =>
  request<{ ok: boolean; item: TenantItem }>('/tenants', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const updateTenant = (id: number, data: { name?: string; remark?: string; status?: string }) =>
  request<{ ok: boolean }>(`/tenants/${id}`, { method: 'PUT', body: JSON.stringify(data) });

export interface ManagedUser {
  id: number;
  email: string;
  display_name: string;
  role: 'super_admin' | 'tenant_admin' | 'member';
  status: string;
  tenant_id: number;
  tenant_name: string | null;
  must_change_password: boolean;
  /** 登录失败锁定：true = 锁定中（locked_until 前无法登录） */
  locked: boolean;
  locked_until: string | null;
  last_login_at: string | null;
  created_by: string;
  created_at: string | null;
}

export interface UsersPage {
  items: ManagedUser[];
  total: number;
}

export const getUsers = (params: {
  tenant_id?: number;
  status?: string;
  q?: string;
  page?: number;
  page_size?: number;
}) => {
  const qs = new URLSearchParams();
  if (params.tenant_id != null) qs.set('tenant_id', String(params.tenant_id));
  if (params.status) qs.set('status', params.status);
  if (params.q) qs.set('q', params.q);
  qs.set('page', String(params.page ?? 1));
  qs.set('page_size', String(params.page_size ?? 50));
  return request<UsersPage>(`/users?${qs.toString()}`);
};

export const createUser = (data: {
  email: string;
  password: string;
  display_name?: string;
  role: string;
  tenant_id?: number;
}) =>
  request<{ ok: boolean; item: ManagedUser }>('/users', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const updateUser = (
  id: number,
  data: { display_name?: string; status?: string; role?: string; tenant_id?: number }
) =>
  request<{ ok: boolean; item: ManagedUser }>(`/users/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });

export const resetUserPassword = (id: number, password: string) =>
  request<{ ok: boolean }>(`/users/${id}/password-reset`, {
    method: 'POST',
    body: JSON.stringify({ password }),
  });

/** 解除登录失败锁定（清零失败计数与锁定时间），不动密码与会话 */
export const unlockUser = (id: number) =>
  request<{ ok: boolean; locked: boolean }>(`/users/${id}/unlock`, { method: 'POST' });

// ———————————————————— 审计日志 ————————————————————

export interface AuditLogItem {
  id: number;
  created_at: string | null;
  user_id: number | null;
  user_email: string;
  tenant_id: number | null;
  action: string;
  detail: Record<string, unknown>;
  ip: string;
  user_agent: string;
}

export const getAuditLogs = (params: {
  action?: string;
  user_id?: number;
  tenant_id?: number;
  q?: string;
  ip?: string;
  time_from?: string;
  time_to?: string;
  page?: number;
  page_size?: number;
}) => {
  const qs = auditLogQs(params);
  return request<{ items: AuditLogItem[]; total: number }>(`/audit-logs?${qs.toString()}`);
};

/** 列表与导出共用的筛选参数序列化 */
function auditLogQs(params: {
  action?: string; user_id?: number; tenant_id?: number; q?: string;
  ip?: string; time_from?: string; time_to?: string; page?: number; page_size?: number;
}) {
  const qs = new URLSearchParams();
  if (params.action) qs.set('action', params.action);
  if (params.user_id != null) qs.set('user_id', String(params.user_id));
  if (params.tenant_id != null) qs.set('tenant_id', String(params.tenant_id));
  if (params.q) qs.set('q', params.q);
  if (params.ip) qs.set('ip', params.ip);
  if (params.time_from) qs.set('time_from', params.time_from);
  if (params.time_to) qs.set('time_to', params.time_to);
  if (params.page != null) qs.set('page', String(params.page));
  if (params.page_size != null) qs.set('page_size', String(params.page_size));
  return qs;
}

/** CSV 导出：走流式响应下载为文件（GET 不需要 CSRF 头，Cookie 同源自动携带） */
export const exportAuditLogs = async (params: {
  action?: string; q?: string; ip?: string; time_from?: string; time_to?: string;
}): Promise<void> => {
  const resp = await fetch(`${API_BASE}/api/v2/admin/audit-logs/export?${auditLogQs(params).toString()}`, {
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (resp.status === 401) setAnon();
  if (!resp.ok) throw httpError(resp.status, `导出失败（HTTP ${resp.status}）`);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `audit-logs-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

export const getAuditActions = () =>
  request<{ items: string[] }>('/audit-actions');
