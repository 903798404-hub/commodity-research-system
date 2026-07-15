import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { createReadStream, existsSync } from "node:fs";
import { resolve, sep } from "node:path";

const previewDataRoot = process.env.OIL_WORLD_PREVIEW_DATA_ROOT;

function previewDataPlugin(root: string | undefined): Plugin {
  return {
    name: "oil-world-preview-data",
    apply: "serve" as const,
    configureServer(server) {
      if (!root) return;
      const previewRoot = resolve(root);
      server.middlewares.use((request, response, next) => {
        const pathname = decodeURIComponent((request.url ?? "").split("?", 1)[0]);
        const prefix = "/data/oil_world/";
        if (!pathname.startsWith(prefix)) {
          next();
          return;
        }
        const relativePath = pathname.slice(prefix.length);
        if (!relativePath.startsWith("releases/") && !relativePath.startsWith("comparisons/")) {
          next();
          return;
        }
        const candidate = resolve(previewRoot, relativePath);
        if (!candidate.startsWith(`${previewRoot}${sep}`) || !existsSync(candidate)) {
          next();
          return;
        }
        response.statusCode = 200;
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        response.setHeader("X-Oil-World-Data-Source", "special-time-axis-preview");
        createReadStream(candidate).pipe(response);
      });
    },
  };
}

export default defineConfig({
  base: process.env.OIL_WORLD_BASE_PATH || "/",
  plugins: [react(), previewDataPlugin(previewDataRoot)],
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
