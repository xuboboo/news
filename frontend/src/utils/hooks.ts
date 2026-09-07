import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { onDataUpdate, bootstrapState } from '../api/client';

/**
 * 可点击 div 卡片的键盘激活：配合 role="button" + tabIndex={0} 使用，
 * 让纯鼠标交互的卡片支持 Enter / 空格触发（可访问性）。
 */
export function activateOnKey(onActivate: () => void): (e: ReactKeyboardEvent) => void {
  return (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      onActivate();
    }
  };
}

/**
 * 定时触发重渲染：让相对时间（"5 分钟前"）自动跳动。
 * 返回值仅作依赖用，页面可直接调用不接收返回值。
 */
export function useTick(intervalMs = 30000): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((x) => x + 1), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return tick;
}

/**
 * 数据版本号：后台 auto-refresh 拉到新数据时 +1，驱动页面重渲染。
 * 需把它放进 useMemo/useEffect 依赖里。
 */
export function useDataVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => onDataUpdate(() => setVersion((x) => x + 1)), []);
  return version;
}

/**
 * 引导就绪状态：startBootstrap 完成（API 就绪或已降级本地引擎）后变 true。
 * 用于页面在骨架态与数据态之间切换。
 * 兜底：引导完成的通知可能与首帧 effect 订阅产生竞态（如初始路由重定向导致
 * 页面晚一个提交挂载），未就绪期间以 250ms 轮询自愈，就绪后停止。
 */
export function useBootstrapReady(): boolean {
  const [ready, setReady] = useState(bootstrapState === 'ready');
  useEffect(() => {
    // setState 仅出现在外部事件回调与定时器中，不在 effect 体内同步调用
    const check = () => {
      if (bootstrapState === 'ready') {
        setReady(true);
        return true;
      }
      return false;
    };
    if (check()) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      unsub();
      if (timer) clearInterval(timer);
    };
    const unsub = onDataUpdate(() => {
      if (check()) stop();
    });
    timer = setInterval(() => {
      if (check()) stop();
    }, 250);
    return stop;
  }, []);
  return ready;
}

/**
 * 页面标题：随路由切换 document.title，格式「{页名} · 华军软件园资讯雷达系统」。
 * 不传或传 undefined 时使用默认标题。
 */
export function usePageTitle(title?: string) {
  useEffect(() => {
    document.title = title ? `${title} · 华军软件园资讯雷达系统` : '华军软件园资讯雷达系统 V2.0';
  }, [title]);
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

/**
 * 弹窗可访问性组合：
 * - Escape 关闭（捕获阶段监听，避免被弹窗内输入框吞掉）
 * - 打开时锁定背景滚动、聚焦弹窗容器，关闭时归还焦点
 * - Tab / Shift+Tab 在弹窗内循环（简单焦点圈）
 * 返回挂在弹窗容器上的 ref（容器需 tabIndex={-1}）。
 */
export function useModalA11y(onClose: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const dialog = ref.current;
    const prevFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeRef.current();
        return;
      }
      if (e.key !== 'Tab' || !dialog) return;
      const focusables = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null
      );
      if (focusables.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      if (e.shiftKey && (active === first || active === dialog)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || active === document.body)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = prevOverflow;
      prevFocus?.focus();
    };
  }, []);

  return ref;
}
