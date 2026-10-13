import { useEffect, useState } from "react";

/**
 * A small hash router, copied from the framework site (`site/src/router.ts`).
 *
 * The demo is served from GitHub Pages, which has no server-side routing, so
 * pages live in the URL hash: `#/hospitals/uk-134`. It is a copy, not an
 * import, because `apps/healthcare-demo/` must stay extractable.
 */

export interface Route {
  /** Path segments after `#/`, for example `["hospitals", "uk-134"]`. */
  segments: string[];
  /** Heading id to scroll to, if any. */
  anchor: string | null;
}

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [path, anchor] = raw.split("#", 2);
  const segments = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { segments, anchor: anchor ? decodeURIComponent(anchor) : null };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

/** Build a link to a page, optionally to a heading on it. */
export function href(path: string, anchor?: string): string {
  const clean = path.replace(/^\/+/, "");
  return `#/${clean}${anchor ? `#${anchor}` : ""}`;
}
