import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In Docker the dev server reaches the API at http://api:8000; on the host it's
// http://localhost:8000. Configurable via VITE_PROXY_TARGET.
const proxyTarget = process.env.VITE_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    host: true,
    // Proxy API calls to the backend in dev so the browser hits same-origin.
    proxy: {
      "/api": { target: proxyTarget, changeOrigin: true, ws: true },
    },
  },
});
