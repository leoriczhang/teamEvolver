import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// teamEvolver 统一控制台前端。
// 构建产物输出到 teamEvolver/web/dist/，由 teamEvolver 服务托管。
// dev 模式下把所有后端 API 转发到本机 52010。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ['react', 'react-dom'],
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@memory': path.resolve(__dirname, '../team_memory/frontend'),
      '@miner': path.resolve(__dirname, '../team_miner/frontend'),
      'react': path.resolve(__dirname, 'node_modules/react'),
      'react-dom': path.resolve(__dirname, 'node_modules/react-dom'),
      'lucide-react': path.resolve(__dirname, 'node_modules/lucide-react'),
      'react-markdown': path.resolve(__dirname, 'node_modules/react-markdown'),
      'remark-gfm': path.resolve(__dirname, 'node_modules/remark-gfm'),
      '@testing-library': path.resolve(__dirname, 'node_modules/@testing-library'),
      'vitest': path.resolve(__dirname, 'node_modules/vitest'),
    },
  },
  test: {
    include: [
      'src/**/*.test.tsx',
      '../team_memory/frontend/**/*.test.tsx',
      '../team_miner/frontend/**/*.test.tsx',
    ],
  },
  base: '/',
  build: {
    outDir: path.resolve(__dirname, '../teamEvolver/web/dist'),
    emptyOutDir: true,
  },
  server: {
    fs: { allow: [path.resolve(__dirname, '..')] },
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/status': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/storage': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/sessions': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/conversations': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/validation': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/skills': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/langfuse': { target: 'http://127.0.0.1:52010', changeOrigin: true },
      '/trigger-dreamcycle': { target: 'http://127.0.0.1:52010', changeOrigin: true },
    },
  },
});
