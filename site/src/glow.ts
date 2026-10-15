/**
 * A soft highlight that follows the pointer over cards and rows.
 *
 * One listener on the document finds the card under the pointer and gives it
 * the pointer position as --mx and --my, plus a data-glow attribute that the
 * stylesheet draws the highlight from. Only a mouse triggers it, so a tap on a
 * phone never leaves a highlight behind. Presentation only.
 */

/** The elements that glow. Add a selector here; the style is in styles.css. */
const GLOW_SELECTOR = [
  ".hero",
  ".steps li",
  ".cards .card",
  ".home .guide-list a",
  ".pager a",
  ".resource",
  ".api-index-module"
].join(", ");

let current: HTMLElement | null = null;

function release(): void {
  current?.removeAttribute("data-glow");
  current = null;
}

function onPointerMove(event: PointerEvent): void {
  if (event.pointerType !== "mouse") return;
  const target = event.target instanceof Element ? event.target.closest<HTMLElement>(GLOW_SELECTOR) : null;
  if (target !== current) {
    release();
    current = target;
    current?.setAttribute("data-glow", "");
  }
  if (!current) return;
  const box = current.getBoundingClientRect();
  current.style.setProperty("--mx", `${event.clientX - box.left}px`);
  current.style.setProperty("--my", `${event.clientY - box.top}px`);
}

export function installGlow(): void {
  document.addEventListener("pointermove", onPointerMove, { passive: true });
  document.addEventListener("pointerleave", release);
}
