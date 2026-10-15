/**
 * The search index of the learning guide (`docs/learn/`) and the guide to
 * building an application (`docs/guide/`): one entry per section of a page,
 * so a result opens at the heading it matched under.
 */

import { BUILD_GUIDE, buildGuideRoute } from "./guide";
import { FuzzyIndex, words, type Match } from "./fuzzy";
import { GUIDE, type GuidePage } from "./learn";
import { slugify } from "./markdown";

export interface DocSection {
  /** "Learn" or "Guide". */
  set: string;
  page: GuidePage;
  /** The section's heading, or the page title for the text above the first heading. */
  heading: string;
  href: string;
  /** The section's text with the Markdown removed. */
  text: string;
}

/** Markdown to plain text, enough to search and quote: links keep their text, diagrams go. */
function plain(markdown: string): string {
  return markdown
    .replace(/```mermaid[\s\S]*?```/g, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]+>/g, " ")
    .replace(/^\s*(```.*|[-*+>|]|\d+\.)\s*/gm, "")
    .replace(/[`*_|#]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function sections(set: string, pages: GuidePage[], route: (slug: string, anchor?: string) => string): DocSection[] {
  const out: DocSection[] = [];
  for (const page of pages) {
    // A heading inside a code block is not a heading, so split outside them only.
    const parts = page.markdown.split(/^(```[\s\S]*?^```.*$)/m);
    let heading = page.title;
    let anchor: string | undefined;
    let body: string[] = [];
    const flush = () => {
      const text = plain(body.join("\n"));
      if (text || anchor) out.push({ set, page, heading, href: route(page.slug, anchor), text });
      body = [];
    };
    parts.forEach((part, i) => {
      if (i % 2) return void body.push(part);
      for (const line of part.split("\n")) {
        const match = line.match(/^(#{1,4})\s+(.+?)\s*#*$/);
        if (!match) {
          body.push(line);
          continue;
        }
        flush();
        if (match[1] === "#") {
          heading = page.title;
          anchor = undefined;
        } else {
          heading = plain(match[2]);
          anchor = slugify(match[2].replace(/[`*_]/g, ""));
        }
      }
    });
    flush();
  }
  return out;
}

let cached: FuzzyIndex<DocSection> | null = null;

/** Built on the first search, from the Markdown already in the bundle. */
function docIndex(): FuzzyIndex<DocSection> {
  cached ??= new FuzzyIndex(
    [
      ...sections("Learn", GUIDE, (slug, anchor) => `#/learn${slug ? `/${slug}` : ""}${anchor ? `#${anchor}` : ""}`),
      ...sections("Guide", BUILD_GUIDE, buildGuideRoute)
    ],
    (section) => [
      { text: section.heading, weight: 1 },
      { text: `${section.page.label} ${section.page.title}`, weight: 0.5 },
      { text: section.text, weight: 0.35 }
    ]
  );
  return cached;
}

export function searchDocs(query: string, limit: number): Match<DocSection>[] {
  return docIndex().search(query, limit);
}

/** About `length` characters of `text` around the first matched word. */
export function snippet(text: string, matched: Set<string>, length = 160): string {
  let at = -1;
  for (const found of text.matchAll(/[\p{L}\p{N}]+/gu)) {
    if (words(found[0]).some((word) => matched.has(word))) {
      at = found.index ?? 0;
      break;
    }
  }
  if (at < 0) return text.slice(0, length) + (text.length > length ? "…" : "");
  let start = Math.max(0, at - 50);
  if (start > 0) start = text.indexOf(" ", start) + 1 || start;
  const end = Math.min(text.length, start + length);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
}
