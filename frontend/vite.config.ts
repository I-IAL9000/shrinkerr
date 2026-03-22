import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:6680",
      "/ws": { target: "ws://localhost:6680", ws: true },
    },
  },
});
