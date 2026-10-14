import { Marked, type Tokens } from "marked";

import { RAW_URL, REPO_URL, SITE_URL } from "./config";

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

/** A link from `docs/learn/` to a page of the guide to building an application. */
const BUILD_LINK = /^\.\.\/guide\/(README|\d\d-[a-z0-9-]+)\.md(?:#(.+))?$/;

/** A link from `docs/learn/` to a page of the API reference. */
const API_LINK = /^\.\.\/reference\/api\/([A-Za-z0-9-]+)\.md(?:#(.+))?$/;

/** The site route of an API reference page: `overview` is `#/api`, a module `#/api/<module>`. */
export function apiRoute(page: string, anchor?: string): string {
  return `#/api${page === "overview" ? "" : `/${page}`}${anchor ? `#${anchor}` : ""}`;
}

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

  // A page of the guide to building an application (docs/guide/) opens on the site.
  const build = target.match(BUILD_LINK);
  if (build) {
    const slug = build[1] === "README" ? "" : build[1];
    return { url: `#/guide${slug ? `/${slug}` : ""}${build[2] ? `#${build[2]}` : ""}`, external: false };
  }

  // A page of the API reference (docs/reference/api/<module>.md) opens on the
  // site; its contributor README opens on GitHub.
  const api = target.match(API_LINK);
  if (api && api[1] !== "README") {
    return { url: apiRoute(api[1], api[2]), external: false };
  }

  // Any other relative path is a file or folder in the repository.
  const kind = target.endsWith("/") ? "tree" : "blob";
  return { url: `${REPO_URL}/${kind}/main/${repoPath(target)}`, external: true };
}

/** A path relative to `base` (a folder of the repository), as a path from the repository root. */
function repoPath(target: string, base = "docs/learn/"): string {
  return new URL(target, `https://repo.invalid/${base}`).pathname.replace(/^\//, "");
}

/** A link to a hash route of the published site, as the same route on this site. */
function siteRoute(target: string): string | null {
  if (!target.startsWith(SITE_URL)) return null;
  const rest = target.slice(SITE_URL.length);
  return rest.startsWith("#/") ? rest : null;
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

/**
 * Where an image in a Markdown page is served from. `base` is the folder of the
 * page, so a relative path resolves the same way it does on GitHub.
 */
export function resolveImage(target: string, base = "docs/learn/"): string {
  if (/^https?:/.test(target)) return target;
  const path = repoPath(target, base);
  // An image outside the bundled folder is loaded from the repository.
  return IMAGES[`../../${path}`] ?? `${RAW_URL}/${path}`;
}

/** Where a link in a Markdown document should point on this site. */
export type LinkResolver = (target: string) => { url: string; external: boolean };

/** Render one guide page. `slug` is the page's route, used for in-page links. */
export function renderGuide(markdown: string, slug: string): string {
  return renderMarkdown(markdown, (target) => resolveLink(target, slug), "docs/learn/");
}

/**
 * Render a Markdown document from the repository with the guide's rules:
 * GitHub heading ids, Mermaid placeholders, bundled images, and links mapped by
 * `resolve`. The API reference uses it with its own resolver. `base` is the
 * folder the page lives in, for relative image paths.
 */
export function renderMarkdown(markdown: string, resolve: LinkResolver, base = "docs/learn/"): string {
  const marked = new Marked({
    gfm: true,
    renderer: {
      heading(this: { parser: { parseInline: (tokens: Tokens.Generic[]) => string } }, token: Tokens.Heading) {
        const id = slugify(token.text.replace(/[`*_]/g, ""));
        const inner = this.parser.parseInline(token.tokens);
        return `<h${token.depth} id="${id}">${inner}</h${token.depth}>\n`;
      },
      link(this: { parser: { parseInline: (tokens: Tokens.Generic[]) => string } }, token: Tokens.Link) {
        const route = siteRoute(token.href);
        const { url, external } = route ? { url: route, external: false } : resolve(token.href);
        const inner = this.parser.parseInline(token.tokens);
        const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : "";
        return `<a href="${escapeHtml(url)}"${attrs}>${inner}</a>`;
      },
      image(token: Tokens.Image) {
        // Wide diagrams shrink to the column, so the image links to its full size.
        const src = escapeHtml(resolveImage(token.href, base));
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
