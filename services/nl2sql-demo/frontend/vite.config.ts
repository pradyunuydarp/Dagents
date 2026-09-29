import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `DEMO_BASE_PATH` lets the same build be served from a subpath, which is how
// the published site nests the demos under the framework site. It defaults to
// "/" so local runs are unaffected.
export default defineConfig({
  base: process.env.DEMO_BASE_PATH ?? "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": process.env.NL2SQL_DEMO_API_URL ?? "http://127.0.0.1:8070"
    }
  }
});
