import { useEffect, useMemo, useState } from "react";
import { Activity, Database, Play, Server, ShieldCheck, Wand2 } from "lucide-react";

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
  dagents_trace: TraceStep[];
};
type ServiceStatus = { name: string; url: string; status: string; detail: string };

const API_BASE = import.meta.env.VITE_NL2SQL_API_BASE ?? "";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

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
    try {
      const tables = JSON.parse(tablesJson) as Table[];
      const payload = await api<GenerateResponse>("/api/v1/generate", {
        method: "POST",
        body: JSON.stringify({ question, tables, model_id: modelId, use_dagents_services: true }),
      });
      setResult(payload);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Dagents NL2SQL Demo</h1>
          <p>Schema + natural language query to SQL, orchestrated through Dagents planners and services.</p>
        </div>
        <button className="primary" onClick={generate} disabled={busy}>
          <Play size={18} /> {busy ? "Running" : "Generate SQL"}
        </button>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="status-strip">
        {services.map((service) => (
          <div className={`status ${service.status}`} key={service.name} title={service.detail}>
            <Server size={16} />
            <span>{service.name}</span>
          </div>
        ))}
      </section>

      <section className="grid">
        <div className="panel controls">
          <h2><Database size={18} /> Inputs</h2>
          <label>
            Sample
            <select
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
          <label>
            Model adapter
            <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
              {models.map((model) => (
                <option key={model.model_id} value={model.model_id}>{model.label}</option>
              ))}
            </select>
          </label>
          {selectedModel && <p className="hint">{selectedModel.adapter_kind}: {selectedModel.notes}</p>}
          <label>
            Natural language question
            <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} />
          </label>
          <label>
            Schema tables JSON
            <textarea className="mono" value={tablesJson} onChange={(event) => setTablesJson(event.target.value)} rows={14} />
          </label>
        </div>

        <div className="panel">
          <h2><Wand2 size={18} /> Generated SQL</h2>
          <pre className="sql">{result?.sql ?? "Run a sample to generate SQL."}</pre>
          {result?.used_fallback && <div className="warning">Model artifact was unavailable; deterministic Dagents demo fallback generated this SQL.</div>}
          <h3>DDL Context</h3>
          <pre>{result?.schema_ddl ?? ddlPreview(parsedTables)}</pre>
          <h3>Prompt Sent To Adapter</h3>
          <pre>{result?.prompt ?? "Prompt will appear after generation."}</pre>
        </div>

        <div className="panel trace">
          <h2><ShieldCheck size={18} /> Dagents Trace</h2>
          {(result?.dagents_trace ?? []).map((step) => (
            <details key={step.name} open>
              <summary><Activity size={16} /> {step.name} <span className={step.status}>{step.status}</span></summary>
              <p>{step.detail}</p>
              {step.payload !== undefined && <pre>{JSON.stringify(step.payload, null, 2)}</pre>}
            </details>
          ))}
          {!result && <p className="hint">The trace will show SourceSpec validation, extraction planning, schema checks, quality rules, pipeline planning, model routing, and Dagents service status.</p>}
        </div>
      </section>
    </main>
  );
}
