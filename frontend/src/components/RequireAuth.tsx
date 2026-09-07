import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Radar, ShieldX } from 'lucide-react';
import { isSuperAdmin, useAuth } from '../auth';
import { bootstrapAuth } from '../api/auth';

/**
 * 全站路由守卫：未登录一律跳 /login（记录来源页，登录后跳回）；
 * 首登强制改密（must_change_password）期间只允许停留在 /account。
 */
export default function RequireAuth() {
  const { status, user } = useAuth();
  const location = useLocation();

  useEffect(() => {
    // 幂等：模块内只引导一次；网络不通时内部自动重试，不误踢有效会话
    void bootstrapAuth();
  }, []);

  if (status === 'loading') {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-3 text-slate-400">
        <Radar className="h-8 w-8 text-accent animate-pulse" />
        <div className="text-sm">正在恢复登录态…</div>
        <div className="text-xs text-slate-500">若后端未启动将每 3 秒自动重试</div>
      </div>
    );
  }

  if (status === 'anon') {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  if (user?.must_change_password && location.pathname !== '/account') {
    return <Navigate to="/account" replace />;
  }

  return <Outlet />;
}

/** 平台超管专属路由守卫：导航按角色隐藏了入口，这里兜住直敲 URL 的情况。 */
export function RequireSuper({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  if (!isSuperAdmin(user)) {
    return (
      <div className="max-w-md card p-8 mx-auto mt-16 text-center">
        <div className="mx-auto mb-4 w-fit p-3 rounded-xl bg-red-500/10 text-red-400">
          <ShieldX className="h-7 w-7" />
        </div>
        <div className="text-sm font-semibold text-slate-100 mb-1.5">无访问权限</div>
        <p className="text-xs text-slate-500 leading-5">该页面对「平台超管」开放。</p>
      </div>
    );
  }
  return <>{children}</>;
}
