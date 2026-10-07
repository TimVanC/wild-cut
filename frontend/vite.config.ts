import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const apiPort = process.env.API_PORT || '8787'
const uiPort = Number(process.env.FRONTEND_PORT || '5173')

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: uiPort,
    strictPort: false,
    proxy: {
      '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true },
    },
  },
})
