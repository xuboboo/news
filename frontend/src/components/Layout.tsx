import { ReactNode, useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity,
  AlertTriangle,
  Clock3,
  Coins,
  Menu,
  Monitor,
  Moon,
  Radar,
  FileText,
  Radio,
  Database,
  Bot,
  LogOut,
  Settings,
  ShieldCheck,
  Sun,
  UserCog,
  Users as UsersIcon,
  X,
} from 'lucide-react';
import {
  ARTICLES_WITH_SCORE,
  EVENTS_WITH_SCORE,
  apiMode,
  LAST_SYNC,
  lastSyncError,
} from '../api/client';
import { useBootstrapReady, useDataVersion, useModalA11y } from '../utils/hooks';
import { setThemeMode, useThemeMode, type ThemeMode } from '../theme';
import { ROLE_LABELS, isSuperAdmin, isTenantAdmin, setAnon, useAuth } from '../auth';
import { logout as authLogout } from '../api/auth';

/** minRole: 'tenant_admin' = 租户管理员及以上；'super_admin' = 仅平台超管；缺省 = 所有登录用户 */
const NAV_ITEMS: { to: string; label: string; icon: typeof Activity; desc: string; minRole?: 'tenant_admin' | 'super_admin' }[] = [
  { to: '/hot', label: '热榜中心', icon: Activity, desc: 'HotScore 实时榜单' },
  { to: '/latest', label: '最新资讯', icon: Clock3, desc: '发布时间倒序时间轴' },
  { to: '/events', label: '热点事件', icon: Radar, desc: '多来源事件聚合' },
  { to: '/report', label: '科技日报', icon: FileText, desc: '每日自动生成' },
  { to: '/users', label: '用户与租户', icon: UsersIcon, desc: '账号 · 组织管理', minRole: 'tenant_admin' },
  { to: '/audit', label: '审计日志', icon: ShieldCheck, desc: '操作留痕 · 可追溯', minRole: 'super_admin' },
  { to: '/usage', label: '成本看板', icon: Coins, desc: 'Token 用量 · 成本计费', minRole: 'super_admin' },
  { to: '/admin', label: '运维监控', icon: Settings, desc: '服务状态 · 运行时配置', minRole: 'super_admin' },
];

