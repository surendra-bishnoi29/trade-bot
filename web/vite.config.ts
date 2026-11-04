import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the api runs on 8080 (desk-api serve); the dev server proxies to it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
