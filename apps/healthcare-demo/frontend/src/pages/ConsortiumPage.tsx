import type { Recording } from "../api";
import { ApiCalls } from "../components/ApiCalls";
import { ErrorNote, Loading, Panel } from "../components/Basics";
import { ChartGrid } from "../components/Chart";
import { DeployPanel } from "../components/DeployPanel";
import { GuardProbe } from "../components/GuardProbe";
import { HospitalsTable } from "../components/HospitalsTable";
import { PilotPanel } from "../components/PilotPanel";
import { WhoDoesWhat } from "../components/WhoDoesWhat";
import { countWord } from "../format";
import type { ChartList, FrameworkStatus, Overview, Pilot } from "../types";
import { useResource } from "../useResource";

/**
 * The coordinator's view of the consortium.
 *
 * It shows what the hospitals report, the Guard's decisions, the pilot and
 * charts of released aggregates. No patient rows reach this page.
 */
export function ConsortiumPage({
  overview,
  status,
  recording,
  pilot,
  onPilot,
  onModelChange,
  onJoined
}: {
  overview: Overview;
  status: FrameworkStatus | null;
  recording: Recording | null;
  pilot: Pilot | null;
  onPilot: (pilot: Pilot) => void;
  /** The model in use may have changed: after an approval, or after one refused with a 409. */
  onModelChange: () => void;
  /** A hospital joined the consortium: the overview and the status change. */
  onJoined: () => void;
}) {
  // The pilot changes three of the charts, so the list is fetched again after it.
  const charts = useResource<ChartList>(`/api/v1/charts`);
  const reloadCharts = charts.reload;
  const list = charts.data?.charts ?? [];
  const trainingChart = pilot ? list.find((chart) => chart.chart_id === "training-curve") : undefined;
  const gridCharts = list.filter((chart) => !(trainingChart && chart.chart_id === trainingChart.chart_id));
  const hospitals = overview.hospitals.length;

  return (
    <div className="page">
      <header className="page-head">
        <p className="ds-eyebrow">Coordinator view</p>
        <h1>Stroke triage across {countWord(hospitals).toLowerCase()} hospitals</h1>
        <p className="lede ds-measure">
          {countWord(hospitals)} hospitals from the International Stroke Trial train and check a triage model
          together, and each keeps its patient records in its own store. This page is the
          coordinator&rsquo;s view: it shows only what the hospitals report, what their Ethical Guards
          decide, and the result of a federated pilot.
        </p>
        <details className="ds-details intended-use">
          <summary>Intended use and limits</summary>
          <p className="ds-note">{overview.condition.intended_use}</p>
          <ul className="reasons ds-reasons">
            {overview.condition.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </details>
      </header>

      <HospitalsTable overview={overview} status={status} />
      <WhoDoesWhat overview={overview} status={status} />
      <GuardProbe overview={overview} recording={recording} />
      <PilotPanel
        overview={overview}
        pilot={pilot}
        onPilot={(next) => {
          onPilot(next);
          reloadCharts();
        }}
        onModelChange={() => {
          onModelChange();
          reloadCharts();
        }}
        trainingChart={trainingChart}
      />

      <Panel hook="charts" eyebrow="Released aggregates only" title="Consortium charts">
        <p className="note ds-note">
          These charts use only what the hospitals report and the results of the last pilot. A chart
          that needs a pilot says so until one has run.
        </p>
        <ErrorNote error={charts.error} />
        {charts.data ? <ChartGrid charts={gridCharts} /> : !charts.error && <Loading what="the charts" />}
        {charts.data && <ApiCalls calls={charts.data.framework_calls} />}
      </Panel>

      <DeployPanel
        onJoined={() => {
          onJoined();
          reloadCharts();
        }}
      />
    </div>
  );
}
