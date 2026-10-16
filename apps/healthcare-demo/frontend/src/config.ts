/**
 * Addresses of the demo's documentation.
 *
 * `VITE_HEALTHCARE_README_URL` overrides the README link at build time, for a
 * copy of the demo extracted into its own repository. The default is the
 * README in the Dagents repository. No backend address belongs here: the
 * transport in `api.ts` keeps that to itself.
 */
export const README_URL =
  import.meta.env.VITE_HEALTHCARE_README_URL ??
  "https://github.com/pradyunuydarp/Dagents/blob/main/apps/healthcare-demo/README.md";

/**
 * The Dagents framework site, which documents the LMA and GMA routes.
 *
 * `VITE_DAGENTS_SITE_URL` overrides it at build time. The default is the
 * published framework site. It is a documentation page, never the demo's
 * backend.
 */
const DAGENTS_SITE_URL = (
  import.meta.env.VITE_DAGENTS_SITE_URL ?? "https://pradyunuydarp.github.io/Dagents/"
).replace(/\/?$/, "/");

/** The API reference page for the LMA and GMA routes on the framework site. */
export const AGENTS_API_URL = `${DAGENTS_SITE_URL}#/api/agents`;
