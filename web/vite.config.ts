import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: process.env.OPENCORTEX_HTTP_URL || 'http://localhost:8921',
        changeOrigin: true,
      }
    }
  }
})
