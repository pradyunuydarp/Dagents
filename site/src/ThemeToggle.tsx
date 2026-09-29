import { useEffect, useState } from "react";

/**
 * Light / dark / system, as an explicit choice.
 *
 * The design tokens already answer all three states — `:root` carries light,
 * a `prefers-color-scheme` block carries dark for anyone who has not chosen,
 * and `:root[data-theme="…"]` lets a choice win over the OS either way. What
 * was missing was any way to make that choice: a reader on a light OS had no
 * route to the dark palette at all.
 *
 * "System" is a real third option rather than a default hidden behind one of
 * the other two, so choosing it removes the attribute and hands control back.
 */

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "dagents-theme";
const OPTIONS: { value: Theme; label: string }[] = [
  { value: "light", label: "light" },
  { value: "dark", label: "dark" },
  { value: "system", label: "system" }
];

/** Read the stored choice, tolerating a browser that refuses storage. */
function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // Private windows and blocked site data throw rather than return null.
  }
  return "system";
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(storedTheme);

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // The choice still applies for this page view; it just will not persist.
    }
  }, [theme]);

  return (
    <div className="theme-toggle" role="group" aria-label="Colour theme">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === theme ? "is-active" : undefined}
          aria-pressed={option.value === theme}
          onClick={() => setTheme(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
