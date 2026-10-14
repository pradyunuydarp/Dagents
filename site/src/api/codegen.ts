/**
 * Code samples, written from a captured request. The response shown next to
 * them belongs to that same request, so the code and the answer match.
 */

import type { ExampleRequest, ExampleResponse } from "./data";

export interface CodeSample {
  label: string;
  code: string;
}

function queryString(query: ExampleRequest["query"]): string {
  if (!query || !Object.keys(query).length) return "";
  const params = new URLSearchParams(Object.entries(query).map(([key, value]) => [key, String(value)]));
  return `?${params.toString()}`;
}

function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

/** A JSON value as a Python literal, indented like `json.dumps(indent=4)`. */
export function pythonLiteral(value: unknown, indent = 0): string {
  const pad = " ".repeat(indent);
  const inner = " ".repeat(indent + 4);
  if (value === null) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    return `[\n${value.map((item) => `${inner}${pythonLiteral(item, indent + 4)}`).join(",\n")},\n${pad}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return "{}";
  return `{\n${entries
    .map(([key, item]) => `${inner}${JSON.stringify(key)}: ${pythonLiteral(item, indent + 4)}`)
    .join(",\n")},\n${pad}}`;
}

/** How the samples read the answer: as JSON, as text (SVG, plain text), or not at all (304, 204). */
function bodyKind(response: ExampleResponse | undefined): "json" | "text" | "none" {
  if (!response) return "json";
  const status = String(response.status);
  if (status === "304" || status === "204") return "none";
  const type = response.headers["content-type"] ?? "";
  return !type || /[/+]json\b/.test(type) ? "json" : "text";
}

export function httpSamples(base: string, request: ExampleRequest, response?: ExampleResponse): CodeSample[] {
  const kind = bodyKind(response);
  const method = request.method ?? "GET";
  const url = `${base}${request.path ?? "/"}${queryString(request.query)}`;
  const headers: Record<string, string> = { ...(request.headers ?? {}) };
  const hasBody = request.json !== undefined;
  const body = hasBody ? JSON.stringify(request.json, null, 2) : "";

  const curl = [`curl -s${method === "GET" ? "" : ` -X ${method}`} "${url}"`];
  if (hasBody) curl.push(`  -H "Content-Type: application/json"`);
  for (const [name, value] of Object.entries(headers)) curl.push(`  -H ${shellQuote(`${name}: ${value}`)}`);
  if (hasBody) curl.push(`  -d ${shellQuote(body)}`);

  const pythonArgs = [`    ${JSON.stringify(`${base}${request.path ?? "/"}`)}`];
  if (request.query && Object.keys(request.query).length) pythonArgs.push(`    params=${pythonLiteral(request.query, 4)}`);
  if (Object.keys(headers).length) pythonArgs.push(`    headers=${pythonLiteral(headers, 4)}`);
  if (hasBody) pythonArgs.push(`    json=${pythonLiteral(request.json, 4)}`);
  const python = [
    "import httpx",
    "",
    `response = httpx.${method.toLowerCase()}(`,
    `${pythonArgs.join(",\n")},`,
    ")",
    kind === "json"
      ? "print(response.status_code, response.json())"
      : kind === "text"
        ? "print(response.status_code, response.text)"
        : "print(response.status_code)  # no body"
  ].join("\n");

  const fetchHeaders = { ...(hasBody ? { "Content-Type": "application/json" } : {}), ...headers };
  const options: string[] = [];
  if (method !== "GET") options.push(`  method: "${method}"`);
  if (Object.keys(fetchHeaders).length) options.push(`  headers: ${JSON.stringify(fetchHeaders)}`);
  if (hasBody) options.push(`  body: JSON.stringify(${JSON.stringify(request.json, null, 2).replace(/\n/g, "\n  ")})`);
  const javascript = [
    `const response = await fetch(${JSON.stringify(url)}${options.length ? `, {\n${options.join(",\n")}\n}` : ""});`,
    kind === "json"
      ? "console.log(response.status, await response.json());"
      : kind === "text"
        ? "console.log(response.status, await response.text());"
        : "console.log(response.status); // no body"
  ].join("\n");

  return [
    { label: "curl", code: curl.join(" \\\n") },
    { label: "Python", code: python },
    { label: "JavaScript", code: javascript }
  ];
}

export function commandSamples(request: ExampleRequest, response?: ExampleResponse): CodeSample[] {
  const args = request.args ?? [];
  const stdin = request.stdin;
  // Exit code 2 is a refused request: read stderr instead of raising. Text and YAML output is printed as it is.
  const failed = response !== undefined && (response.exit_code ?? Number(response.status)) !== 0;
  const json = !failed && response !== undefined && typeof response.body !== "string";
  const shell =
    stdin === undefined
      ? `dagentsc ${args.join(" ")}`
      : `echo ${shellQuote(JSON.stringify(stdin))} \\\n  | dagentsc ${args.join(" ")}`;
  const python = [
    ...(json || stdin !== undefined ? ["import json"] : []),
    "import subprocess",
    "",
    "result = subprocess.run(",
    `    ${pythonLiteral(["dagentsc", ...args], 4)},`,
    ...(stdin === undefined ? [] : [`    input=json.dumps(${pythonLiteral(stdin, 4)}),`]),
    "    capture_output=True,",
    "    text=True,",
    ...(failed ? [] : ["    check=True,"]),
    ")",
    ...(failed
      ? ["print(result.returncode, result.stderr)"]
      : json
        ? ["print(json.loads(result.stdout))"]
        : ["print(result.stdout)"])
  ].join("\n");
  return [
    { label: "Shell", code: shell },
    { label: "Python", code: python }
  ];
}
