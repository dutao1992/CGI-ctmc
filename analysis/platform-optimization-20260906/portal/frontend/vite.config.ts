import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { target: "es2020" },
  server: {
    proxy: {
      "/platform-api": {
        target: "http://127.0.0.1:8010",
        // Preserve the browser-facing Host so backend same-origin checks also
        // protect and permit the local development flow.
        changeOrigin: false,
        xfwd: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyRequest, request) => {
            if (request.headers.host) {
              proxyRequest.setHeader("X-Forwarded-Host", request.headers.host);
            }
            proxyRequest.setHeader("X-Forwarded-Proto", "http");
          });
        },
        rewrite: (path) => path.replace(/^\/platform-api/, ""),
      },
    },
  },
});
