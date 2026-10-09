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
 * How long a live build keeps trying while the hosted API wakes up.
 *
 * The API is deployed on a free tier that stops the container after a quarter
 * of an hour of inactivity, so most visitors arrive at a sleeping service and
 * the first request is the one that wakes it. Thirty seconds of apparent
 * nothing followed by an error would read as a broken demo; this waits, and the
 * page says what it is waiting for.
 */
const COLD_START_BUDGET_MS = 90_000;

/**
 * Statuses that mean the request never reached the application.
 *
 * The platform's edge answers these while the container behind it is starting.
 * Separating them from an application error is what makes retrying safe: a
 * request that was not delivered cannot have had an effect, so the one POST
 * this UI makes is not at risk of running twice.
 */
const WAKING_STATUSES = new Set([502, 503, 504]);

type WakeListener = (waking: boolean) => void;

const wakeListeners = new Set<WakeListener>();
let waking = false;

/**
 * Subscribe to whether a live request is currently waiting on a cold start.
 *
 * The transport knows this and the UI has to say it, so it is published rather
 * than inferred from a slow promise.
 */
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
 * Fetch from the deployed API, waiting out a cold start.
 *
 * Only the two undelivered cases are retried — a network-level failure and a
 * gateway status from `WAKING_STATUSES`. An application error is returned to
 * the caller on the first attempt, because retrying it would hide a real
 * failure behind a minute and a half of patience.
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
          `${Math.round(COLD_START_BUDGET_MS / 1000)}s. The hosted backend may be down; ` +
          `nothing is being shown from a cache, because a recording from another commit ` +
          `presented as live data would misreport both the data and the framework.`
      );
    }
    setWaking(true);
    await pause(Math.min(5_000, 1_000 * attempt));
  }
}

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
