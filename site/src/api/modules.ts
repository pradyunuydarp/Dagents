/**
 * The pages of the API reference, read from `docs/reference/api/`.
 *
 * The Markdown is small, so it is bundled with the page. The facts from the
 * code (`docs/reference/api-reference.json`) and the captured examples are
 * larger and load only on an API page (see `data.ts`).
 */

import { parseModule, type ModuleDoc } from "./parse";

const files = import.meta.glob("../../../docs/reference/api/*.md", {
  query: "?raw",
  import: "default",
  eager: true
}) as Record<string, string>;

function fileText(name: string): string {
  const entry = Object.entries(files).find(([path]) => path.endsWith(`/${name}`));
  return entry ? entry[1] : "";
}

/**
 * Module pages in reading order. `tests/test_api_reference.py` fails when a
 * module file in docs/reference/api/ is missing from this list.
 */
export const MODULES: { file: string; slug: string; label: string }[] = [
  { file: "agents.md", slug: "agents", label: "Agents" },
  { file: "framework-services.md", slug: "framework-services", label: "Framework services" },
  { file: "spring-services.md", slug: "spring-services", label: "Spring Boot services" },
  { file: "demo-apps.md", slug: "demo-apps", label: "Demo apps" },
  { file: "dagentsc.md", slug: "dagentsc", label: "dagentsc" }
];

export const OVERVIEW = fileText("overview.md");

export const MODULE_DOCS: (ModuleDoc & { label: string; file: string })[] = MODULES.map((module) => ({
  ...parseModule(module.slug, fileText(module.file)),
  label: module.label,
  file: module.file
}));

export function moduleDoc(slug: string) {
  return MODULE_DOCS.find((module) => module.slug === slug);
}
