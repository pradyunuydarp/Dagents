/**
 * The demo's transport: live against the backend, or a replay of real responses.
 *
 * The demo has two homes. Run locally it talks to the FastAPI backend, which
 * talks to the OCaml planner, which is the only way to see the governance layer
 * actually decide something. Published as a static site it has no backend at
 * all, and the honest options there are a dead page or a replay.
 *
 * It replays. `scripts/capture_demo_recordings.py` starts the real backend with
 * `dagentsc` built, walks every request this UI can make — including the full
 * matrix of guard-lever combinations — and records what came back. So every
 * verdict, strategy and denial in the published demo is one the typed planner
 * really computed, at a commit the page names.
 *
 * What it will not do is invent an answer. A combination that was never
 * captured raises `NotRecordedError`, and the UI says so rather than showing a
 * plausible-looking result. A governance demo that made up a permit would be
 * worse than no demo.
 */

/** One captured request/response pair. */
export interface RecordedEntry {
  method: string;
  path: string;
  request?: unknown;
  status: number;
  response: unknown;
}

/** A capture run: what was recorded, from which commit, and when. */
export interface Recording {
  captured_at: string;
  commit: string;
  planner: string;
  entries: RecordedEntry[];
}

/** Raised when the UI asks for something the capture never covered. */
export class NotRecordedError extends Error {
  readonly alternatives: string[];

  constructor(message: string, alternatives: string[] = []) {
    super(message);
    this.name = "NotRecordedError";
    this.alternatives = alternatives;
  }
}

/**
 * A deployed API to call, when one is configured at build time.
 *
 * With this set the published build stops replaying and talks to the real
 * backend, which is reading its cohorts from the encounter store. Empty means
 * same-origin, which is what the local dev proxy serves.
 */
export const apiBase = (import.meta.env.VITE_HEALTHCARE_API_BASE ?? "").replace(/\/$/, "");

/**
 * True when this build replays a capture instead of calling a backend.
 *
 * A configured API wins over the capture. There is deliberately no fallback
 * from live to recorded: if the deployed backend is unreachable the page says
 * so, because quietly serving a recording from another commit as though it were
 * live would misreport both the data and the framework's behaviour — and the
 * reader would have no way to tell.
 */
export const isRecorded = import.meta.env.VITE_DAGENTS_STATIC === "1" && apiBase === "";

/**
 * Stable key for a request, so lookup does not depend on key order.
 *
 * The guard probe differs only by its body, so the body has to be part of the
 * key — and `JSON.stringify` alone would make `{a,b}` and `{b,a}` different
 * requests.
 */
function keyFor(method: string, path: string, body?: unknown): string {
  return `${method} ${path}${body === undefined ? "" : ` ${stable(body)}`}`;
}

function stable(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, item]) => `${JSON.stringify(k)}:${stable(item)}`).join(",")}}`;
}

let loaded: Promise<Recording> | null = null;

/**
 * Fetch the capture once and memoise it.
 *
 * It lives in `public/` rather than the bundle so a live build does not carry
 * it, and so CI can refresh it just before the static build without rebuilding
 * anything else.
 */
function loadRecording(): Promise<Recording> {
  if (loaded === null) {
    const url = `${import.meta.env.BASE_URL}recording.json`;
    loaded = fetch(url).then((response) => {
      if (!response.ok) {
        throw new Error(
          `This build replays a recorded run, but ${url} could not be loaded ` +
            `(${response.status}). Rebuild with scripts/capture_demo_recordings.py.`
        );
      }
      return response.json() as Promise<Recording>;
    });
  }
  return loaded;
}

/** Metadata for the banner that tells the reader what they are looking at. */
export async function recordingInfo(): Promise<Recording | null> {
  return isRecorded ? loadRecording() : null;
}

async function replay<T>(method: string, path: string, body?: unknown): Promise<T> {
  const capture = await loadRecording();
  const wanted = keyFor(method, path, body);
  const match = capture.entries.find((entry) => keyFor(entry.method, entry.path, entry.request) === wanted);
  if (match === undefined) {
    const alternatives = capture.entries
      .filter((entry) => entry.method === method && entry.path === path)
      .map((entry) => (entry.request === undefined ? entry.path : stable(entry.request)));
    throw new NotRecordedError(
      `This published demo replays a captured run, and that exact request was not captured.`,
      alternatives
    );
  }
  if (match.status >= 400) {
    throw new Error(`${match.status} — ${JSON.stringify(match.response)}`);
  }
  return match.response as T;
}

/** GET a JSON document, live or recorded. */
export async function getJson<T>(path: string): Promise<T> {
  if (isRecorded) return replay<T>("GET", path);
  const response = await fetch(`${apiBase}${path}`);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

/** POST a JSON body and read a JSON document back, live or recorded. */
export async function postJson<T>(path: string, body: unknown): Promise<T> {
  if (isRecorded) return replay<T>("POST", path, body);
  const response = await fetch(`${apiBase}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}
