import { useCallback, useEffect, useState } from "react";

import {
  NotRecordedError,
  getJson,
  isRecorded,
  postJson,
  recordingInfo,
  type Recording
} from "./api";

/**
 * The demo's operator view.
 *
 * Every panel shows evidence rather than a verdict: which sites were excluded
 * and why, which contributions were rejected, how each release gate decided,
 * and whether each site's audit chain still verifies. A dashboard that only
 * showed the outcome would be asking to be trusted.
 *
 * The published build replays a captured run of the real backend rather than
 * calling one, so it says which commit it was captured from and refuses to
 * answer a combination nobody recorded. See `./api`.
 *
 * Layout follows the shared design system: hairline panels rather than floating
 * cards, mono for anything the framework itself emits, and colour spent only on
 * decision states. Every state also carries its own word, so the panels still
 * read in greyscale.
 */

type Metrics = Record<string, number>;

interface Hospital {
  site_id: string;
  display_name: string;
  cohort_size: number;
  local_metrics: Metrics;
}

interface Gate {
  gate_id: string;
  metric: string;
  comparison: { kind: string; value?: number; margin?: number };
  blocking: boolean;
}

interface Overview {
  study_id: string;
  condition_id: string;
  engine: string;
  secure_aggregation_threshold: number;
  feature_contract: { contract_id: string; version: string; required_fields: string[] };
  hospitals: Hospital[];
  release_gates: Gate[];
  baseline_metrics: Metrics;
}

interface GateResult {
  gate_id: string;
  outcome: "passed" | "failed" | "not_evaluated";
  observed: number | null;
  detail: string;
}

interface RoundReport {
  round_id: string;
  phase: string;
  status: string;
  plan: {
    selected_sites: string[];
    excluded_sites: { site_id: string; reason: string }[];
    quorum_met: boolean;
    required_participants: number;
    round_digest: string;
  };
  results: {
    site_id: string;
    participation: string;
    contributed_examples: number;
    update_norm: number | null;
    metrics: Metrics;
    local_evidence_pointer: string | null;
  }[];
  readiness: {
    aggregation_permitted: boolean;
    accepted_sites: string[];
    rejected_contributions: { site_id: string; reason: string }[];
    site_weights: Record<string, number>;
  } | null;
}

interface Pilot {
  study_id: string;
  engine: string;
  notice: string;
  condition: { display_name: string; intended_use: string; limitations: string[] };
  rounds: Record<string, RoundReport | null>;
  candidate: {
    candidate_version: string;
    parent_version: string;
    contributing_sites: string[];
    contributed_examples: number;
    metrics: Metrics;
  } | null;
  release: {
    gates: {
      action: string;
      gate_results: GateResult[];
      blocking_failures: string[];
      rollback_version: string | null;
    };
    guard: { permitted: boolean; audit_id: string };
  } | null;
  baseline_metrics: Metrics;
  site_audits: Record<string, { chain_intact: boolean; records: AuditRecord[] }>;
}

interface AuditRecord {
  boundary: string;
  decision: string;
  permitted: boolean;
  granularity: string;
  filtering_score: number;
  violations: string[];
}

interface WorklistEntry {
  encounter_id?: string;
  age_band?: string;
  nihss_total?: number;
  assessment: {
    score: number;
    priority: string;
    basis: string[];
    warnings: string[];
    suppressed: boolean;
  };
}

interface GuardProbe {
  permitted: boolean;
  message: string;
  withheld_fields: string[];
  transformed_fields: string[];
  payload: Record<string, unknown>[];
  plan: {
    decision: string;
    filtering_score: number;
    violations: string[];
    obligations: string[];
    assessment: { kyu_score: number; trust: string; rationale: string[] };
    field_restrictions: { field: string; sensitivity: string; strategy: string; reason: string }[];
  };
}

function formatMetric(value: number): string {
  return Math.abs(value) >= 100 ? value.toFixed(1) : value.toFixed(4);
}

/**
 * The design system's four state tones.
 *
 * These are presentation only. The decision itself always arrives from the
 * backend, which asks the planner; the helpers below choose which token set
 * paints a decision the framework already made, and every chip also carries the
 * framework's own word so the state survives greyscale.
 */
type Tone = "permit" | "narrow" | "deny" | "inert";

