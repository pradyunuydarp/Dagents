import { useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * Mermaid diagrams, rendered in the browser.
 *
 * The library is large, so it is loaded only when a page has a diagram. Its
 * colours are read from the design system tokens, so diagrams follow the light
 * and dark themes and stay grey: colour is kept for permit, narrow and deny.
 */

type Scheme = "light" | "dark";

function currentScheme(): Scheme {
  const chosen = document.documentElement.getAttribute("data-theme");
  if (chosen === "dark" || chosen === "light") return chosen;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The colour scheme in effect, updated when the theme or the OS setting changes. */
export function useColorScheme(): Scheme {
  const [scheme, setScheme] = useState<Scheme>(currentScheme);
  useEffect(() => {
    const update = () => setScheme(currentScheme());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", update);
    };
  }, []);
  return scheme;
}

/** Mermaid theme variables taken from the current design tokens. */
function themeVariables(scheme: Scheme): Record<string, string | boolean> {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const panel = token("--ds-panel");
  const inset = token("--ds-inset");
  const ink = token("--ds-ink");
  const ink2 = token("--ds-ink-2");
  const line = token("--ds-line-strong");
  return {
    darkMode: scheme === "dark",
    fontFamily: "Archivo, system-ui, sans-serif",
    fontSize: "15px",
    background: panel,
    textColor: ink,
    lineColor: ink2,
    primaryColor: inset,
    primaryTextColor: ink,
    primaryBorderColor: line,
    secondaryColor: panel,
    tertiaryColor: panel,
    mainBkg: inset,
    nodeBorder: line,
    clusterBkg: panel,
    clusterBorder: line,
    titleColor: ink,
    edgeLabelBackground: panel,
    actorBkg: inset,
    actorBorder: line,
    actorTextColor: ink,
    actorLineColor: line,
    signalColor: ink2,
    signalTextColor: ink,
    labelBoxBkgColor: inset,
    labelBoxBorderColor: line,
    labelTextColor: ink,
    loopTextColor: ink,
    noteBkgColor: inset,
    noteTextColor: ink,
    noteBorderColor: line,
    activationBkgColor: inset,
    activationBorderColor: line
  };
}

let diagramCount = 0;
let queue: Promise<void> = Promise.resolve();

function showError(node: HTMLElement, error: unknown): void {
  node.textContent = `This diagram could not be drawn: ${String(error)}`;
  node.classList.add("is-error");
}

async function draw(root: HTMLElement, scheme: Scheme): Promise<void> {
  const nodes = Array.from(root.querySelectorAll<HTMLElement>(".mermaid[data-diagram]"));
  if (nodes.length === 0) return;
  let mermaid: typeof import("mermaid").default;
  try {
    ({ default: mermaid } = await import("mermaid"));
  } catch (error) {
    nodes.forEach((node) => showError(node, error));
    return;
  }
  // Mermaid sizes boxes by measuring text, so the web fonts must be loaded first.
  await document.fonts.ready;
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    themeVariables: themeVariables(scheme),
    flowchart: { wrappingWidth: 320 }
  });
  for (const node of nodes) {
    const source = decodeURIComponent(node.dataset.diagram ?? "");
    try {
      diagramCount += 1;
      const { svg } = await mermaid.render(`diagram-${diagramCount}`, source);
      node.innerHTML = svg;
      node.classList.remove("is-error");
    } catch (error) {
      showError(node, error);
    }
  }
}

/**
 * Draw every `.mermaid[data-diagram]` placeholder inside `root`.
 *
 * Mermaid keeps global state while it renders, so calls run one at a time.
 */
export function renderDiagrams(root: HTMLElement, scheme: Scheme): Promise<void> {
  queue = queue.then(() => draw(root, scheme)).catch((error) => console.error(error));
  return queue;
}

/**
 * Rendered HTML with its diagrams drawn.
 *
 * `anchor` scrolls to a heading once the page is in place. The diagrams are
 * drawn afterwards, so the scroll may shift slightly as they appear.
 */
export function RenderedHtml({ html, anchor, className }: { html: string; anchor?: string | null; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const scheme = useColorScheme();

  useLayoutEffect(() => {
    if (anchor) document.getElementById(anchor)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [html, anchor]);

  useEffect(() => {
    if (ref.current) void renderDiagrams(ref.current, scheme);
  }, [html, scheme]);

  return <div ref={ref} className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}

/** One diagram from Mermaid source, with an optional caption. */
export function Diagram({ source, caption }: { source: string; caption?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const scheme = useColorScheme();
  const html = `<div class="mermaid" data-diagram="${encodeURIComponent(source)}"></div>`;

  useEffect(() => {
    if (ref.current) void renderDiagrams(ref.current, scheme);
  }, [html, scheme]);

  return (
    <figure className="diagram">
      <div ref={ref} dangerouslySetInnerHTML={{ __html: html }} />
      {caption && <figcaption>{caption}</figcaption>}
    </figure>
  );
}
