/**
 * Browser check for the framework site.
 *
 * A build proves the site compiles. It does not prove that the guide pages
 * render, that every Mermaid diagram draws (a syntax error only shows up in a
 * browser), or that links between pages land somewhere. This serves the built
 * site, visits every page in Chromium and checks each of those.
 *
 * Usage:
 *   npm run build && npm run check
 *   node check.mjs [distDir]
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "passed".
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const distDir = resolve(process.argv[2] ?? "dist");

/** Locate a Chromium the driver can launch, preferring the preinstalled one. */
function findBrowser() {
  const candidates = [
    process.env.CHROMIUM_PATH,
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome"
  ].filter(Boolean);
  return candidates.find((path) => existsSync(path)) ?? null;
}

let chromium;
try {
  ({ chromium } = await import("playwright-core"));
} catch {
  console.log("SKIP: playwright-core is not installed (run npm install)");
  process.exit(2);
}

const executablePath = findBrowser();
if (!executablePath) {
  console.log("SKIP: no Chromium found; set CHROMIUM_PATH to run the check");
  process.exit(2);
}

if (!existsSync(join(distDir, "index.html"))) {
  console.log(`FAIL: ${distDir}/index.html not found; run npm run build first`);
  process.exit(1);
}

// A small static server for the built site. Pages live in the URL hash, so
// every path is either a file in dist/ or the index page.
const TYPES = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json"
};
const server = createServer((request, response) => {
  const path = normalize(decodeURIComponent(new URL(request.url, "http://localhost").pathname));
  let file = join(distDir, path);
  if (!file.startsWith(distDir) || !existsSync(file) || statSync(file).isDirectory()) {
    file = join(distDir, "index.html");
  }
  response.writeHead(200, { "Content-Type": TYPES[extname(file)] ?? "application/octet-stream" });
  response.end(readFileSync(file));
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const base = `http://127.0.0.1:${server.address().port}/`;

const failures = [];
const consoleErrors = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(name);
};

// Web fonts are optional: the design tokens list fallback fonts, and some
// sandboxes block the font host. Everything else must load.
const optional = (text) => /fonts\.(googleapis|gstatic)\.com/.test(text);

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on("console", (message) => {
  const source = `${message.text()} ${message.location()?.url ?? ""}`;
  if (message.type() === "error" && !optional(source)) consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${error.message}`));
page.on("requestfailed", (request) => {
  if (!optional(request.url())) consoleErrors.push(`request failed: ${request.url()}`);
});

/** Wait until every diagram on the page has drawn or failed, and report them. */
async function diagrams() {
  await page.waitForFunction(
    () =>
      Array.from(document.querySelectorAll(".mermaid")).every(
        (node) => node.querySelector("svg") || node.classList.contains("is-error")
      ),
    null,
    { timeout: 30000 }
  );
  return page.evaluate(() =>
    Array.from(document.querySelectorAll(".mermaid"))
      .filter((node) => node.classList.contains("is-error") || !node.querySelector("svg"))
      .map((node) => (node.textContent ?? "").slice(0, 160))
  );
}

async function open(hash) {
  await page.goto(`${base}${hash}`);
  await page.waitForSelector("main h1");
  return (await page.textContent("main h1"))?.trim() ?? "";
}

try {
  console.log(`Dagents framework site — browser check against ${distDir}\n`);

  // 1. The home page renders, with its diagram.
  const home = await open("#/");
  check("the home page renders", home === "Dagents", home);
  check("the home page diagram draws", (await diagrams()).length === 0);

  // 2. Every guide page renders and every diagram on it draws. The page list
  //    comes from the guide's own navigation, so a new page is checked too.
  await open("#/learn");
  const guide = await page.$$eval(".learn-nav a", (links) => links.map((a) => a.getAttribute("href")));
  check("the guide lists its pages", guide.length >= 7, `${guide.length} pages`);

  const internal = new Set();
  for (const hash of guide) {
    const title = await open(hash);
    const broken = await diagrams();
    check(`${hash} renders`, title.length > 0 && !/not found/i.test(title), title);
    check(`${hash} diagrams draw`, broken.length === 0, broken.join(" | "));
    const images = await page.evaluate(async () => {
      const broken = [];
      for (const image of Array.from(document.querySelectorAll("main img"))) {
        // Guide images load lazily, so bring each one into view first.
        image.scrollIntoView();
        await new Promise((done) => {
          if (image.complete) return done();
          image.addEventListener("load", done, { once: true });
          image.addEventListener("error", done, { once: true });
          setTimeout(done, 10000);
        });
        if (!image.naturalWidth) broken.push(image.getAttribute("src"));
      }
      return broken;
    });
    check(`${hash} images load`, images.length === 0, images.join(" | "));
    const text = await page.evaluate(() => document.querySelector("main").innerText);
    const leaked = ["```", "]("].filter((marker) => text.includes(marker));
    check(`${hash} has no raw Markdown`, leaked.length === 0, leaked.join(" "));
    for (const href of await page.$$eval("main a", (links) => links.map((a) => a.getAttribute("href")))) {
      if (href?.startsWith("#/")) internal.add(href);
    }
  }

  // 3. Every link between pages lands on a page, and on the heading it names.
  const missing = [];
  for (const hash of internal) {
    const title = await open(hash);
    if (/not found/i.test(title)) missing.push(hash);
    const anchor = hash.split("#")[2];
    if (anchor && !(await page.evaluate((id) => document.getElementById(id) !== null, anchor))) {
      missing.push(hash);
    }
  }
  check("every internal link resolves", missing.length === 0, missing.join(", ") || `${internal.size} links`);

  // 4. The API reference lists endpoints and the search narrows them.
  await open("#/api");
  const total = await page.textContent(".api-count");
  await page.fill(".api-filter input", "federation");
  const rows = await page.locator(".ds-table tbody tr").count();
  check("the API search finds endpoints", rows > 0, `${total?.trim()} -> ${rows} rows for "federation"`);

  // 5. Dark mode redraws the diagrams instead of breaking them.
  await open("#/learn/03-architecture");
  await page.click('.theme-toggle button:has-text("Dark")');
  await page.waitForTimeout(1000);
  check("diagrams draw in dark mode", (await diagrams()).length === 0);

  // 6. Nothing failed quietly along the way.
  const unique = [...new Set(consoleErrors)];
  check("no console errors or failed requests", unique.length === 0, unique.join(" | "));
} catch (error) {
  console.log(`  FAIL  unexpected error — ${error.message}`);
  failures.push("unexpected error");
} finally {
  await browser.close();
  server.close();
}

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
