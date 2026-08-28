import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 后端地址可用 API_TARGET 覆盖。需要它是因为：安装版跑在 8787，开发时
      // 想同时留着安装版对比，就得把开发后端挪到别的端口，否则只能二选一。
      '/api': { target: process.env.API_TARGET || 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
});
