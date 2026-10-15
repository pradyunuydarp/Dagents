import { Fragment, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { words } from "./fuzzy";

/** One result of a site search. */
export interface SearchResult {
  key: string;
  href: string;
  /** The kind or section, shown as a chip. */
  chip: string;
  label: string;
  detail: string;
  /** A line of text around the match, if the result has body text. */
  snippet?: string;
  /** The words that matched, highlighted in the label and the snippet. */
  matched: Set<string>;
}

/** Marks the words of `text` that matched the query. */
function highlight(text: string, matched: Set<string>): ReactNode {
  if (!matched.size) return text;
  return text.split(/([\p{L}\p{N}]+)/u).map((part, i) =>
    i % 2 && words(part).some((word) => matched.has(word)) ? (
      <mark key={i}>{part}</mark>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    )
  );
}

/**
 * A search field with a list of results under it. Arrow keys move through the
 * results, Enter opens one, Escape clears the field, and `/` anywhere on the
 * page puts the cursor in the field.
 */
export function SearchBox({
  label,
  placeholder,
  disabled = false,
  limit,
  code = false,
  run
}: {
  label: string;
  placeholder: string;
  disabled?: boolean;
  /** The most results `run` returns; a full list shows "N+ matches". */
  limit: number;
  /** Show labels as code, for names such as `GET /api/v1/health`. */
  code?: boolean;
  run: (query: string) => SearchResult[];
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();
  const results = useMemo(() => (query.trim() ? run(query) : []), [run, query]);

  useEffect(() => setActive(0), [results]);

  useEffect(() => {
    list.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [active]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable]")) return;
      event.preventDefault();
      input.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const open = (result: SearchResult) => {
    window.location.hash = result.href.slice(1);
    setQuery("");
  };

  return (
    <div className={code ? "site-search site-search--code" : "site-search"} role="search">
      <label className="ds-field">
        <span>{label}</span>
        <input
          ref={input}
          className="ds-input"
          type="search"
          value={query}
          placeholder={placeholder}
          disabled={disabled}
          role="combobox"
          aria-expanded={results.length > 0}
          aria-controls={id}
          aria-activedescendant={results.length ? `${id}-${active}` : undefined}
          aria-autocomplete="list"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && results.length) {
              event.preventDefault();
              setActive((active + 1) % results.length);
            } else if (event.key === "ArrowUp" && results.length) {
              event.preventDefault();
              setActive((active - 1 + results.length) % results.length);
            } else if (event.key === "Enter" && results[active]) {
              open(results[active]);
            } else if (event.key === "Escape") {
              setQuery("");
            }
          }}
        />
      </label>
      {query.trim() && (
        <div className="site-results">
          <p className="ds-note site-result-count" aria-live="polite">
            {results.length
              ? `${results.length}${results.length === limit ? "+" : ""} ${results.length === 1 ? "match" : "matches"}`
              : `Nothing matches "${query.trim()}".`}
          </p>
          <ul id={id} role="listbox" ref={list}>
            {results.map((result, i) => (
              <li key={result.key} id={`${id}-${i}`} role="option" aria-selected={i === active}>
                <a
                  href={result.href}
                  className={i === active ? "is-active" : undefined}
                  tabIndex={-1}
                  onClick={() => setQuery("")}
                  onMouseMove={() => i !== active && setActive(i)}
                >
                  <span className="ds-chip">{result.chip}</span>{" "}
                  <span className="site-result-label">{highlight(result.label, result.matched)}</span>
                  <span className="ds-note site-result-detail">{result.detail}</span>
                  {result.snippet && (
                    <span className="site-result-snippet">{highlight(result.snippet, result.matched)}</span>
                  )}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
