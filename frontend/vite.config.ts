import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: process.env.HOST || '127.0.0.1',
  },
  preview: {
    port: 8080,
    host: process.env.HOST || '127.0.0.1',
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1024,
  },
});
