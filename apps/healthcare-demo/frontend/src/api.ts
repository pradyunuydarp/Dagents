/**
 * The demo's transport: calls to a live backend, or a replay of real responses.
 *
 * Locally the page calls the FastAPI backend, which calls the OCaml planner.
 * A published build calls the deployed API when one is configured, and
 * otherwise replays a recording.
 *
 * `scripts/capture_demo_recordings.py` makes the recording. It starts the real
 * backend with `dagentsc` built, sends every request this UI can make,
 * including every combination of the guard controls, and saves the responses.
 * The page names the commit they were recorded from.
 *
 * A request the recording does not hold raises `NotRecordedError`, and the UI
 * says so. The replay never invents an answer.
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
 * The deployed API, when one is configured at build time.
 *
 * When set, the published build calls this API instead of replaying. Empty
 * means the same origin, which is what the local dev proxy serves.
 */
export const apiBase = (import.meta.env.VITE_HEALTHCARE_API_BASE ?? "").replace(/\/$/, "");

/**
 * True when this build replays a recording instead of calling a backend.
 *
 * A configured API takes precedence. There is no fallback from live to
 * recorded: if the API is unreachable, the page reports the outage. A recording
 * from another commit shown as live data would misreport both the data and the
 * framework, and the reader could not tell.
 */
export const isRecorded = import.meta.env.VITE_DAGENTS_STATIC === "1" && apiBase === "";

/**
 * How long a live build keeps trying while the hosted API starts.
 *
 * The API's hosting stops the container after 15 minutes without traffic, so
 * the first request usually has to start it. A measured start took about 22
 * seconds. The page shows a banner while it waits.
 */
const COLD_START_BUDGET_MS = 90_000;

/**
 * Statuses that mean the request never reached the application.
 *
 * The hosting platform returns these while the container is starting. A
 * request that was not delivered had no effect, so retrying it is safe, even
 * for the one POST this UI makes.
 */
const WAKING_STATUSES = new Set([502, 503, 504]);

type WakeListener = (waking: boolean) => void;

const wakeListeners = new Set<WakeListener>();
let waking = false;

/** Subscribe to whether a live request is waiting for the API to start. */
export function onApiWaking(listener: WakeListener): () => void {
  wakeListeners.add(listener);
  listener(waking);
  return () => {
    wakeListeners.delete(listener);
  };
}

function setWaking(next: boolean): void {
  if (next === waking) return;
  waking = next;
  for (const listener of wakeListeners) listener(waking);
}

const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Fetch from the deployed API, waiting while it starts.
 *
 * Only undelivered requests are retried: a network failure or a status in
 * `WAKING_STATUSES`. An application error is returned on the first attempt,
 * because retrying it would hide a real failure.
 */
async function liveFetch(path: string, init?: RequestInit): Promise<Response> {
  const deadline = Date.now() + COLD_START_BUDGET_MS;
  for (let attempt = 1; ; attempt += 1) {
    let response: Response | null = null;
    try {
      response = await fetch(`${apiBase}${path}`, init);
    } catch {
      response = null;
    }
    if (response !== null && !WAKING_STATUSES.has(response.status)) {
      setWaking(false);
      return response;
    }
    if (Date.now() >= deadline) {
      setWaking(false);
      if (response !== null) return response;
      throw new Error(
        `${apiBase || "The API"} did not answer within ` +
          `${Math.round(COLD_START_BUDGET_MS / 1000)} seconds. The backend may be down. ` +
          `Try again in a few minutes.`
      );
    }
    setWaking(true);
    await pause(Math.min(5_000, 1_000 * attempt));
  }
}

/**
 * A stable key for a request, so lookup does not depend on key order.
 *
 * Guard probes differ only by their body, so the body is part of the key.
 * `JSON.stringify` alone would treat `{a,b}` and `{b,a}` as different requests.
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
 * Fetch the recording once and keep it.
 *
 * It lives in `public/` rather than in the bundle, so a live build does not
 * carry it and CI can refresh it just before the static build.
 */
function loadRecording(): Promise<Recording> {
  if (loaded === null) {
    const url = `${import.meta.env.BASE_URL}recording.json`;
    loaded = fetch(url).then((response) => {
      if (!response.ok) {
        throw new Error(
          `This build replays a recorded run, but ${url} could not be loaded ` +
            `(${response.status}). Record it again with scripts/capture_demo_recordings.py.`
        );
      }
      return response.json() as Promise<Recording>;
    });
  }
  return loaded;
}

/** Recording details for the banner. */
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
      `This combination was not recorded.`,
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
  const response = await liveFetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

/** POST a JSON body and read a JSON document back, live or recorded. */
export async function postJson<T>(path: string, body: unknown): Promise<T> {
  if (isRecorded) return replay<T>("POST", path, body);
  const response = await liveFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}
