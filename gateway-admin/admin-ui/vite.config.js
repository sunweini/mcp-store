import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // proxy /api to FastAPI backend on :8081
    proxy: { '/api': 'http://localhost:8081' }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
