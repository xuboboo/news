import { ReactNode, useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
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
  Settings,
  Sun,
  X,
} from 'lucide-react';
import {
  ARTICLES_WITH_SCORE,
  EVENTS_WITH_SCORE,
  apiMode,
  LAST_SYNC,
  lastSyncError,
} from '../api/client';
import { useBootstrapReady, useDataVersion } from '../utils/hooks';
import { setThemeMode, useThemeMode, type ThemeMode } from '../theme';

const NAV_ITEMS = [
  { to: '/hot', label: '热榜中心', icon: Activity, desc: 'HotScore 实时榜单' },
  { to: '/latest', label: '最新资讯', icon: Clock3, desc: '发布时间倒序时间轴' },
  { to: '/events', label: '热点事件', icon: Radar, desc: '多来源事件聚合' },
  { to: '/report', label: '科技日报', icon: FileText, desc: '每日自动生成' },
  { to: '/usage', label: '成本看板', icon: Coins, desc: 'Token 用量 · 成本计费' },
  { to: '/admin', label: '运维监控', icon: Settings, desc: '服务状态 · 运行时配置' },
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

/** 主题三态切换（浅色/跟随系统/深色），状态持久化见 src/theme.ts */
export function ThemeToggle({ fullWidth = false }: { fullWidth?: boolean }) {
  const mode = useThemeMode();
  return (
    <div
      className={`flex items-center rounded-lg border border-radar-border bg-radar-card p-0.5 shrink-0 ${
        fullWidth ? 'w-full' : ''
      }`}
      role="group"
      aria-label="界面主题"
    >
      {THEME_OPTIONS.map((o) => {
        const active = mode === o.value;
        return (
          <button
            key={o.value}
            onClick={() => setThemeMode(o.value)}
            aria-pressed={active}
            title={`主题：${o.label}`}
            className={`${fullWidth ? 'flex-1 flex justify-center' : ''} p-1.5 rounded-md cursor-pointer transition-colors ${
              active ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:text-slate-200'
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
function SyncStatus() {
  useDataVersion();
  const [syncMins, setSyncMins] = useState<number | null>(() =>
    apiMode && LAST_SYNC ? Math.max(0, Math.round((Date.now() - LAST_SYNC) / 60000)) : null
  );
  useEffect(() => {
    const timer = setInterval(() => {
      setSyncMins(
        apiMode && LAST_SYNC ? Math.max(0, Math.round((Date.now() - LAST_SYNC) / 60000)) : null
      );
    }, 15000);
    return () => clearInterval(timer);
  }, []);

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
          演示数据 · 后端未连接
        </span>
      )}
    </>
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

  // 抽屉打开时锁定背景滚动，并支持 Escape 关闭
  useEffect(() => {
    if (!drawerOpen) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDrawerForPath(null);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = prevOverflow;
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [drawerOpen]);

  const summarized = ARTICLES_WITH_SCORE.length;
  const events = EVENTS_WITH_SCORE.length;

  const brand = (
    <div className="px-4 py-5 flex items-center gap-2.5 border-b border-radar-border">
      <div className="relative p-2 rounded-xl bg-accent/10 shadow-glow">
        <Radar className="h-6 w-6 text-accent" />
      </div>
      <div className="leading-tight">
        <div className="text-sm font-semibold text-slate-100">资讯雷达</div>
        <div className="text-[10px] text-slate-500 mt-0.5">华军软件园V2.0</div>
      </div>
    </div>
  );

  const nav = (
    <nav className="px-3 py-4 space-y-1 flex-1">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
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
                : `本地演示数据 · ${summarized} 篇`
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

      {/* 移动端抽屉导航 */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 md:hidden" role="presentation">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setDrawerForPath(null)}
          />
          <aside className="absolute left-0 top-0 bottom-0 w-64 bg-radar-panel border-r border-radar-border flex flex-col overflow-y-auto">
            <button
              onClick={() => setDrawerForPath(null)}
              aria-label="关闭导航菜单"
              className="absolute right-2 top-4 p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-radar-card cursor-pointer z-10"
            >
              <X className="h-[18px] w-[18px]" />
            </button>
            {brand}
            {nav}
            <div className="px-4 py-2">
              <ThemeToggle fullWidth />
            </div>
            {statusPanel}
          </aside>
        </div>
      )}

      {/* 主内容区 */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-20 h-14 border-b border-radar-border bg-radar-bg/80 backdrop-blur flex items-center justify-between gap-3 px-4 md:px-6">
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
          <span>华军资讯雷达 V2.0 · 内部科技情报系统</span>
          <span>HotScore = Engagement × TimeDecay · AI 摘要由 AI Worker 自动生成</span>
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
