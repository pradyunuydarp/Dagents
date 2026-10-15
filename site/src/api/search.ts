/**
 * The search index of the API reference: the overview's topics, modules,
 * interfaces, the classes they name or call, endpoints, planner commands and
 * objects.
 */

import { FuzzyIndex, type Match } from "../fuzzy";
import { apiRoute, slugify } from "../markdown";
import { commandSpec, endpointSpec, type ApiData } from "./data";
import { MODULE_DOCS, OVERVIEW } from "./modules";

export type EntryKind = "Endpoint" | "Command" | "Interface" | "Class" | "Object" | "Module" | "Topic";

export interface SearchEntry {
  kind: EntryKind;
  label: string;
  detail: string;
  href: string;
  /** Other words that find the entry: handler names, files, the classes it calls. */
  extra: string;
}

const KIND_ORDER: EntryKind[] = ["Endpoint", "Command", "Object", "Interface", "Topic", "Class", "Module"];

export function buildIndex(data: ApiData): SearchEntry[] {
  const entries: SearchEntry[] = [];
  const classes = new Map<string, SearchEntry>();
  const add = (entry: Omit<SearchEntry, "extra">, ...extra: (string | null | undefined)[]) => {
    entries.push({ ...entry, extra: extra.filter(Boolean).join(" ") });
  };
  // A class name such as Settings is used by several services, so a class is
  // listed once per interface that names or calls it.
  const addClass = (name: string, detail: string, href: string, file: string | null) => {
    const key = `${name} ${href}`;
    if (classes.has(key)) return;
    const entry: SearchEntry = { kind: "Class", label: name, detail, href, extra: file ?? "" };
    classes.set(key, entry);
  };

  // The overview's sections: errors, rate limits, idempotency and the rest.
  for (const match of OVERVIEW.matchAll(/^##\s+(.+)$/gm)) {
    const heading = match[1].trim();
    add({ kind: "Topic", label: heading, detail: "Overview", href: apiRoute("overview", slugify(heading.replace(/[`*_]/g, ""))) });
  }

  for (const module of MODULE_DOCS) {
    add({ kind: "Module", label: module.label, detail: "Module", href: apiRoute(module.slug) }, module.slug);
    for (const iface of module.interfaces) {
      const where = `${module.label} · ${iface.service}`;
      const href = apiRoute(module.slug, iface.anchor);
      add({ kind: "Interface", label: iface.heading, detail: where, href }, ...iface.code, ...iface.classes.map((c) => c.name));
      for (const entry of iface.classes) addClass(entry.name, `${iface.heading} · ${module.label}`, href, entry.file);
      for (const ref of iface.endpoints) {
        if (ref.kind === "command") {
          const spec = commandSpec(data, ref.words);
          if (!spec) continue;
          const doc = iface.docs.find((item) => item.ref.key === ref.key);
          add(
            { kind: "Command", label: ref.key, detail: doc?.heading ?? iface.heading, href: apiRoute(module.slug, spec.id) },
            spec.handler.function,
            ...spec.calls.map((call) => `${call.module}.${call.function}`)
          );
          continue;
        }
        const spec = endpointSpec(data, iface.service, ref.method, ref.path);
        if (!spec) continue;
        const doc = iface.docs.find((item) => item.ref.key === ref.key);
        add(
          {
            kind: "Endpoint",
            label: `${ref.method} ${ref.path}`,
            detail: `${doc?.heading ?? spec.summary ?? iface.heading} · ${iface.service}`,
            href: apiRoute(module.slug, spec.id)
          },
          spec.handler.function,
          ...spec.handler.calls.map((call) => `${call.class ?? ""} ${call.name}`)
        );
        for (const call of spec.handler.calls) {
          if (call.class) addClass(call.class, `${iface.heading} · ${module.label}`, apiRoute(module.slug, iface.anchor), call.file);
        }
      }
    }
    for (const obj of module.objects) {
      add({ kind: "Object", label: obj.name, detail: `${obj.heading} · ${module.label}`, href: apiRoute(module.slug, obj.anchor) }, obj.classPath);
    }
  }
  entries.push(...classes.values());
  return entries;
}

/** The fuzzy index over the entries: a match in the label counts most. */
export function searchIndex(entries: SearchEntry[]): FuzzyIndex<SearchEntry> {
  return new FuzzyIndex(entries, (entry) => [
    { text: entry.label, weight: 1 },
    { text: entry.detail, weight: 0.5 },
    { text: entry.extra, weight: 0.4 }
  ]);
}

/** The best matches, typos and partial words included; equal scores go by kind, then label. */
export function search(index: FuzzyIndex<SearchEntry>, query: string, limit = 30): Match<SearchEntry>[] {
  return index
    .search(query, Infinity)
    .sort(
      (a, b) =>
        b.score - a.score ||
        KIND_ORDER.indexOf(a.item.kind) - KIND_ORDER.indexOf(b.item.kind) ||
        a.item.label.localeCompare(b.item.label)
    )
    .slice(0, limit);
}
