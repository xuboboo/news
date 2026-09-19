/**
 * 首帧前主题解析（由 index.html 内联脚本外置而来，2026-09-19）：
 * 外置阻塞脚本与内联同样在首帧渲染前同步执行，行为不变——换来 CSP script-src 'self'
 * 可强制执行（内联脚本会被强制 CSP 拦截，而 'unsafe-inline' 会掏空 XSS 防线）。
 * ⚠️ 逻辑须与 src/theme.ts 保持一致（STORAGE_KEY / system 解析 / theme-color 值）。
 */
(function () {
  try {
    var m = localStorage.getItem('radar-theme');
    // 与 src/theme.ts 对齐：仅显式 'light' 判浅色，'dark' 判深色，
    // 其余（null/'system'/无效值）跟随系统偏好
    var dark =
      m === 'dark' ||
      (m !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#070B14' : '#F5F5F7');
  } catch (e) {
    /* localStorage 不可用时保持默认深色 */
  }
})();
