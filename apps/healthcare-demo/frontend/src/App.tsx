import { useCallback, useEffect, useState } from "react";

/**
 * The demo's operator view.
 *
 * Every panel shows evidence rather than a verdict: which sites were excluded
 * and why, which contributions were rejected, how each release gate decided,
 * and whether each site's audit chain still verifies. A dashboard that only
 * showed the outcome would be asking to be trusted.
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

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

function formatMetric(value: number): string {
  return Math.abs(value) >= 100 ? value.toFixed(1) : value.toFixed(4);
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
      setError(String(exc));
    }
  }, [probeVerified, probeGranularity, probeCohort]);

  return (
    <div className="page">
      <header>
        <h1>Stroke triage, governed across three hospitals</h1>
        <p className="lede">
          A demo app built on the Dagents framework. Patient data is entirely synthetic and the
          scoring rule is a transparent illustration, not a validated triage model. Nothing shown
          here is clinical advice.
        </p>
      </header>

      {error && <div className="banner error">{error}</div>}

      {overview && (
        <section className="card">
          <h2>Consortium</h2>
          <dl className="facts">
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
          <table>
            <thead>
              <tr>
                <th>Hospital</th>
                <th>Cohort</th>
                <th>AUC</th>
                <th>Sensitivity</th>
                <th>Subgroup gap</th>
              </tr>
            </thead>
            <tbody>
              {overview.hospitals.map((hospital) => (
                <tr key={hospital.site_id}>
                  <td>{hospital.display_name}</td>
                  <td>{hospital.cohort_size}</td>
                  <td>{hospital.local_metrics.auc?.toFixed(3) ?? "—"}</td>
                  <td>{hospital.local_metrics.sensitivity?.toFixed(3) ?? "—"}</td>
                  <td>{hospital.local_metrics.subgroup_auc_gap?.toFixed(3) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="note">
            The sites differ on purpose. A consortium where every hospital looked identical would
            never exercise the drift checks, the cohort floors, or the fairness gate.
          </p>
        </section>
      )}

      <section className="card">
        <div className="card-head">
          <h2>Federated pilot</h2>
          <button onClick={runPilot} disabled={busy}>
            {busy ? "Running…" : "Run governed pilot"}
          </button>
        </div>
        <p className="note">
          Analytics, then baseline evaluation, then training, then cross-site validation of the
          candidate, then the release gates. Aggregation produces a candidate, never a release.
        </p>

        {pilot && (
          <>
            {Object.entries(pilot.rounds).map(([name, round]) =>
              round ? (
                <div key={name} className="round">
                  <h3>
                    {name} <span className="digest">{round.plan.round_digest}</span>
                  </h3>
                  <p>
                    quorum {String(round.plan.quorum_met)} · {round.plan.selected_sites.length}/
                    {round.plan.required_participants} required ·{" "}
                    {round.readiness?.aggregation_permitted
                      ? "aggregation permitted"
                      : "aggregation blocked"}
                  </p>
                  {round.plan.excluded_sites.length > 0 && (
                    <ul className="reasons">
                      {round.plan.excluded_sites.map((exclusion) => (
                        <li key={exclusion.site_id}>
                          <strong>{exclusion.site_id}</strong> excluded — {exclusion.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                  {round.readiness && round.readiness.rejected_contributions.length > 0 && (
                    <ul className="reasons">
                      {round.readiness.rejected_contributions.map((rejection) => (
                        <li key={rejection.site_id}>
                          <strong>{rejection.site_id}</strong> rejected — {rejection.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                  <details>
                    <summary>What each site returned ({round.results.length})</summary>
                    <table>
                      <thead>
                        <tr>
                          <th>Site</th>
                          <th>Participation</th>
                          <th>Examples</th>
                          <th>Bounded norm</th>
                          <th>Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {round.results.map((result) => (
                          <tr key={result.site_id}>
                            <td>{result.site_id}</td>
                            <td>{result.participation}</td>
                            <td>{result.contributed_examples}</td>
                            <td>{result.update_norm?.toFixed(4) ?? "—"}</td>
                            <td className="mono">{result.local_evidence_pointer ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="note">
                      No column here is patient-level. Counts, bounded norms, approved metrics and a
                      pointer that stays addressed to the site — that is the whole egress contract.
                    </p>
                  </details>
                </div>
              ) : null
            )}

            {pilot.candidate && pilot.release && (
              <div className="round">
                <h3>Candidate {pilot.candidate.candidate_version}</h3>
                <table>
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Candidate</th>
                      <th>Baseline</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.keys(pilot.candidate.metrics)
                      .sort()
                      .map((key) => (
                        <tr key={key}>
                          <td>{key}</td>
                          <td>{formatMetric(pilot.candidate!.metrics[key])}</td>
                          <td>
                            {pilot.baseline_metrics[key] !== undefined
                              ? formatMetric(pilot.baseline_metrics[key])
                              : "—"}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>

                <h4>Release gates</h4>
                <ul className="gates">
                  {pilot.release.gates.gate_results.map((gate) => (
                    <li key={gate.gate_id} className={gate.outcome}>
                      <span className="tag">{gate.outcome.replace("_", " ")}</span>
                      <strong>{gate.gate_id}</strong>
                      <span className="detail">{gate.detail}</span>
                    </li>
                  ))}
                </ul>
                <p className={`verdict ${pilot.release.gates.action}`}>
                  Decision: {pilot.release.gates.action.replace("_", " ")}
                  {pilot.release.gates.rollback_version &&
                    pilot.release.gates.action !== "release" &&
                    ` · keeping ${pilot.release.gates.rollback_version}`}
                </p>
                {pilot.release.gates.blocking_failures.length > 0 && (
                  <p className="note">
                    The candidate beats its baseline on AUC and is still not releasable. That is the
                    framework doing its job, not failing at it.
                  </p>
                )}
              </div>
            )}

            <div className="round">
              <h3>Audit</h3>
              <table>
                <thead>
                  <tr>
                    <th>Site</th>
                    <th>Chain</th>
                    <th>Decisions</th>
                    <th>Boundaries seen</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(pilot.site_audits).map(([siteId, audit]) => (
                    <tr key={siteId}>
                      <td>{siteId}</td>
                      <td>{audit.chain_intact ? "intact" : "BROKEN"}</td>
                      <td>{audit.records.length}</td>
                      <td>
                        {Array.from(new Set(audit.records.map((record) => record.boundary))).join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Ethical Guard</h2>
          <div className="controls">
            <label>
              <input
                type="checkbox"
                checked={probeVerified}
                onChange={(event) => setProbeVerified(event.target.checked)}
              />
              requester verified
            </label>
            <select value={probeGranularity} onChange={(event) => setProbeGranularity(event.target.value)}>
              <option value="cell">cell</option>
              <option value="row">row</option>
              <option value="column">column</option>
              <option value="table">table</option>
              <option value="model_update">model update</option>
            </select>
            <label>
              cohort
              <input
                type="number"
                min={0}
                max={999}
                value={probeCohort}
                onChange={(event) => setProbeCohort(Number(event.target.value))}
              />
            </label>
            <button onClick={runProbe}>Ask the guard</button>
          </div>
        </div>
        <p className="note">
          Three levers, each a real one. Unticking <em>verified</em> drops trust and hardens the
          strategy — at row grain, generalizing a score becomes redacting it. Widening the
          granularity coarsens it — a column or a table can only come back as an aggregate. And a{" "}
          <em>cohort</em> below the classification&rsquo;s floor of 20 denies the request outright,
          whoever is asking. None of that is a branch in this app&rsquo;s code; every decision is a
          lookup in the typed planner.
        </p>
        {probe && (
          <div className="round">
            <p className={`verdict ${probe.permitted ? "release" : "reject"}`}>
              {probe.plan.decision.toUpperCase()} · filtering score{" "}
              {probe.plan.filtering_score.toFixed(2)} · trust {probe.plan.assessment.trust} (
              {probe.plan.assessment.kyu_score.toFixed(2)})
            </p>
            {probe.message && <p>{probe.message}</p>}
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Sensitivity</th>
                  <th>Strategy</th>
                </tr>
              </thead>
              <tbody>
                {probe.plan.field_restrictions.map((restriction) => (
                  <tr key={restriction.field}>
                    <td>{restriction.field}</td>
                    <td>{restriction.sensitivity}</td>
                    <td className="mono">{restriction.strategy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {probe.plan.violations.length > 0 && (
              <ul className="reasons">
                {probe.plan.violations.map((violation) => (
                  <li key={violation}>{violation}</li>
                ))}
              </ul>
            )}
            <details>
              <summary>What the guard returned ({probe.payload.length} rows)</summary>
              <pre>{JSON.stringify(probe.payload.slice(0, 5), null, 2)}</pre>
            </details>
            <details>
              <summary>Obligations the guard must discharge</summary>
              <ul className="reasons">
                {probe.plan.obligations.map((obligation) => (
                  <li key={obligation}>{obligation}</li>
                ))}
              </ul>
            </details>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Local worklist</h2>
          <select value={selectedSite} onChange={(event) => setSelectedSite(event.target.value)}>
            {overview?.hospitals.map((hospital) => (
              <option key={hospital.site_id} value={hospital.site_id}>
                {hospital.display_name}
              </option>
            ))}
          </select>
        </div>
        <p className="note">
          This view never leaves the hospital. Cases are re-ordered so an urgent one reaches a
          specialist sooner; nothing is removed from the queue, including cases that could not be
          scored.
        </p>
        <table>
          <thead>
            <tr>
              <th>Encounter</th>
              <th>Age band</th>
              <th>NIHSS</th>
              <th>Priority</th>
              <th>Score</th>
              <th>Basis</th>
            </tr>
          </thead>
          <tbody>
            {worklist.map((entry, index) => (
              <tr key={entry.encounter_id ?? index} className={entry.assessment.priority}>
                <td className="mono">{entry.encounter_id ?? "—"}</td>
                <td>{entry.age_band ?? "—"}</td>
                <td>{entry.nihss_total ?? "—"}</td>
                <td>
                  <span className={`tag ${entry.assessment.priority}`}>
                    {entry.assessment.suppressed ? "not assessed" : entry.assessment.priority}
                  </span>
                </td>
                <td>{entry.assessment.score.toFixed(3)}</td>
                <td className="basis">{entry.assessment.basis.join("; ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <footer>
        <p>
          Dagents healthcare demo. Synthetic data only. Not a medical device, not clinically
          validated, and not a substitute for clinical judgement.
        </p>
      </footer>
    </div>
  );
}
