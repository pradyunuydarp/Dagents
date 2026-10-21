import { useState } from "react";

import { ApiError, isRecorded, postJson } from "../api";
import {
  count,
  fixed,
  formatMetric,
  gateLabel,
  gateRequirement,
  gateTone,
  metricLabel,
  metricList,
  offerText,
  offerTone,
  orderedMetrics,
  phaseLabel,
  phaseNote,
  plannerReason,
  releaseText,
  releaseTone
} from "../format";
import type { ApproveResult, AuditSummary, ChartInfo, DispatchCall, Overview, Pilot, RoundReport } from "../types";
import { ApiCalls } from "./ApiCalls";
import { Chip, ErrorNote, Panel } from "./Basics";
import { ChartFigure } from "./Chart";

/** A hospital's display name, or its id when the overview does not list it. */
function siteName(overview: Overview, siteId: string): string {
  return overview.hospitals.find((hospital) => hospital.site_id === siteId)?.display_name ?? siteId;
}

function participationText(value: string): string {
  return value.replace(/_/g, " ");
}

/** The sites a round's dispatch reached, in the overview's order, then any others. */
function dispatchSites(overview: Overview, dispatch: DispatchCall[]): string[] {
  const reached = new Set(dispatch.map((call) => call.site_id));
  return [
    ...overview.hospitals.map((hospital) => hospital.site_id).filter((siteId) => reached.has(siteId)),
    ...[...reached].filter((siteId) => !overview.hospitals.some((hospital) => hospital.site_id === siteId))
  ];
}

/** An LMA's answer to one call, for a table cell: its HTTP status, or "no answer". */
function answerText(call: DispatchCall | undefined): string {
  if (!call) return "—";
  return call.status_code === null ? "no answer" : String(call.status_code);
}

/**
 * What one hospital did with a round's job, from the GMA's dispatch rows: its
 * answer to the offer, and the participation of the job it ran. A `run` row
 * holds both steps of one call; older reports hold an `offer` and an
 * `execute` row.
 */
function siteDispatch(dispatch: DispatchCall[], siteId: string) {
  const rows = dispatch.filter((call) => call.site_id === siteId);
  const runCall = rows.find((call) => call.step === "run");
  if (runCall) {
    const offer = runCall.outcome === "refused" || runCall.outcome === "failed" ? runCall.outcome : "accepted";
    return {
      offer,
      offerStatus: runCall.status_code,
      run: offer === "accepted" ? runCall.outcome : undefined,
      answers: `run ${answerText(runCall)}`
    };
  }
  const offerCall = rows.find((call) => call.step === "offer");
  const executeCall = rows.find((call) => call.step === "execute");
  return {
    offer: offerCall?.outcome,
    offerStatus: offerCall?.status_code ?? null,
    run: executeCall?.outcome,
    answers: `offer ${answerText(offerCall)}${executeCall ? `, run ${answerText(executeCall)}` : ""}`
  };
}

/**
 * How the GMA dispatched one or more rounds: for each hospital, the job offer
 * and its answer, and whether the hospital's LMA ran the job.
 *
 * Every value comes from the round report (`dispatch` and `acceptances`). A
 * round recorded before the agents existed has no `dispatch`, and nothing is
 * shown for it.
 */
