/**
 * Site addresses and the demo list, in one place.
 *
 * The site can be served at the root (local dev, a custom domain) or under
 * `/<repository>/` (GitHub Pages). Paths are therefore built from
 * `import.meta.env.BASE_URL`, which the Pages workflow sets. The only absolute
 * addresses are the repository's.
 */

export const REPO_URL = "https://github.com/pradyunuydarp/Dagents";

/** Raw files on the main branch, for images in the guide. */
export const RAW_URL = "https://raw.githubusercontent.com/pradyunuydarp/Dagents/main";

export const REPO_LINKS = {
  contributing: `${REPO_URL}/blob/main/AGENTS.md`,
  serviceInventory: `${REPO_URL}/blob/main/docs/reference/service-inventory.md`,
  guide: `${REPO_URL}/tree/main/docs/learn`
} as const;

export interface DemoLink {
  /** The folder the demo is published under. */
  slug: string;
  name: string;
  summary: string;
  /** What to try first. */
  tryThis: string[];
  /** Where the page gets its data. */
  data: string;
  source: string;
}

/**
 * The demos published under this site. `slug` matches the folder the Pages
 * workflow publishes each demo to.
 */
export const DEMOS: DemoLink[] = [
  {
    slug: "healthcare-demo",
    name: "Stroke triage across three hospitals",
    summary:
      "Governance and federated learning in one app. Three simulated hospitals train and evaluate a triage model without sharing patient records.",
    tryThis: [
      "Change who is asking and how much they ask for, and watch the protection change.",
      "Run the pilot. The new model scores higher than the current one, but it fails the fairness and sensitivity gates, so it is not released."
    ],
    data: "Calls a deployed API backed by a Postgres database. All patients are synthetic.",
    source: `${REPO_URL}/tree/main/apps/healthcare-demo`
  },
  {
    slug: "nl2sql-demo",
    name: "Natural language to SQL",
    summary:
      "An ordinary app built on the framework. It uses Dagents for source validation, planning, schema checks and model routing.",
    tryThis: [
      "Ask a question and follow the trace of framework steps behind the answer."
    ],
    data: "Replays a recorded run of the real services, so it needs no backend.",
    source: `${REPO_URL}/tree/main/services/nl2sql-demo`
  }
];

/** A path under the site's base URL. */
export function sitePath(path: string): string {
  return `${import.meta.env.BASE_URL}${path.replace(/^\//, "")}`;
}

/** The address of a published demo. */
export function demoHref(slug: string): string {
  return sitePath(`${slug}/`);
}
