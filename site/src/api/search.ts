/**
 * The search index of the API reference: the overview's topics, modules,
 * interfaces, the classes they name or call, endpoints, planner commands and
 * objects.
 */

import { apiRoute, slugify } from "../markdown";
import { commandSpec, endpointSpec, type ApiData } from "./data";
import { MODULE_DOCS, OVERVIEW } from "./modules";

export type EntryKind = "Endpoint" | "Command" | "Interface" | "Class" | "Object" | "Module" | "Topic";

export interface SearchEntry {
  kind: EntryKind;
  label: string;
  detail: string;
  href: string;
  haystack: string;
}

const KIND_ORDER: EntryKind[] = ["Endpoint", "Command", "Object", "Interface", "Topic", "Class", "Module"];

export function buildIndex(data: ApiData): SearchEntry[] {
  const entries: SearchEntry[] = [];
  const classes = new Map<string, SearchEntry>();
  const add = (entry: Omit<SearchEntry, "haystack">, ...extra: (string | null | undefined)[]) => {
    entries.push({ ...entry, haystack: [entry.label, entry.detail, ...extra].join(" ").toLowerCase() });
  };
  // A class name such as Settings is used by several services, so a class is
  // listed once per interface that names or calls it.
  const addClass = (name: string, detail: string, href: string, file: string | null) => {
    const key = `${name} ${href}`;
    if (classes.has(key)) return;
    const entry: SearchEntry = { kind: "Class", label: name, detail, href, haystack: `${name} ${detail} ${file ?? ""}`.toLowerCase() };
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

export function search(index: SearchEntry[], query: string, limit = 30): SearchEntry[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const needle = terms.join(" ");
  return index
    .filter((entry) => terms.every((term) => entry.haystack.includes(term)))
    .map((entry) => {
      const label = entry.label.toLowerCase();
      const score =
        (label === needle ? 0 : label.startsWith(needle) ? 1 : label.includes(needle) ? 2 : 3) * 10 +
        KIND_ORDER.indexOf(entry.kind);
      return { entry, score };
    })
    .sort((a, b) => a.score - b.score || a.entry.label.localeCompare(b.entry.label))
    .slice(0, limit)
    .map(({ entry }) => entry);
}

