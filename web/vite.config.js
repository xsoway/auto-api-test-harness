import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本地类 Langfuse 前端。build 产物输出到 dist，供后端 serve() 托管。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // dev 模式把 /api 代理到后端服务（CORS 也已放行，双保险）。
      '/api': 'http://127.0.0.1:8111',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})