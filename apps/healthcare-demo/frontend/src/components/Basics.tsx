import { createContext, useContext, type ReactNode } from "react";

import { userMessage } from "../api";
import type { Tone } from "../format";

/**
 * Small building blocks shared by the pages.
 *
 * A chip always carries its word, so a state never depends on colour alone.
 */

export function Chip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`ds-chip ds-chip--${tone}`}>{children}</span>;
}

/** A panel with a heading row. `hook` is the `data-panel` name the smoke test uses. */
export function Panel({
  hook,
  eyebrow,
  title,
  action,
  className,
  children
}: {
  hook: string;
  eyebrow?: string;
  title: string;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`panel ds-panel${className ? ` ${className}` : ""}`} data-panel={hook}>
      <div className="ds-panel-head">
        <div className="head-titles">
          {eyebrow && <p className="ds-eyebrow">{eyebrow}</p>}
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      <div className="ds-panel-body">{children}</div>
    </section>
  );
}

/**
 * The error the page already shows at the top, if any.
 *
 * When the whole API is down, every panel fails with the same message. The
 * shell shows it once at the top, and a panel with the same error says only
 * that it is not loaded, so the page does not repeat one message six times.
 */
export const ShownError = createContext<string | null>(null);

/**
 * An error, in plain words, where the missing content would have been.
 *
 * `notLoaded` replaces the default sentence shown when the top of the page
 * already gives the same message.
 */
export function ErrorNote({ error, notLoaded }: { error: unknown; notLoaded?: string }) {
  const shown = useContext(ShownError);
  if (!error) return null;
  const message = userMessage(error);
  if (message === shown) {
    return (
      <p className="ds-note not-loaded">{notLoaded ?? "Not loaded. The message at the top of the page says why."}</p>
    );
  }
  return (
    <p className="banner error" role="alert">
      {message}
    </p>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="ds-note loading">Loading {what}…</p>;
}

/** Text with bare URLs turned into links, for the dataset notice. */
export function Linked({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/[^\s)]+)/g);
  return (
    <>
      {parts.map((part, index) =>
        /^https?:\/\//.test(part) ? (
          <a key={index} href={part} rel="noreferrer">
            {part}
          </a>
        ) : (
          <span key={index}>{part}</span>
        )
      )}
    </>
  );
}
