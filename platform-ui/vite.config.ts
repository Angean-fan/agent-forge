import { fileURLToPath, URL } from 'node:url';

import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

// 平台 UI 独立于生成项目 Profile 管理：这里的依赖版本为平台自己的锁定集合。
// 开发时把 /api 代理到平台 API，使浏览器视角同源，Cookie 会话与 CSRF 双提交可直接工作。
const apiTarget = process.env.AGENTFORGE_API_TARGET ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: false },
    },
  },
});
