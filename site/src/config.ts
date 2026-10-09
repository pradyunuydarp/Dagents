/**
 * Everything about *where* this site lives, in one place.
 *
 * The site has to work at three different addresses without a code change: a
 * dev server at the root, a project page under `/<repository>/`, and a custom
 * domain back at the root. So nothing here writes an absolute site URL. Paths
 * are resolved against `import.meta.env.BASE_URL`, which Vite fills from the
 * `SITE_BASE_PATH` the Pages workflow derives from `configure-pages` — moving
 * to a custom domain changes that value and nothing else.
 *
 * The only absolute URL is the repository, which does not move with the domain.
 */

/** The source repository. Not derived, because it is not a site path. */
export const REPO_URL = "https://github.com/pradyunuydarp/Dagents";

/** Repository-relative links, so a fork or a rename needs one edit here. */
export const REPO_LINKS = {
  contributing: `${REPO_URL}/blob/main/AGENTS.md`,
  serviceInventory: `${REPO_URL}/blob/main/docs/reference/service-inventory.md`,
  planners: `${REPO_URL}/tree/main/bindings/ocaml`
} as const;

export interface DemoLink {
  /** Directory the demo is published under, and its id in the repository. */
  slug: string;
  name: string;
  owns: string;
  framework: string;
  proves: string;
  /**
   * Where the published page gets its answers.
   *
   * The two demos differ, and the difference is the kind of thing a reader is
   * entitled to know before trusting a panel — so each states its own rather
   * than one sentence covering both.
   */
  transport: string;
  caveat: string;
}

/**
 * The demos published beneath this site.
 *
 * `slug` is the single source for the published path: the Pages workflow
 * assembles `_site/<slug>/` and this list builds the links to it, so the two
 * cannot drift into pointing at different directories.
 */
export const DEMOS: DemoLink[] = [
  {
    slug: "healthcare-demo",
    name: "Stroke triage across three hospitals",
    owns: "Its clinical feature contract, scoring rule, FHIR mapping and intended-use statement",
    framework: "Governance, federation, and everything generic",
    proves:
      "That the governance and federation layers really decide things, and what they cost. Three levers change the guard's strategy; a candidate beats its baseline on AUC and is still refused release because a fairness gate fails.",
    transport:
      "Calls a deployed API that reads its encounters from Postgres, with the planner in the image. The page names the backend and prints the provenance that backend reports, so a replay cannot be mistaken for a live read.",
    caveat: "Synthetic patients. Not a medical device, not clinically validated."
  },
  {
    slug: "nl2sql-demo",
    name: "Natural language to SQL",
    owns: "Its UI and its SQL generation",
    framework: "Validation, planning, service checks and workload compilation",
    proves:
      "That an ordinary app can consume the framework end to end: a trace of SourceSpec validation, extraction planning, schema contracts, quality rules, DAG planning and model routing, then the service calls behind them.",
    transport:
      "No deployed backend, so it replays a capture of a real run — recorded from the live services with the planner built, at a commit the page names. A request the capture does not hold is refused rather than answered.",
    caveat: "The published run uses the deterministic fallback adapter, not a GPU model."
  }
];

/**
 * Resolve a path against whatever base this build is served from.
 *
 * `BASE_URL` already carries its trailing slash, so a leading slash here would
 * produce `//` on a project page and silently leave the site.
 */
export function sitePath(path: string): string {
  return `${import.meta.env.BASE_URL}${path.replace(/^\//, "")}`;
}

/** Link to one published demo. */
export function demoHref(slug: string): string {
  return sitePath(`${slug}/`);
}