function DispatchRows({
  overview,
  rounds
}: {
  overview: Overview;
  rounds: { label: string; round: RoundReport }[];
}) {
  const shown = rounds.filter(({ round }) => Array.isArray(round.dispatch));
  if (shown.length === 0) return null;
  const answers = shown.flatMap(({ round }) => {
    const dispatch = round.dispatch ?? [];
    return dispatchSites(overview, dispatch).map((siteId) => siteDispatch(dispatch, siteId));
  });
  const offers = answers.filter((answer) => answer.offer !== undefined);
  const accepted = offers.filter((answer) => answer.offer === "accepted").length;
  const withRound = shown.length > 1;
  return (
    <details className="ds-details dispatch" data-dispatch>
      <summary>
        {withRound ? "How the GMA dispatched each round's job" : "How the GMA dispatched the job"}:{" "}
        {count(accepted)} of {count(offers.length)} offers accepted
      </summary>
      <p className="note ds-note">
        The GMA offers the round&rsquo;s job to each selected hospital&rsquo;s LMA. The LMA checks the
        job and asks its Guard before it accepts. Only an accepted job runs, and the LMA sends back a
        result without its records. The GMA sends both steps in one call to each LMA.
      </p>
      <div className="ds-table-scroll">
        <table className="ds-table dispatch-table">
          <thead>
            <tr>
              {withRound && <th scope="col">Round</th>}
              <th scope="col">Hospital</th>
              <th scope="col">Job offer</th>
              <th scope="col">Job run</th>
              <th scope="col" className="wide-only">
                LMA answers
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.flatMap(({ label, round }) => {
              const dispatch = round.dispatch ?? [];
              if (dispatch.length === 0) {
                return [
                  <tr key={`${round.round_id}-none`}>
                    {withRound && <td>{label}</td>}
                    <td colSpan={4} className="ds-note">
                      The GMA sent no job for this round.
                    </td>
                  </tr>
                ];
              }
              return dispatchSites(overview, dispatch).map((siteId) => {
                const { offer, offerStatus, run, answers } = siteDispatch(dispatch, siteId);
                const answer = round.acceptances?.find((item) => item.site_id === siteId);
                const refusedReason = offer && offer !== "accepted" ? answer?.reason : undefined;
                return (
                  <tr
                    key={`${round.round_id}-${siteId}`}
                    data-dispatch-round={round.round_id}
                    data-dispatch-site={siteId}
                    data-offer={offer ?? ""}
                    data-run={run ?? ""}
                  >
                    {withRound && <td>{label}</td>}
                    <td>{siteName(overview, siteId)}</td>
                    <td>
                      {offer ? <Chip tone={offerTone(offer)}>{offerText(offer, offerStatus)}</Chip> : "—"}
                      {refusedReason && <span className="cell-sub">{plannerReason(refusedReason)}</span>}
                    </td>
                    <td>{run ? participationText(run) : "not run"}</td>
                    <td className="mono wide-only">{answers}</td>
                  </tr>
                );
              });
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** One non-training round: its plan, its quorum, and what each hospital sent. */
function RoundCard({ overview, round, name }: { overview: Overview; round: RoundReport; name: string }) {
  return (
    <div className="round phase" data-round={name}>
      <div className="round-head">
        <h3>{phaseLabel(name)}</h3>
        <span className="digest mono" title="The digest of the round's manifest">
          {round.plan.round_digest}
        </span>
      </div>
      {phaseNote(name) && <p className="note ds-note">{phaseNote(name)}</p>}
      <div className="round-state ds-row">
        <Chip tone={round.plan.quorum_met ? "permit" : "deny"}>
          {round.plan.quorum_met ? "quorum met" : "quorum not met"}
        </Chip>
        <Chip tone={round.readiness?.aggregation_permitted ? "permit" : "narrow"}>
          {round.readiness?.aggregation_permitted ? "results combined" : "results not combined"}
        </Chip>
        <span className="round-count ds-mono">
          {round.plan.selected_sites.length} of {round.plan.required_participants} hospitals needed
        </span>
      </div>
      <Exclusions overview={overview} round={round} />
      <DispatchRows overview={overview} rounds={[{ label: phaseLabel(name), round }]} />
      <details className="ds-details">
        <summary>What each hospital sent ({round.results.length})</summary>
        <div className="ds-table-scroll">
          <table className="ds-table">
            <thead>
              <tr>
                <th scope="col">Hospital</th>
                <th scope="col">Took part</th>
                <th scope="col" className="num">
                  Records used
                </th>
                <th scope="col">Values sent</th>
              </tr>
            </thead>
            <tbody>
              {round.results.map((result) => (
                <tr key={result.site_id}>
                  <td>{siteName(overview, result.site_id)}</td>
                  <td>{participationText(result.participation)}</td>
                  <td className="num">{count(result.contributed_examples)}</td>
                  <td className="values-sent">{metricList(result.metrics)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="note ds-note">
          None of these values describes one patient. A hospital sends counts, approved metrics and a
          bounded update. Its records and the evidence for each value stay at the hospital.
        </p>
      </details>
    </div>
  );
}

function Exclusions({ overview, round }: { overview: Overview; round: RoundReport }) {
  const excluded = round.plan.excluded_sites;
  const rejected = round.readiness?.rejected_contributions ?? [];
  if (excluded.length === 0 && rejected.length === 0) return null;
  return (
    <ul className="reasons ds-reasons">
      {excluded.map((item) => (
        <li key={`x-${item.site_id}`}>
          <strong>{siteName(overview, item.site_id)}</strong> left out: {plannerReason(item.reason)}
        </li>
      ))}
      {rejected.map((item) => (
        <li key={`r-${item.site_id}`}>
          <strong>{siteName(overview, item.site_id)}</strong> result refused: {plannerReason(item.reason)}
        </li>
      ))}
    </ul>
  );
}

/** The training rounds, one row each. */
function TrainingRounds({ overview, pilot }: { overview: Overview; pilot: Pilot }) {
  const byId = new Map(pilot.rounds.training.map((round) => [round.round_id, round]));
  return (
    <div className="round phase" data-round="training">
      <div className="round-head">
        <h3>{phaseLabel("training")}</h3>
        <span className="digest mono">
          {pilot.training_curve.length} rounds, starting from {pilot.training_settings.initial_version}
        </span>
      </div>
      <p className="note ds-note">
        In each round every hospital&rsquo;s LMA trains the current version on the hospital&rsquo;s own
        records and sends an update to the GMA. The hospital&rsquo;s Guard scales the update down to a
        bounded size before it is sent. The GMA averages the updates, weighted by each hospital&rsquo;s
        number of records, into the next version. The label stays at the hospital.
      </p>
      <div className="ds-table-scroll">
        <table className="ds-table training-rounds">
          <thead>
            <tr>
              <th scope="col">Round</th>
              <th scope="col" className="num">
                Log loss after
              </th>
              <th scope="col" className="num">
                Largest update
              </th>
              <th scope="col" className="num">
                Records
              </th>
              <th scope="col">Quorum</th>
            </tr>
          </thead>
          <tbody>
            {pilot.training_curve.map((point) => {
              const round = byId.get(point.round_id);
              return (
                <tr key={point.round_id}>
                  <td className="mono">{point.round}</td>
                  <td className="num">{fixed(point.loss, 4)}</td>
                  <td className="num">{fixed(point.max_update_norm, 3)}</td>
                  <td className="num">{count(point.contributed_examples)}</td>
                  <td>
                    {round ? (
                      <Chip tone={round.plan.quorum_met ? "permit" : "deny"}>
                        {round.plan.quorum_met ? "met" : "not met"}
                      </Chip>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="note ds-note">
        Log loss measures how far the predictions are from the outcomes; lower is better. The update
        size is the length of the update vector (its L2 norm) after the Guard&rsquo;s bound. Hospitals
        that took part:{" "}
        {overview.hospitals
          .filter((hospital) => pilot.training_curve[0]?.contributing_sites.includes(hospital.site_id))
          .map((hospital) => hospital.display_name)
          .join(", ") || "none"}
        .
      </p>
      {pilot.rounds.training.map((round) => (
        <Exclusions key={round.round_id} overview={overview} round={round} />
      ))}
      <DispatchRows
        overview={overview}
        rounds={pilot.rounds.training.map((round) => {
          const number = /-training-(\d+)$/.exec(round.round_id)?.[1];
          return { label: number ? `Round ${number}` : round.round_id, round };
        })}
      />
    </div>
  );
}

/** How many rounds ran: the training rounds plus each other round that has a report. */
function roundsRun(pilot: Pilot): number {
  const { analytics, evaluation, candidate_validation, training } = pilot.rounds;
  return [analytics, evaluation, candidate_validation].filter((round) => round !== null).length + training.length;
}

/**
 * The number of decisions in a chain. A recording made before the report
 * carried `record_count` holds the records instead.
 */
function decisions(audit: AuditSummary & { records?: unknown[] }): number | null {
  return audit.record_count ?? audit.records?.length ?? null;
}

/**
 * The federated pilot: rounds, training, the candidate, the release gates and
 * the audit chains. Every decision shown comes from the backend.
 */
export function PilotPanel({
  overview,
  pilot,
  onPilot,
  onModelChange,
  trainingChart
}: {
  overview: Overview;
  pilot: Pilot | null;
  onPilot: (pilot: Pilot) => void;
  /** The model in use may have changed: after an approval, and after an approval refused with a 409. */
  onModelChange: () => void;
  trainingChart: ChartInfo | undefined;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [approving, setApproving] = useState(false);
  const [approved, setApproved] = useState<ApproveResult | null>(null);
  const [approveError, setApproveError] = useState<unknown>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    setApproved(null);
    setApproveError(null);
    try {
      onPilot(await postJson<Pilot>("/api/v1/pilot:run", { round_prefix: "ui" }));
    } catch (exc) {
      setError(exc);
    } finally {
      setBusy(false);
    }
  };

  const approve = async (version: string) => {
    setApproving(true);
    setApproveError(null);
    try {
      setApproved(await postJson<ApproveResult>(`/api/v1/models/${encodeURIComponent(version)}:approve`, {}));
      onModelChange();
    } catch (exc) {
      setApproveError(exc);
      // The GMA answers 409 when the candidate cannot be approved, for
      // example because another person approved a candidate first. Fetch the
      // overview again, so "Model in use" names the model the hospitals use now.
      if (exc instanceof ApiError && exc.status === 409) onModelChange();
    } finally {
      setApproving(false);
    }
  };

  const gates = pilot?.release?.gates;
  const definitions = new Map(overview.release_gates.map((gate) => [gate.gate_id, gate]));
  const chains = pilot
    ? [
        ...Object.entries(pilot.site_audits).map(([siteId, audit]) => ({
          key: siteId,
          name: siteName(overview, siteId),
          audit
        })),
        { key: "coordinator", name: "Coordinator", audit: pilot.coordinator_audit }
      ]
    : [];

  return (
    <Panel
      hook="pilot"
      eyebrow="Federated rounds"
      title="Federated pilot"
      action={
        <button type="button" className="ds-btn ds-btn--primary" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run the pilot"}
        </button>
      }
    >
      <p className="note ds-note">
        The pilot runs rounds at every hospital: analytics, evaluation of the model in use, several
        training rounds, and validation of the new model. Training produces a candidate model. The
        release gates then decide whether it may replace the model in use. A run can take up to a
        minute.
      </p>
      <p className="note ds-note">
        The app asks the GMA to run each round and calls no LMA itself. The GMA offers each round&rsquo;s
        job to every hospital&rsquo;s LMA, collects the results, and runs the release Guard and gates.
      </p>
      {busy && (
        <p className="note ds-note" role="status" data-pilot-running>
          The pilot is running. Each hospital trains and checks its results before the gates decide.
        </p>
      )}
      <ErrorNote error={error} />

      {pilot && (
        <>
          <dl className="ds-facts pilot-summary">
            <div>
              <dt>Rounds run</dt>
              <dd data-summary="rounds">{roundsRun(pilot)}</dd>
            </div>
            <div>
              <dt>Candidate</dt>
              <dd className="wrap">{pilot.candidate ? pilot.candidate.candidate_version : "none"}</dd>
            </div>
            <div>
              <dt>Hospitals that took part</dt>
              <dd>{pilot.candidate ? pilot.candidate.contributing_sites.length : 0}</dd>
            </div>
            <div>
              <dt>Release decision</dt>
              <dd>{gates ? <Chip tone={releaseTone(gates.action)}>{releaseText(gates.action)}</Chip> : "—"}</dd>
            </div>
          </dl>

          <div className="phases">
            {pilot.rounds.analytics && <RoundCard overview={overview} round={pilot.rounds.analytics} name="analytics" />}
            {pilot.rounds.evaluation && (
              <RoundCard overview={overview} round={pilot.rounds.evaluation} name="evaluation" />
            )}
            {pilot.rounds.training.length > 0 && <TrainingRounds overview={overview} pilot={pilot} />}
            {pilot.rounds.candidate_validation && (
              <RoundCard overview={overview} round={pilot.rounds.candidate_validation} name="candidate_validation" />
            )}
          </div>

          {trainingChart && (
            <div className="chart-single">
              <ChartFigure chart={trainingChart} />
            </div>
          )}

          {pilot.stop_reason && <p className="guard-message">The pilot stopped early: {pilot.stop_reason}</p>}

          {pilot.candidate && pilot.release && gates && (
            <div className="round candidate">
              <div className="round-head">
                <h3>The candidate</h3>
                <span className="digest mono">{pilot.candidate.candidate_version}</span>
              </div>

              <div
                className={`verdict decision ds-verdict ds-verdict--${releaseTone(gates.action)}`}
                data-decision={gates.action}
              >
                <span className="verdict-key">Release decision</span>
                <Chip tone={releaseTone(gates.action)}>{releaseText(gates.action)}</Chip>
                <span className="verdict-note">
                  {gates.action === "release"
                    ? "Every blocking gate passed. A person must approve the release."
                    : gates.rollback_version
                      ? `The hospitals keep using ${gates.rollback_version}.`
                      : "The hospitals keep using the model in use."}
                </span>
              </div>

              <div className="ds-table-scroll">
                <table className="ds-table metrics">
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col" className="num">
                        Candidate
                      </th>
                      <th scope="col" className="num">
                        Model in use
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {orderedMetrics(Object.keys(pilot.candidate.metrics)).map((key) => (
                      <tr key={key}>
                        <td>{metricLabel(key)}</td>
                        <td className="num">{formatMetric(key, pilot.candidate!.metrics[key])}</td>
                        <td className="num">{formatMetric(key, pilot.baseline_metrics[key])}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="note ds-note">
                Both models were scored the same way, at each hospital, on its labelled records. The
                model in use is {pilot.model_in_use.version}.
              </p>

              <h4>Release gates</h4>
              <ul className="gates">
                {gates.gate_results.map((gate) => {
                  const definition = definitions.get(gate.gate_id);
                  return (
                    <li key={gate.gate_id} className={gate.outcome} data-gate={gate.gate_id}>
                      <Chip tone={gateTone(gate.outcome)}>{gate.outcome.replace(/_/g, " ")}</Chip>
                      <strong>{gateLabel(gate.gate_id)}</strong>
                      <span className="gate-kind">
                        {definition ? (definition.blocking ? "blocking" : "advisory") : ""}
                      </span>
                      <span className="detail">
                        Required: {gateRequirement(definition)}. Observed:{" "}
                        <span className="ds-mono" data-observed>
                          {definition ? formatMetric(definition.metric, gate.observed) : fixed(gate.observed, 3)}
                        </span>
                        .
                      </span>
                    </li>
                  );
                })}
              </ul>
              <p className="note ds-note">
                A blocking gate that fails stops the release. An advisory gate is reported but does not
                stop it. A gate whose metric is missing blocks the release.
              </p>

              {gates.action === "release" && (
                <div className="approve ds-inset" data-panel="approve">
                  <p>
                    The gates allow the release of {pilot.candidate.candidate_version}. The hospitals
                    start using it only after a person approves it.
                  </p>
                  <button
                    type="button"
                    className="ds-btn ds-btn--primary"
                    onClick={() => approve(pilot.candidate!.candidate_version)}
                    disabled={isRecorded || approving || approved !== null}
                  >
                    {approving ? "Approving…" : "Approve release"}
                  </button>
                  {isRecorded && (
                    <p className="ds-note">
                      This page replays a recorded run, so it cannot approve a model. Run the demo
                      locally to try it.
                    </p>
                  )}
                  <ErrorNote error={approveError} />
                  {approved && (
                    <>
                      <p className="guard-message">
                        Model in use: <span className="ds-mono">{approved.released.version}</span>. It
                        replaces {approved.previous}. The hospital pages now score records with it.
                      </p>
                      <ApiCalls calls={approved.framework_calls} label="Dagents API calls to approve the release" />
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="round audit">
            <div className="round-head">
              <h3>Audit chains</h3>
              <span className="digest mono">{chains.length} chains</span>
            </div>
            <div className="ds-table-scroll">
              <table className="ds-table chains">
                <thead>
                  <tr>
                    <th scope="col">Kept by</th>
                    <th scope="col">Chain</th>
                    <th scope="col" className="num">
                      Decisions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {chains.map(({ key, name, audit }) => (
                    <tr key={key} data-chain={key} data-intact={audit.chain_intact ? "yes" : "no"}>
                      <td>{name}</td>
                      <td>
                        <Chip tone={audit.chain_intact ? "permit" : "deny"}>
                          {audit.chain_intact ? "intact" : "broken"}
                        </Chip>
                      </td>
                      <td className="num" data-records>
                        {count(decisions(audit))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="note ds-note" data-audit-note>
              Each hospital and the coordinator keep their own audit log. Each record includes the
              digest of the record before it. Intact means each record links to the one before it.
              Each keeper checks every record the first time it reads a chain that has no saved check
              point. After that it checks each new record once and counts the earlier ones. A hospital reports only whether its chain is intact and how many decisions it
              holds. The records stay at the hospital.
            </p>
          </div>

          <ApiCalls key={pilot.pilot_id} calls={pilot.framework_calls} />
        </>
      )}
    </Panel>
  );
}
