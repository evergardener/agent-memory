import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.indexOf("node_modules/cytoscape") >= 0) return "graph-engine";
          if (
            id.indexOf("node_modules/react") >= 0 ||
            id.indexOf("node_modules/scheduler") >= 0
          ) {
            return "react-vendor";
          }
          return undefined;
        }
      }
    }
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:7788"
    }
  }
});
