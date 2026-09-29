/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import { componentTagger } from "lovable-tagger";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    // Loopback only: the dev server (with HMR control over the page) must never be
    // reachable from the LAN. Published ports stay loopback-bound in compose for the
    // same reason; production serves built assets behind Nginx instead.
    host: "127.0.0.1",
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
    tailwindcss(),
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
    // A fully parallel run shares the machine 64 ways; multi-step browser-simulation
    // tests legitimately exceed the 5s default under that contention. The budget grows
    // instead of the assertions shrinking -- a hung test still fails, only later.
    // (Long individual tests may still set their own `timeout` on top of this.)
    testTimeout: 10000,
  },
}));
