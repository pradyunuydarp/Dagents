import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { socialMeta } from "./social-meta.mjs";

// `DEMO_BASE_PATH` lets the same build be served from a subpath, which is how
// the published site nests the demos under the framework site. It defaults to
// "/" so local runs and the smoke test are unaffected.
export default defineConfig({
  base: process.env.DEMO_BASE_PATH ?? "/",
  // `socialMeta()` writes the published address (PUBLIC_SITE_URL) into the link
  // preview tags of index.html.
  plugins: [react(), socialMeta()],
  server: {
    proxy: {
      "/api": process.env.HEALTHCARE_DEMO_API_URL ?? "http://127.0.0.1:8080"
    }
  }
});
