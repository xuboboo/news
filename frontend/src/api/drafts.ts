/**
 * 文章草稿 API 客户端（对接后端 /api/v2/drafts*）。
 * 鉴权：HttpOnly 会话 Cookie（同源自动携带），前端不接触令牌；
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
    resp = await fetch(`${API_BASE}/api/v2${path}`, {
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
  const text = await resp.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw httpError(resp.status, '后端不可达：接口返回了非 JSON 内容');
  }
}

export const draftRequest = request;

// ———————————————————— 类型 ————————————————————

export type DraftStatus = 'generating' | 'ready' | 'failed';
export type DraftTier = 'brief' | 'depth';
export type DraftSourceType = 'event' | 'article';

export interface DraftSourceReference {
  source: string;
  title: string;
}

export interface DraftItem {
  id: number;
  status: DraftStatus;
  source_type: DraftSourceType;
  source_id: number;
  source_title: string;
  tier: DraftTier;
  title: string;
  alt_titles: string[];
  digest: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  duration_ms: number;
  error: string;
  gen_count: number;
  revision: number;
  source_refs: DraftSourceReference[];
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
  content_md?: string;
}

export interface DraftListPage<T extends DraftItem = DraftItem> {
  items: T[];
  total: number;
}

// ———————————————————— 接口 ————————————————————

/** 按选题创建草稿并入队 AI 生成（后端校验源存在与开关状态）。 */
export const createDraft = (sourceType: DraftSourceType, sourceId: string | number, tier: DraftTier) => {
  // 事件在页面中的 ID 为 "e3"，接口需要数据库的整数 ID。避免 NaN 被 JSON.stringify
  // 静默转成 null，使所有事件选题都以 400 失败。
  const raw = sourceType === 'event' && typeof sourceId === 'string' && sourceId.startsWith('e')
    ? sourceId.slice(1)
    : sourceId;
  const id = Number(raw);
  if (!Number.isSafeInteger(id) || id <= 0 || String(raw).trim() === '') {
    return Promise.reject(new Error('选题编号无效，请刷新页面重试'));
  }
  return request<DraftItem>('/drafts', {
    method: 'POST',
    body: JSON.stringify({ source_type: sourceType, source_id: id, tier }),
  });
};

export const listDrafts = (status: DraftStatus | '', limit = 50, offset = 0, beforeId?: number) => {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) qs.set('status', status);
  if (beforeId !== undefined) qs.set('before_id', String(beforeId));
  return request<DraftListPage>(`/drafts?${qs.toString()}`);
};

export const getDraft = (id: number) => request<DraftItem>(`/drafts/${id}`);

/** 手动编辑保存（仅更新传入字段）。 */
export const updateDraft = (
  id: number,
  patch: { revision: number; title?: string; digest?: string; content_md?: string },
) =>
  request<DraftItem>(`/drafts/${id}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  });

/** 重新生成：正文/标题/摘要将被覆盖。 */
export const regenerateDraft = (id: number, revision: number) =>
  request<DraftItem>(`/drafts/${id}/regenerate`, {
    method: 'POST',
    body: JSON.stringify({ revision }),
  });

export const deleteDraft = (id: number, revision: number) =>
  request<{ deleted: boolean }>(`/drafts/${id}`, {
    method: 'DELETE',
    body: JSON.stringify({ revision }),
  });
