import { useState } from "react";

/**
 * What a federated round actually does, phase by phase.
 *
 * This describes the framework's design; it does not simulate a run. Every
 * decision a real round makes — eligibility, quorum, aggregation readiness, the
 * release gates — is computed by the OCaml federation compiler, and nothing
 * here recomputes one. The stepper exists because the sequence is the argument:
 * the last phase is where a candidate that beats its baseline can still be
 * refused, and a static list lets a reader skim past that.
 *
 * The phases are the ones the healthcare demo runs.
 */

interface Phase {
  key: string;
  name: string;
  produces: string;
  refuses: string;
}

const PHASES: Phase[] = [
  {
    key: "analytics",
    name: "Analytics",
    produces: "Each site profiles its own cohort and returns approved summaries.",
    refuses: "No site sends rows. A site whose cohort is below the classification's floor contributes nothing rather than a thin summary."
  },
  {
    key: "baseline",
    name: "Baseline evaluation",
    produces: "The incumbent model is scored at each site, so there is something to beat.",
    refuses: "A candidate with no baseline to compare against cannot later be argued into a release."
  },
  {
    key: "training",
    name: "Training",
    produces: "Sites train locally and return bounded updates.",
    refuses: "The runner is handed the guarded payload, not the raw records — the code running the round is the coordinator's, so what it observes is what the coordinator observes. That costs accuracy, and the demo quantifies it."
  },
  {
    key: "validation",
    name: "Cross-site validation",
    produces: "The aggregate candidate is scored at sites, including ones that did not train it.",
    refuses: "A contribution that fails its checks is rejected with a reason, and the rejection is reported rather than averaged away."
  },
  {
    key: "gates",
    name: "Release gates",
    produces: "Each gate reports passed, failed, or not evaluated, and the decision follows from them.",
    refuses: "Aggregation produced a candidate, never a release. A gate whose metric is absent blocks. A candidate can beat its baseline on AUC and still be refused because a fairness gate failed — that is the framework working, not failing."
  }
];

export function RoundLifecycle() {
  const [index, setIndex] = useState(0);
  const phase = PHASES[index];

  return (
    <div className="lifecycle">
      <ol className="lifecycle-track">
        {PHASES.map((item, position) => (
          <li key={item.key}>
            <button
              type="button"
              className={position === index ? "is-active" : position < index ? "is-past" : undefined}
              aria-current={position === index ? "step" : undefined}
              onClick={() => setIndex(position)}
            >
              <span className="lifecycle-index">{String(position + 1).padStart(2, "0")}</span>
              <span className="lifecycle-name">{item.name}</span>
            </button>
          </li>
        ))}
      </ol>

      <div className="lifecycle-detail">
        <div>
          <p className="ds-eyebrow">produces</p>
          <p>{phase.produces}</p>
        </div>
        <div>
          <p className="ds-eyebrow">refuses</p>
          <p>{phase.refuses}</p>
        </div>
      </div>

      <div className="lifecycle-controls">
        <button
          type="button"
          className="ds-btn"
          onClick={() => setIndex((value) => Math.max(0, value - 1))}
          disabled={index === 0}
        >
          Previous phase
        </button>
        <button
          type="button"
          className="ds-btn"
          onClick={() => setIndex((value) => Math.min(PHASES.length - 1, value + 1))}
          disabled={index === PHASES.length - 1}
        >
          Next phase
        </button>
        <span className="ds-mono lifecycle-count">
          {index + 1} / {PHASES.length}
        </span>
      </div>
    </div>
  );
}
