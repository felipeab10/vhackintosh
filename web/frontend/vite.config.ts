import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // The appliance serves the SPA from its origin root. Absolute asset paths
  // keep deep links such as /vms/:id/edit from resolving JS/CSS under /vms/.
  base: '/',
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
});
