import { Fragment, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";

import { renderDiagrams, useColorScheme } from "../Mermaid";
import { COPY_BUTTON_INNER, COPY_LABEL } from "../codeCopy";
import { highlight } from "../highlight";
import { apiRoute, renderMarkdown } from "../markdown";
import { commandSamples, httpSamples, type CodeSample } from "./codegen";
import {
  atPointer,
  commandSpec,
  endpointSpec,
  examplesFor,
  serviceSpec,
  type ApiData,
  type CommandSpec,
  type EndpointSpec,
  type Example,
  type ObjectSpec,
  type Parameter,
  type ResponseSpec,
  type Schema
} from "./data";
import { apiResolver, repoHref } from "./links";
import { PLANNER, type EndpointDoc, type EndpointRef, type InterfaceDoc, type ObjectDoc } from "./parse";
import {
  allowedText,
  fieldRows,
  inferSchema,
  isEmptySchema,
  refName,
  typeLabel,
  unwrapNullable,
  type SchemaContext
} from "./schema";

/* ===== Small pieces ======================================================= */

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Text from the code (a docstring or a field description), with `code` spans. */
export function InlineText({ text }: { text: string | null | undefined }) {
  if (!text) return null;
  const html = escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>");
  return <span dangerouslySetInnerHTML={{ __html: html }} />;
}

/** Markdown from docs/reference/api, with its diagrams drawn. */
export function Prose({ markdown, page, className = "" }: { markdown: string; page: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(markdown, apiResolver(page), "docs/reference/api/"), [markdown, page]);
  const ref = useRef<HTMLDivElement>(null);
  const scheme = useColorScheme();
  useEffect(() => {
    if (ref.current && html.includes('class="mermaid"')) void renderDiagrams(ref.current, scheme);
  }, [html, scheme]);
  if (!markdown.trim()) return null;
  return <div ref={ref} className={`doc api-prose ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

/** A path that may break after each slash, and nowhere else. */
export function BreakablePath({ path }: { path: string }) {
  const parts = path.split("/");
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index > 0 && (
            <>
              /<wbr />
            </>
          )}
          {part}
        </Fragment>
      ))}
    </>
  );
}

export function MethodBadge({ method }: { method: string }) {
  return <span className="api-method">{method}</span>;
}

const STATUS_TEXT: Record<string, string> = {
  "0": "exit code 0",
  "200": "OK",
  "201": "Created",
  "202": "Accepted",
  "304": "Not Modified",
  "400": "Bad Request",
  "404": "Not Found",
  "409": "Conflict",
  "413": "Content Too Large",
  "422": "Unprocessable Content",
  "429": "Too Many Requests",
  "500": "Internal Server Error",
  "503": "Service Unavailable"
};

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? "";
}

function JsonBlock({ value }: { value: unknown }) {
  // A string body (plain text, standard error) is shown as it is.
  const isText = typeof value === "string";
  const text = isText ? value : JSON.stringify(value, null, 2);
  return (
    <div className="code-block" data-copy-scope>
      <CopyButton />
      <pre className="ds-code api-json">
        <code dangerouslySetInnerHTML={{ __html: highlight(text, isText ? undefined : "json") }} />
      </pre>
    </div>
  );
}

/** The copy button of a code block. `codeCopy.ts` handles its clicks. */
function CopyButton() {
  return (
    <button
      type="button"
      className="code-copy"
      aria-label={COPY_LABEL}
      title={COPY_LABEL}
      dangerouslySetInnerHTML={{ __html: COPY_BUTTON_INNER }}
    />
  );
}

/* ===== Field tables ======================================================= */

const COLUMNS = ["Field", "Type", "Required", "Default", "Allowed values", "Description"];

export function FieldTable({
  schema,
  context,
  depth = 0,
  seen = []
}: {
  schema: Schema;
  context: SchemaContext;
  depth?: number;
  seen?: string[];
}) {
  const rows = fieldRows(schema, context);
  if (!rows.length) return <p className="ds-note">This object has no fixed fields.</p>;
  return (
    <div className="api-table">
      <table className="api-fields">
        <thead>
          <tr>
            {COLUMNS.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const nested = row.child && depth < 4 && !(row.childName && seen.includes(row.childName));
            return (
              <Fragment key={row.name}>
                <tr>
                  <td data-label="Field">
                    <code className="api-field-name">{row.name}</code>
                  </td>
                  <td data-label="Type" className="api-type">
                    {row.typeLink ? <a href={row.typeLink}>{row.type}</a> : row.type}
                  </td>
                  <td data-label="Required">{row.required ? "required" : "optional"}</td>
                  <td data-label="Default" data-empty={row.defaultValue === null || undefined}>
                    {row.defaultValue === null ? "—" : <code>{row.defaultValue}</code>}
                  </td>
                  <td data-label="Allowed" data-empty={row.allowed === null || undefined}>
                    {row.allowed ?? "—"}
                  </td>
                  <td data-label="Description" data-empty={!row.description || undefined}>
                    {row.description ? <InlineText text={row.description} /> : "—"}
                  </td>
                </tr>
                {nested && row.child && (
                  <tr className="api-nested">
                    <td colSpan={COLUMNS.length}>
                      <details>
                        <summary>
                          Fields of <code>{row.name}</code>
                          {row.childName ? ` (${row.childName})` : ""}
                        </summary>
                        <FieldTable
                          schema={row.child}
                          context={context}
                          depth={depth + 1}
                          seen={row.childName ? [...seen, row.childName] : seen}
                        />
                      </details>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Whether a content type is JSON (`application/json`, `application/problem+json`). */
function isJson(contentType: string | null | undefined): boolean {
  return !contentType || /[/+]json\b/.test(contentType);
}

/** A body that is not JSON, in words. */
function textBodyLabel(contentType: string): string {
  if (contentType.startsWith("image/svg")) return "An SVG document.";
  if (contentType.startsWith("text/plain")) return "Plain text.";
  if (contentType.includes("yaml")) return "YAML text.";
  return `A ${contentType} body.`;
}

/** The objects of a union (`anyOf`/`oneOf` of references), without `null`. */
function unionMembers(schema: Schema): string[] | null {
  const list = (schema.anyOf ?? schema.oneOf) as Schema[] | undefined;
  if (!Array.isArray(list)) return null;
  const names = list.filter((item) => item.type !== "null").map((item) => refName(item));
  return names.length > 1 && names.every(Boolean) ? (names as string[]) : null;
}

/** Each object of a union, linked to its object section when the site has one. */
function UnionNames({ names, context }: { names: string[]; context: SchemaContext }) {
  return (
    <>
      {names.map((name, index) => (
        <Fragment key={name}>
          {index > 0 && (index === names.length - 1 ? " or " : ", ")}
          {context.objectAnchors[name] ? <a href={context.objectAnchors[name]}>{name}</a> : <code>{name}</code>}
        </Fragment>
      ))}
    </>
  );
}

/** The field that says which object of a union a value is, such as `kind`. */
function discriminator(schema: Schema, names: string[], context: SchemaContext): string | null {
  const declared = (schema.discriminator as { propertyName?: string } | undefined)?.propertyName;
  if (declared) return declared;
  const shared = names
    .map((name) => (context.components[name] ?? context.defs[name] ?? {}).properties as Record<string, Schema> | undefined)
    .map((properties) => Object.entries(properties ?? {}).filter(([, field]) => "const" in field || Array.isArray(field.enum)).map(([key]) => key));
  return shared.length ? shared.reduce((common, keys) => common.filter((key) => keys.includes(key)))[0] ?? null : null;
}

/** A body schema: a field table for an object, or a line for a list, a union or a scalar. */
function BodySchema({ schema, context, contentType }: { schema: Schema; context: SchemaContext; contentType?: string | null }) {
  if (contentType && !isJson(contentType)) return <p className="ds-note">{textBodyLabel(contentType)}</p>;
  const { schema: target } = unwrapNullable(schema);
  const listed = target.type === "array" ? unwrapNullable((target.items as Schema | undefined) ?? {}).schema : null;
  const members = unionMembers(listed ?? target);
  if (members) {
    const field = discriminator(listed ?? target, members, context);
    return (
      <p className="ds-note">
        {listed ? "A JSON array; each item is one of " : "One of "}
        <UnionNames names={members} context={context} />
        {field ? (
          <>
            . The <code>{field}</code> field says which one, and each object section lists its fields.
          </>
        ) : (
          ". Each object section lists its fields."
        )}
      </p>
    );
  }
  const name = refName(target);
  const resolved = name ? { ...target, ...(context.components[name] ?? context.defs[name] ?? {}) } : target;
  if (resolved.properties) {
    return (
      <>
        {name && (
          <p className="ds-note">
            Schema{" "}
            {context.objectAnchors[name] ? <a href={context.objectAnchors[name]}>{name}</a> : <code>{name}</code>}
          </p>
        )}
        <FieldTable schema={resolved} context={context} />
      </>
    );
  }
  const items = resolved.type === "array" ? (resolved.items as Schema | undefined) : undefined;
  return (
    <>
      <p className="ds-note">
        A JSON <span className="api-type">{typeLabel(schema, context)}</span>.
      </p>
      {items && fieldRows(items, context).length > 0 && <FieldTable schema={items} context={context} />}
    </>
  );
}

function ParameterTable({ parameters, context }: { parameters: Parameter[]; context: SchemaContext }) {
  return (
    <div className="api-table">
      <table className="api-fields">
        <thead>
          <tr>
            {["Name", "Type", "Required", "Default", "Allowed values", "Description"].map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {parameters.map((parameter) => {
            const fallback = parameter.schema.default;
            const description = parameter.description ?? (parameter.schema.description as string | undefined) ?? null;
            const allowed = allowedText(parameter.schema, context);
            return (
              <tr key={`${parameter.in}:${parameter.name}`}>
                <td data-label="Name">
                  <code className="api-field-name">{parameter.name}</code>
                </td>
                <td data-label="Type" className="api-type">
                  {typeLabel(parameter.schema, context)}
                </td>
                <td data-label="Required">{parameter.required ? "required" : "optional"}</td>
                <td data-label="Default" data-empty={fallback === undefined || undefined}>
                  {fallback === undefined ? "—" : <code>{JSON.stringify(fallback)}</code>}
                </td>
                <td data-label="Allowed" data-empty={allowed === null || undefined}>
                  {allowed ?? "—"}
                </td>
                <td data-label="Description" data-empty={!description || undefined}>
                  {description ? <InlineText text={description} /> : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ===== Code tabs ========================================================== */

export function CodeTabs({ samples, id }: { samples: CodeSample[]; id: string }) {
  const [active, setActive] = useState(0);
  const sample = samples[active];
  return (
    <div className="api-code" data-copy-scope>
      <div className="api-tabs" role="tablist" aria-label="Request code">
        {samples.map((item, index) => (
          <button
            key={item.label}
            type="button"
            role="tab"
            id={`${id}-tab-${index}`}
            aria-selected={index === active}
            aria-controls={`${id}-panel`}
            className={index === active ? "is-active" : undefined}
            onClick={() => setActive(index)}
          >
            {item.label}
          </button>
        ))}
        <CopyButton />
      </div>
      <pre className="ds-code api-code-panel" role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-tab-${active}`}>
        <code dangerouslySetInnerHTML={{ __html: highlight(sample.code, sample.label) }} />
      </pre>
    </div>
  );
}

