import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
  },
  preview: {
    port: 8080,
    host: true,
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1024,
  },
});
