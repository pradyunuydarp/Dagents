import { Marked, type Tokens } from "marked";

import { RAW_URL, REPO_URL } from "./config";

/**
 * Markdown to HTML for the learning guide.
 *
 * Three things differ from plain Markdown rendering:
 *
 * - Links between guide pages become site routes, and links to other files in
 *   the repository go to GitHub.
 * - Headings get the same ids GitHub gives them, so `04-apis.md#some-heading`
 *   works in both places.
 * - `mermaid` code blocks become placeholders that `Mermaid.tsx` renders.
 */

/** GitHub's heading id: lower case, punctuation removed, spaces as hyphens. */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/<[^>]+>/g, "")
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const GUIDE_LINK = /^(README|references|\d\d-[a-z0-9-]+)\.md(?:#(.+))?$/;

/** Where a link in `docs/learn/<page>.md` should point on this site. */
export function resolveLink(target: string, currentSlug: string): { url: string; external: boolean } {
  if (/^(https?:|mailto:)/.test(target)) return { url: target, external: true };

  if (target.startsWith("#")) {
    return { url: `#/learn/${currentSlug}${target}`, external: false };
  }

  const guide = target.match(GUIDE_LINK);
  if (guide) {
    const slug = guide[1] === "README" ? "" : guide[1];
    const anchor = guide[2] ? `#${guide[2]}` : "";
    return { url: `#/learn${slug ? `/${slug}` : ""}${anchor}`, external: false };
  }

  // Any other relative path is a file or folder in the repository.
  const kind = target.endsWith("/") ? "tree" : "blob";
  return { url: `${REPO_URL}/${kind}/main/${repoPath(target)}`, external: true };
}

/** A path relative to `docs/learn/`, as a path from the repository root. */
function repoPath(target: string): string {
  return new URL(target, "https://repo.invalid/docs/learn/").pathname.replace(/^\//, "");
}

/**
 * Diagram images the guide can show. They are bundled with the site, so pages
 * do not depend on GitHub to display them.
 */
const IMAGES = import.meta.glob("../../docs/presentation/puml/*.png", {
  query: "?url",
  import: "default",
  eager: true
}) as Record<string, string>;

/** Where an image in a guide page is served from. */
export function resolveImage(target: string): string {
  if (/^https?:/.test(target)) return target;
  const path = repoPath(target);
  // An image outside the bundled folder is loaded from the repository.
  return IMAGES[`../../${path}`] ?? `${RAW_URL}/${path}`;
}

/** Render one guide page. `slug` is the page's route, used for in-page links. */
export function renderGuide(markdown: string, slug: string): string {
  const marked = new Marked({
    gfm: true,
    renderer: {
      heading(this: { parser: { parseInline: (tokens: Tokens.Generic[]) => string } }, token: Tokens.Heading) {
        const id = slugify(token.text.replace(/[`*_]/g, ""));
        const inner = this.parser.parseInline(token.tokens);
        return `<h${token.depth} id="${id}">${inner}</h${token.depth}>\n`;
      },
      link(this: { parser: { parseInline: (tokens: Tokens.Generic[]) => string } }, token: Tokens.Link) {
        const { url, external } = resolveLink(token.href, slug);
        const inner = this.parser.parseInline(token.tokens);
        const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : "";
        return `<a href="${escapeHtml(url)}"${attrs}>${inner}</a>`;
      },
      image(token: Tokens.Image) {
        // Wide diagrams shrink to the column, so the image links to its full size.
        const src = escapeHtml(resolveImage(token.href));
        const alt = escapeHtml(token.text);
        return `<a class="figure-link" href="${src}" target="_blank" rel="noopener noreferrer"><img src="${src}" alt="${alt}" loading="lazy"></a>`;
      },
      code(token: Tokens.Code) {
        if (token.lang === "mermaid") {
          return `<div class="mermaid" data-diagram="${encodeURIComponent(token.text)}"></div>\n`;
        }
        return `<pre class="code"><code>${escapeHtml(token.text)}</code></pre>\n`;
      }
    }
  });
  const html = marked.parse(markdown, { async: false }) as string;
  // Wide tables scroll inside their own box instead of widening the page.
  return html.replace(/<table>/g, '<div class="table-scroll"><table>').replace(/<\/table>/g, "</table></div>");
}
