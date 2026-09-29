import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const api = process.env.API_PORT ?? '8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5180,
    strictPort: true,
    proxy: {
      '/api': { target: `http://localhost:${api}`, changeOrigin: true },
      '/health': { target: `http://localhost:${api}`, changeOrigin: true },
    },
  },
})
