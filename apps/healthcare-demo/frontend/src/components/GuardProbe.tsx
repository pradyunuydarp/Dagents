import { useCallback, useState } from "react";

import { NotRecordedError, isRecorded, postJson, type Recording } from "../api";
import { fixed, guardTone, plannerReason, strategyText } from "../format";
import { useFieldLabel } from "../labels";
import type { GuardProbe as Probe, Overview } from "../types";
import { ApiCalls } from "./ApiCalls";
import { ErrorNote, Panel } from "./Basics";

/** The granularities a request can ask for, with what each means. */
const GRANULARITIES: { value: string; label: string }[] = [
  { value: "cell", label: "cell (one value)" },
  { value: "row", label: "row (one per patient)" },
  { value: "column", label: "column (one field)" },
  { value: "table", label: "table (all records)" },
  { value: "model_update", label: "model update" }
];

/**
 * The cohort sizes a capture covers.
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

/** The paper the Guard's restriction planning follows. */
const GRAILS_URL = "https://ojs.aaai.org/index.php/AIES/article/view/36650";

/** A list of field names in words, or "none". */
function fieldList(names: string[], label: (name: string) => string): string {
  return names.length > 0 ? names.map(label).join(", ") : "none";
}

/**
 * Ask one hospital's Ethical Guard what it would release.
 *
 * The decision and each field's strategy come from the planner, through the
 * backend. The page only sends the three controls and shows the answer.
 */
