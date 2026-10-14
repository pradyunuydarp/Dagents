import { GUIDE } from "../learn";
import { renderGuide } from "../markdown";
import { DocPages, type DocSet } from "./DocPages";

/** The learning guide (`docs/learn/`). */
const LEARN_SET: DocSet = {
  route: "learn",
  name: "Learn",
  folder: "docs/learn",
  pages: GUIDE,
  render: (page) => renderGuide(page.markdown, page.slug)
};

export function Learn({ slug, anchor }: { slug: string; anchor: string | null }) {
  return <DocPages set={LEARN_SET} slug={slug} anchor={anchor} />;
}
