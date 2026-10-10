import { useEffect, useMemo } from "react";

import { RenderedHtml } from "../Mermaid";
import { REPO_URL } from "../config";
import { GUIDE, guidePage, type GuidePage } from "../learn";
import { renderGuide } from "../markdown";
import { href } from "../router";

function pageHref(page: GuidePage): string {
  return href(page.slug ? `learn/${page.slug}` : "learn");
}

function PageList({ current }: { current: string }) {
  return (
    <ol>
      {GUIDE.map((entry) => {
        const isCurrent = entry.slug === current;
        return (
          <li key={entry.file}>
            <a
              href={pageHref(entry)}
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

export function Learn({ slug, anchor }: { slug: string; anchor: string | null }) {
  const page = guidePage(slug);
  const html = useMemo(() => (page ? renderGuide(page.markdown, page.slug) : ""), [page]);

  useEffect(() => {
    document.title = page ? `${page.title} · Dagents` : "Not found · Dagents";
  }, [page]);

  if (!page) {
    return (
      <div className="block">
        <h1>Page not found</h1>
        <p>
          <a href={href("learn")}>Go to the guide</a>
        </p>
      </div>
    );
  }

  const index = GUIDE.indexOf(page);
  const previous = GUIDE[index - 1];
  const next = GUIDE[index + 1];

  return (
    <div className="learn">
      <nav className="learn-nav" aria-label="Guide pages">
        <p className="ds-eyebrow">Guide</p>
        <PageList current={page.slug} />
      </nav>

      <article className="learn-main">
        {/* On narrow screens the page list folds into this menu. */}
        <details className="learn-menu" key={page.slug}>
          <summary>Guide: {page.label}</summary>
          <nav aria-label="Guide pages">
            <PageList current={page.slug} />
          </nav>
        </details>

        <RenderedHtml className="doc" html={html} anchor={anchor} />

        <nav className="pager" aria-label="Previous and next page">
          {previous ? (
            <a href={pageHref(previous)}>
              <span className="ds-eyebrow">Previous</span>
              {previous.label}
            </a>
          ) : (
            <span />
          )}
          {next && (
            <a className="pager-next" href={pageHref(next)}>
              <span className="ds-eyebrow">Next</span>
              {next.label}
            </a>
          )}
        </nav>

        <p className="ds-note">
          <a href={`${REPO_URL}/blob/main/docs/learn/${page.file}`}>View this page on GitHub</a>
        </p>
      </article>
    </div>
  );
}
