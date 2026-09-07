import { FormEvent, KeyboardEvent as ReactKeyboardEvent, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { AlertCircle, Eye, EyeOff, Loader2, Radar } from 'lucide-react';
import { ApiError, login } from '../api/auth';
import { setAuthed, useAuth, type AuthUser } from '../auth';
import { ThemeToggle } from '../components/Layout';

/**
 * 登录页 · 「雷达站」设计
 *
 * 构图立场：编辑式左右分栏，拒绝"居中白卡+渐变光斑"的通用脸。
 * - 左侧品牌区：恒定深色雷达站——SVG 同心环 + conic 扫描光束 + 随波束点亮的目标光点，
 *   品牌意象（雷达）即视觉本体；标语与真实数据源陈述，不用假数据装饰。
 * - 右侧表单区：跟随明暗主题，悬浮标签输入框、密码可见切换、大写锁定提示、
 *   克制的排版与微交互。动效尊重 prefers-reduced-motion。
 */

const DATA_SOURCES = ['软件之家', 'Win11之家', 'Win10之家', '智能时代'];

/** 雷达盘上的目标光点：位置随机但固定，延迟≈波束扫过的节奏（周期同为 7s） */
const BLIPS = [
  { top: '30%', left: '63%', delay: '0.15s' },
  { top: '57%', left: '35%', delay: '2.15s' },
  { top: '40%', left: '22%', delay: '3.4s' },
  { top: '67%', left: '57%', delay: '4.55s' },
  { top: '22%', left: '43%', delay: '5.6s' },
];

function BrandPanel() {
  return (
    <div
      className="relative hidden w-[47%] shrink-0 select-none flex-col justify-between overflow-hidden bg-[#060A14] lg:flex lg:w-[52%]"
      aria-hidden={false}
    >
      {/* 极淡网格底纹 */}
      <div
        className="absolute inset-0"
        aria-hidden="true"
        style={{
          backgroundImage:
            'linear-gradient(rgba(148,163,184,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,.045) 1px, transparent 1px)',
          backgroundSize: '56px 56px',
        }}
      />

      {/* 雷达扫描盘（装饰性视觉，屏幕阅读器跳过） */}
      <div className="absolute inset-0 flex items-center justify-center" aria-hidden="true">
        <div className="relative h-[min(78vh,680px)] w-[min(78vh,680px)] -translate-x-[6%] translate-y-[2%]">
          {/* 光晕 */}
          <div className="absolute inset-[12%] rounded-full bg-sky-500/[0.07] blur-3xl" />
          {/* 同心环 + 十字轴 */}
          <svg viewBox="0 0 100 100" fill="none" className="absolute inset-0 h-full w-full">
            <circle cx="50" cy="50" r="49" stroke="rgba(148,163,184,0.16)" strokeWidth="0.3" />
            <circle cx="50" cy="50" r="37" stroke="rgba(148,163,184,0.13)" strokeWidth="0.25" />
            <circle cx="50" cy="50" r="25" stroke="rgba(148,163,184,0.10)" strokeWidth="0.25" />
            <circle cx="50" cy="50" r="13" stroke="rgba(148,163,184,0.10)" strokeWidth="0.25" />
            <line x1="50" y1="1" x2="50" y2="99" stroke="rgba(148,163,184,0.09)" strokeWidth="0.25" />
            <line x1="1" y1="50" x2="99" y2="50" stroke="rgba(148,163,184,0.09)" strokeWidth="0.25" />
            <circle cx="50" cy="50" r="0.9" fill="rgba(125,211,252,0.95)" />
          </svg>
          {/* 扫描光束 */}
          <div className="radar-sweep absolute inset-0 rounded-full" />
          {/* 目标光点 */}
          {BLIPS.map((b, i) => (
            <span
              key={i}
              className="radar-blip"
              style={{ top: b.top, left: b.left, animationDelay: b.delay }}
            />
          ))}
        </div>
      </div>

      {/* 顶部品牌锁定 */}
      <div className="relative z-10 flex items-center gap-3 px-10 pt-10 lg:px-14">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-sky-400/25 bg-sky-400/10">
          <Radar className="h-5 w-5 text-sky-400" />
        </div>
        <div className="leading-tight">
          <div className="text-[15px] font-semibold tracking-wide text-white">华军软件园资讯雷达系统</div>
          <div className="mt-0.5 text-[11px] tracking-wider text-slate-500">HUAJUN SOFTWARE PARK · NEWS RADAR V2.0</div>
        </div>
      </div>

      {/* 底部编辑式文案区 */}
      <div className="relative z-10 px-10 pb-10 lg:px-14">
        <p className="text-[11px] font-medium uppercase tracking-[0.42em] text-sky-400/80">
          News Radar
        </p>
        <h2 className="mt-4 text-4xl font-semibold leading-[1.18] tracking-tight text-white lg:text-[44px]">
          热点风向，
          <br />
          先人一步。
        </h2>
        <p className="mt-5 max-w-md text-[13px] leading-6 text-slate-400">
          多源科技资讯实时聚合，AI 智能摘要与热点事件聚类，每日 08:00 自动生成昨日科技日报。
        </p>
        <div className="mt-7 flex flex-wrap items-center gap-2">
          <span className="mr-1 text-[11px] text-slate-500">数据源</span>
          {DATA_SOURCES.map((s) => (
            <span
              key={s}
              className="rounded-full border border-slate-700/70 bg-slate-800/40 px-3 py-1 text-[11px] text-slate-300"
            >
              {s}
            </span>
          ))}
        </div>
        <p className="mt-10 text-[11px] text-slate-600">
          版权所有 © {new Date().getFullYear()} 华军软件园
        </p>
      </div>
    </div>
  );
}

/** 悬浮标签输入框：label 常驻框内，聚焦/填写后上浮为小字 */
function FloatInput({
  id,
  label,
  type = 'text',
  value,
  onChange,
  onKeyDown,
  autoComplete,
  autoFocus,
  trailing,
}: {
  id: string;
  label: string;
  type?: string;
  value: string;
  onChange: (v: string) => void;
  onKeyDown?: (e: ReactKeyboardEvent<HTMLInputElement>) => void;
  autoComplete?: string;
  autoFocus?: boolean;
  trailing?: React.ReactNode;
}) {
  return (
    <label htmlFor={id} className="relative block">
      <input
        id={id}
        type={type}
        placeholder=" "
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        className={`peer h-14 w-full rounded-xl border border-radar-border bg-radar-panel/70 px-4 ${
          trailing ? 'pr-12' : ''
        } pt-[22px] pb-2 text-[15px] text-slate-100 outline-none transition-colors duration-150 placeholder-transparent hover:border-radar-border/80 focus:border-accent/70 focus:bg-radar-panel`}
      />
      <span
        className={`pointer-events-none absolute left-4 text-slate-500 transition-all duration-150 ${
          trailing ? 'pr-12' : ''
        } top-1/2 -translate-y-1/2 text-sm
          peer-focus:top-[13px] peer-focus:translate-y-0 peer-focus:text-[11px] peer-focus:text-accent
          peer-[:not(:placeholder-shown)]:top-[13px] peer-[:not(:placeholder-shown)]:translate-y-0 peer-[:not(:placeholder-shown)]:text-[11px]`}
      >
        {label}
      </span>
      {trailing}
    </label>
  );
}

export default function Login() {
  const { status, user } = useAuth();
  const navigate = useNavigate();
  const location = useLocation() as { state?: { from?: string } };

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [capsOn, setCapsOn] = useState(false);
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

  const checkCaps = (e: ReactKeyboardEvent<HTMLInputElement>) =>
    setCapsOn(e.getModifierState?.('CapsLock') ?? false);

  return (
    <div className="flex min-h-screen bg-radar-bg">
      <BrandPanel />

      {/* ———— 表单面板 ———— */}
      <div className="relative flex min-h-screen w-full flex-col md:w-[53%] lg:w-[48%]">
        <div className="absolute right-5 top-5 z-10">
          <ThemeToggle />
        </div>

        <div className="flex flex-1 items-center justify-center px-6 sm:px-14">
          <div className="w-full max-w-[400px] py-16">
            {/* 移动端品牌锁定（品牌区在窄屏隐藏） */}
            <div className="mb-12 flex items-center gap-2.5 md:hidden">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/10">
                <Radar className="h-[18px] w-[18px] text-accent" />
              </div>
              <div className="text-[15px] font-semibold tracking-wide text-slate-100">
                华军软件园资讯雷达系统
              </div>
            </div>

            <p className="text-[11px] font-medium uppercase tracking-[0.32em] text-accent/80">
              Welcome back
            </p>
            <h1 className="mt-3 text-[32px] font-semibold tracking-tight text-slate-100">登录</h1>
            <p className="mt-2 text-[13px] text-slate-500">使用管理员分配的账号继续</p>

            {error && (
              <div
                className="mt-7 flex items-start gap-2.5 rounded-xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-[13px] leading-5 text-red-400"
                role="alert"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <form onSubmit={onSubmit} className="mt-8 space-y-4" noValidate>
              <FloatInput
                id="login-email"
                label="邮箱"
                type="email"
                autoComplete="username"
                autoFocus
                value={email}
                onChange={setEmail}
              />
              <FloatInput
                id="login-password"
                label="密码"
                type={showPwd ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={setPassword}
                onKeyDown={(e) => checkCaps(e)}
                trailing={
                  <button
                    type="button"
                    onClick={() => setShowPwd((v) => !v)}
                    aria-label={showPwd ? '隐藏密码' : '显示密码'}
                    className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-slate-500 transition-colors hover:text-slate-300 cursor-pointer"
                  >
                    {showPwd ? <EyeOff className="h-[18px] w-[18px]" /> : <Eye className="h-[18px] w-[18px]" />}
                  </button>
                }
              />

              {capsOn && (
                <p className="text-xs text-amber-400/90" role="status">
                  已开启大写锁定（Caps Lock）
                </p>
              )}

              <button
                type="submit"
                disabled={submitting || !email || !password}
                className="mt-2 h-12 w-full rounded-xl text-[15px] font-medium text-white shadow-[0_8px_24px_-8px_rgba(0,113,227,0.55)] transition-all duration-150 hover:brightness-110 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none cursor-pointer"
                style={{ background: 'linear-gradient(180deg, #2E8BFF 0%, #0071E3 100%)' }}
              >
                <span className="inline-flex items-center justify-center gap-2">
                  {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                  {submitting ? '登录中…' : '进入雷达'}
                </span>
              </button>
            </form>

            <p className="mt-8 text-[13px] text-slate-500">
              没有账号？<span className="text-slate-400">联系平台管理员开通</span>
            </p>
          </div>
        </div>

        <p className="pb-6 pt-4 text-center text-[11px] text-slate-600">
          版权所有 © {new Date().getFullYear()} 华军软件园
        </p>
      </div>
    </div>
  );
}
