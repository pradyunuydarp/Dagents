import { sourceKindText } from "../format";
import type { FrameworkStatus, Overview } from "../types";
import { ApiCalls } from "./ApiCalls";
import { Panel } from "./Basics";

/** Where the agents run, in words, from the mode the backend reports. */
function modeText(mode: string | null): string {
  if (mode === "in-process") {
    return "Inside the demo server. The app calls each agent's routes without a network socket.";
  }
  if (mode === "network") return "Each agent runs in its own process. The app calls them over HTTP.";
  return "The demo server did not report it.";
}

/** Where one agent runs, for a table cell. */
function runsText(mode: string | null): string {
  if (mode === "in-process") return "Inside the demo server";
  if (mode === "network") return "Its own process";
  return "Not reported";
}

/**
 * Who does what: the demo app, the study's GMA and each hospital's LMA.
 *
 * The mode, the round engine and each LMA's store come from
 * `/api/v1/framework/status`, which asks the GMA. The descriptions say what
 * each part is for; they hold no result.
 */
export function WhoDoesWhat({ overview, status }: { overview: Overview; status: FrameworkStatus | null }) {
  const agents = status?.agents ?? null;
  const mode = agents?.mode ?? null;
  const runs = runsText(mode);
  const lmas = overview.hospitals.map((hospital) => ({
    hospital,
    reach: agents?.lmas?.find((item) => item.site_id === hospital.site_id) ?? null
  }));

  return (
    <Panel hook="who-does-what" eyebrow="The Dagents agents behind this demo" title="Who does what">
      <p className="note ds-note">
        Each hospital has a Dagents LMA (local monitoring agent). The study has one GMA (global monitoring
        agent), which runs the federated rounds. The demo app holds the stroke domain and calls the agents
        only through their HTTP APIs. Both agents ask the dagentsc planner for every decision.
      </p>
      <dl className="ds-facts">
        <div>
          <dt>Where the agents run</dt>
          <dd className="plain" data-agents-mode={mode ?? "unknown"}>
            {modeText(mode)}
          </dd>
        </div>
        <div>
          <dt>GMA round engine</dt>
          <dd>{agents?.engine ?? "not reported"}</dd>
        </div>
      </dl>
      <div className="ds-table-scroll">
        <table className="ds-table who-table">
          <thead>
            <tr>
              <th scope="col">Part</th>
              <th scope="col" className="wide-only">
                Runs
              </th>
              <th scope="col">What it does</th>
              <th scope="col" className="wide-only">
                What it reads
              </th>
            </tr>
          </thead>
          <tbody>
            <tr data-agent="app">
              <td>
                Demo app
                <span className="cell-sub">the stroke domain</span>
                <span className="cell-sub narrow-only">The demo server</span>
              </td>
              <td className="wide-only">The demo server</td>
              <td className="who-does">
                Stores each hospital&rsquo;s records and new entries, ranks the worklist, draws the charts and
                runs the study&rsquo;s rounds in order through the GMA.
              </td>
              <td className="wide-only">
                {status?.cohort_source === "postgres"
                  ? "Each hospital's Postgres schema, acting as that hospital's role"
                  : status?.cohort_source === "in-memory"
                    ? "Each hospital's store in memory"
                    : "Each hospital's store"}
              </td>
            </tr>
            <tr data-agent="gma">
              <td>
                <span className="mono">GMA</span>
                <span className="cell-sub">for the study</span>
                <span className="cell-sub narrow-only">{runs}</span>
              </td>
              <td className="wide-only">{runs}</td>
              <td className="who-does">
                Enrols the LMAs and sends them each round&rsquo;s job. Combines their clipped updates into a
                candidate model with the averaging rule the app contributes. Keeps the model versions, runs
                the release Guard and gates, and records approvals.
              </td>
              <td className="wide-only">What the LMAs report. No patient row.</td>
            </tr>
            {lmas.map(({ hospital, reach }) => (
              <tr key={hospital.site_id} data-agent="lma" data-site={hospital.site_id}>
                <td>
                  <span className="mono">LMA {hospital.site_id}</span>
                  <span className="cell-sub">{hospital.display_name}</span>
                  {reach && !reach.reachable && <span className="cell-sub">Did not answer the GMA.</span>}
                  <span className="cell-sub narrow-only">{runs}</span>
                </td>
                <td className="wide-only">{runs}</td>
                <td className="who-does">
                  Runs the hospital&rsquo;s Guard and audit log, keeps its running statistics, measures the
                  model in use and runs the round jobs it accepts.
                </td>
                <td className="wide-only">
                  {reach?.source_kind
                    ? `The hospital's records, from ${sourceKindText(reach.source_kind)}`
                    : "Not reported"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ApiCalls calls={status?.framework_calls} />
    </Panel>
  );
}
