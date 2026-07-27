import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// SkillGene 统一控制台前端。
// 构建产物输出到 skillgene/web/dist/，由 SkillGene 服务托管。
// dev 模式下把所有后端 API 转发到本机 52010。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: '/',
  build: {
    outDir: path.resolve(__dirname, '../skillgene/web/dist'),
    emptyOutDir: true,
  },
  server: {
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/status': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/storage': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/sessions': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/conversations': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/validation': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/skills': { target: 'http://127.0.0.1:52010', changeOrigin: true },
    },
  },
});
