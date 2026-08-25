import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';
import guardLegacy from './integrations/guard-legacy.cjs';

export default defineConfig({
  root: resolve('./'),
  outDir: resolve('../site'),
  publicDir: resolve('./public'),
  output: 'static',

  build: { assets: '_astro' },
  vite: {
    plugins: [tailwindcss()],

    // The API sends no CORS headers (OPTIONS returns 501), so a browser can
    // only call it from the same origin. In production the built app is
    // served from the VM alongside the API; in dev we proxy, so local work
    // exercises the real service through the same relative paths and the
    // client never needs an environment-specific base URL.
    //
    // NOTE: this must live in the SINGLE vite block. A second `vite:` key
    // silently overwrites the first — object literals take the last one and
    // no error is raised. That bug cost a debugging round already.
    server: {
      proxy: {
        '/v1': { target: 'https://weft.switzerlandnorth.cloudapp.azure.com', changeOrigin: true, secure: true },
        '/mcp': { target: 'https://weft.switzerlandnorth.cloudapp.azure.com', changeOrigin: true, secure: true },
        '/healthz': { target: 'https://weft.switzerlandnorth.cloudapp.azure.com', changeOrigin: true, secure: true },
        '/downloads': { target: 'https://weft.switzerlandnorth.cloudapp.azure.com', changeOrigin: true, secure: true },
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            three: ['three'],
            r3f: ['@react-three/fiber', '@react-three/drei'],
            gsap: ['gsap', 'lenis'],
          },
        },
      },
    },
  },
  integrations: [react(), guardLegacy()],
  trailingSlash: 'never',
});
