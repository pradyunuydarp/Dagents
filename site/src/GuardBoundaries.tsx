import { useState } from "react";

/**
 * The four places the Ethical Guard stands.
 *
 * These are `Dagents_common_ir.guard_boundary` — `BeforeRead`, `BeforeTrain`,
 * `BeforeSend`, `BeforeRelease` — not a narrative invention. The point the
 * drawing has to make is that they are arranged along one path, so a request
 * cannot reach the end by going around: a guard only at the API edge is a
 * warning label.
 *
 * Built from HTML rather than SVG because it is a row of gates with hover and
 * focus states that reflows to a column on a phone; SVG would buy nothing and
 * cost the responsiveness.
 */

interface Boundary {
  id: string;
  owner: "LMA" | "GMA";
  stage: string;
  stops: string;
}

const BOUNDARIES: Boundary[] = [
  {
    id: "before_read",
    owner: "LMA",
    stage: "local data is read",
    stops: "A request that should never see these records is refused before any are loaded, so there is nothing to leak."
  },
  {
    id: "before_train",
    owner: "LMA",
    stage: "training touches it",
    stops: "A model cannot learn from a field the requester was not granted. The runner is handed the guarded payload, not the raw records."
  },
  {
    id: "before_send",
    owner: "LMA",
    stage: "anything leaves the site",
    stops: "Counts, bounded norms and approved metrics leave; patient-level columns do not. This is the egress contract."
  },
  {
    id: "before_release",
    owner: "GMA",
    stage: "a candidate is released",
    stops: "Aggregation produced a candidate. Releasing it is a separate decision, and a gate whose metric is missing blocks rather than passing."
  }
];

export function GuardBoundaries() {
  const [active, setActive] = useState<string>(BOUNDARIES[0].id);
  const current = BOUNDARIES.find((boundary) => boundary.id === active) ?? BOUNDARIES[0];

  return (
    <figure className="boundaries">
      <ol className="boundary-track">
        {BOUNDARIES.map((boundary) => (
          <li key={boundary.id}>
            <button
              type="button"
              className={boundary.id === active ? "is-active" : undefined}
              aria-pressed={boundary.id === active}
              onClick={() => setActive(boundary.id)}
              onMouseEnter={() => setActive(boundary.id)}
              onFocus={() => setActive(boundary.id)}
            >
              <span className="ds-chip">{boundary.owner}</span>
              <span className="boundary-id">{boundary.id}</span>
              <span className="boundary-stage">before {boundary.stage}</span>
            </button>
          </li>
        ))}
      </ol>
      <figcaption>
        <p className="ds-eyebrow">{current.id}</p>
        <p>{current.stops}</p>
      </figcaption>
    </figure>
  );
}
