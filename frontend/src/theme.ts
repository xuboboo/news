import { useSyncExternalStore } from 'react';

/**
 * 主题状态：三态模式（浅色 / 深色 / 跟随系统），持久化到 localStorage。
 * 解析结果写入 <html data-theme="light|dark">，配合 index.css 的
 * :root[data-theme='light'] 变量覆盖实现双主题；同步更新 meta theme-color。
 * 无状态库依赖，与 client.ts 同样的模块级单例 + 订阅风格。
 */

export type ThemeMode = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

const STORAGE_KEY = 'radar-theme';

/** 与 index.html 内联 pre-paint 脚本保持一致的 meta theme-color 值 */
export const THEME_META_COLORS: Record<ResolvedTheme, string> = {
  dark: '#070B14',
  light: '#F5F7FB',
};

function readStoredMode(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch {
    /* 隐私模式等场景下不可用，回落 system */
  }
  return 'system';
}

const systemDark = window.matchMedia('(prefers-color-scheme: dark)');

let mode: ThemeMode = readStoredMode();
const listeners = new Set<() => void>();

function resolve(m: ThemeMode): ResolvedTheme {
  if (m !== 'system') return m;
  return systemDark.matches ? 'dark' : 'light';
}

function apply() {
  const resolved = resolve(mode);
  document.documentElement.dataset.theme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_META_COLORS[resolved]);
}

// system 模式下跟随操作系统实时切换
systemDark.addEventListener('change', () => {
  if (mode !== 'system') return;
  apply();
  listeners.forEach((l) => l());
});

export function getThemeMode(): ThemeMode {
  return mode;
}

export function getResolvedTheme(): ResolvedTheme {
  return resolve(mode);
}

export function setThemeMode(next: ThemeMode) {
  mode = next;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    /* 持久化失败不影响本次会话切换 */
  }
  apply();
  listeners.forEach((l) => l());
}

/** 模块加载即应用一次，兜底 pre-paint 脚本未执行的场景（如热更新后） */
apply();

/** 订阅主题模式变化（切换或 system 模式下系统偏好变化） */
export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => mode
  );
}
