import { useState } from "react";

import { isRecorded, postJson } from "../api";
import { SiteCalls } from "../components/ApiCalls";
import { Chip, ErrorNote, Loading, Panel } from "../components/Basics";
import { ChartGrid } from "../components/Chart";
import {
  boundaryText,
  count,
  fieldOrder,
  fieldValue,
  fixed,
  formatMetric,
  guardTone,
  metricLabel,
  metricList,
  orderedMetrics,
  outcomeRateText,
  phaseLabel,
  plannerReason,
  priorityTone,
  profileSummary,
  shortId,
  storeText,
  when
} from "../format";
import { useFieldLabel } from "../labels";
import { href } from "../router";
import type {
  AuditResponse,
  ChartList,
  Dashboard,
  EnteredResponse,
  FrameworkCall,
  OutcomeResult,
  Overview,
  Store,
  WorklistResponse
} from "../types";
import { useResource } from "../useResource";

/** Where this hospital's records are, and who can read them, in plain words. */
function StoreNote({ store }: { store: Store }) {
  if (store.kind === "postgres") {
    return (
      <>
        <p>
          This hospital&rsquo;s records are in the Postgres schema{" "}
          <span className="ds-mono">{store.schema}</span>. Every query the demo makes for this
          hospital runs as the database role <span className="ds-mono">{store.role}</span>. The
          coordinator&rsquo;s role and the other hospitals&rsquo; roles have no grant on this schema.
        </p>
        <p className="ds-note">
          In this demo the hospitals share one database server, and the server&rsquo;s single
          login can read every schema. So the isolation applies to the demo&rsquo;s code, not to that
          login. A real deployment gives each hospital its own database and login.
        </p>
      </>
    );
  }
  return (
    <p>
      The demo server holds this hospital&rsquo;s records in memory, in a store separate from the
      other hospitals&rsquo;. Only this hospital&rsquo;s code reads it. With a database configured,
      each hospital gets its own Postgres schema and role instead.
    </p>
  );
}

/** A round's name from its id: the pilot's rounds end in their phase and number. */
function roundName(roundId: string, phase: string): string {
  const training = /-training-(\d+)$/.exec(roundId);
  if (training) return `Training round ${training[1]}`;
  if (roundId.endsWith("-candidate-validation")) return phaseLabel("candidate_validation");
  return phaseLabel(phase);
}

function modelKind(kind: string): string {
  if (kind === "points_rule") return "points rule";
  if (kind === "logistic_regression") return "logistic regression";
  return kind.replace(/_/g, " ");
}

/**
 * What staff at one hospital see.
 *
 * Everything here is the hospital's own data, read at the hospital. None of
 * it is shown on the coordinator's page. The page has two groups:
 *
 * - the hospital's Dagents LMA: its running statistics, the model in use, its
 *   audit log and its round jobs, read through the LMA's API;
 * - the app at the hospital: where the records are stored, the worklist, the
 *   records entered in the demo and the charts.
 *
 * Each group or panel lists the agent calls behind its answer.
 */
