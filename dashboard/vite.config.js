import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
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
