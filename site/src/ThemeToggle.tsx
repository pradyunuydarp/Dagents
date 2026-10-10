import { useEffect, useState } from "react";

/**
 * Light, dark or system theme.
 *
 * The design tokens define light on `:root`, dark under `prefers-color-scheme`,
 * and `:root[data-theme]` overrides the OS setting. Choosing "system" removes
 * the attribute so the OS setting applies again.
 */

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "dagents-theme";
const OPTIONS: { value: Theme; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" }
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
