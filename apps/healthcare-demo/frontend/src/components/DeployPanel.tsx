import { useEffect, useState } from "react";

import { ApiError, isRecorded, postJson } from "../api";
import { count } from "../format";
import type { Deployment, DeploymentStepName, DeploymentStepStatus, DeploymentsView, JoinableHospital } from "../types";
import { useResource } from "../useResource";
import { ApiCalls } from "./ApiCalls";
import { ErrorNote, Loading, Panel } from "./Basics";

/** What each step does, in the order the backend runs them. */
const STEP_LABELS: Record<DeploymentStepName, string> = {
  plan: "Compose file from core-service",
  start: "Start the hospital's LMA",
  answer: "LMA answers",
  attach: "GMA accepts its address",
  enrol: "Enrolled with the GMA",
  join: "Joined the consortium in the app",
  check: "Next round selects it"
};

/** A step's status as a word. Statuses are words, never colours: colour marks only permit, narrow and deny. */
const STATUS_WORDS: Record<DeploymentStepStatus, string> = {
  done: "Done",
  skipped: "Skipped",
  failed: "Failed",
  undone: "Undone",
  not_run: "Not run"
};

const SERVICE_WORDS: Record<JoinableHospital["service"], string> = {
  running: "running",
  stopped: "stopped",
  not_started: "not started",
  unknown: "—"
};

const REPLAY_NOTE =
  "This page replays a recorded run. Adding a hospital starts a new process on the machine that runs the demo, " +
  "so the replay does not offer it. To try it, run the demo on your own machine with --deploy, as the README describes.";

const NOT_OFFERED = "This server does not offer adding a hospital.";

function recordsText(hospital: JoinableHospital): string {
  return hospital.records_source === "dataset"
    ? `${count(hospital.records)} records from the International Stroke Trial`
    : "starts with no records";
}

function appliedText(deployment: Deployment): string {
  if (deployment.adopted) return "It kept a service this app had started earlier.";
  return deployment.applier === "compose"
    ? "The service runs as a container, started with docker compose."
    : "The service runs as a process on this machine, started by the app.";
}

/**
 * The steps of one attempt, the Compose file core-service rendered, the
 * planner's answer for the next round, and the agent calls the attempt made.
 */
function Attempt({ deployment, name }: { deployment: Deployment; name: string }) {
  const failed = deployment.steps.find((step) => step.status === "failed");
  const undone = deployment.steps.some((step) => step.status === "undone");
  return (
    <div className="round deploy-attempt" data-deployment={deployment.deployment_id}>
      <div className="round-head">
        {/* Focus moves here when an attempt the visitor started returns. */}
        <h3 id={`attempt-${deployment.deployment_id}`} tabIndex={-1}>
          {deployment.status === "joined" ? `${name} joined` : `${name} did not join`}
        </h3>
        <span className="digest mono">{deployment.deployment_id}</span>
      </div>
      <ol className="deploy-steps" data-role="steps">
        {deployment.steps.map((step) => (
          <li key={step.step} data-step={step.step} data-status={step.status}>
            <span className="step-label">{STEP_LABELS[step.step] ?? step.step}</span>
            <span className="step-state">
              <span className="step-status">{STATUS_WORDS[step.status] ?? step.status}</span>
              <span className="step-seconds mono">{step.seconds.toFixed(2)} s</span>
            </span>
            <span className="step-detail">{step.detail}</span>
          </li>
        ))}
      </ol>
      {deployment.status === "joined" && <p className="note ds-note">{appliedText(deployment)}</p>}
      {deployment.status === "failed" && (
        <p className="note ds-note">
          {failed ? `The step "${STEP_LABELS[failed.step] ?? failed.step}" failed, and the steps after it did not run.` : ""}
          {undone ? " The steps marked Undone were rolled back." : ""}
        </p>
      )}
      {deployment.compose_yaml !== null && (
        <details className="ds-details" data-role="compose-file">
          <summary>Compose file from core-service</summary>
          <pre className="compose-file">{deployment.compose_yaml}</pre>
        </details>
      )}
      {/* What the planner's answer means. The last step already gives the answer and its reason. */}
      {deployment.next_round && (
        <p className="guard-message" data-role="next-round">
          {deployment.next_round.selected
            ? `Run the pilot to see ${name} take part in its rounds.`
            : `${name} stays in the consortium, but the next round would leave it out, for the reason in the last step.`}
        </p>
      )}
      <ApiCalls calls={deployment.framework_calls} label="Dagents API calls of this attempt" />
    </div>
  );
}

/**
 * Whether the panel offers to start a hospital's LMA: one not in the
 * consortium, or one in it whose LMA has stopped. A joined hospital whose LMA
 * stops makes the overview and the pilot fail until it runs again; the
 * backend starts it again and skips the steps already done.
 */
function canStart(hospital: JoinableHospital): boolean {
  return !hospital.in_consortium || hospital.service === "stopped" || hospital.service === "not_started";
}

/**
 * Add a hospital to the running consortium.
 *
 * Only a network-mode run on the visitor's own machine, started with
 * `--deploy`, can do this. The backend says whether it can
 * (`GET /api/v1/deployments`), and why not; the page shows that reason and
 * offers no button. The backend runs every step: core-service renders the
 * Compose file, the GMA accepts the address and enrols the site, and the GMA's
 * planner decides whether the next round selects it. The page only shows what
 * it reports.
 */
