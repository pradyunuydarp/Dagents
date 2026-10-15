/**
 * The copy button on code blocks.
 *
 * Guide pages are HTML strings and API samples are React elements, so both
 * write the same button markup and one click handler on the document serves
 * every block. The button copies the text of the `<pre>` inside the nearest
 * element marked `data-copy-scope`.
 */

const COPY_ICON =
  '<svg class="code-copy-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">' +
  '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5v-.5A1.5 1.5 0 0 0 9 1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5"/></svg>';

const DONE_ICON =
  '<svg class="code-copy-done" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">' +
  '<path d="M3 8.5l3 3 7-7"/></svg>';

export const COPY_LABEL = "Copy code";
const DONE_LABEL = "Copied";

/** The inside of a copy button, shared by the HTML and React versions. */
export const COPY_BUTTON_INNER = COPY_ICON + DONE_ICON;

/** The button as an HTML string, for code blocks rendered from Markdown. */
export const COPY_BUTTON_HTML = `<button type="button" class="code-copy" aria-label="${COPY_LABEL}" title="${COPY_LABEL}">${COPY_BUTTON_INNER}</button>`;

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

const timers = new WeakMap<HTMLElement, number>();

async function onClick(event: MouseEvent): Promise<void> {
  const button = (event.target as Element | null)?.closest<HTMLButtonElement>("button.code-copy");
  if (!button) return;
  const code = button.closest("[data-copy-scope]")?.querySelector("pre");
  if (!code) return;
  if (!(await writeClipboard(code.textContent ?? ""))) return;

  button.dataset.copied = "true";
  button.setAttribute("aria-label", DONE_LABEL);
  button.title = DONE_LABEL;
  window.clearTimeout(timers.get(button));
  timers.set(
    button,
    window.setTimeout(() => {
      delete button.dataset.copied;
      button.setAttribute("aria-label", COPY_LABEL);
      button.title = COPY_LABEL;
    }, 1500)
  );
}

/** Listen for copy clicks once, for the whole page. */
export function installCodeCopy(): void {
  document.addEventListener("click", (event) => void onClick(event));
}
