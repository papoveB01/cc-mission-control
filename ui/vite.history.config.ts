import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import pkg from "./package.json" with { type: "json" };

// The history page, served by the hub at /history (SPEC 13.4). It has no socket and no live
// state; it shares the HUD components and theme with the live dashboard.
export default defineConfig({
  base: "/",
  define: { __UI_VERSION__: JSON.stringify(pkg.version) },
  plugins: [react()],
  build: {
    outDir: "../cc_mission_control/hub/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    // The hub's CSP is `default-src 'self'`: nothing may be inlined into the page.
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
    rollupOptions: { input: "history.html" },
  },
  // Dev server limitation: the hub only accepts an Origin that matches its own Host (or one listed in
  // CCMC_HUB_HOSTS). Through this proxy the browser sends Origin http://localhost:5174, so a POST to
  // /login is rejected with 403 unless "localhost:5174" is added to CCMC_HUB_HOSTS. GETs carry no Origin.
  // The built bundle is served by the hub itself, where this does not arise.
  server: {
    port: 5174,
    proxy: {
      "/api/history": { target: "http://127.0.0.1:4318", changeOrigin: true },
      "/login": { target: "http://127.0.0.1:4318", changeOrigin: true },
    },
  },
});
