import { Component, ReactNode } from 'react';

/** 稳定错误编号：message 的简易 FNV-1a 哈希前 8 位（供用户反馈定位，无实现细节）。 */
function hash(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

/**
 * 页面级错误边界：单个页面渲染崩溃时只替换该页面区域，
 * 侧边栏与其余路由不受影响（避免整站白屏）。
 */
export default class ErrorBoundary extends Component<
  { page: string; children: ReactNode },
  { error: Error | null; errorId: string }
> {
  state: { error: Error | null; errorId: string } = { error: null, errorId: '' };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    // 渲染崩溃细节留档（控制台），便于线上排查
    console.error(`[「${this.props.page}」页面渲染崩溃]`, error);
    // 生产环境只向用户展示稳定错误编号（ message 哈希前 8 位），
    // 不暴露内部实现细节（2026-09-09 审计）；开发环境保留完整信息
    this.setState({ errorId: hash(error.message) });
  }

  render() {
    if (!this.state.error) return this.props.children;
    const isDev = Boolean((import.meta as any).env?.DEV);
    return (
      <div className="card py-16 text-center max-w-xl mx-auto mt-10">
        <div className="text-sm font-medium text-red-300 mb-2">
          「{this.props.page}」页面渲染出错
        </div>
        <div className="text-xs text-slate-500 mb-6 px-8 break-all leading-relaxed">
          {isDev ? (
            this.state.error.message
          ) : (
            <>页面遇到问题，请重试或刷新；错误编号 <code className="text-slate-400">{this.state.errorId}</code></>
          )}
        </div>
        <div className="flex items-center justify-center gap-2">
          <button
            onClick={() => this.setState({ error: null })}
            className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25"
          >
            重试
          </button>
          <button
            onClick={() => window.location.reload()}
            className="chip bg-radar-card text-slate-300 border border-radar-border px-4 py-2 cursor-pointer hover:text-slate-100"
          >
            刷新页面
          </button>
        </div>
      </div>
    );
  }
}