function nowClock(): string {
  return new Date().toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** 独立时钟组件：每秒重渲染的只有它自己，不带动整个页面树 reconcile */
function LiveClock() {
  const [clock, setClock] = useState(nowClock);
  useEffect(() => {
    const timer = setInterval(() => setClock(nowClock()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <div className="hidden sm:block text-sm text-slate-400 tabular-nums shrink-0">{clock}</div>
  );
}

const THEME_OPTIONS: { value: ThemeMode; icon: typeof Sun; label: string }[] = [
  { value: 'light', icon: Sun, label: '浅色' },
  { value: 'system', icon: Monitor, label: '跟随系统' },
  { value: 'dark', icon: Moon, label: '深色' },
];

/** 主题三态切换（浅色/跟随系统/深色）：macOS 分段控件样式，状态持久化见 src/theme.ts */
export function ThemeToggle({ fullWidth = false }: { fullWidth?: boolean }) {
  const mode = useThemeMode();
  return (
    <div className={`seg ${fullWidth ? 'w-full' : ''}`} role="group" aria-label="界面主题">
      {THEME_OPTIONS.map((o) => {
        const active = mode === o.value;
        return (
          <button
            key={o.value}
            onClick={() => setThemeMode(o.value)}
            aria-pressed={active}
            title={`主题：${o.label}`}
            className={`seg-item ${fullWidth ? 'flex-1 flex justify-center' : ''} ${
              active ? 'seg-item-active' : ''
            }`}
          >
            <o.icon className="h-4 w-4" />
            <span className="sr-only">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** 同步状态区：每 15s 更新一次“最近同步”相对时间；同时订阅数据通知，
 *  同步失败/恢复（lastSyncError、apiMode 变化）时立即反映，不等下一个 15s。 */
function computeSyncMins(): number | null {
  return apiMode && LAST_SYNC ? Math.max(0, Math.round((Date.now() - LAST_SYNC) / 60000)) : null;
}

function SyncStatus() {
  useDataVersion(); // 订阅后台同步通知：每轮刷新/断线恢复立即重渲染，相对时间随之新鲜
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((x) => x + 1), 15000);
    return () => clearInterval(timer);
  }, []);
  // 每次渲染现算而非存 state：无「effect 里同步 setState」的级联渲染问题
  const syncMins = computeSyncMins();

  return (
    <>
      {apiMode ? (
        lastSyncError ? (
          <span className="chip bg-amber-500/10 text-amber-300 border border-amber-400/30">
            <AlertTriangle className="h-3 w-3" />
            {lastSyncError}
          </span>
        ) : (
          <>
            <span className="hidden sm:inline-flex chip bg-emerald-500/10 text-emerald-300 border border-emerald-400/20">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
              实时同步中 · 每 60s 自动更新
            </span>
            <span className="hidden sm:inline tabular-nums">
              {syncMins == null
                ? '连接中…'
                : syncMins < 1
                ? '刚刚同步'
                : `最近同步 ${syncMins} 分钟前`}
            </span>
          </>
        )
      ) : (
        <span className="chip bg-amber-500/10 text-amber-300 border border-amber-400/30">
          <AlertTriangle className="h-3 w-3" />
          后端未连接 · 自动重连中
        </span>
      )}
    </>
  );
}

/** 顶栏用户菜单：头像 + 邮箱/角色，展开账号设置与退出登录 */
function UserMenu() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!user) return null;
  const initial = (user.display_name || user.email).charAt(0).toUpperCase();

  const doLogout = async () => {
    setOpen(false);
    try {
      await authLogout();
    } finally {
      setAnon(); // RequireAuth 感知后跳 /login
      navigate('/login', { replace: true });
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={`${user.email} · ${ROLE_LABELS[user.role]}`}
        className="flex items-center gap-2 rounded-full pl-1 pr-2 py-1 hover:bg-radar-card cursor-pointer transition-colors"
      >
        <span className="h-7 w-7 rounded-full bg-accent/15 text-accent text-xs font-semibold flex items-center justify-center">
          {initial}
        </span>
        <span className="hidden sm:flex flex-col items-start leading-tight max-w-[10rem]">
          <span className="text-xs text-slate-200 truncate w-full">{user.display_name || user.email}</span>
          <span className="text-[10px] text-slate-500">{ROLE_LABELS[user.role]}</span>
        </span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-2 w-56 card p-1.5 z-30 shadow-card"
        >
          <div className="px-2.5 py-2 border-b border-radar-border mb-1">
            <div className="text-xs text-slate-200 truncate">{user.email}</div>
            <div className="text-[10px] text-slate-500 mt-0.5">{ROLE_LABELS[user.role]}</div>
          </div>
          <button
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate('/account');
            }}
            className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs text-slate-300 hover:bg-radar-panel hover:text-accent cursor-pointer"
          >
            <UserCog className="h-3.5 w-3.5" /> 账号设置 / 修改密码
          </button>
          <button
            role="menuitem"
            onClick={() => void doLogout()}
            className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs text-slate-300 hover:bg-radar-panel hover:text-red-300 cursor-pointer"
          >
            <LogOut className="h-3.5 w-3.5" /> 退出登录
          </button>
        </div>
      )}
    </div>
  );
}

export default function Layout() {
  // 抽屉记录打开时的路径：路由变化后自动视为关闭，无需 effect
  const [drawerForPath, setDrawerForPath] = useState<string | null>(null);
  const location = useLocation();
  const drawerOpen = drawerForPath !== null && drawerForPath === location.pathname;
  const bootReady = useBootstrapReady();
  const dataVer = useDataVersion();
  // dataVer 订阅驱动状态栏计数随后台刷新更新（返回值本身不直接使用）
  void dataVer;

  // 路由切换（页面级）时回到顶部；仅查询参数变化（筛选切换）不触发
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  const { user } = useAuth();
  const visibleNav = NAV_ITEMS.filter((item) => {
    if (item.minRole === 'super_admin') return isSuperAdmin(user);
    if (item.minRole === 'tenant_admin') return isTenantAdmin(user);
    return true;
  });

  const summarized = ARTICLES_WITH_SCORE.length;
  const events = EVENTS_WITH_SCORE.length;

  const brand = (
    <div className="border-b border-radar-border">
      {/* macOS 窗口红绿灯（装饰） */}
      <div className="px-4 pt-3.5 flex items-center gap-2" aria-hidden="true">
        <span className="h-3 w-3 rounded-full bg-[#FF5F57]" />
        <span className="h-3 w-3 rounded-full bg-[#FEBC2E]" />
        <span className="h-3 w-3 rounded-full bg-[#28C840]" />
      </div>
      <div className="px-4 py-4 flex items-center gap-2.5">
        <div className="relative p-2 rounded-xl bg-accent/10 shadow-glow">
          <Radar className="h-6 w-6 text-accent" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold text-slate-100">资讯雷达</div>
          <div className="text-[10px] text-slate-500 mt-0.5">华军软件园 V2.0</div>
        </div>
      </div>
    </div>
  );

  const nav = (
    <nav className="px-3 py-4 space-y-1 flex-1">
      {visibleNav.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          // 移动端抽屉内点击导航（含当前页）后收起抽屉；桌面端为无害空操作
          onClick={() => setDrawerForPath(null)}
          className={({ isActive }) =>
            `nav-item ${isActive ? 'nav-item-active' : 'hover:bg-radar-card hover:text-slate-200'}`
          }
        >
          <item.icon className="h-[18px] w-[18px]" />
          <div>
            <div>{item.label}</div>
            <div className="text-[10px] text-slate-500 leading-3 mt-0.5">{item.desc}</div>
          </div>
        </NavLink>
      ))}
    </nav>
  );

  const statusPanel = (
    <div className="px-4 py-4 border-t border-radar-border space-y-2.5">
      <div className="text-[11px] text-slate-500 font-medium">数据链路状态</div>
      {!bootReady ? (
        <div className="text-[11px] text-slate-500 flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-slate-600 animate-pulse" />
          正在初始化数据…
        </div>
      ) : (
        <>
          <StatusRow
            icon={<Database className="h-3.5 w-3.5" />}
            text={apiMode ? '采集服务 · 运行中' : '采集服务 · 未连接'}
            ok={apiMode}
          />
          <StatusRow
            icon={<Bot className="h-3.5 w-3.5" />}
            text={
              apiMode
                ? `AI Worker · 已处理 ${summarized} 篇`
                : 'AI Worker · 未连接'
            }
            ok={apiMode}
          />
          <StatusRow
            icon={<Radio className="h-3.5 w-3.5" />}
            text={`事件聚合 · ${events} 个热点`}
            ok={apiMode}
          />
        </>
      )}
    </div>
  );

  return (
    <div className="flex min-h-screen">
      {/* 侧边栏（桌面端） */}
      <aside className="w-56 shrink-0 border-r border-radar-border bg-radar-panel hidden md:flex flex-col sticky top-0 h-screen">
        {brand}
        {nav}
        {statusPanel}
      </aside>

      {/* 移动端抽屉导航（独立组件：挂载期启用焦点陷阱/滚动锁/焦点归还） */}
      {drawerOpen && (
        <MobileDrawer onClose={() => setDrawerForPath(null)}>
          {brand}
          {nav}
          <div className="px-4 py-2">
            <ThemeToggle fullWidth />
          </div>
          {statusPanel}
        </MobileDrawer>
      )}

      {/* 主内容区 */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-20 h-14 border-b border-radar-border bg-radar-bg/70 backdrop-blur-xl backdrop-saturate-150 flex items-center justify-between gap-3 px-4 md:px-6">
          <div className="flex items-center gap-2 text-xs text-slate-500 min-w-0">
            <button
              onClick={() => setDrawerForPath(location.pathname)}
              aria-label="打开导航菜单"
              className="md:hidden -ml-1.5 p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-radar-card cursor-pointer"
            >
              <Menu className="h-5 w-5" />
            </button>
            <SyncStatus />
            <span className="hidden lg:inline">
              数据源：软件之家 · Win11之家 · Win10之家 · 智能时代
            </span>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <ThemeToggle />
            <LiveClock />
            <div className="h-6 w-px bg-radar-border hidden sm:block" aria-hidden="true" />
            <UserMenu />
          </div>
        </header>
        {bootReady && !apiMode && (
          <div
            className="bg-amber-500/10 border-b border-amber-400/30 px-4 md:px-6 py-2.5 flex items-center gap-2 text-xs text-amber-300"
            role="alert"
          >
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>
              演示模式：后端未连接，当前展示的是内置演示数据，非真实采集内容；连接恢复后将自动切换为实时数据。
            </span>
          </div>
        )}
        <main className="flex-1 px-4 md:px-6 py-6 max-w-[1400px] w-full mx-auto">
          <Outlet />
        </main>
        <footer className="px-4 md:px-6 py-4 border-t border-radar-border text-[11px] text-slate-500 flex flex-wrap items-center justify-between gap-2">
          <span>华军软件园资讯雷达系统 V2.0</span>
          <span>版权所有 © {new Date().getFullYear()} 华军软件园</span>
        </footer>
      </div>
    </div>
  );
}

function StatusRow({ icon, text, ok }: { icon: ReactNode; text: string; ok?: boolean }) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-slate-400">
      <span className={ok ? 'text-emerald-400' : 'text-slate-500'}>{icon}</span>
      <span className="truncate">{text}</span>
      <span className={`h-1.5 w-1.5 rounded-full ml-auto ${ok ? 'bg-emerald-400 animate-pulse' : 'bg-slate-600'}`} />
    </div>
  );
}

/** 移动端抽屉：复用弹窗可访问性组合（Escape 关闭 / Tab 焦点圈 / 滚动锁 / 关闭后焦点归还），遮罩点击关闭 */
function MobileDrawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const drawerRef = useModalA11y(onClose);
  return (
    <div className="fixed inset-0 z-40 md:hidden" role="presentation">
      <div
        className="absolute inset-0 bg-[rgb(0_0_0/var(--overlay-alpha))] backdrop-blur-sm"
        onClick={onClose}
      />
      <aside
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-label="导航菜单"
        tabIndex={-1}
        className="absolute left-0 top-0 bottom-0 w-64 bg-radar-panel border-r border-radar-border flex flex-col overflow-y-auto outline-none"
      >
        <button
          onClick={onClose}
          aria-label="关闭导航菜单"
          className="absolute right-2 top-4 p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-radar-card cursor-pointer z-10"
        >
          <X className="h-[18px] w-[18px]" />
        </button>
        {children}
      </aside>
    </div>
  );
}
