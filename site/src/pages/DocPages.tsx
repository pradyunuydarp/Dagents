import { useEffect, useMemo } from "react";

import { RenderedHtml } from "../Mermaid";
import { REPO_URL } from "../config";
import type { GuidePage } from "../learn";
import { href } from "../router";

/**
 * A set of Markdown pages from one folder of `docs/`, shown with a page list
 * and previous and next links. The learning guide (`#/learn`) and the guide to
 * building an application (`#/guide`) both use it.
 */
export interface DocSet {
  /** The route segment: `learn` or `guide`. */
  route: string;
  /** Short name shown above the page list, such as "Learn". */
  name: string;
  /** The folder in the repository, such as `docs/learn`. */
  folder: string;
  pages: GuidePage[];
  /** Markdown to HTML for one page of this set. */
  render: (page: GuidePage) => string;
}

function pageHref(set: DocSet, page: GuidePage): string {
  return href(page.slug ? `${set.route}/${page.slug}` : set.route);
}

function PageList({ set, current }: { set: DocSet; current: string }) {
  return (
    <ol>
      {set.pages.map((entry) => {
        const isCurrent = entry.slug === current;
        return (
          <li key={entry.file}>
            <a
              href={pageHref(set, entry)}
              className={isCurrent ? "is-current" : undefined}
              aria-current={isCurrent ? "page" : undefined}
            >
              {entry.label}
            </a>
          </li>
        );
      })}
    </ol>
  );
}

export function DocPages({ set, slug, anchor }: { set: DocSet; slug: string; anchor: string | null }) {
  const page = set.pages.find((entry) => entry.slug === slug);
  const html = useMemo(() => (page ? set.render(page) : ""), [page, set]);

  useEffect(() => {
    document.title = page ? `${page.title} · Dagents` : "Not found · Dagents";
  }, [page]);

  if (!page) {
    return (
      <div className="block">
        <h1>Page not found</h1>
        <p>
          <a href={href(set.route)}>Go to the first page</a>
        </p>
      </div>
    );
  }

  const index = set.pages.indexOf(page);
  const previous = set.pages[index - 1];
  const next = set.pages[index + 1];

  return (
    <div className="learn">
      <nav className="learn-nav" aria-label={`${set.name} pages`}>
        <p className="ds-eyebrow">{set.name}</p>
        <PageList set={set} current={page.slug} />
      </nav>

      <article className="learn-main" key={page.slug}>
        {/* On narrow screens the page list folds into this menu. */}
        <details className="learn-menu" key={page.slug}>
          <summary>
            {set.name}: {page.label}
          </summary>
          <nav aria-label={`${set.name} pages`}>
            <PageList set={set} current={page.slug} />
          </nav>
        </details>

        <RenderedHtml className="doc" html={html} anchor={anchor} />

        <nav className="pager" aria-label="Previous and next page">
          {previous ? (
            <a href={pageHref(set, previous)}>
              <span className="ds-eyebrow">Previous</span>
              {previous.label}
            </a>
          ) : (
            <span />
          )}
          {next && (
            <a className="pager-next" href={pageHref(set, next)}>
              <span className="ds-eyebrow">Next</span>
              {next.label}
            </a>
          )}
        </nav>

        <p className="ds-note">
          <a href={`${REPO_URL}/blob/main/${set.folder}/${page.file}`}>View this page on GitHub</a>
        </p>
      </article>
    </div>
  );
}