function chipClass(tone: Tone): string {
  return `ds-chip ds-chip--${tone}`;
}

function releaseTone(action: string): Tone {
  if (action === "release") return "permit";
  if (action === "reject") return "deny";
  return "narrow";
}

function gateTone(outcome: GateResult["outcome"]): Tone {
  if (outcome === "passed") return "permit";
  if (outcome === "failed") return "deny";
  return "narrow";
}

function priorityTone(priority: string): Tone {
  if (priority === "urgent") return "deny";
  if (priority === "elevated") return "narrow";
  return "inert";
}

function guardTone(probe: GuardProbe): Tone {
  if (!probe.permitted) return "deny";
  return probe.plan.decision.toLowerCase() === "permit" ? "permit" : "narrow";
}

/**
 * The cohort sizes a capture actually covers.
 *
 * Read out of the recording rather than hardcoded, so the message cannot claim
 * a value the capture does not hold.
 */
function recordedCohorts(recording: Recording | null): number[] {
  if (!recording) return [];
  const sizes = new Set<number>();
  for (const entry of recording.entries) {
    const request = entry.request as { cohort_size?: number } | undefined;
    if (entry.path === "/api/v1/governance:probe" && typeof request?.cohort_size === "number") {
      sizes.add(request.cohort_size);
    }
  }
  return [...sizes].sort((a, b) => a - b);
}

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [pilot, setPilot] = useState<Pilot | null>(null);
  const [worklist, setWorklist] = useState<WorklistEntry[]>([]);
  const [selectedSite, setSelectedSite] = useState<string>("");
  const [probe, setProbe] = useState<GuardProbe | null>(null);
  const [probeVerified, setProbeVerified] = useState(true);
  const [probeGranularity, setProbeGranularity] = useState("row");
  // The classification's minimum cohort is 20, so 25 starts above the floor:
  // the trust and granularity levers are visible before the floor masks them.
  const [probeCohort, setProbeCohort] = useState(25);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A replayed build can be asked for a lever combination the capture never
  // covered. That is not an error in the demo, and it must not be reported as
  // one — nor answered with a made-up verdict.
  const [notRecorded, setNotRecorded] = useState<NotRecordedError | null>(null);
  const [recording, setRecording] = useState<Recording | null>(null);

  useEffect(() => {
    recordingInfo()
      .then(setRecording)
      .catch((exc) => setError(String(exc)));
  }, []);

  useEffect(() => {
    getJson<Overview>("/api/v1/overview")
      .then((data) => {
        setOverview(data);
        setSelectedSite(data.hospitals[0]?.site_id ?? "");
      })
      .catch((exc) => setError(String(exc)));
  }, []);

  useEffect(() => {
    if (!selectedSite) return;
    getJson<{ worklist: WorklistEntry[] }>(`/api/v1/hospitals/${selectedSite}/worklist?limit=12`)
      .then((data) => setWorklist(data.worklist))
      .catch((exc) => setError(String(exc)));
  }, [selectedSite]);

  const runPilot = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setPilot(await postJson<Pilot>("/api/v1/pilot:run", { round_prefix: "ui" }));
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  }, []);

  const runProbe = useCallback(async () => {
    setError(null);
    setNotRecorded(null);
    try {
      setProbe(
        await postJson<GuardProbe>("/api/v1/governance:probe", {
          verified: probeVerified,
          granularity: probeGranularity,
          cohort_size: probeCohort,
          boundary: "before_read"
        })
      );
    } catch (exc) {
      if (exc instanceof NotRecordedError) {
        setProbe(null);
        setNotRecorded(exc);
      } else {
        setError(String(exc));
      }
    }
  }, [probeVerified, probeGranularity, probeCohort]);

  return (
    <div className="shell ds-shell">
      <header className="masthead">
        <p className="ds-eyebrow">Dagents framework · healthcare demo</p>
        <h1>Stroke triage, governed across three hospitals</h1>
        <p className="lede ds-measure">
          A demo app built on the Dagents framework. Patient data is entirely synthetic and the
          scoring rule is a transparent illustration, not a validated triage model. Nothing shown
          here is clinical advice.
        </p>
      </header>

      {isRecorded && (
        <div className="banner recorded">
          <strong>Recorded run.</strong> This published page has no backend. Every verdict,
          strategy and denial below is one the OCaml planner really returned, captured from a
          live run of this demo
          {recording ? (
            <>
              {" "}
              at commit <span className="mono">{recording.commit.slice(0, 10)}</span> on{" "}
              {recording.captured_at}
            </>
          ) : null}
          . The levers work because every combination was captured; nothing here is generated
          by the page. To drive the real backend, run it locally — see the README.
        </div>
      )}

      {error && <div className="banner error">{error}</div>}

      <main className="panels">
        {overview && (
          <section className="panel ds-panel" data-panel="consortium">
            <div className="ds-panel-head">
              <div className="head-titles">
                <p className="ds-eyebrow">consortium · {overview.condition_id}</p>
                <h2>Three hospitals, one feature contract</h2>
              </div>
              <span className="head-meta ds-mono">{overview.hospitals.length} sites</span>
            </div>
            <div className="ds-panel-body">
              <dl className="ds-facts">
                <div>
                  <dt>Study</dt>
                  <dd>{overview.study_id}</dd>
                </div>
                <div>
                  <dt>Feature contract</dt>
                  <dd>{overview.feature_contract.version}</dd>
                </div>
                <div>
                  <dt>Federated engine</dt>
                  <dd>{overview.engine}</dd>
                </div>
                <div>
                  <dt>Secure-aggregation threshold</dt>
                  <dd>{overview.secure_aggregation_threshold} sites</dd>
                </div>
              </dl>
              <div className="ds-table-scroll">
                <table className="ds-table">
                  <thead>
                    <tr>
                      <th>Hospital</th>
                      <th className="num">Cohort</th>
                      <th className="num">AUC</th>
                      <th className="num">Sensitivity</th>
                      <th className="num">Subgroup gap</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.hospitals.map((hospital) => (
                      <tr key={hospital.site_id}>
                        <td>{hospital.display_name}</td>
                        <td className="num">{hospital.cohort_size}</td>
                        <td className="num">{hospital.local_metrics.auc?.toFixed(3) ?? "—"}</td>
                        <td className="num">
                          {hospital.local_metrics.sensitivity?.toFixed(3) ?? "—"}
                        </td>
                        <td className="num">
                          {hospital.local_metrics.subgroup_auc_gap?.toFixed(3) ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="note ds-note">
                The sites differ on purpose. A consortium where every hospital looked identical
                would never exercise the drift checks, the cohort floors, or the fairness gate.
              </p>
            </div>
          </section>
        )}

        <section className="panel guard ds-panel" data-panel="guard">
          <div className="ds-panel-head">
            <div className="head-titles">
              <p className="ds-eyebrow">guard boundary · before_read</p>
              <h2>Ethical Guard</h2>
            </div>
            <span className="head-meta ds-mono">three levers · one planner</span>
          </div>
          <div className="ds-panel-body">
            <p className="note ds-note">
              Three levers, each a real one. Unticking <em>verified</em> drops trust and hardens
              the strategy — at row grain, generalizing a score becomes redacting it. Widening the
              granularity coarsens it — a column or a table can only come back as an aggregate.
              And a <em>cohort</em> below the classification&rsquo;s floor of 20 denies the request
              outright, whoever is asking. None of that is a branch in this app&rsquo;s code; every
              decision is a lookup in the typed planner.
            </p>

            <div className="guard-grid">
              <div className="levers ds-inset">
                <p className="ds-eyebrow">request</p>
                <label className="ds-check lever-check">
                  <input
                    type="checkbox"
                    checked={probeVerified}
                    onChange={(event) => setProbeVerified(event.target.checked)}
                  />
                  requester verified
                </label>
                <label className="ds-field">
                  <span>granularity</span>
                  <select
                    className="ds-input"
                    value={probeGranularity}
                    onChange={(event) => setProbeGranularity(event.target.value)}
                  >
                    <option value="cell">cell</option>
                    <option value="row">row</option>
                    <option value="column">column</option>
                    <option value="table">table</option>
                    <option value="model_update">model update</option>
                  </select>
                </label>
                <label className="ds-field">
                  <span>cohort</span>
                  <input
                    className="ds-input"
                    type="number"
                    min={0}
                    max={999}
                    value={probeCohort}
                    onChange={(event) => setProbeCohort(Number(event.target.value))}
                  />
                </label>
                <button className="ds-btn ds-btn--primary lever-go" onClick={runProbe}>
                  Ask the guard
                </button>
              </div>

              <div className="outcome">
                {notRecorded && (
                  <div className="banner recorded">
                    <strong>Not captured.</strong> {notRecorded.message} Rather than show you a
                    verdict nobody computed, this page is telling you so. The captured cohort sizes
                    are <span className="mono">{recordedCohorts(recording).join(", ")}</span>; the
                    floor for this classification is 20, so 19 and 20 are the interesting pair.
                  </div>
                )}

                {probe ? (
                  <>
                    <div className={`verdict ds-verdict ds-verdict--${guardTone(probe)}`}>
                      <strong>{probe.plan.decision.toUpperCase()}</strong>
                      <span className="verdict-metrics">
                        <span>filtering score {probe.plan.filtering_score.toFixed(2)}</span>
                        <span>
                          trust {probe.plan.assessment.trust} (
                          {probe.plan.assessment.kyu_score.toFixed(2)})
                        </span>
                      </span>
                    </div>

                    {probe.message && <p className="guard-message">{probe.message}</p>}

                    {/* A denial the planner never reached carries no per-field
                        plan, so there is nothing to tabulate. Showing an empty
                        header would read as a rendering fault at exactly the
                        moment the reader needs the refusal to be clear. */}
                    {probe.plan.field_restrictions.length > 0 ? (
                      <div className="ds-table-scroll">
                        <table className="ds-table restrictions">
                          <thead>
                            <tr>
                              <th>Field</th>
                              <th>Sensitivity</th>
                              <th>Reason</th>
                              <th className="strategy-col">Strategy</th>
                            </tr>
                          </thead>
                          <tbody>
                            {probe.plan.field_restrictions.map((restriction) => (
                              <tr key={restriction.field}>
                                <td className="mono">{restriction.field}</td>
                                <td>{restriction.sensitivity}</td>
                                <td className="why">{restriction.reason || "—"}</td>
                                <td className="mono strategy">{restriction.strategy}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <p className="ds-note">
                        No per-field plan came back with this decision, so there is nothing to
                        show here. The request was refused before it reached one.
                      </p>
                    )}

                    {probe.plan.violations.length > 0 && (
                      <ul className="reasons ds-reasons">
                        {probe.plan.violations.map((violation) => (
                          <li key={violation}>{violation}</li>
                        ))}
                      </ul>
                    )}

                    <details className="ds-details">
                      <summary>What the guard returned ({probe.payload.length} rows)</summary>
                      <pre className="ds-code">{JSON.stringify(probe.payload.slice(0, 5), null, 2)}</pre>
                    </details>
                    <details className="ds-details">
                      <summary>Obligations the guard must discharge</summary>
                      <ul className="reasons ds-reasons">
                        {probe.plan.obligations.map((obligation) => (
                          <li key={obligation}>{obligation}</li>
                        ))}
                      </ul>
                    </details>
                  </>
                ) : (
                  !notRecorded && (
                    <p className="outcome-idle ds-note">
                      Nothing asked yet. Set the levers and ask the guard: the verdict, and the
                      strategy for every field, come back from the planner.
                    </p>
                  )
                )}
              </div>
            </div>
          </div>
        </section>

        <section className="panel ds-panel" data-panel="pilot">
          <div className="ds-panel-head">
            <div className="head-titles">
              <p className="ds-eyebrow">round phases</p>
              <h2>Federated pilot</h2>
            </div>
            <button className="ds-btn ds-btn--primary" onClick={runPilot} disabled={busy}>
              {busy ? "Running…" : "Run governed pilot"}
            </button>
          </div>
          <div className="ds-panel-body">
            <p className="note ds-note">
              Analytics, then baseline evaluation, then training, then cross-site validation of the
              candidate, then the release gates. Aggregation produces a candidate, never a release.
            </p>

            {pilot && (
              <>
                <dl className="ds-facts pilot-summary">
                  <div>
                    <dt>Phases run</dt>
                    <dd>{Object.keys(pilot.rounds).length}</dd>
                  </div>
                  <div>
                    <dt>Candidate</dt>
                    <dd>{pilot.candidate ? pilot.candidate.candidate_version : "none"}</dd>
                  </div>
                  <div>
                    <dt>Contributing sites</dt>
                    <dd>{pilot.candidate ? pilot.candidate.contributing_sites.length : 0}</dd>
                  </div>
                  <div>
                    <dt>Release</dt>
                    <dd>
                      {pilot.release ? (
                        <span className={chipClass(releaseTone(pilot.release.gates.action))}>
                          {pilot.release.gates.action.replace("_", " ")}
                        </span>
                      ) : (
                        "—"
                      )}
                    </dd>
                  </div>
                </dl>

                <div className="phases">
                  {Object.entries(pilot.rounds).map(([name, round]) =>
                    round ? (
                      <div key={name} className="round phase">
                        <div className="round-head">
                          <h3>{name}</h3>
                          <span className="digest mono">{round.plan.round_digest}</span>
                        </div>
                        <div className="round-state ds-row">
                          <span
                            className={chipClass(round.plan.quorum_met ? "permit" : "deny")}
                          >
                            {round.plan.quorum_met ? "quorum met" : "quorum not met"}
                          </span>
                          <span
                            className={chipClass(
                              round.readiness?.aggregation_permitted ? "permit" : "narrow"
                            )}
                          >
                            {round.readiness?.aggregation_permitted
                              ? "aggregation permitted"
                              : "aggregation blocked"}
                          </span>
                          <span className="round-count ds-mono">
                            {round.plan.selected_sites.length} of{" "}
                            {round.plan.required_participants} required sites
                          </span>
                        </div>
                        {round.plan.excluded_sites.length > 0 && (
                          <ul className="reasons ds-reasons">
                            {round.plan.excluded_sites.map((exclusion) => (
                              <li key={exclusion.site_id}>
                                <strong>{exclusion.site_id}</strong> excluded — {exclusion.reason}
                              </li>
                            ))}
                          </ul>
                        )}
                        {round.readiness && round.readiness.rejected_contributions.length > 0 && (
                          <ul className="reasons ds-reasons">
                            {round.readiness.rejected_contributions.map((rejection) => (
                              <li key={rejection.site_id}>
                                <strong>{rejection.site_id}</strong> rejected — {rejection.reason}
                              </li>
                            ))}
                          </ul>
                        )}
                        <details className="ds-details">
                          <summary>What each site returned ({round.results.length})</summary>
                          <div className="ds-table-scroll">
                            <table className="ds-table">
                              <thead>
                                <tr>
                                  <th>Site</th>
                                  <th>Participation</th>
                                  <th className="num">Examples</th>
                                  <th className="num">Bounded norm</th>
                                  <th>Evidence</th>
                                </tr>
                              </thead>
                              <tbody>
                                {round.results.map((result) => (
                                  <tr key={result.site_id}>
                                    <td className="mono">{result.site_id}</td>
                                    <td>{result.participation}</td>
                                    <td className="num">{result.contributed_examples}</td>
                                    <td className="num">{result.update_norm?.toFixed(4) ?? "—"}</td>
                                    <td className="mono wrap">
                                      {result.local_evidence_pointer ?? "—"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                          <p className="note ds-note">
                            No column here is patient-level. Counts, bounded norms, approved
                            metrics and a pointer that stays addressed to the site — that is the
                            whole egress contract.
                          </p>
                        </details>
                      </div>
                    ) : null
                  )}
                </div>

                {pilot.candidate && pilot.release && (
                  <div className="round candidate">
                    <div className="round-head">
                      <h3>candidate</h3>
                      <span className="digest mono">{pilot.candidate.candidate_version}</span>
                    </div>

                    <div className={`verdict ds-verdict ds-verdict--${releaseTone(pilot.release.gates.action)}`}>
                      <span className="verdict-key">release decision</span>
                      <strong>{pilot.release.gates.action.replace("_", " ")}</strong>
                      {pilot.release.gates.rollback_version &&
                      pilot.release.gates.action !== "release" ? (
                        <span className="verdict-note">
                          keeping {pilot.release.gates.rollback_version}
                        </span>
                      ) : null}
                    </div>

                    <div className="ds-table-scroll">
                      <table className="ds-table">
                        <thead>
                          <tr>
                            <th>Metric</th>
                            <th className="num">Candidate</th>
                            <th className="num">Baseline</th>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.keys(pilot.candidate.metrics)
                            .sort()
                            .map((key) => (
                              <tr key={key}>
                                <td className="mono">{key}</td>
                                <td className="num">{formatMetric(pilot.candidate!.metrics[key])}</td>
                                <td className="num">
                                  {pilot.baseline_metrics[key] !== undefined
                                    ? formatMetric(pilot.baseline_metrics[key])
                                    : "—"}
                                </td>
                              </tr>
                            ))}
                        </tbody>
                      </table>
                    </div>

                    <h4>Release gates</h4>
                    <ul className="gates">
                      {pilot.release.gates.gate_results.map((gate) => (
                        <li key={gate.gate_id} className={gate.outcome}>
                          <span className={`tag ${chipClass(gateTone(gate.outcome))}`}>
                            {gate.outcome.replace("_", " ")}
                          </span>
                          <strong>{gate.gate_id}</strong>
                          <span className="detail">{gate.detail}</span>
                        </li>
                      ))}
                    </ul>
                    {pilot.release.gates.blocking_failures.length > 0 && (
                      <p className="note ds-note">
                        The candidate beats its baseline on AUC and is still not releasable. That
                        is the framework doing its job, not failing at it.
                      </p>
                    )}
                  </div>
                )}

                <div className="round audit">
                  <div className="round-head">
                    <h3>audit</h3>
                    <span className="digest mono">
                      {Object.keys(pilot.site_audits).length} site chains
                    </span>
                  </div>
                  <div className="ds-table-scroll">
                    <table className="ds-table">
                      <thead>
                        <tr>
                          <th>Site</th>
                          <th>Chain</th>
                          <th className="num">Decisions</th>
                          <th>Boundaries seen</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(pilot.site_audits).map(([siteId, audit]) => (
                          <tr key={siteId}>
                            <td className="mono">{siteId}</td>
                            <td>
                              <span className={chipClass(audit.chain_intact ? "permit" : "deny")}>
                                {audit.chain_intact ? "intact" : "BROKEN"}
                              </span>
                            </td>
                            <td className="num">{audit.records.length}</td>
                            <td className="mono wrap">
                              {Array.from(
                                new Set(audit.records.map((record) => record.boundary))
                              ).join(", ")}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </div>
        </section>

        <section className="panel ds-panel" data-panel="worklist">
          <div className="ds-panel-head">
            <div className="head-titles">
              <p className="ds-eyebrow">site view · stays inside the hospital</p>
              <h2>Local worklist</h2>
            </div>
            <label className="ds-field head-select">
              <span>site</span>
              <select
                className="ds-input"
                value={selectedSite}
                onChange={(event) => setSelectedSite(event.target.value)}
              >
                {overview?.hospitals.map((hospital) => (
                  <option key={hospital.site_id} value={hospital.site_id}>
                    {hospital.display_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="ds-panel-body">
            <p className="ds-eyebrow">queue · {worklist.length} cases · re-ordered, never filtered</p>
            <p className="note ds-note">
              This view never leaves the hospital. Cases are re-ordered so an urgent one reaches a
              specialist sooner; nothing is removed from the queue, including cases that could not
              be scored.
            </p>
            <div className="ds-table-scroll">
              <table className="ds-table worklist">
                <thead>
                  <tr>
                    <th>Encounter</th>
                    <th>Age band</th>
                    <th className="num">NIHSS</th>
                    <th>Priority</th>
                    <th className="num">Score</th>
                    <th className="basis">Basis</th>
                  </tr>
                </thead>
                <tbody>
                  {worklist.map((entry, index) => (
                    <tr key={entry.encounter_id ?? index} className={entry.assessment.priority}>
                      <td className="mono">{entry.encounter_id ?? "—"}</td>
                      <td>{entry.age_band ?? "—"}</td>
                      <td className="num">{entry.nihss_total ?? "—"}</td>
                      <td>
                        <span
                          className={`tag ${entry.assessment.priority} ${chipClass(
                            priorityTone(entry.assessment.priority)
                          )}`}
                        >
                          {entry.assessment.suppressed ? "not assessed" : entry.assessment.priority}
                        </span>
                      </td>
                      <td className="num">{entry.assessment.score.toFixed(3)}</td>
                      <td className="basis">{entry.assessment.basis.join("; ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </main>

      <footer className="foot">
        <p>
          Dagents healthcare demo. Synthetic data only. Not a medical device, not clinically
          validated, and not a substitute for clinical judgement.
        </p>
      </footer>
    </div>
  );
}
