import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve('./'),
  outDir: resolve('../site'),
  publicDir: resolve('./public'),
  output: 'static',
  build: { assets: '_astro' },
  vite: {
    plugins: [tailwindcss()],
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
  integrations: [react()],
  trailingSlash: 'never',
});
