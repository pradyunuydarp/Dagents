/**
 * The guide to building an application on Dagents, read from `docs/guide/`.
 *
 * Like the learning guide (`learn.ts`), it is written once as Markdown in the
 * repository and bundled at build time, so it reads the same on GitHub and on
 * this site. Its pages are shown at `#/guide` and `#/guide/<page>`.
 */

import type { GuidePage } from "./learn";
import { apiRoute, resolveLink, type LinkResolver } from "./markdown";
import { REPO_URL } from "./config";

const files = import.meta.glob("../../docs/guide/*.md", {
  query: "?raw",
  import: "default",
  eager: true
}) as Record<string, string>;

/** Reading order. Files not listed here are not shown. */
const ORDER: { file: string; label: string }[] = [
  { file: "README.md", label: "Overview" },
  { file: "01-setup.md", label: "1. Set up" },
  { file: "02-describe-your-data.md", label: "2. Describe your data" },
  { file: "03-store-per-source.md", label: "3. A store per source" },
  { file: "04-govern-every-read.md", label: "4. Govern every read" },
  { file: "05-compute-and-combine.md", label: "5. Compute and combine" },
  { file: "06-federated-rounds.md", label: "6. Federated rounds" },
  { file: "07-framework-services.md", label: "7. Framework services" },
  { file: "08-deploy.md", label: "8. Deploy" },
  { file: "09-test-your-integration.md", label: "9. Test your integration" },
  { file: "10-checklist.md", label: "10. Checklist" }
];

function titleOf(markdown: string, fallback: string): string {
  const match = markdown.match(/^#\s+(.+)$/m);
  return match ? match[1].trim() : fallback;
}

export const BUILD_GUIDE: GuidePage[] = ORDER.flatMap(({ file, label }) => {
  const entry = Object.entries(files).find(([path]) => path.endsWith(`/${file}`));
  if (!entry) return [];
  const markdown = entry[1];
  return [
    {
      slug: file === "README.md" ? "" : file.replace(/\.md$/, ""),
      file,
      title: titleOf(markdown, label),
      label,
      markdown
    }
  ];
});

export function buildGuidePage(slug: string): GuidePage | undefined {
  return BUILD_GUIDE.find((page) => page.slug === slug);
}

/** The site route of a page of this guide. */
export function buildGuideRoute(slug: string, anchor?: string): string {
  return `#/guide${slug ? `/${slug}` : ""}${anchor ? `#${anchor}` : ""}`;
}

const PAGE_LINK = /^(README|\d\d-[a-z0-9-]+)\.md(?:#(.+))?$/;
const LEARN_LINK = /^\.\.\/learn\/(.+)$/;
const API_LINK = /^\.\.\/reference\/api\/([A-Za-z0-9-]+)\.md(?:#(.+))?$/;

/**
 * Where a link in `docs/guide/<page>.md` points on this site.
 *
 * Links between pages of this guide, to the learning guide and to the API
 * reference stay on the site. Any other relative path is a file in the
 * repository and opens on GitHub.
 */
export function buildGuideResolver(currentSlug: string): LinkResolver {
  return (target) => {
    if (/^(https?:|mailto:)/.test(target)) return { url: target, external: true };
    if (target.startsWith("#")) return { url: buildGuideRoute(currentSlug, target.slice(1)), external: false };

    const page = target.match(PAGE_LINK);
    if (page) return { url: buildGuideRoute(page[1] === "README" ? "" : page[1], page[2]), external: false };

    const learn = target.match(LEARN_LINK);
    if (learn) return resolveLink(learn[1], "");

    const api = target.match(API_LINK);
    if (api && api[1] !== "README") return { url: apiRoute(api[1], api[2]), external: false };

    const path = new URL(target, "https://repo.invalid/docs/guide/").pathname.replace(/^\//, "");
    return { url: `${REPO_URL}/${target.endsWith("/") ? "tree" : "blob"}/main/${path}`, external: true };
  };
}
