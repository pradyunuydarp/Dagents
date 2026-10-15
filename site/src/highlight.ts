import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import yaml from "highlight.js/lib/languages/yaml";

/**
 * Syntax highlighting for code blocks.
 *
 * Only the languages the site shows are registered, so the bundle carries no
 * grammar it never uses. The colours come from the `--ds-syntax-*` tokens in
 * the design system, so they follow the light, dark and system theme.
 */

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("dockerfile", dockerfile);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("python", python);
hljs.registerLanguage("sql", sql);
hljs.registerLanguage("yaml", yaml);

/** Names a code block may use for a registered language. */
const ALIASES: Record<string, string> = {
  curl: "bash",
  js: "javascript",
  py: "python",
  sh: "bash",
  shell: "bash",
  yml: "yaml"
};

/** The registered language for a code block's label, or null for plain text. */
export function languageOf(label: string | undefined): string | null {
  if (!label) return null;
  const name = label.trim().toLowerCase();
  const language = ALIASES[name] ?? name;
  return hljs.getLanguage(language) ? language : null;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * HTML for the inside of a `<code>` element. Text in a language that is not
 * registered is escaped and left plain.
 */
export function highlight(code: string, label: string | undefined): string {
  const language = languageOf(label);
  if (!language) return escapeHtml(code);
  return hljs.highlight(code, { language, ignoreIllegals: true }).value;
}
