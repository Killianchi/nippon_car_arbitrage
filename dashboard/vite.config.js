import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // GitHub Pages serves a project site under /<repo>/, so the bundle needs a
  // matching base or every asset 404s. CI passes it in; local dev stays at /.
  base: process.env.VITE_BASE || '/',
  build: {
    outDir: 'dist',
    // Keep the bundle small: recharts is the heaviest thing here, so it gets
    // its own chunk and only loads on the pages that draw charts.
    rollupOptions: {
      output: {
        manualChunks: { charts: ['recharts'] },
      },
    },
    chunkSizeWarningLimit: 700,
  },
})