export function GuardProbe({ overview, recording }: { overview: Overview; recording: Recording | null }) {
  const [siteId, setSiteId] = useState(overview.hospitals[0]?.site_id ?? "");
  const [verified, setVerified] = useState(true);
  const [granularity, setGranularity] = useState("row");
  // Above the minimum cohort, so the trust and granularity controls show their
  // effect before the cohort rule hides it.
  const [cohort, setCohort] = useState(25);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notRecorded, setNotRecorded] = useState(false);
  const [busy, setBusy] = useState(false);

  const label = useFieldLabel();
  const site = overview.hospitals.find((hospital) => hospital.site_id === siteId);
  const answeredBy = overview.hospitals.find((hospital) => hospital.site_id === probe?.site_id);

  const ask = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNotRecorded(false);
    try {
      setProbe(
        await postJson<Probe>("/api/v1/governance:probe", {
          site_id: siteId,
          verified,
          granularity,
          cohort_size: cohort,
          boundary: "before_read"
        })
      );
    } catch (exc) {
      setProbe(null);
      if (exc instanceof NotRecordedError) setNotRecorded(true);
      else setError(exc);
    } finally {
      setBusy(false);
    }
  }, [siteId, verified, granularity, cohort]);

  return (
    <Panel hook="guard" className="guard" eyebrow="Before data leaves a hospital" title="Ethical Guard">
      <p className="note ds-note">
        Every request for a hospital&rsquo;s data goes through that hospital&rsquo;s Guard, which runs in
        the hospital&rsquo;s LMA. The Guard asks the planner how to protect each field, applies the
        answer, and writes the decision to the hospital&rsquo;s audit log. Choose who is asking and how
        much they ask for, then select <em>Ask the guard</em>.
      </p>
      <p className="note ds-note">
        The planner follows{" "}
        <a href={GRAILS_URL} rel="noreferrer">
          GRAILS
        </a>
        , a published method for ethical restrictions on data use. Three inputs decide how each field
        is protected: the field&rsquo;s sensitivity, the trust in the requester and the granularity of
        the request.
      </p>
      <ul className="note ds-note lever-notes">
        <li>An unverified requester gets lower trust, so the strategies get stricter.</li>
        <li>A column or table request comes back only as a summary.</li>
        <li>
          A column, table or model-update request about fewer than {overview.minimum_cohort} records
          is denied, whoever asks. Cell and row requests return individual records, so the cohort
          rule does not apply to them. The strategy for each field protects them.
        </li>
      </ul>

      <div className="guard-grid">
        <div className="levers ds-inset">
          <p className="ds-eyebrow">The request</p>
          <label className="ds-field">
            <span>Hospital</span>
            <select className="ds-input" value={siteId} onChange={(event) => setSiteId(event.target.value)}>
              {overview.hospitals.map((hospital) => (
                <option key={hospital.site_id} value={hospital.site_id}>
                  {hospital.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="ds-check lever-check">
            <input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} />
            Requester verified
          </label>
          <label className="ds-field">
            <span>Granularity</span>
            <select className="ds-input" value={granularity} onChange={(event) => setGranularity(event.target.value)}>
              {GRANULARITIES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="ds-field">
            <span>Cohort size</span>
            <input
              className="ds-input"
              type="number"
              min={0}
              max={1000000}
              value={cohort}
              onChange={(event) => setCohort(Number(event.target.value))}
            />
          </label>
          <button type="button" className="ds-btn ds-btn--primary lever-go" onClick={ask} disabled={busy || !site}>
            Ask the guard
          </button>
        </div>

        <div className="outcome" aria-live="polite">
          {notRecorded && (
            <div className="banner recorded">
              <strong>Not recorded.</strong> This page replays a recorded run. It has answers for the
              cohort sizes <span className="mono">{recordedCohorts(recording).join(", ")}</span>.
              Choose granularity <em>table</em> and compare {overview.minimum_cohort - 1} and{" "}
              {overview.minimum_cohort} to see the minimum cohort rule.
            </div>
          )}
          <ErrorNote error={error} />

          {probe ? (
            <>
              <div className={`verdict ds-verdict ds-verdict--${guardTone(probe.plan.decision, probe.permitted)}`}>
                <strong>{probe.plan.decision.toUpperCase()}</strong>
                <span className="verdict-metrics">
                  <span>filtering score {fixed(probe.plan.filtering_score, 2)}</span>
                  <span>
                    trust {probe.plan.assessment.trust} ({fixed(probe.plan.assessment.kyu_score, 2)})
                  </span>
                </span>
              </div>
              <p className="ds-note">
                Answer from {answeredBy?.display_name ?? probe.site_id}&rsquo;s Guard. The filtering
                score runs from 0 (every field sent as recorded) to 1 (every field withheld). Trust is
                the planner&rsquo;s assessment of the requester.
              </p>

              {probe.message && <p className="guard-message">{plannerReason(probe.message)}</p>}

              {probe.plan.field_restrictions.length > 0 ? (
                <div className="ds-table-scroll">
                  <table className="ds-table restrictions">
                    <thead>
                      <tr>
                        <th scope="col">Field</th>
                        <th scope="col">Sensitivity</th>
                        <th scope="col" className="wide-only">
                          What happens
                        </th>
                        <th scope="col" className="strategy-col">
                          Strategy
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {probe.plan.field_restrictions.map((restriction) => (
                        <tr key={restriction.field}>
                          <td className="mono">{restriction.field}</td>
                          <td>{restriction.sensitivity}</td>
                          <td className="why wide-only">{strategyText(restriction.strategy)}</td>
                          <td className="mono strategy">{restriction.strategy}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="ds-note">
                  The request was refused before any field was planned, so there is no table of fields.
                </p>
              )}

              <ul className="reasons ds-reasons strategy-notes narrow-only">
                {probe.plan.field_restrictions.map((restriction) => (
                  <li key={restriction.field}>
                    <strong>{restriction.field}</strong> {strategyText(restriction.strategy)}
                  </li>
                ))}
              </ul>

              {probe.plan.violations.some((violation) => violation !== probe.message) && (
                <ul className="reasons ds-reasons">
                  {probe.plan.violations
                    .filter((violation) => violation !== probe.message)
                    .map((violation) => (
                      <li key={violation}>{plannerReason(violation)}</li>
                    ))}
                </ul>
              )}

              <dl className="ds-facts guard-release" data-guard-release>
                <div>
                  <dt>Records released</dt>
                  <dd data-released="records">{probe.released_count}</dd>
                </div>
                <div>
                  <dt>Fields released</dt>
                  <dd className="plain">{fieldList(probe.released_fields, label)}</dd>
                </div>
                <div>
                  <dt>Withheld</dt>
                  <dd className="plain">{fieldList(probe.withheld_fields, label)}</dd>
                </div>
                <div>
                  <dt>Coarsened or bounded</dt>
                  <dd className="plain">{fieldList(probe.transformed_fields, label)}</dd>
                </div>
              </dl>
              <p className="ds-note">
                The probe runs the request over a sample of the hospital&rsquo;s first records. This
                page is the coordinator&rsquo;s view, so the hospital reports how many records its Guard
                would release and which fields, never the values. Patient rows stay at the hospital.
              </p>
              <details className="ds-details">
                <summary>Obligations attached to this decision</summary>
                <ul className="reasons ds-reasons">
                  {probe.plan.obligations.map((obligation) => (
                    <li key={obligation}>{obligation}</li>
                  ))}
                </ul>
              </details>
              <ApiCalls key={probe.audit_id ?? probe.site_id} calls={probe.framework_calls} />
            </>
          ) : (
            !notRecorded &&
            !error && (
              <p className="outcome-idle ds-note">
                Set the controls and select <em>Ask the guard</em>. The decision and a strategy for each
                field come back from the planner.
                {isRecorded ? " This page replays recorded answers." : ""}
              </p>
            )
          )}
        </div>
      </div>
    </Panel>
  );
}
