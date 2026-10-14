/**
 * Facts from the code and captured examples, loaded only on API pages.
 *
 * `docs/reference/api-reference.json` is written by `scripts/api_reference.py`
 * and `docs/reference/api/examples/<service>.json` by
 * `scripts/capture_api_examples.py`. Tests fail when either is stale, so the
 * page shows what the code does. Both are imported lazily: Vite puts them in
 * their own chunks, so the home page and the guide do not download them.
 */

import { useEffect, useState } from "react";

export type Schema = { [key: string]: unknown };

export interface Parameter {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required: boolean;
  description: string | null;
  schema: Schema;
}

export interface ResponseSpec {
  status: string;
  description: string;
  content_type: string | null;
  schema: Schema | null;
}

export interface Call {
  kind: "method" | "function" | "route";
  class: string | null;
  module: string | null;
  file: string | null;
  name: string;
  endpoint?: string | null;
}

export interface EndpointSpec {
  id: string;
  method: string;
  path: string;
  summary: string | null;
  description: string | null;
  status_code: number;
  deprecated: boolean;
  handler: { function: string; module: string | null; file: string | null; calls: Call[] };
  parameters: Parameter[];
  request_body: { required: boolean; content_type: string; schema: Schema } | null;
  responses: ResponseSpec[];
  alias_of: { method: string; path: string; id: string } | null;
}

export interface ServiceSpec {
  name: string;
  language: "python" | "java";
  framework: string;
  role: string;
  port: number;
  title: string;
  version: string | null;
  description: string | null;
  app: { module: string | null; attribute: string | null; file: string | null };
  endpoints: EndpointSpec[];
  schemas: Record<string, Schema>;
}

export interface ObjectSpec {
  service: string;
  class: string;
  name: string;
  module: string;
  file: string | null;
  summary: string | null;
  description: string | null;
  schema: Schema;
  defs: Record<string, Schema>;
}

export interface CommandSpec {
  id: string;
  words: string[];
  usage: string;
  /** The flags the handler reads, from dagentsc.ml. `default` is `-` for standard input. */
  flags: { name: string; default: string | null; repeated: boolean }[];
  handler: { function: string; file: string };
  doc: string | null;
  calls: { module: string; function: string; folder: string | null }[];
  reads: string[];
  writes: string[];
}

export interface Reference {
  endpoint_count: number;
  services: ServiceSpec[];
  objects: ObjectSpec[];
  planner: { name: string; source: string; commands: CommandSpec[] };
}

export interface ExampleRequest {
  method?: string;
  path?: string;
  query?: Record<string, string | number | boolean>;
  headers?: Record<string, string>;
  json?: unknown;
  args?: string[];
  stdin?: unknown;
}

export interface ExampleResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
  trimmed: { pointer: string; note: string }[];
  exit_code?: number;
  stderr?: string;
}

export interface Example {
  id: string;
  endpoint: string;
  title?: string;
  description?: string;
  show?: boolean;
  request: ExampleRequest;
  /** Set when the path holds a value from an earlier example's answer (`{from:<id>:<pointer>}`). */
  request_template?: { path: string };
  response: ExampleResponse;
  planner_calls: string[];
}

export interface ExampleFile {
  service: string;
  note: string | null;
  examples: Example[];
}

export interface ApiData {
  reference: Reference;
  /** Captured examples by service name (`dagentsc` for the planner). */
  examples: Record<string, ExampleFile>;
}

const exampleFiles = import.meta.glob(
  ["../../../docs/reference/api/examples/*.json", "!../../../docs/reference/api/examples/*.requests.json"],
  { import: "default" }
) as Record<string, () => Promise<ExampleFile>>;

let pending: Promise<ApiData> | null = null;

export function loadApiData(): Promise<ApiData> {
  if (!pending) {
    pending = Promise.all([
      import("../../../docs/reference/api-reference.json").then((module) => module.default as unknown as Reference),
      Promise.all(Object.values(exampleFiles).map((load) => load()))
    ]).then(([reference, files]) => ({
      reference,
      examples: Object.fromEntries(files.map((file) => [file.service, file]))
    }));
  }
  return pending;
}

/** The API data, or null while it loads. A load failure is shown, not hidden. */
export function useApiData(): { data: ApiData | null; error: string | null } {
  const [data, setData] = useState<ApiData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    loadApiData().then(
      (loaded) => live && setData(loaded),
      (failure) => live && setError(String(failure))
    );
    return () => {
      live = false;
    };
  }, []);
  return { data, error };
}

export function serviceSpec(data: ApiData, name: string): ServiceSpec | undefined {
  return data.reference.services.find((service) => service.name === name);
}

export function endpointSpec(data: ApiData, service: string, method: string, path: string): EndpointSpec | undefined {
  return serviceSpec(data, service)?.endpoints.find((endpoint) => endpoint.method === method && endpoint.path === path);
}

export function commandSpec(data: ApiData, words: string[]): CommandSpec | undefined {
  const key = words.join(" ");
  return data.reference.planner.commands.find((command) => command.words.join(" ") === key);
}

/** The examples captured for one endpoint key (`METHOD /path` or `dagentsc <words>`). */
export function examplesFor(data: ApiData, service: string, keys: string[]): Example[] {
  const file = data.examples[service];
  if (!file) return [];
  return file.examples.filter((example) => keys.includes(example.endpoint) && example.show !== false);
}

/** Follow a JSON pointer such as `/response/body/manifests/0`. */
export function atPointer(value: unknown, pointer: string): unknown {
  let node = value;
  for (const raw of pointer.replace(/^\//, "").split("/")) {
    if (raw === "") continue;
    const token = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(node)) node = node[Number(token)];
    else if (node && typeof node === "object") node = (node as Record<string, unknown>)[token];
    else return undefined;
  }
  return node;
}
