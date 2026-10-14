/**
 * Copies the teaching resources into the built site.
 *
 * The decks, their PDFs and preview images stay where they are in the
 * repository (`docs/presentation/`, `docs/reports/`). This plugin serves them in
 * development and copies them into `dist/resources/` at build time, under the
 * stable names `publishedFiles` gives them, so no binary is stored twice.
 */
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

import { allPublishedFiles, RESOURCE_DIR } from "./src/resources";

const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));

const TYPES: Record<string, string> = {
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".pdf": "application/pdf",
  ".webp": "image/webp"
};

/** Published path (`resources/<id>.<ext>`) to the absolute source path. */
function sources(): Map<string, string> {
  return new Map(allPublishedFiles().map((file) => [file.path, resolve(REPO_ROOT, file.from)]));
}

export function resources(): Plugin {
  return {
    name: "dagents-resources",

    configureServer(server) {
      const files = sources();
      server.middlewares.use((request, response, next) => {
        const prefix = `${server.config.base}${RESOURCE_DIR}/`;
        const url = (request.url ?? "").split("?")[0];
        if (!url.startsWith(prefix)) return next();
        const source = files.get(`${RESOURCE_DIR}/${decodeURIComponent(url.slice(prefix.length))}`);
        if (!source || !existsSync(source)) {
          response.statusCode = 404;
          response.end("Not found");
          return;
        }
        response.setHeader("Content-Type", TYPES[extname(source)] ?? "application/octet-stream");
        response.setHeader("Content-Length", statSync(source).size);
        createReadStream(source).pipe(response);
      });
    },

    generateBundle() {
      for (const [path, source] of sources()) {
        if (!existsSync(source)) {
          this.error(
            `${path}: ${source} does not exist. Run python scripts/export_presentations.py --write.`
          );
        }
        this.emitFile({ type: "asset", fileName: path, source: readFileSync(source) });
      }
    }
  };
}
