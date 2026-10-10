/**
 * The learning guide, read from `docs/learn/`.
 *
 * The guide is written once, as Markdown in the repository, so it reads the same
 * on GitHub and on this site. Vite bundles the files at build time.
 */

const files = import.meta.glob("../../docs/learn/*.md", {
  query: "?raw",
  import: "default",
  eager: true
}) as Record<string, string>;

export interface GuidePage {
  /** URL slug: `""` for the index, otherwise the file name without `.md`. */
  slug: string;
  /** File name in `docs/learn/`. */
  file: string;
  /** Title from the page's first `#` heading. */
  title: string;
  /** Short label for navigation. */
  label: string;
  markdown: string;
}

/** Reading order. Files not listed here are not shown. */
const ORDER: { file: string; label: string }[] = [
  { file: "README.md", label: "Overview" },
  { file: "01-federated-learning.md", label: "1. Federated learning" },
  { file: "02-privacy-and-governance.md", label: "2. Privacy and governance" },
  { file: "03-architecture.md", label: "3. How Dagents is built" },
  { file: "04-apis.md", label: "4. Using the APIs" },
  { file: "05-hands-on.md", label: "5. Run it yourself" },
  { file: "references.md", label: "References" }
];

function titleOf(markdown: string, fallback: string): string {
  const match = markdown.match(/^#\s+(.+)$/m);
  return match ? match[1].trim() : fallback;
}

export const GUIDE: GuidePage[] = ORDER.flatMap(({ file, label }) => {
  const entry = Object.entries(files).find(([path]) => path.endsWith(`/${file}`));
  if (!entry) return [];
  const markdown = entry[1];
  return [
    {
      slug: file === "README.md" ? "" : file.replace(/\.md$/, ""),
      file,
      title: titleOf(markdown, label),
      label,
      markdown
    }
  ];
});

export function guidePage(slug: string): GuidePage | undefined {
  return GUIDE.find((page) => page.slug === slug);
}

/** The first mermaid diagram in a guide page, for reuse on the home page. */
export function firstDiagram(file: string): string | null {
  const page = GUIDE.find((entry) => entry.file === file);
  const match = page?.markdown.match(/```mermaid\n([\s\S]*?)```/);
  return match ? match[1] : null;
}
