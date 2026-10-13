/**
 * The demo's transport: calls to a live backend, or a replay of real responses.
 *
 * Locally the page calls the FastAPI backend, which calls the OCaml planner.
 * A published build calls the deployed API when one is configured, and
 * otherwise replays a recording.
 *
 * `scripts/capture_demo_recordings.py` makes the recording. It starts the real
 * backend with `dagentsc` built, sends every GET this UI makes (both themes for
 * each chart), the guard probes for every hospital and a set of cohort sizes,
 * and one pilot, and saves the responses. The page names the commit they were
 * recorded from.
 *
 * A request the recording does not hold raises `NotRecordedError`, and the UI
 * says so. The replay never invents an answer, and it never stores anything:
 * data entry, outcomes and approvals are disabled in replay mode.
 *
 * Nothing in this module puts the API's address into an error message. The
 * page never shows it.
 */

/** One captured request/response pair. */
export interface RecordedEntry {
  method: string;
  path: string;
  request?: unknown;
  status: number;
  /** `before_pilot` for what the page shows on arrival, `after_pilot` for what the pilot changed. */
  phase?: string;
  content_type?: string;
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
 * The backend answered with an error status.
 *
 * Every error body from the healthcare API is `{"detail": str}`, and a 422 adds
 * `"errors": {field: reason}`, so a form can show each reason next to its field.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly detail: string | null;
  readonly fieldErrors: Record<string, string>;

  constructor(status: number, body: unknown) {
    const detail = readDetail(body);
    super(detail ?? `The demo server returned an error (${status}).`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.detail = detail;
    this.fieldErrors = readFieldErrors(body);
  }
}

/** The server did not answer at all within the cold-start budget. */
export class UnreachableError extends Error {
  constructor() {
    super(
      `The demo server did not answer within ${Math.round(COLD_START_BUDGET_MS / 1000)} seconds. ` +
        "It may be down. Try again in a few minutes."
    );
    this.name = "UnreachableError";
  }
}

function readDetail(body: unknown): string | null {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return null;
}

function readFieldErrors(body: unknown): Record<string, string> {
  if (body && typeof body === "object" && "errors" in body) {
    const errors = (body as { errors: unknown }).errors;
    if (errors && typeof errors === "object") {
      const out: Record<string, string> = {};
      for (const [field, reason] of Object.entries(errors as Record<string, unknown>)) {
        out[field] = String(reason);
      }
      return out;
    }
  }
  return {};
}

/**
 * A plain sentence for any error this module raises.
 *
 * The backend's own messages are already written for people, so they are shown
 * as they are. Anything else gets a generic sentence without technical detail.
 */
export function userMessage(error: unknown): string {
  if (error instanceof NotRecordedError) {
    return "This page replays a recorded run, and this request was not recorded.";
  }
  if (error instanceof ApiError || error instanceof UnreachableError) return error.message;
  if (error instanceof RecordingUnavailableError) return error.message;
  return "Something went wrong while loading this part of the page. Reload the page to try again.";
}

/**
 * The deployed API, when one is configured at build time.
 *
 * When set, the published build calls this API instead of replaying. Empty
 * means the same origin, which is what the local dev proxy serves. It is kept
 * inside this module so no component can print it.
 */
const apiBase = (import.meta.env.VITE_HEALTHCARE_API_BASE ?? "").replace(/\/$/, "");

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
 * The hosted API stops after 15 minutes without traffic, so the first request
 * usually has to start it. A measured start took about 22 seconds, which is
 * what the connecting banner tells the reader.
 * `apps/healthcare-demo/tests/test_deployed_api.py` uses the same budget.
 */
export const COLD_START_BUDGET_MS = 90_000;

/**
 * Statuses the hosting platform returns while the container is starting.
 *
 * A request the platform answered never reached the application and had no
 * effect, so retrying it is safe, even for a POST. Data entry also sends an
 * Idempotency-Key, so a retried entry is stored once.
 */
const WAKING_STATUSES = new Set([502, 503, 504]);

/**
 * The header the API puts on every response it sends itself.
 *
 * The API also answers 503 when a hospital's records cannot be reached. That
 * response has this header and the platform's responses do not, so it is shown
 * at once instead of being retried.
 */
const APP_HEADER = "X-Dagents-App";

/** True when the platform, not the app, answered: a waking status without the app's header. */
function answeredByPlatform(response: Response): boolean {
  return WAKING_STATUSES.has(response.status) && !response.headers.has(APP_HEADER);
}

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
 * Fetch from the API, waiting while it starts.
 *
 * Only undelivered requests are retried: a network failure, or a status in
 * `WAKING_STATUSES` without the `APP_HEADER` the API puts on its own responses.
 * Every response from the app is returned on the first attempt, including its
 * own 503, because retrying it would hide a real failure.
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
    if (response !== null && !answeredByPlatform(response)) {
      setWaking(false);
      return response;
    }
    if (Date.now() >= deadline) {
      setWaking(false);
      if (response !== null) return response;
      throw new UnreachableError();
    }
    setWaking(true);
    await pause(Math.min(5_000, 1_000 * attempt));
  }
}

