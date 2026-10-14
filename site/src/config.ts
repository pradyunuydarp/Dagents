/**
 * Site addresses and the demo list, in one place.
 *
 * The site can be served at the root (local dev, a custom domain) or under
 * `/<repository>/` (GitHub Pages). Paths are therefore built from
 * `import.meta.env.BASE_URL`, which the Pages workflow sets. The only absolute
 * addresses are the repository's.
 */

export const REPO_URL = "https://github.com/pradyunuydarp/Dagents";

/**
 * The published framework site. A Markdown page that links to one of its hash
 * routes (`<SITE_URL>#/resources`) works on GitHub, and on this site the link
 * opens the route here instead of the production copy.
 */
export const SITE_URL = "https://pradyunuydarp.github.io/Dagents/";

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
      "Governance and federated learning on real patient records. Three hospitals from the International Stroke Trial each keep their records in a separate store, and train and evaluate a triage model without sharing them.",
    tryThis: [
      "Change who is asking and how much they ask for, and watch the protection change.",
      "Run the pilot: five rounds of federated training, then the release gates decide whether the new model may replace the current one.",
      "Open a hospital's view to see what its staff see. Enter a record there: once its outcome is recorded, the next pilot trains on it."
    ],
    data: "Calls the demo's API when it is up and reading Postgres, where each hospital keeps its de-identified records from 1991-1996 in its own schema. Otherwise the page replays a recorded run and says so. Contains information from the International Stroke Trial database (version 2), made available under the ODC Attribution License.",
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
