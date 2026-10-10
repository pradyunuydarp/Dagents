import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The site reads two things from outside this folder: the generated service
// inventory (docs/reference/) and the learning guide (docs/learn/). Each keeps
// a single home in the repository, so the dev server may read the repository
// root and the build bundles both.
export default defineConfig({
  base: process.env.SITE_BASE_PATH ?? "/",
  plugins: [react()],
  build: {
    // Mermaid's diagram chunks are large, but each loads only when a page
    // draws that kind of diagram. The main bundle is far below this.
    chunkSizeWarningLimit: 1600
  },
  server: {
    host: "0.0.0.0",
    port: 5175,
    fs: { allow: [".."] }
  }
});
