import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Local dev: proxy so you don't need CORS headers in development
  server: {
    proxy: {
      '/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true },
      '/negotiations': { target: 'http://localhost:8000', changeOrigin: true },
      '/bridges': { target: 'http://localhost:8000', changeOrigin: true },
      '/hospitals': { target: 'http://localhost:8000', changeOrigin: true },
      '/intake': { target: 'http://localhost:8000', changeOrigin: true },
      '/donors': { target: 'http://localhost:8000', changeOrigin: true },
      '/stats': { target: 'http://localhost:8000', changeOrigin: true },
      '/twilio': { target: 'http://localhost:8000', changeOrigin: true },
      '/live-phones': { target: 'http://localhost:8000', changeOrigin: true },
      '/policy': { target: 'http://localhost:8000', changeOrigin: true },
      '/aws': { target: 'http://localhost:8000', changeOrigin: true },
      '/bedrock': { target: 'http://localhost:8000', changeOrigin: true },
      '/translate': { target: 'http://localhost:8000', changeOrigin: true },
      '/settings': { target: 'http://localhost:8000', changeOrigin: true },
      '/recovery': { target: 'http://localhost:8000', changeOrigin: true },
      '/prescreen': { target: 'http://localhost:8000', changeOrigin: true },
      '/screening': { target: 'http://localhost:8000', changeOrigin: true },
      '/churn': { target: 'http://localhost:8000', changeOrigin: true },
      '/prevention': { target: 'http://localhost:8000', changeOrigin: true },
      '/insights': { target: 'http://localhost:8000', changeOrigin: true },
    }
  }
})
