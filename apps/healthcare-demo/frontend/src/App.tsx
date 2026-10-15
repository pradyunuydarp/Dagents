import { useCallback, useEffect, useState } from "react";

import { COLD_START_BUDGET_MS, getJson, isRecorded, onApiWaking, recordingInfo, userMessage, type Recording } from "./api";
import { ErrorNote, Linked, Loading, ShownError } from "./components/Basics";
import { README_URL } from "./config";
import { sourceText } from "./format";
import { FieldLabelsProvider } from "./labels";
import { ConsortiumPage } from "./pages/ConsortiumPage";
import { EntryPage } from "./pages/EntryPage";
import { HospitalPage } from "./pages/HospitalPage";
import { NotFound } from "./pages/NotFound";
import { href, useRoute } from "./router";
import { ThemeToggle } from "./ThemeToggle";
import type { FrameworkStatus, Overview, Pilot } from "./types";

/**
 * The banner for stores that are not ready, or null when every store holds
 * records. The backend decides readiness (`/api/v1/framework/status`); this
 * only words it, naming the hospitals whose store is empty.
 */
function notReadyText(status: FrameworkStatus | null, overview: Overview | null): string | null {
  if (!status || status.ready !== false) return null;
  if (!status.hospitals) {
    return "The hospital records are not loaded yet: the demo server cannot read the hospitals' stores. Try again in a minute.";
  }
  const empty = status.hospitals
    .filter((item) => item.record_count === 0)
    .map((item) => overview?.hospitals.find((hospital) => hospital.site_id === item.site_id)?.display_name ?? "One hospital");
  if (empty.length === 0) return "The hospital records are not loaded yet.";
  const names = empty.length === 1 ? empty[0] : `${empty.slice(0, -1).join(", ")} and ${empty[empty.length - 1]}`;
  return `The hospital records are not loaded yet. ${names} ${empty.length === 1 ? "has" : "have"} no records in ${
    empty.length === 1 ? "its store" : "their stores"
  }, so ${empty.length === 1 ? "its page shows" : "their pages show"} no numbers.`;
}

/**
 * The demo's shell: the header, the pages and the footer.
 *
 * Pages live in the URL hash, because GitHub Pages has no server-side routing:
 *
 * - `#/` the coordinator's view of the consortium
 * - `#/hospitals/<site_id>` what one hospital's staff see
 * - `#/hospitals/<site_id>/new` data entry at that hospital
 *
 * The hospital ids and names come from `/api/v1/overview`; none is written
 * here. Every number and decision on the pages comes from the backend.
 */
