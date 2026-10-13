import { useEffect, useState } from "react";

/**
 * The colour scheme in effect, copied from the framework site
 * (`site/src/Mermaid.tsx`).
 *
 * Charts are drawn by the backend in a light and a dark version, so the page
 * asks for the one that matches. It follows both the theme toggle (the
 * `data-theme` attribute) and the OS setting when the toggle is on "System".
 */

export type Scheme = "light" | "dark";

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
