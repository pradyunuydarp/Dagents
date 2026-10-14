import { BUILD_GUIDE, buildGuideResolver } from "../guide";
import { renderMarkdown } from "../markdown";
import { DocPages, type DocSet } from "./DocPages";

/** The guide to building an application on Dagents (`docs/guide/`). */
const GUIDE_SET: DocSet = {
  route: "guide",
  name: "Guide",
  folder: "docs/guide",
  pages: BUILD_GUIDE,
  render: (page) => renderMarkdown(page.markdown, buildGuideResolver(page.slug), "docs/guide/")
};

export function Guide({ slug, anchor }: { slug: string; anchor: string | null }) {
  return <DocPages set={GUIDE_SET} slug={slug} anchor={anchor} />;
}