export default function App() {
  const route = useRoute();
  const [section, siteId, sub] = route.segments;

  const [overview, setOverview] = useState<Overview | null>(null);
  const [overviewError, setOverviewError] = useState<unknown>(null);
  const [status, setStatus] = useState<FrameworkStatus | null>(null);
  const [recording, setRecording] = useState<Recording | null>(null);
  const [recordingError, setRecordingError] = useState<unknown>(null);
  const [waking, setWaking] = useState(false);
  // The last pilot run from this page. It stays when you move between pages.
  const [pilot, setPilot] = useState<Pilot | null>(null);

  const refreshOverview = useCallback(() => {
    getJson<Overview>("/api/v1/overview")
      .then((data) => {
        setOverview(data);
        setOverviewError(null);
      })
      .catch(setOverviewError);
    // The status gives the data source line and whether the stores hold
    // records. A failure only leaves both out; the data calls report an
    // outage themselves.
    getJson<FrameworkStatus>("/api/v1/framework/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => onApiWaking(setWaking), []);
  useEffect(refreshOverview, [refreshOverview]);
  useEffect(() => {
    recordingInfo().then(setRecording).catch(setRecordingError);
  }, []);

  const hospital = overview?.hospitals.find((item) => item.site_id === siteId);

  useEffect(() => {
    const product = "Stroke triage demo";
    let title = product;
    if (!section) title = `Consortium · ${product}`;
    else if (section === "hospitals" && siteId) {
      const name = hospital?.display_name ?? "Hospital";
      title = sub === "new" ? `Add a record · ${name} · ${product}` : `${name} · ${product}`;
    }
    document.title = title;
  }, [section, siteId, sub, hospital]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [section, siteId, sub]);

  let page;
  if (!section) {
    page = overview ? (
      <ConsortiumPage
        overview={overview}
        status={status}
        recording={recording}
        pilot={pilot}
        onPilot={setPilot}
        onApproved={refreshOverview}
        onJoined={refreshOverview}
      />
    ) : (
      !overviewError && <Loading what="the consortium" />
    );
  } else if (section === "hospitals" && siteId && (sub === undefined || sub === "new")) {
    if (overview && !hospital) {
      page = <NotFound what={`There is no hospital with the id ${siteId}.`} />;
    } else if (sub === "new") {
      page = <EntryPage key={siteId} siteId={siteId} overview={overview} onSaved={refreshOverview} />;
    } else {
      page = <HospitalPage key={siteId} siteId={siteId} overview={overview} onChanged={refreshOverview} />;
    }
  } else {
    page = <NotFound />;
  }

  const dataset = overview?.dataset ?? status?.dataset ?? null;
  const shellError = recordingError ?? overviewError;
  const notReady = notReadyText(status, overview);

  return (
    <>
      <header className="masthead">
        <div className="ds-shell masthead-inner">
          <a className="wordmark" href={href("")}>
            Stroke triage demo
            <span className="wordmark-sub">Dagents</span>
          </a>
          <nav aria-label="Pages">
            <a href={href("")} aria-current={!section ? "page" : undefined}>
              Consortium
            </a>
            {overview?.hospitals.map((item) => (
              <a
                key={item.site_id}
                href={href(`hospitals/${item.site_id}`)}
                aria-current={section === "hospitals" && siteId === item.site_id ? "page" : undefined}
              >
                {item.display_name}
              </a>
            ))}
          </nav>
          <ThemeToggle />
        </div>
      </header>

      <div className="ds-shell banners">
        {isRecorded && (
          <div className="banner recorded" data-banner="recorded">
            <strong>Recorded run.</strong> This page replays answers recorded from the real backend
            {recording ? (
              <>
                {" "}
                at commit <span className="mono">{recording.commit.slice(0, 10)}</span> on{" "}
                {recording.captured_at.slice(0, 10)}
              </>
            ) : null}
            . It cannot store new records, record outcomes or approve a model. To try those, run the demo
            locally as the <a href={README_URL}>README</a> describes.
          </div>
        )}
        {waking && (
          <div className="banner" data-banner="connecting" role="status">
            Connecting to the demo server. The first request usually takes about 20 seconds. The page
            stops waiting after {Math.round(COLD_START_BUDGET_MS / 1000)} seconds.
          </div>
        )}
        {notReady && !shellError && (
          <div className="banner" data-banner="not-ready" role="status">
            {notReady}
          </div>
        )}
        <ErrorNote error={shellError} />
      </div>

      <ShownError.Provider value={shellError ? userMessage(shellError) : null}>
        <FieldLabelsProvider value={overview?.field_labels ?? null}>
          <main className="ds-shell main">{page}</main>
        </FieldLabelsProvider>
      </ShownError.Provider>

      <footer className="foot">
        <div className="ds-shell foot-inner">
          {dataset && (
            <>
              <p>
                <Linked text={dataset.notice} />
              </p>
              <p>{dataset.credit}</p>
            </>
          )}
          {status && <p>Records: {sourceText(status.cohort_source)}</p>}
          <p>
            <strong>Not a medical device.</strong> The models here are demonstrations, not validated
            clinical tools, and nothing on this page is medical advice.
          </p>
        </div>
      </footer>
    </>
  );
}
