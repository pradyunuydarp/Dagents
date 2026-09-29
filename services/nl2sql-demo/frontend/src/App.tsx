import { useEffect, useMemo, useState } from "react";
import { Play } from "lucide-react";

import { NotRecordedError, api, isRecorded, recordingInfo, type Recording } from "./api";

type Column = { name: string; dtype: string; description?: string };
type Table = { name: string; columns: Column[] };
type Sample = { sample_id: string; title: string; question: string; tables: Table[] };
type ModelDescriptor = {
  model_id: string;
  label: string;
  adapter_kind: string;
  prompt_format: string;
  notes: string;
};
type TraceStep = { name: string; status: string; detail: string; payload?: unknown };
type GenerateResponse = {
  sql: string;
  model_id: string;
  model_label: string;
  prompt: string;
  schema_ddl: string;
  adapter_kind: string;
  used_fallback: boolean;
  fallback_detail?: string | null;
  dagents_trace: TraceStep[];
};
type ServiceStatus = { name: string; url: string; status: string; detail: string };

function ddlPreview(tables: Table[]): string {
  return tables
    .map((table) => `CREATE TABLE ${table.name} (${table.columns.map((c) => `${c.name} ${c.dtype}`).join(", ")})`)
    .join("\n");
}

export default function App() {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [models, setModels] = useState<ModelDescriptor[]>([]);
  const [services, setServices] = useState<ServiceStatus[]>([]);
  const [sampleId, setSampleId] = useState("");
  const [modelId, setModelId] = useState("");
  const [question, setQuestion] = useState("");
  const [tablesJson, setTablesJson] = useState("[]");
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A replayed build can be asked for a question the capture never covered. It
  // says so rather than showing SQL nobody generated.
  const [notRecorded, setNotRecorded] = useState<NotRecordedError | null>(null);
  const [recording, setRecording] = useState<Recording | null>(null);

  useEffect(() => {
    recordingInfo()
      .then(setRecording)
      .catch((err) => setError(String(err)));
  }, []);

  useEffect(() => {
    Promise.all([
      api<Sample[]>("/api/v1/samples"),
      api<ModelDescriptor[]>("/api/v1/models"),
      api<{ services: ServiceStatus[] }>("/api/v1/dagents/status").catch(() => ({ services: [] })),
    ])
      .then(([samplePayload, modelPayload, statusPayload]) => {
        setSamples(samplePayload);
        setModels(modelPayload);
        setServices(statusPayload.services);
        if (samplePayload[0]) {
          loadSample(samplePayload[0]);
        }
        if (modelPayload[0]) {
          setModelId(modelPayload[0].model_id);
        }
      })
      .catch((err) => setError(String(err)));
  }, []);

  const selectedModel = useMemo(
    () => models.find((model) => model.model_id === modelId),
    [models, modelId]
  );
  const parsedTables = useMemo(() => {
    try {
      return JSON.parse(tablesJson || "[]") as Table[];
    } catch {
      return [] as Table[];
    }
  }, [tablesJson]);

  function loadSample(sample: Sample) {
    setSampleId(sample.sample_id);
    setQuestion(sample.question);
    setTablesJson(JSON.stringify(sample.tables, null, 2));
    setResult(null);
    setError("");
  }

  async function generate() {
    setBusy(true);
    setError("");
    setNotRecorded(null);
    try {
      const tables = JSON.parse(tablesJson) as Table[];
      const payload = await api<GenerateResponse>("/api/v1/generate", {
        method: "POST",
        body: JSON.stringify({ question, tables, model_id: modelId, use_dagents_services: true }),
      });
      setResult(payload);
    } catch (err) {
      if (err instanceof NotRecordedError) {
        setResult(null);
        setNotRecorded(err);
      } else {
        setError(String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  // Read-only views of what the backend returned. Nothing here decides
  // anything: the statuses, the steps and their outcomes are the framework's.
  const traceSteps = result?.dagents_trace ?? [];
  const traceWarnings = traceSteps.filter((step) => step.status === "warning").length;
  const reachable = services.filter((service) => service.status === "ok").length;

  return (
    <div className="ds-shell app">
      <header className="masthead">
        <div className="masthead-id">
          <p className="ds-eyebrow">Dagents / nl2sql-demo</p>
          <h1>Natural language question to SQL</h1>
          <p className="ds-note">
            A schema and a question go in. Dagents validates the source, plans the extraction and
            the pipeline DAG, routes the model and reports every service it touched; this app's
            adapter writes the SQL.
          </p>
        </div>
        <div className="masthead-run">
          <button className="ds-btn ds-btn--primary run-btn" onClick={generate} disabled={busy}>
            <Play size={15} aria-hidden="true" /> {busy ? "Running" : "Generate SQL"}
          </button>
          <span className="ds-eyebrow run-note">
            {busy ? "calling the framework" : isRecorded ? "replays a recorded run" : "calls the live backend"}
          </span>
        </div>
      </header>

      {isRecorded && (
        <div className="ds-banner notice">
          <span className="ds-chip">recorded</span>
          <p>
            <strong>Recorded run.</strong> This published page has no backend. The SQL, the prompt
            and every trace step below came from a live run of this demo against the Dagents
            planners and services
            {recording ? (
              <>
                {" "}at commit <span className="ds-mono">{recording.commit.slice(0, 10)}</span> on{" "}
                {recording.captured_at}
              </>
            ) : null}
            . Edit the question and it will tell you the answer was not captured rather than invent
            one. To generate against a live backend, run it locally — see the README.
          </p>
        </div>
      )}

      {error && (
        <div className="ds-banner ds-banner--deny">
          <span className="ds-chip ds-chip--deny">error</span>
          <p className="ds-mono break">{error}</p>
        </div>
      )}

      {notRecorded && (
        <div className="ds-banner notice notice--warn">
          <span className="ds-chip ds-chip--narrow">not captured</span>
          <p>
            <strong>Not captured.</strong> {notRecorded.message} Pick one of the bundled samples to
            see a generation the framework really produced.
          </p>
        </div>
      )}

      <section className="services" aria-label="Framework service status">
        <div className="services-key">
          <span className="ds-eyebrow">Framework services</span>
          <span className="ds-mono services-count">
            {services.length === 0 ? "no status reported" : `${reachable}/${services.length} reachable`}
          </span>
        </div>
        {services.length === 0 ? (
          <p className="ds-note">
            Nothing has reported a service status yet, so this strip claims none. A run records what
            every service call actually did, in the trace below.
          </p>
        ) : (
          <ul className="service-list">
            {services.map((service) => (
              <li className={`service service--${service.status}`} key={service.name} title={service.detail}>
                <span className="service-mark" aria-hidden="true" />
                <span className="ds-mono service-name">{service.name}</span>
                <span className="service-state">
                  {service.status === "ok" ? "reachable" : service.status}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="workspace">
        <section className="ds-panel controls">
          <div className="ds-panel-head">
            <h2>Question and schema</h2>
          </div>
          <div className="ds-panel-body">
            <label className="ds-field">
              <span>Sample</span>
              <select
                className="ds-input"
                value={sampleId}
                onChange={(event) => {
                  const next = samples.find((sample) => sample.sample_id === event.target.value);
                  if (next) loadSample(next);
                }}
              >
                {samples.map((sample) => (
                  <option key={sample.sample_id} value={sample.sample_id}>{sample.title}</option>
                ))}
              </select>
            </label>
            <label className="ds-field">
              <span>Model adapter</span>
              <select className="ds-input" value={modelId} onChange={(event) => setModelId(event.target.value)}>
                {models.map((model) => (
                  <option key={model.model_id} value={model.model_id}>{model.label}</option>
                ))}
              </select>
            </label>
            {selectedModel && (
              <p className="ds-note adapter-note">
                <span className="ds-mono">{selectedModel.adapter_kind}</span> — {selectedModel.notes}
              </p>
            )}
            <label className="ds-field">
              <span>Natural language question</span>
              <textarea
                className="ds-input question"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                rows={4}
              />
            </label>
            <label className="ds-field">
              <span>Schema tables (JSON)</span>
              <textarea
                className="ds-input schema"
                value={tablesJson}
                onChange={(event) => setTablesJson(event.target.value)}
                rows={12}
                spellCheck={false}
              />
            </label>
          </div>
        </section>

        <div className="outputs">
          <section className="ds-panel sql-panel">
            <div className="ds-panel-head">
              <h2>Generated SQL</h2>
              {result ? (
                <p className="run-facts">
                  <span className="ds-mono">{result.model_id}</span>
                  <span className="ds-chip">{result.adapter_kind}</span>
                  {result.used_fallback && <span className="ds-chip ds-chip--narrow">fallback</span>}
                </p>
              ) : null}
            </div>
            <div className="ds-panel-body">
              {result?.used_fallback && (
                <div className="ds-banner fallback">
                  <span className="ds-chip ds-chip--narrow">fallback</span>
                  <p>
                    Model adapter fallback generated this SQL.
                    {result.fallback_detail ? ` Detail: ${result.fallback_detail}` : ""}
                  </p>
                </div>
              )}
              {result ? (
                <pre className="ds-code sql">{result.sql}</pre>
              ) : (
                <p className="ds-note placeholder">
                  No SQL yet. Pick a sample and press Generate SQL — the statement the adapter
                  returns appears here.
                </p>
              )}

              <section className="sub">
                <h3 className="ds-eyebrow">DDL the adapter was given</h3>
                <pre className="ds-code">{result?.schema_ddl ?? ddlPreview(parsedTables)}</pre>
                {!result && (
                  <p className="ds-note">
                    Rendered from the schema JSON on the left. After a run this is the DDL the
                    backend built.
                  </p>
                )}
              </section>

              <section className="sub">
                <h3 className="ds-eyebrow">Prompt sent to the adapter</h3>
                {result ? (
                  <pre className="ds-code">{result.prompt}</pre>
                ) : (
                  <p className="ds-note">The prompt appears here after a run.</p>
                )}
              </section>
            </div>
          </section>

          <section className="ds-panel trace-panel">
            <div className="ds-panel-head">
              <h2>Dagents trace</h2>
              <p className="trace-meta ds-mono">
                {traceSteps.length === 0
                  ? "no run yet"
                  : `${traceSteps.length} steps · ${traceWarnings} warning${traceWarnings === 1 ? "" : "s"}`}
              </p>
            </div>
            <div className="ds-panel-body">
              {traceSteps.length === 0 ? (
                <p className="ds-note placeholder">
                  A run records each framework step in order: SourceSpec validation, extraction
                  planning, the schema contract, quality rules, pipeline DAG planning, model
                  routing, then the calls to the core, pipeline and model services and to the LMA
                  and GMA.
                </p>
              ) : (
                <ol className="trace">
                  {traceSteps.map((step, index) => (
                    <li className={`step step--${step.status}`} key={step.name}>
                      <div className="step-head">
                        <span className="ds-mono step-index">{String(index + 1).padStart(2, "0")}</span>
                        <h4 className="step-name">{step.name}</h4>
                        <span
                          className={`ds-chip ${step.status === "warning" ? "ds-chip--narrow" : "ds-chip--permit"}`}
                        >
                          {step.status}
                        </span>
                      </div>
                      <p className="step-detail">{step.detail}</p>
                      {step.payload !== undefined && (
                        <details className="ds-details step-payload">
                          <summary>payload</summary>
                          <pre className="ds-code">{JSON.stringify(step.payload, null, 2)}</pre>
                        </details>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
