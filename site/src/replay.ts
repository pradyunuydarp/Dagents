/**
 * Start an element's CSS animations again from the beginning.
 *
 * A page that changes only its content keeps the same element, so the
 * element's opening animation does not run again on its own. Replaying it
 * keeps the element, and with it the reader's keyboard focus, instead of
 * replacing it. Nothing happens when no animation applies, for example when
 * the reader asks for less motion. Presentation only.
 */
export function replayAnimations(element: Element | null): void {
  for (const animation of element?.getAnimations?.() ?? []) {
    if (typeof CSSAnimation === "undefined" || !(animation instanceof CSSAnimation)) continue;
    animation.cancel();
    animation.play();
  }
}