export function DeployPanel({ onJoined, onlyToRestart = false }: { onJoined: () => void; onlyToRestart?: boolean }) {
  const view = useResource<DeploymentsView>("/api/v1/deployments");
  const [adding, setAdding] = useState<string | null>(null);
  const [result, setResult] = useState<Deployment | null>(null);
  const [error, setError] = useState<unknown>(null);
  // What the live region says when a request ends, and where focus goes then.
  // The button is disabled while the app works, so focus would otherwise
  // fall to the page and stay there. The effect runs after React has drawn
  // the answer and enabled the buttons again.
  const [announcement, setAnnouncement] = useState("");
  const [focusNext, setFocusNext] = useState<{ id: string; at: number } | null>(null);

  useEffect(() => {
    if (focusNext !== null) document.getElementById(focusNext.id)?.focus();
  }, [focusNext]);

  const data = view.data;
  const notOffered = view.error instanceof ApiError && view.error.status === 404;
  const offered = Boolean(data?.available) && !isRecorded;
  const latest = result ?? data?.deployments[0] ?? null;
  const startable =
    offered && data
      ? data.hospitals.filter((hospital) => canStart(hospital) && (!onlyToRestart || hospital.in_consortium))
      : [];
  const nameOf = (siteId: string) =>
    data?.hospitals.find((hospital) => hospital.site_id === siteId)?.display_name ?? siteId;

  const add = async (hospital: JoinableHospital) => {
    setAdding(hospital.site_id);
    setError(null);
    setAnnouncement("");
    try {
      const deployment = await postJson<Deployment>("/api/v1/deployments", { site_id: hospital.site_id });
      setResult(deployment);
      setAnnouncement(
        deployment.status === "joined"
          ? `${hospital.display_name} joined the consortium.`
          : `${hospital.display_name} did not join. The steps below say why.`
      );
      setFocusNext({ id: `attempt-${deployment.deployment_id}`, at: Date.now() });
      if (deployment.status === "joined") onJoined();
    } catch (exc) {
      setError(exc);
      // The error note under the panel gives the reason, as an alert.
      setAnnouncement(`${hospital.display_name} was not added.`);
      setFocusNext({ id: `deploy-${hospital.site_id}`, at: Date.now() });
    } finally {
      setAdding(null);
      view.reload();
    }
  };

  // Without the overview the page shows only an error, and this panel only
  // when a hospital in the consortium has stopped: starting it is the way back.
  if (onlyToRestart && startable.length === 0) return null;

  let reason: string | null = null;
  if (isRecorded) reason = REPLAY_NOTE;
  else if (notOffered) reason = NOT_OFFERED;
  else if (data && !data.available) reason = data.reason;

  return (
    <Panel hook="deploy" eyebrow="While the demo runs" title="Add a hospital">
      <p className="note ds-note">
        A network-mode run on your own machine, started with <code>--deploy</code>, can add a hospital that is not
        in the consortium at start. The app asks the Dagents core service to render the hospital&rsquo;s Compose
        file and starts the hospital&rsquo;s local monitoring agent (LMA), the Dagents process that reads the
        hospital&rsquo;s records from its own store and runs its part of each round. It then asks the global
        monitoring agent (GMA), the Dagents coordinator that runs the rounds, to accept the LMA&rsquo;s address and
        enrol the hospital. The GMA&rsquo;s planner then decides whether the next round selects it.
      </p>
      {reason && (
        <p className="note ds-note" data-role="reason">
          {reason}
        </p>
      )}
      {!notOffered && <ErrorNote error={view.error} />}
      {!data && !view.error && <Loading what="the hospitals that can join" />}

      {data && data.hospitals.length > 0 && (
        <div className="ds-table-scroll">
          <table className="ds-table joinable">
            <thead>
              <tr>
                <th scope="col">Hospital</th>
                <th scope="col">Records</th>
                <th scope="col">In the consortium</th>
                <th scope="col" className="wide-only">
                  Its LMA
                </th>
              </tr>
            </thead>
            <tbody>
              {data.hospitals.map((hospital) => (
                <tr key={hospital.site_id} data-joinable={hospital.site_id}>
                  <td>
                    {hospital.display_name}
                    <span className="cell-sub">{hospital.country}</span>
                  </td>
                  <td>{recordsText(hospital)}</td>
                  <td>{hospital.in_consortium ? "yes" : "no"}</td>
                  <td className="wide-only">{SERVICE_WORDS[hospital.service] ?? hospital.service}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {/* The buttons sit under the table, not in it, so a narrow screen never
          hides one inside the table's scroll box. */}
      {startable.length > 0 && (
        <div className="deploy-actions">
          {startable.map((hospital) => (
            <button
              key={hospital.site_id}
              id={`deploy-${hospital.site_id}`}
              type="button"
              className="ds-btn ds-btn--primary"
              data-action="deploy"
              data-site={hospital.site_id}
              onClick={() => add(hospital)}
              disabled={adding !== null}
            >
              {hospital.in_consortium ? `Start ${hospital.display_name} again` : `Add ${hospital.display_name}`}
            </button>
          ))}
        </div>
      )}
      {/* The agent calls behind the list: the GMA's site-addresses route,
          which the app reads to see whether the GMA still holds a joined
          hospital's address. */}
      {data && (
        <div data-role="view-calls">
          <ApiCalls calls={data.framework_calls} />
        </div>
      )}

      {/* The live region stays on the page, so the end of a request is announced. */}
      <div role="status" data-role="deploy-status">
        {adding ? (
          <p className="note ds-note" data-deploy-running>
            Adding {nameOf(adding)}. The steps appear when the app has finished.
          </p>
        ) : (
          <span className="visually-hidden">{announcement}</span>
        )}
      </div>
      <ErrorNote error={error} />
      {/* While a request runs, the last attempt is hidden: its steps would read as the new one's. */}
      {latest && !adding && <Attempt deployment={latest} name={nameOf(latest.site_id)} />}
    </Panel>
  );
}
