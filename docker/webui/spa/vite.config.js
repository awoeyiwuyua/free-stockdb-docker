import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：开发时热更新 + /api、/mcp 代理到本地 webui（app.py，8080）；
// 构建产物输出 dist/（文件名带内容哈希，可 immutable 缓存）。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/mcp': 'http://127.0.0.1:8080',
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.js'],
  },
})
