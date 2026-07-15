import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  base: process.env.OIL_WORLD_BASE_PATH || "/",
  plugins: [react()],
  publicDir: fileURLToPath(new URL("../../public", import.meta.url)),
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5175,
  },
});
