import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': { target: env.KF_API_PROXY_TARGET || 'http://127.0.0.1:8000' },
      },
    },
    preview: { port: 4173, strictPort: true },
  }
})
