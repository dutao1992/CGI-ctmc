import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The app is mounted below the portal's protected /failure-analysis/ route.
  // Keep production bundles inside that route instead of requesting the
  // portal's top-level /assets directory.
  base: "/failure-analysis/",
  server: {
    host: "0.0.0.0",
    proxy: {
      "/failure-analysis-api": {
        target: "http://127.0.0.1:8010",
        changeOrigin: false,
        xfwd: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyRequest, request) => {
            if (request.headers.host) proxyRequest.setHeader("X-Forwarded-Host", request.headers.host);
            proxyRequest.setHeader("X-Forwarded-Proto", "http");
          });
        },
        rewrite: (path) => path.replace(/^\/failure-analysis-api/, ""),
      },
    }
  }
});
