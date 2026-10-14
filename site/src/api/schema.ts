/**
 * Read JSON schemas (from OpenAPI, pydantic or the Spring records) into the
 * rows the field tables show: name, type, required, default, allowed values
 * and description.
 */

import type { Schema } from "./data";

export interface SchemaContext {
  /** Component schemas for `#/components/schemas/<name>`. */
  components: Record<string, Schema>;
  /** Pydantic definitions for `#/$defs/<name>`. */
  defs: Record<string, Schema>;
  /** Object names documented on the site, to link a type to its object. */
  objectAnchors: Record<string, string>;
}

export interface FieldRow {
  name: string;
  type: string;
  /** The object a type refers to, when the site documents it. */
  typeLink: string | null;
  required: boolean;
  defaultValue: string | null;
  allowed: string | null;
  description: string | null;
  /** The nested object's schema, when the field holds one (directly, in a list or a map). */
  child: Schema | null;
  childName: string | null;
}

export function refName(schema: Schema): string | null {
  const ref = schema.$ref;
  return typeof ref === "string" ? ref.split("/").pop() ?? null : null;
}

export function resolve(schema: Schema, context: SchemaContext): Schema {
  const name = refName(schema);
  if (!name) return schema;
  const ref = String(schema.$ref);
  const target = ref.startsWith("#/$defs/") ? context.defs[name] : context.components[name] ?? context.defs[name];
  return target ?? {};
}

function variants(schema: Schema): Schema[] | null {
  const list = (schema.anyOf ?? schema.oneOf) as Schema[] | undefined;
  return Array.isArray(list) ? list : null;
}

/** Drop `null` from an `anyOf`, and say whether it was there. */
export function unwrapNullable(schema: Schema): { schema: Schema; nullable: boolean } {
  const list = variants(schema);
  if (!list) return { schema, nullable: false };
  const others = list.filter((item) => item.type !== "null");
  if (others.length === 1 && others.length < list.length) {
    return { schema: { ...others[0], ...pick(schema, ["default", "description", "title"]) }, nullable: true };
  }
  return { schema, nullable: list.length !== others.length };
}

function pick(schema: Schema, keys: string[]): Schema {
  return Object.fromEntries(keys.filter((key) => key in schema).map((key) => [key, schema[key]]));
}

/** A short type label: `string`, `array of WorkloadPort`, `map of string`, `WorkloadResources or null`. */
export function typeLabel(input: Schema, context: SchemaContext): string {
  const { schema, nullable } = unwrapNullable(input);
  let label: string;
  const name = refName(schema);
  const list = variants(schema);
  if (name) label = name;
  else if (list) label = list.map((item) => typeLabel(item, context)).join(" or ");
  else if (schema.type === "array") label = `array of ${typeLabel((schema.items as Schema) ?? {}, context)}`;
  else if (schema.type === "object" && schema.additionalProperties && typeof schema.additionalProperties === "object") {
    label = `map of ${typeLabel(schema.additionalProperties as Schema, context)}`;
  } else if (Array.isArray(schema.type)) label = (schema.type as string[]).join(" or ");
  else if (typeof schema.type === "string") label = schema.format ? `${schema.type} (${schema.format})` : schema.type;
  else if ("const" in schema) label = typeof schema.const;
  else if (schema.title && !schema.type && !schema.properties) label = String(schema.title);
  else if (schema.properties) label = "object";
  else label = "any";
  return nullable ? `${label} or null` : label;
}

function formatValue(value: unknown): string {
  return typeof value === "string" ? `"${value}"` : JSON.stringify(value);
}

