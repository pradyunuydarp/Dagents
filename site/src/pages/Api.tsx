import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import { BreakablePath, InterfaceSection, MethodBadge, ObjectSection, Prose, endpointCards } from "../api/components";
import { useApiData, type ApiData } from "../api/data";
import { MODULE_DOCS, OVERVIEW, moduleDoc } from "../api/modules";
import { buildIndex, search, searchIndex } from "../api/search";
import { SearchBox, type SearchResult } from "../SearchBox";
import { REPO_URL } from "../config";
import { apiRoute } from "../markdown";
import { replayAnimations } from "../replay";
import { href } from "../router";

/**
 * The API reference: `#/api` is the overview, `#/api/<module>` a module page.
 * A second `#` selects an interface, an endpoint or an object on the page.
 */
export function Api({ slug, anchor }: { slug: string; anchor: string | null }) {
  const module = slug ? moduleDoc(slug) : undefined;
  const { data, error } = useApiData();
  const last = useRef<string | null>(null);
  const menu = useRef<HTMLDetailsElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  useSidebarFollowsPage(sidebar, slug, data);

  useEffect(() => {
    document.title = module ? `${module.label} · API reference · Dagents` : "API reference · Dagents";
  }, [module]);

  // Scroll to the anchor once the page holds it, or to the top on a new page.
  useLayoutEffect(() => {
    if (anchor) {
      const target = document.getElementById(anchor);
      if (target) target.scrollIntoView();
    } else if (last.current !== slug) {
      window.scrollTo(0, 0);
    }
    last.current = slug;
  }, [slug, anchor, data]);

  if (slug && !module) {
    return (
      <div className="block">
        <h1>Page not found</h1>
        <p>
          <a href={href("api")}>Go to the API reference</a>
        </p>
      </div>
    );
  }

  return (
    <div className="api-layout">
      <nav className="api-nav" aria-label="API reference" ref={sidebar}>
        <NavTree current={slug} data={data} />
      </nav>
      {/* data-api-ready tells the browser check that the data has loaded (or failed). */}
      <div className="api-main" data-api-ready={data || error ? "true" : undefined}>
        <details className="api-menu" key={slug} ref={menu}>
          <summary>API menu{module ? `: ${module.label}` : ""}</summary>
          <nav
            aria-label="API reference"
            onClick={(event) => {
              // Close the menu once a link is chosen, so the page it opens is visible.
              if ((event.target as HTMLElement).closest("a") && menu.current) menu.current.open = false;
            }}
          >
            <NavTree current={slug} data={data} />
          </nav>
        </details>
        <Search data={data} />
        {error && (
          <p className="ds-banner ds-banner--deny" role="alert">
            The reference data could not be loaded: {error}
          </p>
        )}
        {module ? <ModulePage slug={slug} data={data} /> : <Overview data={data} />}
      </div>
    </div>
  );
}

/* ===== Navigation ========================================================= */

/**
 * Mark the interface or object the reader is in, in the sidebar, and keep that
 * entry in view. A module page is long (the agents page is about 260,000 px),
 * so the sidebar is the main way to see where one is.
 */
function useSidebarFollowsPage(sidebar: RefObject<HTMLElement | null>, slug: string, data: ApiData | null) {
  useEffect(() => {
    const nav = sidebar.current;
    if (!nav || !slug || !data) return;
    let frame = 0;
    let current: HTMLAnchorElement | null = null;
    const update = () => {
      frame = 0;
      const sections = Array.from(document.querySelectorAll<HTMLElement>(".api-interface, .api-object"));
      const above = sections.filter((section) => section.getBoundingClientRect().top < 140);
      const section = above[above.length - 1];
      const link = section ? nav.querySelector<HTMLAnchorElement>(`a[href="${apiRoute(slug, section.id)}"]`) : null;
      if (link === current) return;
      current?.removeAttribute("aria-current");
      current = link;
      if (!link) return;
      link.setAttribute("aria-current", "location");
      const box = nav.getBoundingClientRect();
      const place = link.getBoundingClientRect();
      if (place.top < box.top || place.bottom > box.bottom) nav.scrollTop += place.top - box.top - box.height / 3;
    };
    const onScroll = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame) window.cancelAnimationFrame(frame);
      current?.removeAttribute("aria-current");
    };
  }, [sidebar, slug, data]);
}