/* ===== Examples =========================================================== */

/** Strings with line breaks (YAML, SQL) in a response, so they can be read as text. */
function multilineStrings(value: unknown, pointer = "", found: { pointer: string; text: string }[] = []) {
  if (found.length >= 4) return found;
  if (typeof value === "string") {
    if (value.includes("\n") && value.length > 80) found.push({ pointer: pointer || "/", text: value });
  } else if (Array.isArray(value)) {
    value.forEach((item, index) => multilineStrings(item, `${pointer}/${index}`, found));
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) multilineStrings(item, `${pointer}/${key}`, found);
  }
  return found;
}

function exampleAnchor(owner: string, example: Example): string {
  return `${owner}--${example.id}`;
}

function ExampleView({ example, owner, port }: { example: Example; owner: string; port: number | null }) {
  const isCommand = example.endpoint.startsWith(`${PLANNER} `);
  const samples = isCommand
    ? commandSamples(example.request, example.response)
    : httpSamples(`http://127.0.0.1:${port}`, example.request, example.response);
  const response = example.response;
  const status = String(response.status);
  const contentType = response.headers["content-type"];
  return (
    <div className="api-example" id={exampleAnchor(owner, example)}>
      <h5>{example.title ?? example.id}</h5>
      {example.description && (
        <p className="ds-note">
          <InlineText text={example.description} />
        </p>
      )}
      {example.request_template && (
        <p className="ds-note">
          The path uses a value from an earlier example's answer:{" "}
          <code>{example.request_template.path}</code>. Use the value your own earlier call returned.
        </p>
      )}
      <CodeTabs samples={samples} id={exampleAnchor(owner, example)} />
      <p className="api-response-line">
        <span className="ds-eyebrow">{isCommand ? "Output" : "Response"}</span>{" "}
        <span className="api-status">{isCommand ? `exit ${response.exit_code ?? status}` : status}</span>{" "}
        {!isCommand && statusText(status)}
        {contentType && <span className="ds-note"> · {contentType}</span>}
        {response.headers["retry-after"] && <span className="ds-note"> · Retry-After: {response.headers["retry-after"]}</span>}
      </p>
      {isCommand && (response.body === "" || response.body === null) && response.stderr ? null : <JsonBlock value={response.body} />}
      {isCommand && response.stderr && (
        <>
          <p className="ds-eyebrow">Standard error</p>
          <JsonBlock value={response.stderr} />
        </>
      )}
      {multilineStrings(response.body).map(({ pointer, text }) => (
        <details className="ds-details api-text" key={pointer}>
          <summary>
            <code>{pointer}</code> as text
          </summary>
          <div className="code-block" data-copy-scope>
            <CopyButton />
            <pre className="ds-code">
              <code>{text}</code>
            </pre>
          </div>
        </details>
      ))}
      {response.trimmed.length > 0 && (
        <p className="ds-note">
          Trimmed for this page:{" "}
          {response.trimmed.map((cut, index) => (
            <Fragment key={cut.pointer}>
              {index > 0 && "; "}
              {cut.pointer === "" || cut.pointer === "/" ? "the response," : <code>{cut.pointer}</code>} {cut.note}
            </Fragment>
          ))}
          .
        </p>
      )}
      {!isCommand && example.planner_calls.length > 0 && (
        <p className="ds-note">
          This request ran{" "}
          {example.planner_calls.map((call, index) => (
            <Fragment key={call}>
              {index > 0 && ", "}
              <a href={apiRoute("dagentsc", `dagentsc-${call.replace(/[^a-z0-9]+/g, "-")}`)}>
                <code>dagentsc {call}</code>
              </a>
            </Fragment>
          ))}
          .
        </p>
      )}
    </div>
  );
}

