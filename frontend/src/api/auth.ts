/**
 * 认证 API 客户端（对接后端 /api/v2/auth/*）。
 * 会话凭证是 HttpOnly Cookie：同源请求浏览器自动携带，前端不接触令牌本体。
 * 全部变更请求带 X-Requested-With 头（后端 CSRF 防线之一）。
 */
import { type AuthUser, setAnon, setAuthed } from '../auth';

const API_BASE: string = (import.meta as any).env?.VITE_API_BASE ?? '';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T = any>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/api/v2/auth${path}`, {
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
    throw new ApiError(0, '网络错误 · 后端服务不可达');
  }
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`;
    try {
      const body = JSON.parse(await resp.text());
      if (typeof body?.detail === 'string') message = body.detail;
    } catch {
      /* 保留 HTTP 状态提示 */
    }
    // 会话失效统一反映到全局登录态（/me 之外的业务请求也可能复用此客户端）
    if (resp.status === 401 && path !== '/login') setAnon();
    throw new ApiError(resp.status, message);
  }
  return (await resp.json()) as T;
}

export async function fetchMe(): Promise<AuthUser> {
  return request<AuthUser>('/me');
}

export async function login(email: string, password: string): Promise<AuthUser> {
  return request<AuthUser>('/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<void> {
  await request('/logout', { method: 'POST' });
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await request('/me/password', {
    method: 'PUT',
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
}

export interface SessionItem {
  id: number;
  created_at: string | null;
  last_seen_at: string | null;
  expires_at: string | null;
  ip: string;
  user_agent: string;
  current: boolean;
}

export const getMySessions = () => request<{ items: SessionItem[] }>('/sessions');

export const revokeSession = (id: number) =>
  request<{ ok: boolean }>(`/sessions/${id}`, { method: 'DELETE' });

// ———————————————————— 启动会话引导 ————————————————————

let bootstrapped = false;

/** 应用启动时调用一次：GET /me 恢复会话镜像。
 *  401 → 未登录；网络失败（status 0）→ 3s 后重试（后端部署窗口不误踢有效会话）。 */
export async function bootstrapAuth(): Promise<void> {
  if (bootstrapped) return;
  bootstrapped = true;
  const attempt = async () => {
    try {
      const me = await fetchMe();
      setAuthed(me);
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      if (status === 0) {
        // 后端不可达：保持 loading 态并重试，避免部署窗口把有效会话踢到登录页
        setTimeout(() => void attempt(), 3000);
        return;
      }
      setAnon();
    }
  };
  await attempt();
}
