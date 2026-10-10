/**
 * The demo's transport: calls to a live backend, or a replay of real responses.
 *
 * Locally the page calls the NL2SQL backend, which calls the Dagents planners
 * and the framework services. The published build has no backend, so it
 * replays a recording.
 *
 * `scripts/capture_demo_recordings.py` makes the recording. It starts the
 * backend with `dagentsc` built and the framework services running, sends every
 * request this UI can make, and saves the responses. The page names the commit
 * they were recorded from.
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

/** True when this build replays a recording instead of calling a backend. */
export const isRecorded = import.meta.env.VITE_DAGENTS_STATIC === "1";

/** Live-mode API origin. Empty means the Vite dev proxy. */
const API_BASE = import.meta.env.VITE_NL2SQL_API_BASE ?? "";

/**
 * A stable key for a request, so lookup does not depend on key order.
 *
 * Generate requests differ only by their body, so the body is part of the key.
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
      `This request was not recorded.`,
      alternatives
    );
  }
  if (match.status >= 400) {
    throw new Error(`${match.status} — ${JSON.stringify(match.response)}`);
  }
  return match.response as T;
}

/**
 * Call the backend, live or recorded.
 *
 * Takes a path and an optional `RequestInit`. In a replay, its `method` and
 * `body` select the recorded entry.
 */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const parsed = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
  if (isRecorded) return replay<T>(method, path, parsed);
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init
  });
  if (!response.ok) throw new Error(await response.text());
  return (await response.json()) as T;
}