/* ===== Endpoint cards ===================================================== */

export interface Card {
  primary: EndpointSpec;
  twin: EndpointSpec | null;
  doc: EndpointDoc | null;
}

/** Group an interface's endpoints into cards: an alias pair shares one card. */
export function endpointCards(data: ApiData, iface: InterfaceDoc): Card[] {
  const specs = iface.endpoints
    .filter((ref) => ref.kind === "http")
    .map((ref) => endpointSpec(data, iface.service, ref.method, ref.path))
    .filter((spec): spec is EndpointSpec => Boolean(spec));
  const listed = new Map(specs.map((spec) => [spec.id, spec]));
  const cards: Card[] = [];
  const placed = new Set<string>();
  for (const spec of specs) {
    if (placed.has(spec.id)) continue;
    // The versioned twin leads its card; a legacy path joins the twin that names it.
    const twinOf = specs.find((other) => other.alias_of?.id === spec.id);
    const primary = twinOf ?? spec;
    const twin = twinOf ? spec : primary.alias_of && listed.has(primary.alias_of.id) ? listed.get(primary.alias_of.id)! : null;
    placed.add(primary.id);
    if (twin) placed.add(twin.id);
    const keys = [primary, twin].filter(Boolean).map((item) => `${item!.method} ${item!.path}`);
    const doc = iface.docs.find((entry) => keys.includes(entry.ref.key)) ?? null;
    cards.push({ primary, twin, doc });
  }
  return cards;
}

