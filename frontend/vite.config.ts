// Imported from "vite", not "vitest/config", so `npm run build` never needs
// vitest resolved. The `test` block below is read by vitest all the same.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Keep node as the default; browser-interaction tests opt into jsdom per file.
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"] },
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.LYNEWRIGHT_DEV_API_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  }
});
