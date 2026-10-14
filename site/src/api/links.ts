/**
 * Links inside the API reference's Markdown (`docs/reference/api/*.md`).
 *
 * A link to another page of the reference (`dagentsc.md#...`) opens that page
 * on the site, a link to either guide (`../../learn/04-apis.md`,
 * `../../guide/06-federated-rounds.md`) opens that guide on the site, and any
 * other relative path opens the file on GitHub.
 */

import { REPO_URL } from "../config";
import { apiRoute, resolveLink, type LinkResolver } from "../markdown";

/** `page` is `overview` or a module slug: the page the Markdown is shown on. */
export function apiResolver(page: string): LinkResolver {
  return (target) => {
    if (/^(https?:|mailto:)/.test(target)) return { url: target, external: true };
    if (target.startsWith("#")) return { url: apiRoute(page, target.slice(1)), external: false };
    const reference = target.match(/^([A-Za-z0-9-]+)\.md(?:#(.+))?$/);
    if (reference && reference[1] !== "README") return { url: apiRoute(reference[1], reference[2]), external: false };
    const learn = target.match(/^\.\.\/\.\.\/learn\/(.+)$/);
    if (learn) return resolveLink(learn[1], "");
    // The guide to building an application (docs/guide/) opens on the site too.
    const guide = target.match(/^\.\.\/\.\.\/guide\/(.+)$/);
    if (guide) return resolveLink(`../guide/${guide[1]}`, "");
    const path = new URL(target, "https://repo.invalid/docs/reference/api/").pathname.replace(/^\//, "");
    return { url: `${REPO_URL}/${target.endsWith("/") ? "tree" : "blob"}/main/${path}`, external: true };
  };
}

/** A file or folder in the repository, on GitHub. */
export function repoHref(path: string): string {
  const folder = !/\.[A-Za-z0-9]+$/.test(path);
  return `${REPO_URL}/${folder ? "tree" : "blob"}/main/${path}`;
}
