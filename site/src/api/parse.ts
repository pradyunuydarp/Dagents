/**
 * Read a module page of the API reference (`docs/reference/api/<module>.md`).
 *
 * The rules match `parse_module` in `scripts/api_reference.py`, which the tests
 * use, so the site and the tests see the same hierarchy:
 *
 * - A `##` heading directly followed by an `api-interface` block starts an
 *   interface. Its anchor is the heading's id.
 * - A `###` heading inside an interface, directly followed by an
 *   `api-endpoint` block, holds the prose for one endpoint.
 * - A `###` heading directly followed by an `api-object` block documents an
 *   object.
 * - Prose runs to the next `##` or `###` heading. Everything else is shown as
 *   ordinary Markdown, in place.
 */

import { slugify } from "../markdown";

export const PLANNER = "dagentsc";

export interface EndpointRef {
  kind: "http" | "command";
  method: string;
  path: string;
  words: string[];
  /** `METHOD /path`, or `dagentsc <words>`: how the hierarchy and the examples name it. */
  key: string;
}

export interface EndpointDoc {
  ref: EndpointRef;
  heading: string;
  prose: string;
}

export interface InterfaceDoc {
  heading: string;
  anchor: string;
  service: string;
  status: "draft" | "complete";
  code: string[];
  classes: { name: string; file: string }[];
  endpoints: EndpointRef[];
  prose: string;
  docs: EndpointDoc[];
}

export interface ObjectDoc {
  heading: string;
  anchor: string;
  service: string;
  classPath: string;
  name: string;
  /** `<service> <example id> <JSON pointer>`, or null. */
  example: string | null;
  prose: string;
}

export type Segment =
  | { kind: "prose"; markdown: string }
  | { kind: "interface"; doc: InterfaceDoc }
  | { kind: "object"; doc: ObjectDoc };

export interface ModuleDoc {
  slug: string;
  title: string;
  segments: Segment[];
  interfaces: InterfaceDoc[];
  objects: ObjectDoc[];
}

