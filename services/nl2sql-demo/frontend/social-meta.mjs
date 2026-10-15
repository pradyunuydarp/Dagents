/* GENERATED FILE — DO NOT EDIT.
 *
 * Copied from design/social/social-meta.mjs by scripts/sync_design_system.py.
 * Edit the source and re-run:
 *
 *     python scripts/sync_design_system.py --write
 */

/**
 * A Vite plugin that writes the published address into a page's link preview tags.
 *
 * LinkedIn, WhatsApp, Slack and X build a link preview from the page's
 * `og:image`, `og:url` and `twitter:image` tags, and they ignore a relative
 * address. So each frontend's index.html writes `%PUBLIC_SITE_URL%`, and this
 * plugin replaces it at build time with the `PUBLIC_SITE_URL` environment
 * variable. The Pages workflow reads that address from env/.env.published, so
 * no HTML file holds it.
 *
 * Without the variable, as in a local build or the CI build, the placeholder
 * becomes the base path. The build still works and the tags point at the
 * build's own files, but a crawler cannot use them. The Pages workflow sets the
 * variable, and it checks every built page before it deploys.
 *
 * The plugin refuses an address that is not https, carries a query string or
 * credentials, does not end in `/`, or whose path differs from Vite's base
 * path: each of those would publish an `og:image` that does not load.
 */

export const PLACEHOLDER = "%PUBLIC_SITE_URL%";

/**
 * The prefix that replaces the placeholder.
 *
 * @param {string | undefined} value the PUBLIC_SITE_URL environment variable
 * @param {string} base Vite's resolved base path
 * @returns {{ prefix: string, absolute: boolean }}
 */
export function publicSitePrefix(value, base) {
  if (!value) return { prefix: base, absolute: false };
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`PUBLIC_SITE_URL is not an address: ${JSON.stringify(value)}`);
  }
  if (url.protocol !== "https:") throw new Error(`PUBLIC_SITE_URL must use https: ${value}`);
  if (url.search || url.hash || url.username || url.password) {
    throw new Error(`PUBLIC_SITE_URL must not carry a query string, a fragment or credentials: ${value}`);
  }
  if (!value.endsWith("/")) throw new Error(`PUBLIC_SITE_URL must end with /: ${value}`);
  if (base.startsWith("/") && url.pathname !== base) {
    throw new Error(
      `PUBLIC_SITE_URL is ${value}, but this build is served from ${base}. ` +
        `og:image would name a file that is not there.`
    );
  }
  return { prefix: url.href, absolute: true };
}

/**
 * @param {string} html
 * @param {string} prefix
 */
export function fillPublicSiteUrl(html, prefix) {
  return html.split(PLACEHOLDER).join(prefix);
}

/** @returns {import("vite").Plugin} */
export function socialMeta() {
  let base = "/";
  let warn = (/** @type {string} */ message) => console.warn(message);
  let building = false;
  return {
    name: "dagents-social-meta",
    configResolved(config) {
      base = config.base;
      building = config.command === "build";
      warn = (message) => config.logger.warn(message);
    },
    transformIndexHtml: {
      // Before Vite reads the tags, so it sees the final addresses.
      order: "pre",
      handler(html) {
        const { prefix, absolute } = publicSitePrefix(process.env.PUBLIC_SITE_URL, base);
        if (building && !absolute) {
          warn(
            `PUBLIC_SITE_URL is not set, so the link preview tags use the path ${prefix}. ` +
              `Link previews need the published address; the Pages workflow sets it from env/.env.published.`
          );
        }
        return fillPublicSiteUrl(html, prefix);
      }
    }
  };
}