/** Allowed values and ranges, in words: `Deployment, Job, CronJob`, `1 or more`, `1 to 200`. */
export function allowedText(input: Schema, context: SchemaContext): string | null {
  const { schema: unwrapped } = unwrapNullable(input);
  const schema = refName(unwrapped) ? { ...resolve(unwrapped, context), ...unwrapped } : unwrapped;
  const parts: string[] = [];
  if (Array.isArray(schema.enum)) parts.push((schema.enum as unknown[]).map(formatValue).join(", "));
  if ("const" in schema) parts.push(`only ${formatValue(schema.const)}`);
  const items = schema.type === "array" ? (schema.items as Schema | undefined) : undefined;
  if (items && Array.isArray(items.enum)) parts.push(`each one of ${(items.enum as unknown[]).map(formatValue).join(", ")}`);
  const min = (schema.minimum ?? schema.exclusiveMinimum) as number | undefined;
  const max = (schema.maximum ?? schema.exclusiveMaximum) as number | undefined;
  const strictMin = schema.exclusiveMinimum !== undefined;
  const strictMax = schema.exclusiveMaximum !== undefined;
  if (min !== undefined && max !== undefined) parts.push(`${min}${strictMin ? " (excluded)" : ""} to ${max}${strictMax ? " (excluded)" : ""}`);
  else if (min !== undefined) parts.push(strictMin ? `more than ${min}` : `${min} or more`);
  else if (max !== undefined) parts.push(strictMax ? `less than ${max}` : `${max} or less`);
  if (schema.minLength !== undefined && schema.maxLength !== undefined) {
    parts.push(`${schema.minLength} to ${schema.maxLength} characters`);
  } else if (schema.maxLength !== undefined) {
    parts.push(`at most ${schema.maxLength} characters`);
  } else if (schema.minLength !== undefined) {
    parts.push(`at least ${schema.minLength} characters`);
  }
  if (typeof schema.pattern === "string") parts.push(`matches ${schema.pattern}`);
  if (schema.minItems !== undefined) parts.push(`at least ${schema.minItems} items`);
  if (schema.maxItems !== undefined) parts.push(`at most ${schema.maxItems} items`);
  return parts.length ? parts.join("; ") : null;
}

/** The object schema a field holds, directly, in a list, or as a map's values. */
export function nestedObject(input: Schema, context: SchemaContext, depth = 0): { schema: Schema; name: string | null } | null {
  if (depth > 3) return null;
  const { schema } = unwrapNullable(input);
  const name = refName(schema);
  const resolved = resolve(schema, context);
  if (resolved.properties && Object.keys(resolved.properties as object).length) return { schema: resolved, name };
  if (resolved.type === "array" && resolved.items) return nestedObject(resolved.items as Schema, context, depth + 1);
  if (resolved.additionalProperties && typeof resolved.additionalProperties === "object") {
    return nestedObject(resolved.additionalProperties as Schema, context, depth + 1);
  }
  return null;
}

/** The rows of an object schema's field table, in the schema's order. */
export function fieldRows(input: Schema, context: SchemaContext): FieldRow[] {
  const schema = resolve(input, context);
  const properties = (schema.properties ?? {}) as Record<string, Schema>;
  const required = new Set((schema.required as string[] | undefined) ?? []);
  return Object.entries(properties).map(([name, property]) => {
    const target = unwrapNullable(property).schema;
    const typeRef = refName(target) ?? refName((target.items as Schema | undefined) ?? {});
    const nested = nestedObject(property, context);
    const description = (property.description ?? resolve(target, context).description ?? null) as string | null;
    return {
      name,
      type: typeLabel(property, context),
      typeLink: typeRef ? context.objectAnchors[typeRef] ?? null : null,
      required: required.has(name),
      defaultValue: "default" in property ? formatValue(property.default) : null,
      allowed: allowedText(property, context),
      description,
      child: nested?.schema ?? null,
      childName: nested?.name ?? null
    };
  });
}

/**
 * A schema guessed from a recorded value, for a route that declares no response
 * model. The page says the fields come from a recorded response.
 */
export function inferSchema(value: unknown): Schema {
  if (value === null) return { type: "null" };
  if (Array.isArray(value)) return { type: "array", items: value.length ? inferSchema(value[0]) : {} };
  if (typeof value === "object") {
    const properties = Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, inferSchema(item)])
    );
    return { type: "object", properties };
  }
  if (typeof value === "number") return { type: Number.isInteger(value) ? "integer" : "number" };
  return { type: typeof value };
}

/**
 * Whether a schema names no fields, so a recorded response says more than it
 * does. That is FastAPI's `{}` for an untyped body, its
 * `{type: "object", additionalProperties: true}` for `dict[str, Any]`, a map of
 * plain values such as `dict[str, str]`, and a list of any of these.
 */
export function isEmptySchema(schema: Schema | null | undefined): boolean {
  if (!schema) return true;
  const keys = Object.keys(schema).filter((key) => key !== "title" && key !== "description");
  if (keys.length === 0) return true;
  if (schema.$ref || schema.anyOf || schema.oneOf || schema.allOf) return false;
  if (schema.type === "array") return isEmptySchema((schema.items as Schema | undefined) ?? null);
  if (schema.type !== "object" || schema.properties) return false;
  const values = schema.additionalProperties;
  if (values === undefined || values === true) return true;
  if (typeof values !== "object" || values === null) return false;
  const inner = values as Schema;
  return !inner.$ref && !inner.properties && !inner.anyOf && !inner.oneOf && inner.type !== "array";
}
