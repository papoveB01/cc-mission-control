import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import pkg from "./package.json" with { type: "json" };

const BACKEND = "http://127.0.0.1:4317";

export default defineConfig({
  base: "/",
  // The build version comes from package.json, so the output stays deterministic.
  define: { __UI_VERSION__: JSON.stringify(pkg.version) },
  plugins: [react()],
  build: {
    outDir: "../cc_mission_control/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
      "/api": { target: BACKEND, changeOrigin: true },
      "/hook": { target: BACKEND, changeOrigin: true },
      "/health": { target: BACKEND, changeOrigin: true },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
