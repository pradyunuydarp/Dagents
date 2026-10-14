/**
 * The teaching resources: slide decks, written notes and reports.
 *
 * The list is `docs/presentation/resources.json`, so it has one home in the
 * repository. `scripts/export_presentations.py` fills in each item's `facts`
 * (sizes, slide counts, dates, the PDF and preview it made), and
 * `tests/test_site_resources.py` fails when a listed file changes without that.
 *
 * This module has no browser-only code, because `vite.config.ts` imports
 * `publishedFiles` to copy the same files into the built site.
 */

import document from "../../docs/presentation/resources.json";

interface FileFacts {
  path: string;
  bytes: number;
  sha256: string;
}

export interface ResourceFacts {
  /** Date of the last commit that changed the source file, `YYYY-MM-DD`. */
  updated: string;
  bytes: number;
  sha256: string;
  /** Decks: the number of slides. */
  slides?: number;
  /** Report PDFs: the number of pages. */
  pages?: number;
  /** Decks: the PDF made from the deck. */
  pdf?: FileFacts & { pages: number };
  /** Decks: a small image of one slide. */
  preview?: FileFacts & { slide: number; width: number; height: number };
}

export interface ResourceItem {
  /** Stable slug. The published files are named after it. */
  id: string;
  kind: "deck" | "pdf" | "markdown";
  title: string;
  description: string;
  audience: string;
  /** A limitation of this item, stated once. */
  note?: string;
  /** The file in the repository, relative to its root. */
  source: string;
  previewSlide?: number;
  facts: ResourceFacts;
}

export interface ResourceGroup {
  id: string;
  title: string;
  intro: string;
  note?: string;
  noteLink?: { label: string; path: string };
  items: ResourceItem[];
}

export interface ResourceList {
  groups: ResourceGroup[];
  excluded: { path: string; reason: string }[];
}

export const RESOURCES = document as unknown as ResourceList;

/** The folder in the built site that holds the files. */
export const RESOURCE_DIR = "resources";

export interface PublishedFile {
  /** Path in the built site, relative to its base: `resources/<id>.<ext>`. */
  path: string;
  /** The file in the repository it is copied from. */
  from: string;
  bytes: number;
}

/**
 * The files the site publishes for one item. Markdown files are not copied;
 * the page links to them on GitHub, where they render.
 */
export function publishedFiles(item: ResourceItem): {
  pptx?: PublishedFile;
  pdf?: PublishedFile;
  preview?: PublishedFile;
} {
  const at = (ext: string) => `${RESOURCE_DIR}/${item.id}.${ext}`;
  const { facts } = item;
  if (item.kind === "deck" && facts.pdf && facts.preview) {
    return {
      pptx: { path: at("pptx"), from: item.source, bytes: facts.bytes },
      pdf: { path: at("pdf"), from: facts.pdf.path, bytes: facts.pdf.bytes },
      preview: { path: at("webp"), from: facts.preview.path, bytes: facts.preview.bytes }
    };
  }
  if (item.kind === "pdf") {
    return { pdf: { path: at("pdf"), from: item.source, bytes: facts.bytes } };
  }
  return {};
}

/** Every published file, for the build step. */
export function allPublishedFiles(list: ResourceList = RESOURCES): PublishedFile[] {
  return list.groups.flatMap((group) =>
    group.items.flatMap((item) => Object.values(publishedFiles(item)).filter(Boolean) as PublishedFile[])
  );
}

/** A file size in the units a download link shows: `315 KB`, `1.6 MB`. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December"
];

/** `2026-09-04` as `4 September 2026`, the same in every browser locale. */
export function formatDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  return `${day} ${MONTHS[month - 1]} ${year}`;
}
