import { COPY_BUTTON_INNER, COPY_LABEL } from "./codeCopy";

/**
 * The copy button of a code block written in React. It must sit inside an
 * element marked `data-copy-scope`, next to the block's `<pre>`;
 * `codeCopy.ts` handles its clicks.
 */
export function CopyButton() {
  return (
    <button
      type="button"
      className="code-copy"
      aria-label={COPY_LABEL}
      title={COPY_LABEL}
      dangerouslySetInnerHTML={{ __html: COPY_BUTTON_INNER }}
    />
  );
}
