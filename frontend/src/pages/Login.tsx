import { FormEvent, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { AlertCircle, Loader2, Radar } from 'lucide-react';
import { ApiError, login } from '../api/auth';
import { setAuthed, useAuth, type AuthUser } from '../auth';
import { ThemeToggle } from '../components/Layout';

/**
 * 登录页（全站唯一入口，无注册——账号由管理员后台创建）。
 * 视觉沿用 macOS 设计语言：白卡悬浮 + 红绿灯窗体装饰 + Apple 蓝主按钮 +
 * 渐变窗体底（浅色主题 body 渐变自动生效），深色主题由 CSS 变量自动适配。
 */
export default function Login() {
  const { status, user } = useAuth();
  const navigate = useNavigate();
  const location = useLocation() as { state?: { from?: string } };

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 已登录直接回业务页（会话有效时手动访问 /login 的场景）
  if (status === 'authed' && user && !user.must_change_password) {
    return <Navigate to={location.state?.from ?? '/hot'} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const me: AuthUser = await login(email.trim(), password);
      setAuthed(me);
      navigate(location.state?.from ?? '/hot', { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4 overflow-hidden">
      {/* 右上角主题切换：登录页独立于 Layout，需自带 */}
      <div className="absolute top-4 right-4 z-10">
        <ThemeToggle />
      </div>

      {/* 背景柔光装饰（macOS 渐变窗体底之上的两团弥散光） */}
      <div className="pointer-events-none absolute -top-32 -left-24 h-96 w-96 rounded-full bg-accent/10 blur-3xl" aria-hidden="true" />
      <div className="pointer-events-none absolute -bottom-40 -right-24 h-[28rem] w-[28rem] rounded-full bg-violet-500/10 blur-3xl" aria-hidden="true" />

      <div className="card w-full max-w-md relative">
        {/* macOS 窗口红绿灯（装饰） */}
        <div className="flex items-center gap-2 mb-6" aria-hidden="true">
          <span className="h-3 w-3 rounded-full bg-[#FF5F57]" />
          <span className="h-3 w-3 rounded-full bg-[#FEBC2E]" />
          <span className="h-3 w-3 rounded-full bg-[#28C840]" />
        </div>

        <div className="flex items-center gap-3 mb-1.5">
          <div className="relative p-2 rounded-xl bg-accent/10 shadow-glow">
            <Radar className="h-6 w-6 text-accent" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-slate-100">资讯雷达</h1>
            <p className="text-[11px] text-slate-500 mt-0.5">华军科技 · 内部情报系统 V2.0</p>
          </div>
        </div>
        <p className="text-sm text-slate-400 mb-6">请使用管理员分配的账号登录</p>

        {error && (
          <div
            className="mb-4 flex items-start gap-2 rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2.5 text-xs text-red-400"
            role="alert"
          >
            <AlertCircle className="h-4 w-4 shrink-0 mt-px" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div>
            <label htmlFor="login-email" className="block text-xs font-medium text-slate-400 mb-1.5">
              邮箱
            </label>
            <input
              id="login-email"
              type="email"
              autoComplete="username"
              placeholder="name@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              required
              className="w-full rounded-lg border border-radar-border bg-radar-panel px-3.5 py-2.5 text-sm text-slate-100 placeholder:text-slate-600 outline-none focus:border-accent/60"
            />
          </div>
          <div>
            <label htmlFor="login-password" className="block text-xs font-medium text-slate-400 mb-1.5">
              密码
            </label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full rounded-lg border border-radar-border bg-radar-panel px-3.5 py-2.5 text-sm text-slate-100 placeholder:text-slate-600 outline-none focus:border-accent/60"
            />
          </div>

          <button
            type="submit"
            disabled={submitting || !email || !password}
            className="w-full flex items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            {submitting ? '登录中…' : '登录'}
          </button>
        </form>

        <p className="mt-6 text-center text-[11px] text-slate-500">
          没有账号？联系平台管理员开通 · 连续登录失败将临时锁定
        </p>
      </div>
    </div>
  );
}
