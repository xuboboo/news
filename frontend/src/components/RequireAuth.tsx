import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Radar } from 'lucide-react';
import { useAuth } from '../auth';
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