function NavTree({ current, data }: { current: string; data: ApiData | null }) {
  return (
    <ol className="api-tree">
      <li>
        <a href={href("api")} className={!current ? "is-current" : undefined} aria-current={!current ? "page" : undefined}>
          Overview
        </a>
      </li>
      {MODULE_DOCS.map((module) => {
        const open = module.slug === current;
        return (
          <li key={module.slug}>
            <a
              href={apiRoute(module.slug)}
              className={open ? "is-current" : undefined}
              aria-current={open ? "page" : undefined}
            >
              {module.label}
            </a>
            {open && (
              <ol>
                {module.interfaces.map((iface) => (
                  <li key={iface.anchor}>
                    <a href={apiRoute(module.slug, iface.anchor)}>{iface.heading}</a>
                    {data && (
                      <ol className="api-tree-endpoints">
                        {iface.service === "dagentsc"
                          ? iface.endpoints.map((ref) => (
                              <li key={ref.key}>
                                <a href={apiRoute(module.slug, `dagentsc-${ref.words.join("-")}`)}>{ref.words.join(" ")}</a>
                              </li>
                            ))
                          : endpointCards(data, iface).map((card) => (
                              <li key={card.primary.id}>
                                <a href={apiRoute(module.slug, card.primary.id)}>
                                  <MethodBadge method={card.primary.method} /> <BreakablePath path={card.primary.path} />
                                </a>
                              </li>
                            ))}
                      </ol>
                    )}
                  </li>
                ))}
                {module.objects.length > 0 && (
                  <li>
                    <a href={apiRoute(module.slug, "objects")}>Objects</a>
                    <ol className="api-tree-endpoints">
                      {module.objects.map((obj) => (
                        <li key={obj.anchor}>
                          <a href={apiRoute(module.slug, obj.anchor)}>{obj.name}</a>
                        </li>
                      ))}
                    </ol>
                  </li>
                )}
              </ol>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/* ===== Search ============================================================= */

const SEARCH_LIMIT = 30;

function Search({ data }: { data: ApiData | null }) {
  const index = useMemo(() => searchIndex(data ? buildIndex(data) : []), [data]);
  const run = useCallback(
    (query: string): SearchResult[] =>
      search(index, query, SEARCH_LIMIT).map(({ item, words }) => ({
        key: `${item.kind}:${item.href}:${item.label}`,
        href: item.href,
        chip: item.kind,
        label: item.label,
        detail: item.detail,
        matched: words
      })),
    [index]
  );
  return (
    <SearchBox
      label="Search topics, modules, interfaces, classes, endpoints and objects"
      placeholder={data ? "workloads:compile, ManifestService, federation" : "Loading the reference…"}
      disabled={!data}
      limit={SEARCH_LIMIT}
      code
      run={run}
    />
  );
}

/* ===== Pages ============================================================== */

function Loading() {
  return <p className="ds-note">Loading the reference data…</p>;
}

function objectAnchors(): Record<string, string> {
  return Object.fromEntries(
    MODULE_DOCS.flatMap((module) => module.objects.map((obj) => [obj.name, apiRoute(module.slug, obj.anchor)]))
  );
}

function Overview({ data }: { data: ApiData | null }) {
  return (
    <article className="api-page">
      <Prose markdown={OVERVIEW} page="overview" />
      <section className="api-index" aria-labelledby="index">
        <h2 id="index">Index</h2>
        {!data ? (
          <Loading />
        ) : (
          <>
            <div className="api-index-grid">
              {MODULE_DOCS.map((module) => {
                const count = module.interfaces.reduce(
                  (total, iface) =>
                    total + (iface.service === "dagentsc" ? iface.endpoints.length : endpointCards(data, iface).length),
                  0
                );
                const endpoints = module.interfaces.reduce((total, iface) => total + iface.endpoints.length, 0);
                const commands = module.slug === "dagentsc";
                return (
                  <div className="api-index-module" key={module.slug}>
                    <h3>
                      <a href={apiRoute(module.slug)}>{module.label}</a>
                    </h3>
                    <p className="ds-note">
                      {module.interfaces.length} interfaces ·{" "}
                      {commands ? `${endpoints} commands` : `${endpoints} endpoints on ${count} cards`}
                    </p>
                    <ul>
                      {module.interfaces.map((iface) => (
                        <li key={iface.anchor}>
                          <a href={apiRoute(module.slug, iface.anchor)}>{iface.heading}</a>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
            <h3 id="all-objects">Objects</h3>
            <ul className="api-object-index">
              {MODULE_DOCS.flatMap((module) =>
                module.objects.map((obj) => (
                  <li key={obj.anchor}>
                    <a href={apiRoute(module.slug, obj.anchor)}>{obj.heading}</a>{" "}
                    <span className="ds-note">{module.label}</span>
                  </li>
                ))
              )}
            </ul>
            <p className="ds-note">
              Generated from the code: {data.reference.endpoint_count} endpoints in {data.reference.services.length}{" "}
              services and {data.reference.planner.commands.length} planner commands. How the reference is built:{" "}
              <a href={`${REPO_URL}/blob/main/docs/reference/api/README.md`}>docs/reference/api/README.md</a>.
            </p>
          </>
        )}
      </section>
    </article>
  );
}

function ModulePage({ slug, data }: { slug: string; data: ApiData | null }) {
  const module = moduleDoc(slug)!;
  const anchors = useMemo(objectAnchors, []);
  const firstStructured = module.segments.findIndex((item) => item.kind !== "prose");
  const page = useRef<HTMLElement>(null);

  // Moving to another module keeps this article, so its opening fade is
  // replayed rather than the article replaced.
  useLayoutEffect(() => {
    replayAnimations(page.current);
  }, [slug]);

  return (
    <article className="api-page" data-module={slug} ref={page}>
      <p className="ds-eyebrow">
        <a href={href("api")}>API reference</a>
      </p>
      {module.segments.map((segment, index) => {
        if (segment.kind === "prose") return <Prose key={index} markdown={segment.markdown} page={slug} />;
        if (!data) return index === firstStructured ? <Loading key={index} /> : null;
        if (segment.kind === "interface") {
          return (
            <InterfaceSection key={segment.doc.anchor} iface={segment.doc} data={data} page={slug} objectAnchors={anchors} />
          );
        }
        const spec = data.reference.objects.find(
          (entry) => entry.service === segment.doc.service && entry.class === segment.doc.classPath
        );
        return <ObjectSection key={segment.doc.anchor} doc={segment.doc} spec={spec} data={data} page={slug} objectAnchors={anchors} />;
      })}
      <p className="ds-note">
        <a href={`${REPO_URL}/blob/main/docs/reference/api/${module.file}`}>View this page's Markdown on GitHub</a>
      </p>
    </article>
  );
}
