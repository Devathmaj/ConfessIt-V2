/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import { componentTagger } from "lovable-tagger";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    host: "::",
    port: 8080,
    // Document-level hardening for local dev. Deliberately no `Content-Security-Policy`
    // here: Vite's dev server needs inline scripts and eval for HMR, so any dev CSP is
    // either broken or `unsafe-*` theatre -- the real policy is enforced by prod Nginx
    // (see nginx.prod.conf.example). Framing, sniffing and referrers need no such
    // exemption, so they are locked down in dev too.
    headers: {
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "no-referrer",
      "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    },
  },
  plugins: [
    react(),
    mode === 'development' &&
    componentTagger(),
  ].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './tests/setup.ts',
    // Tests are consolidated in ./tests rather than living beside the code they cover.
    include: ['tests/**/*.{test,spec}.{ts,tsx}'],
  },
}));