/** The id the renderer gives a heading; see `heading_anchor` in the generator. */
export function headingAnchor(text: string): string {
  return slugify(text.replace(/[`*_]/g, ""));
}

const ENDPOINT_LINE = /^(GET|POST|PUT|PATCH|DELETE)\s+(\/\S*)$/;

export function parseEndpointRef(text: string): EndpointRef | null {
  const line = text.trim();
  const http = line.match(ENDPOINT_LINE);
  if (http) return { kind: "http", method: http[1], path: http[2], words: [], key: `${http[1]} ${http[2]}` };
  const words = line.split(/\s+/);
  if (words.length >= 2 && words[0] === PLANNER) {
    return { kind: "command", method: "", path: "", words: words.slice(1), key: words.join(" ") };
  }
  return null;
}

type Fields = Record<string, string | string[]> & { _lines: string[] };

/** `key: value` lines, and `key:` followed by `- item` lines for a list. */
export function parseBlock(lines: string[]): Fields {
  const fields = { _lines: [] as string[] } as Fields;
  let current: string | null = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("- ") && current) {
      (fields[current] as string[]).push(line.slice(2).trim());
      continue;
    }
    const pair = line.match(/^([a-z_]+):\s*(.*)$/);
    if (pair && !ENDPOINT_LINE.test(line)) {
      if (pair[2].trim()) {
        fields[pair[1]] = pair[2].trim();
        current = null;
      } else {
        fields[pair[1]] = [];
        current = pair[1];
      }
      continue;
    }
    fields._lines.push(line);
    current = null;
  }
  return fields;
}

function asList(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  if (Array.isArray(value)) return value;
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

type Item =
  | { kind: "heading"; level: number; text: string; raw: string[] }
  | { kind: "block"; info: string; fields: Fields; raw: string[] }
  | { kind: "text"; raw: string[] };

const FENCE = /^(```+|~~~+)\s*([\w-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const BLOCKS = new Set(["api-interface", "api-endpoint", "api-object"]);

function tokenize(text: string): Item[] {
  const lines = text.split("\n");
  const items: Item[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = line.match(FENCE);
    if (fence) {
      const [, marker, info] = fence;
      const body: string[] = [];
      const raw = [line];
      index += 1;
      while (index < lines.length && !lines[index].startsWith(marker)) {
        body.push(lines[index]);
        raw.push(lines[index]);
        index += 1;
      }
      raw.push(lines[index] ?? marker);
      if (BLOCKS.has(info)) items.push({ kind: "block", info, fields: parseBlock(body), raw });
      else items.push({ kind: "text", raw });
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) items.push({ kind: "heading", level: heading[1].length, text: heading[2].trim(), raw: [line] });
    else items.push({ kind: "text", raw: [line] });
  }
  return items;
}

function isBlank(item: Item): boolean {
  return item.kind === "text" && item.raw.every((line) => !line.trim());
}

export function parseModule(slug: string, text: string): ModuleDoc {
  const items = tokenize(text);
  const segments: Segment[] = [];
  const interfaces: InterfaceDoc[] = [];
  const objects: ObjectDoc[] = [];
  let prose: string[] = [];
  let current: InterfaceDoc | null = null;
  const first = items.find(
    (item): item is Extract<Item, { kind: "heading" }> => item.kind === "heading" && item.level === 1
  );
  const title = first?.text ?? slug;

  const flush = () => {
    const markdown = prose.join("\n").trim();
    if (markdown) segments.push({ kind: "prose", markdown });
    prose = [];
  };

  /** The section after a heading: its block, and the text up to the next `##` or `###`. */
  const section = (start: number): { block: Item | null; text: string; end: number } => {
    let block: Item | null = null;
    const text: string[] = [];
    let index = start + 1;
    while (index < items.length && isBlank(items[index])) index += 1;
    if (index < items.length && items[index].kind === "block") {
      block = items[index];
      index += 1;
    }
    for (; index < items.length; index += 1) {
      const item = items[index];
      if (item.kind === "heading" && item.level <= 3) break;
      text.push(...item.raw);
    }
    return { block, text: text.join("\n").trim(), end: index };
  };

  for (let index = 0; index < items.length; ) {
    const item = items[index];
    if (item.kind !== "heading") {
      prose.push(...item.raw);
      index += 1;
      continue;
    }
    const { block, text: body, end } = section(index);
    if (block?.kind === "block" && block.info === "api-interface" && item.level === 2) {
      flush();
      const fields = block.fields;
      const classes = asList(fields.classes).flatMap((entry) => {
        const match = entry.match(/^([A-Za-z_][\w.]*)\s*\(([^)]+)\)$/);
        return match ? [{ name: match[1], file: match[2].trim() }] : [];
      });
      current = {
        heading: item.text,
        anchor: headingAnchor(item.text),
        service: String(fields.service ?? ""),
        status: fields.status === "complete" ? "complete" : "draft",
        code: asList(fields.code),
        classes,
        endpoints: asList(fields.endpoints).flatMap((entry) => {
          const ref = parseEndpointRef(entry);
          return ref ? [ref] : [];
        }),
        prose: body,
        docs: []
      };
      interfaces.push(current);
      segments.push({ kind: "interface", doc: current });
      index = end;
      continue;
    }
    if (block?.kind === "block" && block.info === "api-endpoint" && item.level === 3 && current) {
      const ref = parseEndpointRef(block.fields._lines[0] ?? "");
      if (ref) current.docs.push({ ref, heading: item.text, prose: body });
      index = end;
      continue;
    }
    if (block?.kind === "block" && block.info === "api-object" && item.level === 3) {
      flush();
      const classPath = String(block.fields.class ?? "");
      const doc: ObjectDoc = {
        heading: item.text,
        anchor: headingAnchor(item.text),
        service: String(block.fields.service ?? ""),
        classPath,
        name: classPath.split(".").pop() ?? classPath,
        example: block.fields.example ? String(block.fields.example) : null,
        prose: body
      };
      objects.push(doc);
      segments.push({ kind: "object", doc });
      index = end;
      continue;
    }
    if (item.level <= 2) current = null;
    prose.push(...item.raw);
    index += 1;
  }
  flush();
  return { slug, title: String(title), segments, interfaces, objects };
}