export function HospitalPage({
  siteId,
  overview,
  onChanged
}: {
  siteId: string;
  overview: Overview | null;
  onChanged: () => void;
}) {
  const base = `/api/v1/hospitals/${encodeURIComponent(siteId)}`;
  const dashboard = useResource<Dashboard>(base);
  const worklist = useResource<WorklistResponse>(`${base}/worklist?limit=12`);
  const entered = useResource<EnteredResponse>(`${base}/encounters?origin=entered&limit=20`);
  const charts = useResource<ChartList>(`${base}/charts`);
  const audit = useResource<AuditResponse>(`${base}/audit?limit=50`);
  const [saving, setSaving] = useState<string | null>(null);
  const [outcomeError, setOutcomeError] = useState<unknown>(null);
  // The agent calls behind the last outcome recorded on this page.
  const [outcomeCalls, setOutcomeCalls] = useState<{ id: string; calls: FrameworkCall[] | undefined } | null>(null);

  const label = useFieldLabel();
  const summary = overview?.hospitals.find((hospital) => hospital.site_id === siteId);
  // Never the raw site id: during a cold start or an outage the name is not
  // known yet, and an id is not a heading.
  const name = dashboard.data?.display_name ?? summary?.display_name ?? null;
  const data = dashboard.data;
  const empty = data !== null && data.counts.total === 0;

  const recordOutcome = async (encounterId: string, poorOutcome: boolean) => {
    setSaving(encounterId);
    setOutcomeError(null);
    try {
      const result = await postJson<OutcomeResult>(`${base}/encounters/${encodeURIComponent(encounterId)}/outcome`, {
        poor_outcome_6m: poorOutcome
      });
      setOutcomeCalls({ id: encounterId, calls: result?.framework_calls });
      entered.reload();
      dashboard.reload();
      charts.reload();
      onChanged();
    } catch (exc) {
      setOutcomeError(exc);
    } finally {
      setSaving(null);
    }
  };

  const head = (
    <header className="page-head">
      <p className="ds-eyebrow">Hospital view</p>
      <div className="page-title-row">
        <h1>{name ?? "This hospital"}</h1>
        <a className="ds-btn ds-btn--primary" href={href(`hospitals/${siteId}/new`)}>
          Add a record
        </a>
      </div>
      <p className="lede ds-measure">
        What staff at {name ?? "this hospital"}
        {data?.country ? ` (${data.country})` : ""} see. The records on this page never leave the
        hospital. The coordinator sees only the totals and results this hospital sends.
      </p>
    </header>
  );

  // Every panel depends on the hospital's own store. When its summary cannot
  // be read, the other requests fail the same way, so the page says it once.
  if (dashboard.error && !data) {
    return (
      <div className="page">
        {head}
        <ErrorNote
          error={dashboard.error}
          notLoaded="The hospital page could not be loaded. The message at the top of the page says why."
        />
      </div>
    );
  }

  return (
    <div className="page">
      {head}

      {/* A reload that fails keeps the last numbers on screen, so the page says so. */}
      {Boolean(dashboard.error) && data && (
        <div className="stale" data-stale>
          <ErrorNote
            error={dashboard.error}
            notLoaded="This page could not be updated. The message at the top of the page says why."
          />
          <p className="ds-note">The numbers below are from the last successful load.</p>
        </div>
      )}

      <section className="group" data-group="lma" aria-labelledby="group-lma">
        <header className="group-head">
          <p className="ds-eyebrow">Dagents at the hospital</p>
          <h2 id="group-lma">
            Dagents LMA <span className="ds-mono">{siteId}</span>
          </h2>
          <p className="note ds-note">
            The hospital&rsquo;s local monitoring agent. It runs the hospital&rsquo;s Guard and audit log,
            keeps its running statistics, measures the model in use on the hospital&rsquo;s records, and
            runs the round jobs the hospital accepts. The app reads all of this through the LMA&rsquo;s
            API.
          </p>
          {data && <SiteCalls calls={data.framework_calls} siteId={siteId} />}
        </header>

        <Panel level={3} hook="profile" eyebrow="Kept by the LMA" title="Running statistics">
          <p className="note ds-note">
            The LMA builds these statistics from the hospital&rsquo;s records. It reads the records again
            only when the store changes, for example after a new entry. The statistics stay at the
            hospital.
          </p>
          {data && empty ? (
            <p className="ds-note empty">No statistics yet. The store holds no records.</p>
          ) : data ? (
            <div className="ds-table-scroll">
              <table className="ds-table profile">
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    <th scope="col" className="num">
                      Recorded
                    </th>
                    <th scope="col" className="num">
                      Missing
                    </th>
                    <th scope="col">Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {fieldOrder(data.profile.tracked_fields)
                    .filter((field) => data.profile.fields[field])
                    .map((field) => {
                      const stats = data.profile.fields[field];
                      return (
                        <tr key={field}>
                          <td title={field}>{label(field)}</td>
                          <td className="num">{count(stats.present)}</td>
                          <td className="num">{count(stats.missing)}</td>
                          <td className="summary">{profileSummary(field, stats)}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          ) : (
            !dashboard.error && <Loading what="the statistics" />
          )}
        </Panel>

        <Panel level={3} hook="model" eyebrow="Scoring" title="Model in use">
          {data ? (
            <>
              <dl className="ds-facts">
                <div>
                  <dt>Version</dt>
                  <dd className="wrap">{data.current_model.version}</dd>
                </div>
                <div>
                  <dt>Kind</dt>
                  <dd>{modelKind(data.current_model.kind)}</dd>
                </div>
                <div>
                  <dt>Records with an outcome</dt>
                  <dd>{count(data.local_metrics.labelled_records)}</dd>
                </div>
                <div>
                  <dt>Digest</dt>
                  <dd title={data.current_model.digest}>{shortId(data.current_model.digest, 19)}</dd>
                </div>
              </dl>
              {Object.keys(data.local_metrics.metrics).length > 0 ? (
                <div className="ds-table-scroll">
                  <table className="ds-table metrics">
                    <thead>
                      <tr>
                        <th scope="col">Metric at this hospital</th>
                        <th scope="col" className="num">
                          Value
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {orderedMetrics(Object.keys(data.local_metrics.metrics)).map((key) => (
                        <tr key={key}>
                          <td>{metricLabel(key)}</td>
                          <td className="num">{formatMetric(key, data.local_metrics.metrics[key])}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="ds-note">Not enough records with an outcome to measure the model here.</p>
              )}
              <p className="note ds-note">
                The LMA receives the model in use from the GMA and scores the hospital&rsquo;s records that
                have an outcome with it. These numbers stay here unless a round asks for them.
              </p>
            </>
          ) : (
            !dashboard.error && <Loading what="the model" />
          )}
        </Panel>

        <Panel level={3} hook="audit" eyebrow="Ethical Guard" title="Audit log">
          <ErrorNote error={audit.error} />
          {audit.data ? (
            <>
              <p className="ds-row">
                <Chip tone={audit.data.chain_intact ? "permit" : "deny"}>
                  {audit.data.chain_intact ? "intact" : "broken"}
                </Chip>
                <span className="ds-note" data-audit-note>
                  Each record includes the digest of the record before it. Intact means each record links
                  to the one before it. The LMA checks every record the first time it reads a chain that
                  has no saved check point. After that it checks each new record once and counts the
                  earlier ones.
                </span>
              </p>
              {audit.data.records.length > 0 ? (
                <ul className="audit-list">
                  {audit.data.records.map((record) => (
                    <li key={record.audit_id}>
                      <Chip tone={guardTone(record.decision, record.permitted)}>{record.decision}</Chip>
                      <span className="audit-text">
                        {when(record.recorded_at)}: <span className="ds-mono">{record.requester_id}</span>{" "}
                        asked for {record.granularity.replace(/_/g, " ")} data, checked{" "}
                        {boundaryText(record.boundary)}.
                        {record.withheld_fields.length > 0 &&
                          ` Withheld: ${record.withheld_fields.map(label).join(", ")}.`}
                        {record.contribution_norm_before !== null &&
                          record.contribution_norm_before !== undefined &&
                          record.contribution_norm_after !== null &&
                          record.contribution_norm_after !== undefined &&
                          record.contribution_norm_before !== record.contribution_norm_after &&
                          ` Update size ${fixed(record.contribution_norm_before, 3)} before the Guard, ${fixed(
                            record.contribution_norm_after,
                            3
                          )} after.`}
                        {record.violations.length > 0 &&
                          ` Reason: ${record.violations.map((violation) => plannerReason(violation).replace(/\.$/, "")).join("; ")}.`}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="ds-note">
                  The Guard has made no decisions at this hospital yet. Ask the guard or run the pilot on
                  the consortium page.
                </p>
              )}
              {audit.data.records.length > 0 && (
                <p className="ds-note">Newest first. Showing up to 50 decisions.</p>
              )}
              <SiteCalls calls={audit.data.framework_calls} siteId={siteId} />
            </>
          ) : (
            !audit.error && <Loading what="the audit log" />
          )}
        </Panel>

        <Panel level={3} hook="contributions" eyebrow="Sent to the coordinator" title="What this hospital sent in each round">
          {data ? (
            data.contributions.length > 0 ? (
              <div className="ds-table-scroll">
                <table className="ds-table contributions">
                  <thead>
                    <tr>
                      <th scope="col">Round</th>
                      <th scope="col" className="wide-only">
                        When
                      </th>
                      <th scope="col" className="num">
                        Records used
                      </th>
                      <th scope="col" className="num">
                        Update size
                      </th>
                      <th scope="col" className="wide-only">
                        Values sent
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.contributions.map((item) => (
                      <tr key={item.round_id}>
                        <td title={item.round_id}>
                          {roundName(item.round_id, item.phase)}
                          {item.participation !== "completed" && (
                            <span className="cell-sub">
                              {item.participation.replace(/_/g, " ")}
                              {item.reason ? `: ${plannerReason(item.reason).replace(/\.$/, "")}` : ""}
                            </span>
                          )}
                        </td>
                        <td className="wide-only">{when(item.submitted_at)}</td>
                        <td className="num">{count(item.contributed_examples)}</td>
                        <td className="num">{item.update_norm ? fixed(item.update_norm, 3) : "—"}</td>
                        <td className="values-sent wide-only">{metricList(item.metrics)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="ds-note">
                This hospital has not taken part in a round yet. Run the pilot on the{" "}
                <a href={href("")}>consortium page</a>.
              </p>
            )
          ) : (
            !dashboard.error && <Loading what="the rounds" />
          )}
          <p className="note ds-note">
            The LMA keeps a log of each round job it ran. A round receives counts, metrics and a bounded
            model update, never a patient record.
          </p>
        </Panel>
      </section>

      <section className="group" data-group="app" aria-labelledby="group-app">
        <header className="group-head">
          <p className="ds-eyebrow">The demo app at the hospital</p>
          <h2 id="group-app">App at the hospital</h2>
          <p className="note ds-note">
            The app stores this hospital&rsquo;s records and new entries, ranks the worklist and draws the
            charts. It reads the records only in this hospital&rsquo;s store, and scores them with the
            model the LMA reports in use.
          </p>
        </header>

        <Panel level={3} hook="hospital-store" eyebrow="Storage" title="Where the records are stored">
          {data && empty ? (
            <>
              <StoreNote store={data.store} />
              <p className="empty-store" data-empty-store>
                This hospital&rsquo;s store holds no records yet.
              </p>
            </>
          ) : data ? (
            <>
              <StoreNote store={data.store} />
              <dl className="ds-facts">
                <div>
                  <dt>Records</dt>
                  <dd data-count="total">{count(data.counts.total)}</dd>
                </div>
                <div>
                  <dt>Entered in the demo</dt>
                  <dd data-count="entered">{count(data.counts.entered)}</dd>
                </div>
                <div>
                  <dt>With an outcome</dt>
                  <dd data-count="labelled">{count(data.counts.labelled)}</dd>
                </div>
                <div>
                  <dt>Poor outcome rate</dt>
                  <dd data-rate-status={data.outcome_rate_status ?? "unknown"}>
                    {outcomeRateText(data.outcome_rate, data.outcome_rate_status, data.minimum_cohort)}
                  </dd>
                </div>
                <div>
                  <dt>Store</dt>
                  <dd>{storeText(data.store)}</dd>
                </div>
              </dl>
              <p className="note ds-note">
                The app counts the records in the store. The poor outcome rate comes from the
                hospital&rsquo;s LMA, which reports it only when its planner permits it.
              </p>
            </>
          ) : (
            !dashboard.error && <Loading what="the hospital" />
          )}
        </Panel>

        <Panel level={3} hook="worklist" eyebrow="Highest priority first" title="Worklist">
          <p className="note ds-note">
            The app ranks this hospital&rsquo;s records with the model in use, so the patients at highest
            risk reach a specialist first. No record is removed from the queue, including records that
            could not be scored. On a wide screen, each row shows the basis for its score.
          </p>
          <ErrorNote error={worklist.error} />
          {worklist.data && worklist.data.worklist.length === 0 ? (
            <p className="ds-note empty">No records to rank.</p>
          ) : worklist.data ? (
            <>
              <div className="ds-table-scroll">
                <table className="ds-table worklist">
                  <thead>
                    <tr>
                      <th scope="col">Record</th>
                      <th scope="col">Age band</th>
                      <th scope="col" className="wide-only">
                        Conscious level
                      </th>
                      <th scope="col">Priority</th>
                      <th scope="col" className="num">
                        Score
                      </th>
                      <th scope="col" className="basis">
                        Basis
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {worklist.data.worklist.map((entry) => (
                      <tr key={entry.encounter_id} className={entry.assessment.priority}>
                        <td className="mono">{entry.encounter_id}</td>
                        <td>{fieldValue("age_band", entry.age_band)}</td>
                        <td className="wide-only">{fieldValue("conscious_level", entry.conscious_level)}</td>
                        <td>
                          <Chip tone={priorityTone(entry.assessment.priority)}>
                            {entry.assessment.suppressed ? "not assessed" : entry.assessment.priority}
                          </Chip>
                        </td>
                        <td className="num">{fixed(entry.assessment.score, 3)}</td>
                        <td className="basis">{entry.assessment.basis.join("; ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {data && (
                <p className="ds-note">
                  Showing the {worklist.data.worklist.length} highest-priority of {count(data.counts.total)}{" "}
                  records.
                </p>
              )}
            </>
          ) : (
            !worklist.error && <Loading what="the worklist" />
          )}
          {worklist.data && <SiteCalls calls={worklist.data.framework_calls} siteId={siteId} />}
        </Panel>

        <Panel
          level={3}
          hook="entered"
          eyebrow="Entered in the demo"
          title="Records entered here"
          action={
            <a className="ds-btn" href={href(`hospitals/${siteId}/new`)}>
              Add a record
            </a>
          }
        >
          <p className="note ds-note">
            Records entered on the data-entry page are stored in this hospital&rsquo;s store. Record the
            six-month outcome when it is known; the next pilot then trains and evaluates on the record.
            An outcome is recorded once and cannot be changed afterwards.
          </p>
          {isRecorded && (
            <p className="banner recorded">
              <strong>Replay.</strong> This page replays a recorded run, so it cannot store an outcome.
            </p>
          )}
          <ErrorNote error={entered.error ?? outcomeError} />
          {entered.data ? (
            entered.data.encounters.length > 0 ? (
              <div className="ds-table-scroll">
                <table className="ds-table entered">
                  <thead>
                    <tr>
                      <th scope="col">Record</th>
                      <th scope="col" className="wide-only">
                        Entered
                      </th>
                      <th scope="col">Priority</th>
                      <th scope="col" className="wrap-head">
                        Poor outcome at six months
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {entered.data.encounters.map((entry) => (
                      <tr key={entry.encounter_id} data-encounter={entry.encounter_id}>
                        <td className="mono">{entry.encounter_id}</td>
                        <td className="wide-only">{when(entry.recorded_at)}</td>
                        <td>
                          <Chip tone={priorityTone(entry.assessment.priority)}>
                            {entry.assessment.suppressed ? "not assessed" : entry.assessment.priority}
                          </Chip>
                        </td>
                        <td>
                          {typeof entry.poor_outcome_6m === "boolean" ? (
                            <span className="outcome-value">{entry.poor_outcome_6m ? "yes" : "no"}</span>
                          ) : (
                            <span
                              className="outcome-control"
                              role="group"
                              aria-label={`Record outcome for ${entry.encounter_id}`}
                            >
                              <span className="outcome-label">Record outcome</span>
                              <button
                                type="button"
                                className="ds-btn"
                                disabled={isRecorded || saving !== null}
                                onClick={() => recordOutcome(entry.encounter_id, true)}
                              >
                                Yes
                              </button>
                              <button
                                type="button"
                                className="ds-btn"
                                disabled={isRecorded || saving !== null}
                                onClick={() => recordOutcome(entry.encounter_id, false)}
                              >
                                No
                              </button>
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="ds-note empty">
                No records have been entered at this hospital yet.{" "}
                {!isRecorded && <a href={href(`hospitals/${siteId}/new`)}>Add a record</a>}
              </p>
            )
          ) : (
            !entered.error && <Loading what="the entered records" />
          )}
          {entered.data && (
            <p className="ds-note">
              {count(entered.data.counts.entered)} of {count(entered.data.max_entered)} entries used at this
              hospital.
            </p>
          )}
          {entered.data && <SiteCalls calls={entered.data.framework_calls} siteId={siteId} />}
          {outcomeCalls && (
            <SiteCalls
              key={outcomeCalls.id}
              calls={outcomeCalls.calls}
              siteId={siteId}
              label={`Dagents API calls to record the outcome of ${outcomeCalls.id}`}
            />
          )}
        </Panel>

        <Panel level={3} hook="hospital-charts" eyebrow="This hospital's records" title="Charts">
          <p className="note ds-note">
            Drawn from this hospital&rsquo;s own records, including any entered in the demo. They are
            shown only here.
          </p>
          <ErrorNote error={charts.error} />
          {charts.data ? <ChartGrid charts={charts.data.charts} /> : !charts.error && <Loading what="the charts" />}
          {charts.data && <SiteCalls calls={charts.data.framework_calls} siteId={siteId} />}
        </Panel>
      </section>
    </div>
  );
}
