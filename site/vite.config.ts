import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The site reads the generated service inventory from `docs/reference/`, which
// is outside this project root: that file is the framework's published HTTP
// contract, and copying it here would give it a second, driftable home. So the
// dev server is allowed to read two levels up, and the build bundles it.
export default defineConfig({
  base: process.env.SITE_BASE_PATH ?? "/",
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5175,
    fs: { allow: [".."] }
  }
});
