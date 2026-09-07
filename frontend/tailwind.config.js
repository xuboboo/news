/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        radar: {
          bg: '#070B14',
          panel: '#0C1220',
          card: '#0E1628',
          cardHover: '#121C33',
          border: '#1B2742',
          borderStrong: '#27375C',
        },
        accent: '#38BDF8',
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'PingFang SC',
          'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', 'Arial', 'sans-serif',
        ],
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.35), 0 0 0 1px rgba(27,39,66,0.6)',
        glow: '0 0 24px rgba(56,189,248,0.15)',
      },
    },
  },
  plugins: [],
};
