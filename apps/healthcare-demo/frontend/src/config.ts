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