function cardTitle(card: Card): string {
  if (card.doc) return card.doc.heading;
  const summaries = [card.primary.summary, card.twin?.summary].filter(Boolean) as string[];
  const plain = summaries.find((summary) => !/^versioned/i.test(summary)) ?? summaries[0];
  return plain ? plain.replace(/\.$/, "").replace(/`/g, "") : `${card.primary.method} ${card.primary.path}`;
}

function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="api-facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function callLabel(call: EndpointSpec["handler"]["calls"][number]): string {
  return call.class ? `${call.class}.${call.name}` : `${call.module ? `${call.module}.` : ""}${call.name}`;
}

function CallList({ endpoint, page }: { endpoint: EndpointSpec; page: string }) {
  const calls = endpoint.handler.calls;
  if (!calls.length) return <>none found in the handler's source</>;
  return (
    <>
      {calls.map((call, index) => (
        <Fragment key={`${call.kind}:${callLabel(call)}`}>
          {index > 0 && ", "}
          {call.kind === "route" && call.endpoint ? (
            <a href={apiRoute(page, call.endpoint)}>the handler of {call.name}</a>
          ) : call.file ? (
            <a href={repoHref(call.file)}>{callLabel(call)}</a>
          ) : (
            callLabel(call)
          )}
        </Fragment>
      ))}
    </>
  );
}

/** Statuses that FastAPI declares on every route with parameters, even when nothing can fail. */
function canFailValidation(endpoint: EndpointSpec): boolean {
  if (endpoint.request_body) return true;
  return endpoint.parameters.some((parameter) => {
    const schema = unwrapNullable(parameter.schema).schema;
    const constrained = ["pattern", "enum", "minLength", "maxLength", "format", "minimum", "maximum", "const"].some((key) => key in schema);
    if (parameter.in === "path") return schema.type !== "string" || constrained;
    return parameter.required || schema.type !== "string" || constrained;
  });
}

function isAutomatic422(endpoint: EndpointSpec, response: ResponseSpec): boolean {
  return response.status === "422" && refName(response.schema ?? {}) === "HTTPValidationError" && !canFailValidation(endpoint);
}

function responseList(endpoint: EndpointSpec, examples: Example[], twin: EndpointSpec | null = null): ResponseSpec[] {
  const documented = endpoint.responses.filter((response) => !isAutomatic422(endpoint, response));
  // A status only the short twin answers (it may answer 200 where this path answers 202)
  // keeps the twin's declared schema, labelled with the path that returns it.
  for (const response of twin?.responses ?? []) {
    if (isAutomatic422(twin!, response) || documented.some((item) => item.status === response.status)) continue;
    documented.push({ ...response, description: `${response.description} (${twin!.method} ${twin!.path})` });
  }
  for (const example of examples) {
    const status = String(example.response.status);
    if (!documented.some((response) => response.status === status)) {
      documented.push({
        status,
        description: statusText(status),
        content_type: example.response.headers["content-type"] ?? null,
        schema: null
      });
    }
  }
  return documented.sort((a, b) => Number(a.status) - Number(b.status));
}

function Responses({
  endpoint,
  twin,
  examples,
  context,
  page
}: {
  endpoint: EndpointSpec;
  twin: EndpointSpec | null;
  examples: Example[];
  context: SchemaContext;
  page: string;
}) {
  return (
    <>
      {responseList(endpoint, examples, twin).map((response) => {
        const recorded = examples.find((example) => String(example.response.status) === response.status);
        const fromRecording =
          isEmptySchema(response.schema) && isJson(response.content_type) && recorded && recorded.response.body !== null && typeof recorded.response.body === "object";
        return (
          <div className="api-response" key={response.status}>
            <p>
              <span className="api-status">{response.status}</span> <InlineText text={response.description || statusText(response.status)} />
              {response.content_type && <span className="ds-note"> · {response.content_type}</span>}
            </p>
            {response.content_type && !isJson(response.content_type) ? (
              <BodySchema schema={response.schema ?? {}} context={context} contentType={response.content_type} />
            ) : response.status === "304" || response.status === "204" ? (
              <p className="ds-note">No body.</p>
            ) : !isEmptySchema(response.schema) && response.schema ? (
              <BodySchema schema={response.schema} context={context} />
            ) : fromRecording && recorded ? (
              <>
                <p className="ds-note">
                  The route declares no response model. These fields come from a recorded response (
                  <a
                    href={apiRoute(page, exampleAnchor(endpoint.id, recorded))}
                    onClick={(event) => scrollToId(event, exampleAnchor(endpoint.id, recorded))}
                  >
                    {recorded.title ?? recorded.id}
                  </a>
                  ).
                </p>
                <BodySchema schema={inferSchema(recorded.response.body)} context={context} />
              </>
            ) : (
              <p className="ds-note">No schema is declared for this response.</p>
            )}
          </div>
        );
      })}
    </>
  );
}

function scrollToId(event: MouseEvent, id: string) {
  event.preventDefault();
  document.getElementById(id)?.scrollIntoView();
}

/** What an error row says when no example of it was captured. */
function UncapturedBody({ response, context }: { response: ResponseSpec; context: SchemaContext }) {
  if (response.content_type?.startsWith("text/plain")) {
    return (
      <>
        <code className="api-error-body">Internal Server Error</code> as plain text (no example captured)
      </>
    );
  }
  const name = refName(response.schema ?? {}) ?? ((response.schema?.title as string | undefined) || null);
  if (name) {
    return (
      <>
        A JSON {context.objectAnchors[name] ? <a href={context.objectAnchors[name]}>{name}</a> : <code>{name}</code>} (no example
        captured)
      </>
    );
  }
  return <>no example captured</>;
}

function Errors({
  endpoint,
  twin,
  examples,
  context
}: {
  endpoint: EndpointSpec;
  twin: EndpointSpec | null;
  examples: Example[];
  context: SchemaContext;
}) {
  const errors = responseList(endpoint, examples, twin).filter((response) => Number(response.status) >= 400);
  if (!errors.length) return null;
  return (
    <section className="api-part">
      <h4>Errors</h4>
      <div className="api-table">
        <table className="api-fields api-errors">
          <thead>
            <tr>
              <th>Status</th>
              <th>Meaning</th>
              <th>Example body</th>
            </tr>
          </thead>
          <tbody>
            {errors.map((error) => {
              const recorded = examples.find((example) => String(example.response.status) === error.status);
              const raw = recorded?.response.body;
              const body = recorded ? (typeof raw === "string" ? raw : JSON.stringify(raw)) : null;
              return (
                <tr key={error.status}>
                  <td data-label="Status">
                    <span className="api-status">{error.status}</span>
                  </td>
                  <td data-label="Meaning">
                    <InlineText text={error.description || statusText(error.status)} />
                  </td>
                  <td data-label="Example">
                    {body ? (
                      <code className="api-error-body">{body.length > 240 ? `${body.slice(0, 240)}…` : body}</code>
                    ) : (
                      <UncapturedBody response={error} context={context} />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function EndpointCard({
  card,
  iface,
  data,
  page,
  context
}: {
  card: Card;
  iface: InterfaceDoc;
  data: ApiData;
  page: string;
  context: SchemaContext;
}) {
  const { primary, twin, doc } = card;
  const service = serviceSpec(data, iface.service);
  // A versioned twin usually only calls its short path's handler; show the work that one does.
  const delegates =
    twin !== null &&
    primary.handler.calls.length > 0 &&
    primary.handler.calls.every((call) => call.kind === "route" && call.endpoint === twin.id);
  const worker = delegates && twin ? twin : primary;
  const keys = [primary, twin].filter(Boolean).map((item) => `${item!.method} ${item!.path}`);
  const examples = examplesFor(data, iface.service, keys);
  const plannerCalls = [...new Set(examples.flatMap((example) => example.planner_calls))];
  const fallback = [twin && /^versioned/i.test(primary.summary ?? "") ? twin : primary]
    .map((item) => [item?.summary, item?.description].filter(Boolean).join("\n\n"))
    .join("");
  const groups: [string, Parameter[]][] = [
    ["Path parameters", primary.parameters.filter((parameter) => parameter.in === "path")],
    ["Query parameters", primary.parameters.filter((parameter) => parameter.in === "query")],
    ["Headers", primary.parameters.filter((parameter) => parameter.in === "header")]
  ];
  return (
    <article
      className="api-endpoint"
      id={primary.id}
      data-endpoint={`${iface.service} ${primary.method} ${primary.path}`}
      data-alias={twin ? `${iface.service} ${twin.method} ${twin.path}` : undefined}
    >
      {twin && <span id={twin.id} className="api-anchor" aria-hidden="true" />}
      <header className="api-endpoint-head">
        <h3>
          <a href={apiRoute(page, primary.id)}>{cardTitle(card)}</a>
        </h3>
        <p className="api-route">
          <MethodBadge method={primary.method} />{" "}
          <code>
            <BreakablePath path={primary.path} />
          </code>
        </p>
        {twin && (
          <p className="api-route api-route-twin">
            <span className="ds-note">Also at</span> <MethodBadge method={twin.method} />{" "}
            <code>
              <BreakablePath path={twin.path} />
            </code>
            <span className="ds-note">
              {" "}
              (same handler{twin.status_code !== primary.status_code ? `; answers ${twin.status_code} where this path answers ${primary.status_code}` : ""})
            </span>
          </p>
        )}
      </header>

      <Prose markdown={doc?.prose || fallback} page={page} />

      <Facts
        items={[
          ["Service", `${iface.service} · port ${service?.port ?? "?"}`],
          [
            "Handler",
            <>
              {primary.handler.file ? (
                <a href={repoHref(primary.handler.file)}>{primary.handler.function}</a>
              ) : (
                primary.handler.function
              )}
              {delegates && ` (runs ${worker.handler.function})`}
            </>
          ],
          ["Calls", <CallList endpoint={worker} page={page} />],
          [
            "Planner",
            plannerCalls.length ? (
              plannerCalls.map((call, index) => (
                <Fragment key={call}>
                  {index > 0 && ", "}
                  <a href={apiRoute("dagentsc", `dagentsc-${call.replace(/[^a-z0-9]+/g, "-")}`)}>{call}</a>
                </Fragment>
              ))
            ) : examples.length ? (
              "not called by the examples"
            ) : (
              "no example captured yet"
            )
          ],
          ["Success status", String(primary.status_code)]
        ]}
      />

      {groups.map(([label, parameters]) =>
        parameters.length ? (
          <section className="api-part" key={label}>
            <h4>{label}</h4>
            <ParameterTable parameters={parameters} context={context} />
          </section>
        ) : null
      )}

      {primary.request_body && (
        <section className="api-part">
          <h4>
            Request body <span className="ds-note">{primary.request_body.content_type}{primary.request_body.required ? ", required" : ""}</span>
          </h4>
          <BodySchema schema={primary.request_body.schema} context={context} />
        </section>
      )}

      <section className="api-part">
        <h4>Responses</h4>
        <Responses endpoint={primary} twin={twin} examples={examples} context={context} page={page} />
      </section>

      <Errors endpoint={primary} twin={twin} examples={examples} context={context} />

      <section className="api-part">
        <h4>Examples</h4>
        {examples.length ? (
          examples.map((example) => (
            <ExampleView key={example.id} example={example} owner={primary.id} port={service?.port ?? null} />
          ))
        ) : (
          <p className="ds-note">No example has been captured for this endpoint yet.</p>
        )}
      </section>
    </article>
  );
}

/* ===== Planner commands =================================================== */

export function CommandCard({
  command,
  doc,
  data,
  page
}: {
  command: CommandSpec;
  doc: EndpointDoc | null;
  data: ApiData;
  page: string;
}) {
  const key = `${PLANNER} ${command.words.join(" ")}`;
  const examples = examplesFor(data, PLANNER, [key]);
  // The prose's flag table says what each flag means; without one, list the flags the handler reads.
  const flags = /^\|\s*Flag\s*\|/m.test(doc?.prose ?? "") ? [] : command.flags;
  return (
    <article className="api-endpoint" id={command.id} data-endpoint={key}>
      <header className="api-endpoint-head">
        <h3>
          <a href={apiRoute(page, command.id)}>{doc?.heading ?? key}</a>
        </h3>
        <p className="api-route">
          <code>{command.usage}</code>
        </p>
      </header>
      <Prose markdown={doc?.prose || command.doc || ""} page={page} />
      <Facts
        items={[
          ["Handler", <a href={repoHref(command.handler.file)}>{command.handler.function}</a>],
          [
            "Calls",
            command.calls.length
              ? command.calls.map((call, index) => (
                  <Fragment key={`${call.module}.${call.function}`}>
                    {index > 0 && ", "}
                    {call.folder ? <a href={repoHref(call.folder)}>{`${call.module}.${call.function}`}</a> : `${call.module}.${call.function}`}
                  </Fragment>
                ))
              : "none"
          ],
          ["Reads", command.reads.length ? command.reads.join(", ") : "built-in values"],
          ["Writes", command.writes.length ? command.writes.join(", ") : "an object built in the handler"]
        ]}
      />
      {flags.length > 0 && (
        <section className="api-part">
          <h4>Flags</h4>
          <div className="api-table">
            <table className="api-fields">
              <thead>
                <tr>
                  <th>Flag</th>
                  <th>Default</th>
                  <th>Required</th>
                </tr>
              </thead>
              <tbody>
                {flags.map((flag) => (
                  <tr key={flag.name}>
                    <td data-label="Flag">
                      <code className="api-field-name">{flag.name}</code>
                      {flag.repeated && " (repeatable)"}
                    </td>
                    <td data-label="Default">
                      {flag.default === null ? "none" : flag.default === "-" ? <>standard input (<code>-</code>)</> : <code>{flag.default}</code>}
                    </td>
                    <td data-label="Required">optional</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <section className="api-part">
        <h4>Examples</h4>
        {examples.length ? (
          examples.map((example) => <ExampleView key={example.id} example={example} owner={command.id} port={null} />)
        ) : (
          <p className="ds-note">No example has been captured for this command yet.</p>
        )}
      </section>
    </article>
  );
}

/* ===== Interfaces ========================================================= */

function schemaNames(schemas: (Schema | null | undefined)[]): string[] {
  const names = new Set<string>();
  for (const schema of schemas) {
    if (!schema) continue;
    const { schema: target } = unwrapNullable(schema);
    const listed = target.type === "array" ? unwrapNullable((target.items as Schema | undefined) ?? {}).schema : target;
    const found = unionMembers(listed) ?? [refName(listed)];
    for (const name of found) {
      if (name && name !== "HTTPValidationError" && name !== "ErrorResponse") names.add(name);
    }
  }
  return [...names];
}

function TypeList({ names, context, empty }: { names: string[]; context: SchemaContext; empty: string }) {
  if (!names.length) return <>{empty}</>;
  return (
    <>
      {names.map((name, index) => (
        <Fragment key={name}>
          {index > 0 && ", "}
          {context.objectAnchors[name] ? <a href={context.objectAnchors[name]}>{name}</a> : name}
        </Fragment>
      ))}
    </>
  );
}

export function InterfaceSection({
  iface,
  data,
  page,
  objectAnchors
}: {
  iface: InterfaceDoc;
  data: ApiData;
  page: string;
  objectAnchors: Record<string, string>;
}) {
  const service = serviceSpec(data, iface.service);
  const context: SchemaContext = { components: service?.schemas ?? {}, defs: {}, objectAnchors };
  const isPlanner = iface.service === PLANNER;
  const cards = isPlanner ? [] : endpointCards(data, iface);
  const commands = isPlanner
    ? iface.endpoints
        .map((ref) => ({ ref, spec: commandSpec(data, ref.words) }))
        .filter((entry): entry is { ref: EndpointRef; spec: CommandSpec } => Boolean(entry.spec))
    : [];
  const takes = schemaNames(cards.map((card) => card.primary.request_body?.schema));
  const returns = schemaNames(
    cards.flatMap((card) => card.primary.responses.filter((r) => r.status.startsWith("2")).map((r) => r.schema))
  );
  return (
    <section className="api-interface" id={iface.anchor} data-interface={iface.anchor}>
      <h2>{iface.heading}</h2>
      <Facts
        items={[
          ["Service", isPlanner ? PLANNER : `${iface.service} · port ${service?.port ?? "?"}`],
          [
            "Code",
            iface.code.map((file, index) => (
              <Fragment key={file}>
                {index > 0 && ", "}
                <a href={repoHref(file)}>{file}</a>
              </Fragment>
            ))
          ],
          [
            isPlanner ? "Planner modules" : "Classes",
            iface.classes.length
              ? iface.classes.map((entry, index) => (
                  <Fragment key={entry.name}>
                    {index > 0 && ", "}
                    <a href={repoHref(entry.file)}>{entry.name}</a>
                  </Fragment>
                ))
              : "none listed"
          ],
          ...(isPlanner
            ? []
            : ([
                [
                  "Takes",
                  <TypeList
                    names={takes}
                    context={context}
                    empty={cards.some((card) => card.primary.request_body) ? "a body no model declares" : "no request body"}
                  />
                ],
                ["Returns", <TypeList names={returns} context={context} empty="bodies no model declares" />]
              ] as [string, ReactNode][]))
        ]}
      />
      <Prose markdown={iface.prose} page={page} />
      <ol className="api-toc" aria-label={`Endpoints in ${iface.heading}`}>
        {isPlanner
          ? commands.map(({ spec }) => (
              <li key={spec.id}>
                <a href={apiRoute(page, spec.id)}>
                  <code>{spec.usage.split(" [")[0]}</code>
                </a>
              </li>
            ))
          : cards.map((card) => (
              <li key={card.primary.id}>
                <a href={apiRoute(page, card.primary.id)}>
                  <MethodBadge method={card.primary.method} />{" "}
                  <code>
                    <BreakablePath path={card.primary.path} />
                  </code>
                </a>
              </li>
            ))}
      </ol>
      {cards.map((card) => (
        <EndpointCard key={card.primary.id} card={card} iface={iface} data={data} page={page} context={context} />
      ))}
      {commands.map(({ ref, spec }) => (
        <CommandCard
          key={spec.id}
          command={spec}
          doc={iface.docs.find((entry) => entry.ref.key === ref.key) ?? null}
          data={data}
          page={page}
        />
      ))}
    </section>
  );
}

/* ===== Objects ============================================================ */

export function ObjectSection({
  doc,
  spec,
  data,
  page,
  objectAnchors
}: {
  doc: ObjectDoc;
  spec: ObjectSpec | undefined;
  data: ApiData;
  page: string;
  objectAnchors: Record<string, string>;
}) {
  const context: SchemaContext = { components: {}, defs: spec?.defs ?? {}, objectAnchors };
  let example: unknown = undefined;
  if (doc.example) {
    const [service, exampleId, pointer] = doc.example.split(/\s+/);
    const recorded = data.examples[service]?.examples.find((entry) => entry.id === exampleId);
    if (recorded) example = atPointer(recorded, pointer ?? "");
  }
  return (
    <section className="api-object" id={doc.anchor} data-object={doc.name}>
      <h3>{doc.heading}</h3>
      <Facts
        items={[
          ["Class", spec?.file ? <a href={repoHref(spec.file)}>{doc.classPath}</a> : doc.classPath],
          ["Service", doc.service]
        ]}
      />
      {spec?.summary && (
        <p className="ds-measure">
          <InlineText text={spec.summary} />
        </p>
      )}
      <Prose markdown={doc.prose} page={page} />
      {spec ? <FieldTable schema={spec.schema} context={context} /> : <p className="ds-note">Run the generator to describe this class.</p>}
      {example !== undefined && (
        <>
          <p className="ds-eyebrow">
            Example, from a captured {doc.example?.split(/\s+/)[2]?.startsWith("/request") ? "request" : "response"}
          </p>
          <JsonBlock value={example} />
        </>
      )}
    </section>
  );
}
