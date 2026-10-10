import { useEffect } from "react";

import { ThemeToggle } from "./ThemeToggle";
import { REPO_LINKS, REPO_URL } from "./config";
import { Api } from "./pages/Api";
import { Home } from "./pages/Home";
import { Learn } from "./pages/Learn";
import { href, useRoute } from "./router";

/** Old single-section links from the previous version of the site. */
const LEGACY: Record<string, string> = {
  architecture: "learn/03-architecture",
  governance: "learn/02-privacy-and-governance"
};

export default function App() {
  const route = useRoute();
  const [section, slug] = route.segments;

  useEffect(() => {
    const target = section ? LEGACY[section] : undefined;
    if (target) window.location.replace(href(target));
  }, [section]);

  useEffect(() => {
    if (!section || section === "demos") document.title = "Dagents";
  }, [section]);

  let page;
  if (!section) page = <Home anchor={route.anchor} />;
  else if (section === "demos") page = <Home anchor="demos" />;
  else if (section === "learn") page = <Learn slug={slug ?? ""} anchor={route.anchor} />;
  else if (section === "api") page = <Api />;
  else
    page = (
      <div className="block">
        <h1>Page not found</h1>
        <p>
          <a href={href("")}>Go to the home page</a>
        </p>
      </div>
    );

  const current = section === "learn" ? "learn" : section === "api" ? "api" : "home";

  return (
    <>
      <header className="masthead">
        <div className="ds-shell masthead-inner">
          <a className="wordmark" href={href("")}>
            Dagents
          </a>
          <nav aria-label="Sections">
            <a href={href("")} aria-current={current === "home" ? "page" : undefined}>
              Home
            </a>
            <a href={href("learn")} aria-current={current === "learn" ? "page" : undefined}>
              Learn
            </a>
            <a href={href("api")} aria-current={current === "api" ? "page" : undefined}>
              API
            </a>
            <a href={href("", "demos")}>Demos</a>
            <a href={REPO_URL}>GitHub</a>
          </nav>
          <ThemeToggle />
        </div>
      </header>

      <main className="ds-shell page">{page}</main>

      <footer className="footer">
        <div className="ds-shell footer-inner">
          <p className="ds-note">Dagents is open source under the MIT license.</p>
          <nav aria-label="More links">
            <a href={REPO_URL}>Source</a>
            <a href={REPO_LINKS.guide}>Guide on GitHub</a>
            <a href={REPO_LINKS.contributing}>Contributor guide</a>
          </nav>
        </div>
      </footer>
    </>
  );
}
