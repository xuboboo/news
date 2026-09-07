/** @type {import('tailwindcss').Config} */

/**
 * 主题变量化桥接层：颜色真值统一定义在 src/index.css 的 :root（深色，默认）
 * 与 :root[data-theme='light']（浅色）两组 CSS 变量中，本文件只做「变量 → 色 token」引用。
 * 透明度修饰符（如 bg-amber-500/10）通过 <alpha-value> 自动兼容，无需关心主题。
 */
const palette = (name, shades) =>
  Object.fromEntries(shades.map((s) => [s, `rgb(var(--c-${name}-${s}) / <alpha-value>)`]));

const FULL = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        radar: {
          bg: 'rgb(var(--c-radar-bg) / <alpha-value>)',
          panel: 'rgb(var(--c-radar-panel) / <alpha-value>)',
          card: 'rgb(var(--c-radar-card) / <alpha-value>)',
          cardHover: 'rgb(var(--c-radar-cardHover) / <alpha-value>)',
          border: 'rgb(var(--c-radar-border) / <alpha-value>)',
          borderStrong: 'rgb(var(--c-radar-borderStrong) / <alpha-value>)',
        },
        accent: 'rgb(var(--c-accent) / <alpha-value>)',
        slate: palette('slate', FULL),
        amber: palette('amber', FULL),
        emerald: palette('emerald', FULL),
        red: palette('red', FULL),
        orange: palette('orange', FULL),
        cyan: palette('cyan', FULL),
        sky: palette('sky', FULL),
        violet: palette('violet', FULL),
        blue: palette('blue', FULL),
        fuchsia: palette('fuchsia', FULL),
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', 'SF Pro Display', 'SF Pro Text',
          'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei',
          'Helvetica Neue', 'Arial', 'sans-serif',
        ],
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        glow: 'var(--shadow-glow)',
      },
    },
  },
  plugins: [],
};
