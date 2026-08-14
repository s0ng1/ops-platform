import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    // WSL 访问 /mnt/d（DrvFs）时 chokidar 收不到文件事件，需轮询监听
    watch: { usePolling: true },
    // 开发环境将 /api 代理到本地后端
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: true,
        // 支持 WebSocket（/api/ws 实时推送）
        ws: true,
      },
    },
  },
})