async function readError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return new ApiError(response.status, body);
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

/** The recording could not be fetched. */
class RecordingUnavailableError extends Error {
  constructor() {
    super("This page replays a recorded run, but the recording could not be loaded. Reload the page to try again.");
    this.name = "RecordingUnavailableError";
  }
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
      if (!response.ok) throw new RecordingUnavailableError();
      return response.json() as Promise<Recording>;
    });
    loaded.catch(() => {
      loaded = null;
    });
  }
  return loaded;
}

/** Recording details for the banner. */
export async function recordingInfo(): Promise<Recording | null> {
  return isRecorded ? loadRecording() : null;
}

/**
 * Whether the replay has played the pilot.
 *
 * Some GETs were recorded twice: before the pilot and after it (the consortium
 * charts, the models, each hospital's dashboard and audit log). Once the pilot
 * has been replayed, the page shows the answers recorded after it.
 */
let pilotReplayed = false;

function findEntry(capture: Recording, wanted: string): RecordedEntry | undefined {
  const matches = capture.entries.filter((entry) => keyFor(entry.method, entry.path, entry.request) === wanted);
  if (pilotReplayed) {
    const after = matches.find((entry) => entry.phase === "after_pilot");
    if (after) return after;
  }
  return matches.find((entry) => entry.phase !== "after_pilot") ?? matches[0];
}

async function replay(method: string, path: string, body?: unknown): Promise<RecordedEntry> {
  const capture = await loadRecording();
  const match = findEntry(capture, keyFor(method, path, body));
  if (match === undefined) {
    const alternatives = capture.entries
      .filter((entry) => entry.method === method && entry.path === path)
      .map((entry) => (entry.request === undefined ? entry.path : stable(entry.request)));
    throw new NotRecordedError("This combination was not recorded.", alternatives);
  }
  if (match.status >= 400) throw new ApiError(match.status, match.response);
  if (method === "POST" && path === "/api/v1/pilot:run") pilotReplayed = true;
  return match;
}

/** GET a JSON document, live or recorded. */
export async function getJson<T>(path: string): Promise<T> {
  if (isRecorded) return (await replay("GET", path)).response as T;
  const response = await liveFetch(path);
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

/**
 * POST a JSON body and read a JSON document back, live or recorded.
 *
 * `headers` carries extra request headers, such as the Idempotency-Key of a
 * data entry. A replay matches on the path and body only.
 */
export async function postJson<T>(path: string, body: unknown, headers: Record<string, string> = {}): Promise<T> {
  if (isRecorded) return (await replay("POST", path, body)).response as T;
  const response = await liveFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

/**
 * GET a chart as SVG text, live or recorded.
 *
 * Charts go through the same transport as the JSON calls, so they get the
 * cold-start wait and the replay. A plain `<img src>` pointed at a sleeping
 * API would get a 502 and log an error instead.
 */
export async function getSvg(path: string): Promise<string> {
  if (isRecorded) {
    const entry = await replay("GET", path);
    const response = entry.response as { text?: unknown } | null;
    if (!response || typeof response.text !== "string") {
      throw new NotRecordedError("This chart was not recorded as an image.");
    }
    return response.text;
  }
  const response = await liveFetch(path, { headers: { Accept: "image/svg+xml" } });
  if (!response.ok) throw await readError(response);
  return response.text();
}

/**
 * A new idempotency key for one data-entry attempt.
 *
 * `crypto.randomUUID` needs a secure context. Local pages on 127.0.0.1 and the
 * published https page have one; the fallback covers anything else.
 */
export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
