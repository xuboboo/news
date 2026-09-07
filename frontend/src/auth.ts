import { useSyncExternalStore } from 'react';

/**
 * 登录态：模块级单例 + useSyncExternalStore（与 theme.ts 同风格，无状态库依赖）。
 * 会话凭证是 HttpOnly Cookie（前端 JS 不可读），这里只维护"当前用户是谁"的镜像：
 * 启动时 GET /auth/me 引导；任何 API 请求吃到 401 都会调 setAnon() 触发登录跳转。
 */

export type Role = 'super_admin' | 'tenant_admin' | 'member';
export type AuthStatus = 'loading' | 'anon' | 'authed';

export interface AuthUser {
  id: number;
  email: string;
  display_name: string;
  role: Role;
  tenant_id: number;
  must_change_password: boolean;
  last_login_at: string | null;
}

export const ROLE_LABELS: Record<Role, string> = {
  super_admin: '平台超管',
  tenant_admin: '租户管理员',
  member: '成员',
};

let status: AuthStatus = 'loading';
let user: AuthUser | null = null;
const listeners = new Set<() => void>();

type AuthListener = (status: AuthStatus) => void;
const transitionListeners = new Set<AuthListener>();

function emit() {
  listeners.forEach((cb) => {
    try {
      cb();
    } catch {
      /* 单个订阅者渲染失败不影响其他 */
    }
  });
  transitionListeners.forEach((cb) => {
    try {
      cb(status);
    } catch {
      /* ignore */
    }
  });
}

export function getAuthStatus(): AuthStatus {
  return status;
}

export function getAuthUser(): AuthUser | null {
  return user;
}

/** 登录成功（登录页提交或会话恢复） */
export function setAuthed(next: AuthUser): void {
  status = 'authed';
  user = next;
  emit();
}

/** 会话失效/登出：触发 RequireAuth 跳登录页 */
export function setAnon(): void {
  if (status !== 'anon') {
    status = 'anon';
    user = null;
    emit();
  }
}

/** 改密成功后更新镜像（解除首登强制改密） */
export function markPasswordChanged(): void {
  if (user) {
    user = { ...user, must_change_password: false };
    emit();
  }
}

/** 订阅登录态变迁（供 client.ts 在 anon→authed 后重启数据引导） */
export function onAuthChange(cb: AuthListener): () => void {
  transitionListeners.add(cb);
  return () => {
    transitionListeners.delete(cb);
  };
}

// useSyncExternalStore 要求 getSnapshot 引用稳定：按 status 缓存快照对象
const snapshotCache: Record<Exclude<AuthStatus, 'authed'>, { status: AuthStatus; user: null }> = {
  loading: { status: 'loading', user: null },
  anon: { status: 'anon', user: null },
};
let authedSnapshot: { status: AuthStatus; user: AuthUser | null } | null = null;

function getSnapshot(): { status: AuthStatus; user: AuthUser | null } {
  if (status === 'authed') {
    if (!authedSnapshot || authedSnapshot.user !== user) {
      authedSnapshot = { status, user };
    }
    return authedSnapshot;
  }
  return snapshotCache[status];
}

export function useAuth(): { status: AuthStatus; user: AuthUser | null } {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
    getSnapshot
  );
}

export function isSuperAdmin(u: AuthUser | null): boolean {
  return u?.role === 'super_admin';
}

export function isTenantAdmin(u: AuthUser | null): boolean {
  return u?.role === 'tenant_admin' || u?.role === 'super_admin';
}
