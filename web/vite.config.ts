import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the api runs on 8080 (desk-api serve); the dev server proxies to it.
// DESK_API points the proxy elsewhere, for example a second api on a development database.
const target = process.env.DESK_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/ws": { target: target.replace("http", "ws"), ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
