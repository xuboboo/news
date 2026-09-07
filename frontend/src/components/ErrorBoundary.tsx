import { Component, ReactNode } from 'react';

/**
 * 页面级错误边界：单个页面渲染崩溃时只替换该页面区域，
 * 侧边栏与其余路由不受影响（避免整站白屏）。
 */
export default class ErrorBoundary extends Component<
  { page: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    // 渲染崩溃细节留档，便于线上排查（UI 上只展示 message）
    console.error(`[「${this.props.page}」页面渲染崩溃]`, error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card py-16 text-center max-w-xl mx-auto mt-10">
        <div className="text-sm font-medium text-red-300 mb-2">
          「{this.props.page}」页面渲染出错
        </div>
        <div className="text-xs text-slate-500 mb-6 px-8 break-all leading-relaxed">
          {this.state.error.message}
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
