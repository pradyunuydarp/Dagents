#!/usr/bin/env node
/**
 * Open the published pages in a browser after a Pages deploy, and check what a
 * visitor sees.
 *
 * The deploy job's other checks request each page and its link preview image
 * with curl. A page can answer 200 and still show nothing: a script file that
 * 404s, a recording from an older commit, or a demo that cannot reach its API
 * all sit behind a 200. This script opens each page in Chromium on its real
 * origin and checks it:
 *
 * | Page | What it checks | When a check fails |
 * |---|---|---|
 * | NL2SQL demo | the recorded banner and `recording.json` name this commit; the first sample shows SQL and a trace; the page calls no API | the run fails |
 * | framework site | the home page renders "Dagents"; a Learn page draws its Mermaid diagrams; each demo card opens its demo | the run fails |
 * | stroke demo, replay build | the recorded banner and `recording.json` name this commit; the overview lists 3 or more hospitals; a hospital page draws its charts; the page calls no API | the run fails |
 * | stroke demo, live build | the page loads and shows no recorded banner | the run fails |
 * | stroke demo, live build | the API answers within the page's cold-start budget; the footer says the records are in Postgres; the overview lists 3 or more hospitals; a hospital page draws its charts; "Who does what" says the agents run in process | a warning, or a failure with `--api-problems fail` |
 *
 * Every page may send GET, HEAD and OPTIONS requests only. The browser stops
 * any other request before it leaves, and the run fails, so this check never
 * writes to the deployed API. It clicks nothing that posts: the stroke demo
 * sends a probe, a pilot, an entry or a new hospital only when a person clicks.
 *
 * Pages serves files through a cache for up to 10 minutes, so in the minutes
 * after a deploy a page can still be the old one. Each page is checked again,
 * in a new browser context, until it passes or `--wait` seconds have gone by.
 * Each page address gets a query string that changes on every attempt, and
 * `recording.json` is fetched the same way.
 *
 * The deployed API's address is never printed. A request to it is shown as
 * `<the API>` and its path.
 *
 * Usage:
 *   node scripts/check_published_pages.mjs                  # every page, in the Pages deploy job
 *   node scripts/check_published_pages.mjs --only healthcare --api-problems fail --warm-up
 *   node scripts/check_published_pages.mjs --published /tmp/local.env --healthcare-mode replay --wait 0
 *
 * Options:
 *   --only LIST            site, nl2sql and healthcare, comma-separated (default: all three)
 *   --healthcare-mode M    live or replay: how the Pages build built the stroke demo
 *                          (default: the HEALTHCARE_MODE variable)
 *   --commit SHA           the commit the replays must name (default: GITHUB_SHA,
 *                          then `git rev-parse HEAD`)
 *   --published FILE       the env file with the page addresses (default: env/.env.published)
 *   --wait SECONDS         how long to keep checking a page that fails (default: 360)
 *   --delay SECONDS        the pause between two attempts (default: 30)
 *   --api-problems MODE    warn (default) or fail: what a problem of the live API does
 *   --warm-up              before a live stroke demo, send one timed GET to the health
 *                          route of the API named by HEALTHCARE_DEMO_PUBLIC_API_URL in
 *                          the published file, so an API that does not answer is
 *                          reported apart from a broken page
 *
 * Variables:
 *   HEALTHCARE_MODE        live or replay, from the Pages build job
 *   CHROMIUM_PATH          the browser to launch
 *   GITHUB_STEP_SUMMARY    where the run summary goes, in GitHub Actions
 *
 * playwright-core comes from site/node_modules, so run `npm ci` in site/ first.
 *
 * Exits 0 when every page passes (warnings allowed), 1 when one fails or the
 * options are wrong, and 2 when no browser or no playwright-core is available,
 * so a machine without Chromium reports "skipped" rather than "passed".
 * `tests/test_published_pages_wiring.py` tests the request rule and the
 * workflow steps that run this script, and runs it in Chromium against a
 * stand-in Pages tree and a stand-in API (in the CI frontends job).
 */
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** The key of each page's address in env/.env.published. */
export const PAGE_KEYS = {
  site: "DAGENTS_PUBLIC_SITE_URL",
  nl2sql: "NL2SQL_DEMO_PUBLIC_SITE_URL",
  healthcare: "HEALTHCARE_DEMO_PUBLIC_SITE_URL"
};

/** The order the pages are checked in. NL2SQL first: its commit shows that the deploy has reached the cache. */
export const PAGES = ["nl2sql", "site", "healthcare"];

/** The only methods a published page may send. */
export const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Web fonts load from Google Fonts. They are optional: the design tokens list fallback fonts. */
const FONT_HOST = /^fonts\.(googleapis|gstatic)\.com$/;

/** The query parameter that makes each attempt miss the cache. */
const BUST = "published-check";

/** What a hosting platform answers while the API starts; the stroke demo retries these (`api.ts`). */
const WAKING_STATUSES = new Set([502, 503, 504]);

/** Extra time after the page's own cold-start budget, for the page to draw what the API sent. */
const COLD_START_MARGIN_MS = 30_000;

/** How long a page may take to show something that needs no API. */
const PAGE_TIMEOUT_MS = 30_000;

// --- pure rules, tested by tests/test_published_pages_wiring.py -------------

/**
 * Every `KEY=value` line of an env file, read as the shell reads it: the value
 * is everything after the first "=", and the last line wins.
 */
export function readPublished(text) {
  const values = {};
  for (const line of String(text).split(/\r?\n/)) {
    const index = line.indexOf("=");
    if (index <= 0 || line.startsWith("#")) continue;
    values[line.slice(0, index)] = line.slice(index + 1);
  }
  return values;
}

/**
 * Where a request goes: `page` (an origin a published page is served from),
 * `font` (Google Fonts), `local` (a `data:` or `blob:` address) or
 * `external` (anything else; for the stroke demo, that is its API).
 */
export function sideOf(url, pageOrigins) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return "external";
  }
  if (["data:", "blob:", "about:"].includes(parsed.protocol)) return "local";
  if (FONT_HOST.test(parsed.hostname)) return "font";
  return pageOrigins.has(parsed.origin) ? "page" : "external";
}

/**
 * Whether a published page may send this request, and where it goes.
 *
 * Only GET, HEAD and OPTIONS are allowed, to every origin. The pages are
 * static, a replay answers from its recording without the network, and a live
 * stroke demo reads the API with GET. Any other method from a published page
 * is a request that could write to the deployed API.
 */
export function requestVerdict(method, url, pageOrigins) {
  return {
    allowed: SAFE_METHODS.has(String(method).toUpperCase()),
    side: sideOf(url, pageOrigins)
  };
}

/**
 * Whose problem a console error is: `external` when it names or comes from an
 * external address (the API, for the stroke demo), `font` for Google Fonts,
 * and `page` otherwise.
 */
export function consoleSide(text, locationUrl, pageOrigins) {
  if (locationUrl) {
    const side = sideOf(locationUrl, pageOrigins);
    if (side === "external" || side === "font") return side;
  }
  const named = String(text).match(/https?:\/\/[^\s'"<>)]+/g) ?? [];
  const sides = named.map((url) => sideOf(url, pageOrigins));
  if (sides.includes("external")) return "external";
  if (sides.includes("font")) return "font";
  return "page";
}

/** The commit a recorded banner names ("... at commit 66b3691e5f on ..."), or null. */
export function bannerCommit(text) {
  return String(text).match(/at commit\s+([0-9a-f]{7,40})/i)?.[1] ?? null;
}

/** Whether a banner's short commit is the start of the expected full commit. */
export function sameCommit(shown, expected) {
  return Boolean(shown && expected && shown.length >= 7 && expected.toLowerCase().startsWith(shown.toLowerCase()));
}

// --- output ----------------------------------------------------------------

/** Origins whose address must never be printed: the stroke demo's API. */
const hidden = new Set();
/** The same addresses without their scheme, for a message that names only the host. */
const hiddenHosts = new Set();

function hideOrigin(url) {
  try {
    const { origin, protocol, host } = new URL(url);
    if (protocol === "http:" || protocol === "https:") {
      hidden.add(origin);
      hiddenHosts.add(host);
    }
  } catch {
    // Not an address; nothing to hide.
  }
}

/** `text` with every one of `origins` replaced by "<the API>". */
export function redactText(text, origins) {
  let out = String(text);
  for (const origin of origins) out = out.split(origin).join("<the API>");
  return out;
}

// Whole origins first, so "https://host" becomes "<the API>" and not "https://<the API>".
const redact = (text) => redactText(text, [...hidden, ...hiddenHosts]);

function say(line = "") {
  console.log(redact(line));
}

/** An address for a message: without the cache-busting query, and the API by name. */
function where(url) {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete(BUST);
    if (hidden.has(parsed.origin)) return `<the API>${parsed.pathname}`;
    return parsed.href;
  } catch {
    return String(url);
  }
}

const summary = [];

function annotate(level, text) {
  // GitHub reads one annotation per line.
  say(`::${level}::${String(text).replace(/\r?\n/g, " ")}`);
}

// --- options ---------------------------------------------------------------

function fail(message) {
  say(`FAIL: ${message}`);
  process.exit(1);
}

function parseOptions(argv) {
  const options = {
    only: [...PAGES],
    healthcareMode: process.env.HEALTHCARE_MODE ?? "",
    commit: process.env.GITHUB_SHA ?? "",
    published: join(REPO_ROOT, "env", ".env.published"),
    waitSeconds: 360,
    delaySeconds: 30,
    apiProblems: "warn",
    warmUp: false
  };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    const value = argv[index + 1];
    const need = () => {
      if (value === undefined || value.startsWith("--")) fail(`${name} needs a value`);
      index += 1;
      return value;
    };
    if (name === "--warm-up") options.warmUp = true;
    else if (name === "--only") options.only = need().split(",").map((item) => item.trim()).filter(Boolean);
    else if (name === "--healthcare-mode") options.healthcareMode = need();
    else if (name === "--commit") options.commit = need();
    else if (name === "--published") options.published = resolve(need());
    else if (name === "--wait") options.waitSeconds = Number(need());
    else if (name === "--delay") options.delaySeconds = Number(need());
    else if (name === "--api-problems") options.apiProblems = need();
    else fail(`unknown option ${name}`);
  }
  const unknown = options.only.filter((item) => !PAGES.includes(item));
  if (unknown.length || options.only.length === 0) fail(`--only takes ${PAGES.join(", ")}, not ${unknown.join(", ") || "nothing"}`);
  if (!["warn", "fail"].includes(options.apiProblems)) fail("--api-problems must be warn or fail");
  for (const key of ["waitSeconds", "delaySeconds"]) {
    if (!Number.isFinite(options[key]) || options[key] < 0) fail("--wait and --delay take a number of seconds");
  }
  if (options.only.includes("healthcare") && !["live", "replay"].includes(options.healthcareMode)) {
    fail(
      `the stroke demo's build mode must be live or replay, not '${options.healthcareMode}'. ` +
        "The Pages build job reports it as its healthcare_mode output, and the deploy job passes it as HEALTHCARE_MODE."
    );
  }
  if (options.warmUp && !(options.only.includes("healthcare") && options.healthcareMode === "live")) {
    fail("--warm-up applies only to a live stroke demo: pass --only with healthcare and the live build mode");
  }
  const needsCommit = options.only.includes("nl2sql") || (options.only.includes("healthcare") && options.healthcareMode === "replay");
  if (needsCommit && !options.commit) {
    try {
      options.commit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: REPO_ROOT, encoding: "utf8" }).trim();
    } catch {
      fail("no commit to compare the replays with: pass --commit or set GITHUB_SHA");
    }
  }
  if (needsCommit && !/^[0-9a-f]{40}$/i.test(options.commit)) fail(`--commit must be a full commit hash, not '${options.commit}'`);
  return options;
}

/** The stroke demo's cold-start budget, read from its transport, so the two cannot differ. */
function coldStartBudgetMs() {
  const source = readFileSync(join(REPO_ROOT, "apps/healthcare-demo/frontend/src/api.ts"), "utf8");
  const match = source.match(/COLD_START_BUDGET_MS\s*=\s*([\d_]+)/);
  if (!match) fail("apps/healthcare-demo/frontend/src/api.ts no longer defines COLD_START_BUDGET_MS");
  return Number(match[1].replace(/_/g, ""));
}

// --- the browser -----------------------------------------------------------

export function loadPlaywright() {
  for (const folder of ["site", "apps/healthcare-demo/frontend"]) {
    const manifest = join(REPO_ROOT, folder, "package.json");
    try {
      return createRequire(manifest)("playwright-core");
    } catch {
      // Try the next folder.
    }
  }
  return null;
}

/** A Chromium the driver can launch: CHROMIUM_PATH, this repo's container image, or the GitHub runner's Chrome. */
export function findBrowser() {
  const candidates = [
    process.env.CHROMIUM_PATH,
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser"
  ].filter(Boolean);
  return candidates.find((path) => existsSync(path)) ?? null;
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

/** An address with a query string that changes on every call, so no cache answers it. */
function busted(url) {
  const parsed = new URL(url);
  parsed.searchParams.set(BUST, `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`);
  return parsed.href;
}

function check(name, ok, detail = "") {
  return { name, ok: Boolean(ok), detail: String(detail ?? "") };
}

/** The text a visitor reads in the first element a locator finds, with its spaces collapsed; empty when there is none. */
async function textOf(locator) {
  if ((await locator.count()) === 0) return "";
  return ((await locator.first().innerText().catch(() => "")) ?? "").replace(/\s+/g, " ").trim();
}

/**
 * A new browser context that records every request, failure and console error,
 * and stops every request a published page may not send.
 */
async function watchedContext(browser, origins) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const seen = { refused: [], external: [], responses: [], failed: [], console: [], pageErrors: [], platform: new Set() };
  await context.route("**/*", async (route) => {
    const request = route.request();
    const verdict = requestVerdict(request.method(), request.url(), origins);
    if (verdict.side === "external") {
      // A request to /api/ on another origin is the stroke demo calling its API.
      if (new URL(request.url()).pathname.startsWith("/api/")) hideOrigin(request.url());
      seen.external.push({ method: request.method(), url: request.url() });
    }
    if (!verdict.allowed) {
      seen.refused.push({ method: request.method(), url: request.url(), side: verdict.side });
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  const open = async () => {
    const page = await context.newPage();
    page.on("response", (response) => {
      if (response.status() < 400) return;
      // While a sleeping API starts, the hosting platform answers 502, 503 or
      // 504 without the app's X-Dagents-App header, and the stroke demo
      // retries such a request by design. Only the app's own errors count.
      if (WAKING_STATUSES.has(response.status()) && !response.headers()["x-dagents-app"]) {
        seen.platform.add(response.url());
        return;
      }
      seen.responses.push({ url: response.url(), status: response.status() });
    });
    page.on("requestfailed", (request) => {
      const error = request.failure()?.errorText ?? "";
      // A refused request is reported as refused, and a request cut off by the
      // check's own navigation is not a fault of the page.
      if (/ERR_BLOCKED_BY_CLIENT|ERR_ABORTED/.test(error)) return;
      seen.failed.push({ url: request.url(), error });
    });
    page.on("console", (message) => {
      if (message.type() === "error") seen.console.push({ text: message.text(), url: message.location()?.url ?? "" });
    });
    page.on("pageerror", (error) => seen.pageErrors.push(error.message));
    return page;
  };
  return { context, seen, open };
}

/** The failures in what a context saw, split into the published files' side and the external side. */
function problems(seen, origins) {
  const page = [];
  const external = [];
  const add = (side, text) => {
    if (side === "page" || side === "local") page.push(text);
    else if (side === "external") external.push(text);
  };
  for (const { url, status } of seen.responses) add(sideOf(url, origins), `${where(url)} answered ${status}`);
  for (const { url, error } of seen.failed) add(sideOf(url, origins), `${where(url)} failed (${error})`);
  for (const { text, url } of seen.console) {
    if (!seen.platform.has(url)) add(consoleSide(text, url, origins), `console: ${text}`);
  }
  for (const message of seen.pageErrors) page.push(`uncaught error: ${message}`);
  return { page: [...new Set(page)], external: [...new Set(external)] };
}

function refusedCheck(seen) {
  return check(
    "the page sent only GET, HEAD and OPTIONS requests",
    seen.refused.length === 0,
    seen.refused.map(({ method, url }) => `stopped a ${method} to ${where(url)}`).join(" | ") || "no other method"
  );
}

/** Fetch a recording past the cache and return its commit, or a reason it has none. */
async function recordingCommit(context, pageUrl) {
  const url = busted(new URL("recording.json", pageUrl).href);
  try {
    const response = await context.request.get(url, { timeout: PAGE_TIMEOUT_MS });
    if (response.status() !== 200) return { commit: null, detail: `recording.json answered ${response.status()}` };
    const body = await response.json();
    return { commit: typeof body.commit === "string" ? body.commit : null, detail: `recording.json names ${body.commit}` };
  } catch (error) {
    return { commit: null, detail: `recording.json could not be read: ${error.message}` };
  }
}

/** Wait for the recorded banner to name its commit, and compare it and recording.json with the expected commit. */
async function replayCommitChecks(page, context, pageUrl, bannerSelector, expected) {
  const banner = page.locator(bannerSelector).first();
  await page
    .waitForFunction((selector) => /at commit/i.test(document.querySelector(selector)?.textContent ?? ""), bannerSelector, {
      timeout: PAGE_TIMEOUT_MS
    })
    .catch(() => {});
  const shown = bannerCommit(await textOf(banner));
  const recording = await recordingCommit(context, pageUrl);
  return [
    check("the recorded banner names this commit", sameCommit(shown, expected), `shows ${shown ?? "no commit"}, expected ${expected.slice(0, 10)}`),
    check("recording.json names this commit", recording.commit === expected, recording.detail)
  ];
}

/** Wait until every available chart on a hospital page has drawn or failed, and check that each one drew. */
async function hospitalChartsCheck(page, timeout) {
  await page
    .waitForFunction(
      () => {
        const figures = Array.from(document.querySelectorAll('[data-panel="hospital-charts"] figure[data-available="yes"]'));
        if (figures.length === 0) return false;
        return figures.every((figure) => {
          const image = figure.querySelector("img.chart-image");
          return (image && image.complete && image.naturalWidth > 0) || figure.querySelector(".banner.error");
        });
      },
      null,
      { timeout, polling: 500 }
    )
    .catch(() => {});
  const charts = await page.evaluate(() => {
    const figures = Array.from(document.querySelectorAll('[data-panel="hospital-charts"] figure[data-available="yes"]'));
    const drawn = figures.filter((figure) => {
      const image = figure.querySelector("img.chart-image");
      return image && image.complete && image.naturalWidth > 0;
    });
    return { listed: figures.length, drawn: drawn.length, title: document.querySelector("main h1")?.textContent ?? "" };
  });
  return check(
    "a hospital page draws its charts",
    charts.listed > 0 && charts.drawn === charts.listed,
    `${charts.drawn} of ${charts.listed} charts drawn on ${charts.title.trim() || "the hospital page"}`
  );
}

async function openFirstHospital(page) {
  const link = page.locator('[data-panel="consortium"] tbody tr[data-site] a').first();
  await link.click({ timeout: PAGE_TIMEOUT_MS });
  await page.waitForSelector('[data-panel="hospital-charts"]', { timeout: PAGE_TIMEOUT_MS });
}

// --- the pages ---------------------------------------------------------------

async function checkNl2sql({ open, context, seen }, run) {
  const url = run.urls.nl2sql;
  const page = await open();
  const results = [];
  await page.goto(busted(url), { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
  results.push(...(await replayCommitChecks(page, context, url, ".ds-banner.notice", run.commit)));

  const loaded = await page
    .waitForFunction(() => (document.querySelector("textarea.question")?.value ?? "").length > 0, null, { timeout: PAGE_TIMEOUT_MS })
    .then(() => true)
    .catch(() => false);
  if (loaded) {
    await page.click(".run-btn");
    await page
      .waitForFunction(
        () => (document.querySelector("pre.ds-code.sql")?.textContent ?? "").trim() || document.querySelector(".notice--warn, .ds-banner--deny"),
        null,
        { timeout: PAGE_TIMEOUT_MS }
      )
      .catch(() => {});
  }
  const sql = await textOf(page.locator("pre.ds-code.sql").first());
  const steps = await page.locator("ol.trace > li").count();
  const notice = await textOf(page.locator(".notice--warn, .ds-banner--deny").first());
  results.push(
    check(
      "the first sample shows SQL and a trace",
      loaded && /\bselect\b/i.test(sql) && steps > 0,
      loaded ? `${steps} trace steps; SQL "${sql.slice(0, 60)}"${notice ? `; ${notice}` : ""}` : "no sample loaded"
    )
  );
  const found = problems(seen, run.origins);
  results.push(refusedCheck(seen));
  results.push(check("the replay calls no API", seen.external.length === 0, seen.external.map((item) => where(item.url)).join(" | ") || "no request to another origin"));
  results.push(check("no failed request and no console error", found.page.length + found.external.length === 0, [...found.page, ...found.external].join(" | ") || "none"));
  return { results };
}

async function checkSite({ open, seen }, run) {
  const url = run.urls.site;
  const page = await open();
  const results = [];
  await page.goto(busted(url), { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
  const title = await page.waitForSelector("main h1", { timeout: PAGE_TIMEOUT_MS }).then(() => textOf(page.locator("main h1"))).catch(() => "");
  results.push(check('the home page renders "Dagents"', title === "Dagents", title || "no heading"));

  await page.goto(`${busted(url)}#/learn/03-architecture`, { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
  await page.waitForSelector("main h1", { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
  await page
    .waitForFunction(
      () => {
        const nodes = Array.from(document.querySelectorAll(".mermaid"));
        return nodes.length > 0 && nodes.every((node) => node.querySelector("svg") || node.classList.contains("is-error"));
      },
      null,
      { timeout: PAGE_TIMEOUT_MS }
    )
    .catch(() => {});
  const diagrams = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll(".mermaid"));
    return { total: nodes.length, drawn: nodes.filter((node) => node.querySelector("svg") && !node.classList.contains("is-error")).length };
  });
  results.push(
    check(
      "a Learn page draws its Mermaid diagrams",
      diagrams.total > 0 && diagrams.drawn === diagrams.total,
      `#/learn/03-architecture: ${diagrams.drawn} of ${diagrams.total} diagrams drawn`
    )
  );

  // Each card on the home page opens its demo. The demos' own checks below
  // check the rest; here the page only has to land and show its heading.
  const demos = [
    { key: "healthcare", shell: ".wordmark", text: "Stroke triage demo" },
    { key: "nl2sql", shell: "h1", text: "Natural language to SQL" }
  ];
  for (const demo of demos) {
    await page.goto(`${busted(url)}#/`, { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
    await page.waitForSelector("#demos a.ds-btn--primary", { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
    const target = run.urls[demo.key];
    const cards = page.locator("#demos a.ds-btn--primary");
    const hrefs = await cards.evaluateAll((links) => links.map((link) => link.href));
    const index = hrefs.findIndex((href) => where(href).split("#")[0] === target);
    let shown = "";
    if (index !== -1) {
      await Promise.all([
        page.waitForURL((address) => address.href.startsWith(target), { timeout: PAGE_TIMEOUT_MS }).catch(() => {}),
        cards.nth(index).click()
      ]);
      shown = await page.waitForSelector(demo.shell, { timeout: PAGE_TIMEOUT_MS }).then(() => textOf(page.locator(demo.shell).first())).catch(() => "");
    }
    results.push(
      check(
        `the ${demo.key === "healthcare" ? "stroke" : "NL2SQL"} demo card opens the demo`,
        index !== -1 && shown.includes(demo.text),
        index === -1 ? `no card links ${target} (cards link ${hrefs.map(where).join(", ")})` : `${target} shows "${shown || "nothing"}"`
      )
    );
  }

  // The stroke demo's API calls belong to its own check, so only the
  // published files' side counts here.
  const found = problems(seen, run.origins);
  results.push(refusedCheck(seen));
  results.push(check("no failed request and no console error from the published files", found.page.length === 0, found.page.join(" | ") || "none"));
  return { results };
}

async function checkHealthcareReplay({ open, context, seen }, run) {
  const url = run.urls.healthcare;
  const page = await open();
  const results = [];
  await page.goto(busted(url), { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
  await page.waitForSelector(".wordmark", { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
  const banner = (await page.locator('[data-banner="recorded"]').count()) > 0;
  results.push(check("the page says it replays a recorded run", banner, banner ? "recorded banner shown" : "no recorded banner: this looks like a live build"));
  results.push(...(await replayCommitChecks(page, context, url, '[data-banner="recorded"]', run.commit)));

  await page.waitForSelector('[data-panel="consortium"] tbody tr[data-site]', { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
  const hospitals = await page.locator('[data-panel="consortium"] tbody tr[data-site]').count();
  results.push(check("the overview lists 3 or more hospitals", hospitals >= 3, `${hospitals} hospitals`));
  const mode = await page.locator('[data-panel="who-does-what"] [data-agents-mode]').getAttribute("data-agents-mode").catch(() => null);
  results.push(check('"Who does what" says the agents run in process', mode === "in-process", `data-agents-mode=${mode}`));
  await page.waitForFunction(() => /Records:/.test(document.querySelector(".foot")?.textContent ?? ""), null, { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
  const footer = await textOf(page.locator(".foot"));
  results.push(check("the footer says where the records are", footer.includes("Records:"), footer.match(/Records:[^.]*\.?/)?.[0] ?? "no records line"));

  if (hospitals > 0) {
    await openFirstHospital(page).catch(() => {});
    results.push(await hospitalChartsCheck(page, PAGE_TIMEOUT_MS));
  } else {
    results.push(check("a hospital page draws its charts", false, "no hospital to open"));
  }

  const found = problems(seen, run.origins);
  results.push(refusedCheck(seen));
  results.push(
    check(
      "the replay calls no API",
      seen.external.filter((item) => sideOf(item.url, run.origins) === "external").length === 0,
      seen.external.map((item) => where(item.url)).join(" | ") || "no request to another origin"
    )
  );
  results.push(check("no failed request and no console error", found.page.length + found.external.length === 0, [...found.page, ...found.external].join(" | ") || "none"));
  return { results };
}

/** The part of the live stroke demo that depends only on Pages: the page loads, and it is a live build. */
async function checkHealthcareLivePage({ open, seen }, run) {
  const url = run.urls.healthcare;
  const page = await open();
  // Whether the page ever showed the banner it shows while the API starts.
  await page.addInitScript(() => {
    window.__dagentsConnecting = false;
    new MutationObserver(() => {
      if (document.querySelector('[data-banner="connecting"]')) window.__dagentsConnecting = true;
    }).observe(document, { childList: true, subtree: true });
  });
  const results = [];
  await page.goto(busted(url), { waitUntil: "domcontentloaded", timeout: PAGE_TIMEOUT_MS });
  const shell = await page.waitForSelector(".wordmark", { timeout: PAGE_TIMEOUT_MS }).then(() => true).catch(() => false);
  results.push(check("the page loads", shell, shell ? "the header rendered" : "the header did not render, so the script did not run"));
  const recorded = (await page.locator('[data-banner="recorded"]').count()) > 0;
  results.push(
    check(
      "the page is a live build, with no recorded banner",
      shell && !recorded,
      recorded
        ? "the page replays a recording, so it does not call the API. The last Pages build found the API unavailable; that run's summary says why."
        : "no recorded banner"
    )
  );
  const found = problems(seen, run.origins);
  results.push(refusedCheck(seen));
  results.push(check("no failed request and no console error from the published files", found.page.length === 0, found.page.join(" | ") || "none"));
  return { results, page };
}

/** The part of the live stroke demo that depends on the API. Run once, after the page passed. */
async function checkHealthcareLiveApi(page, seen, run) {
  const results = [];
  const budget = run.coldStartBudgetMs;
  const started = Date.now();
  const outcome = await page
    .waitForFunction(
      () => {
        if (document.querySelector('[data-panel="consortium"] tbody tr[data-site]')) return "loaded";
        const error = document.querySelector(".banners .banner.error");
        return error ? `error: ${error.textContent}` : null;
      },
      null,
      { timeout: budget + COLD_START_MARGIN_MS, polling: 500 }
    )
    .then((handle) => handle.jsonValue())
    .catch(() => "timeout");
  const seconds = Math.round((Date.now() - started) / 1000);
  const connecting = await page.evaluate(() => window.__dagentsConnecting === true).catch(() => false);
  const retried = seen.platform.size ? `; the platform answered ${seen.platform.size} address(es) while the API started` : "";
  const waited = `${connecting ? '; the page showed "Connecting to the demo server" first' : ""}${retried}`;
  results.push(
    check(
      "the consortium loads within the page's cold-start budget",
      outcome === "loaded",
      outcome === "loaded"
        ? `loaded after ${seconds} s${waited}`
        : outcome === "timeout"
          ? `nothing after ${seconds} s; the page waits ${budget / 1000} s for the API${waited}`
          : `${outcome.slice(0, 200)} (after ${seconds} s${waited})`
    )
  );
  if (outcome === "loaded") {
    const hospitals = await page.locator('[data-panel="consortium"] tbody tr[data-site]').count();
    results.push(check("the overview lists 3 or more hospitals", hospitals >= 3, `${hospitals} hospitals`));
    await page.waitForFunction(() => /Records:/.test(document.querySelector(".foot")?.textContent ?? ""), null, { timeout: PAGE_TIMEOUT_MS }).catch(() => {});
    const footer = await textOf(page.locator(".foot"));
    results.push(
      check(
        "the footer says the records are in Postgres",
        footer.includes("Records: Stored in Postgres, one schema per hospital."),
        footer.match(/Records:[^.]*\.?/)?.[0] ?? "no records line"
      )
    );
    const modeLocator = page.locator('[data-panel="who-does-what"] [data-agents-mode]');
    await page
      .waitForFunction(() => document.querySelector('[data-panel="who-does-what"] [data-agents-mode]')?.getAttribute("data-agents-mode") !== "unknown", null, {
        timeout: PAGE_TIMEOUT_MS
      })
      .catch(() => {});
    const mode = await modeLocator.getAttribute("data-agents-mode").catch(() => null);
    results.push(check('"Who does what" says the agents run in process', mode === "in-process", `data-agents-mode=${mode}`));
    const notReady = await textOf(page.locator('[data-banner="not-ready"]'));
    results.push(check("no store is reported not ready", !notReady, notReady || "every store ready"));
    await openFirstHospital(page).catch(() => {});
    results.push(await hospitalChartsCheck(page, budget));
  }
  const gets = seen.external.filter((item) => sideOf(item.url, run.origins) === "external" && item.method === "GET").length;
  results.push(check("the page read the API with GET requests", gets > 0, `${gets} GET requests to the API`));
  const found = problems(seen, run.origins);
  results.push(check("no failed API request, console error or uncaught error", found.external.length === 0 && seen.pageErrors.length === 0,
    [...found.external, ...seen.pageErrors.map((message) => `uncaught error: ${message}`)].join(" | ") || "none"));
  return results;
}

/** One warm-up GET to the API's health route, timed, before the browser opens the live demo. */
async function warmUp(base, budgetMs) {
  const started = Date.now();
  const deadline = started + budgetMs + COLD_START_MARGIN_MS;
  let last = "no answer";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(new URL("/api/v1/health", base), {
        signal: AbortSignal.timeout(Math.max(1000, Math.min(30_000, deadline - Date.now())))
      });
      last = `status ${response.status}`;
      if (response.status === 200) break;
    } catch {
      last = "no answer";
    }
    await sleep(5000);
  }
  const seconds = Math.round((Date.now() - started) / 1000);
  const ok = last === "status 200" && seconds * 1000 <= budgetMs;
  return check(
    "the API answers its health check within the page's cold-start budget",
    ok,
    last === "status 200"
      ? `answered after ${seconds} s; the page waits ${budgetMs / 1000} s`
      : `${last} after ${seconds} s; the page waits ${budgetMs / 1000} s, so a visitor sees an error`
  );
}

// --- running the checks ----------------------------------------------------

/**
 * Check one page until it passes or the wait is over. Each attempt gets a new
 * browser context, so no cache and no state carries over.
 */
async function untilPassing(label, run, attempt) {
  for (let number = 1; ; number += 1) {
    const watched = await watchedContext(run.browser, run.origins);
    let outcome;
    try {
      outcome = await attempt(watched, run);
    } catch (error) {
      outcome = { results: [check("the check ran to the end", false, error.message.split("\n")[0])] };
    }
    const failed = outcome.results.filter((item) => !item.ok);
    if (failed.length === 0 || Date.now() + run.delayMs > run.deadline) {
      return { ...outcome, watched, attempts: number };
    }
    say(`  attempt ${number}: ${failed.map((item) => item.name).join("; ")}. Checking again in ${run.delayMs / 1000} s.`);
    await watched.context.close();
    await sleep(run.delayMs);
  }
}

function report(heading, results, level) {
  say(`\n${heading}`);
  summary.push(`#### ${heading}`, "");
  let failures = 0;
  for (const item of results) {
    const mark = item.ok ? "ok  " : level === "warning" ? "warn" : "FAIL";
    say(`  ${mark}  ${item.name}${item.detail ? ` — ${item.detail}` : ""}`);
    summary.push(`- ${item.ok ? "ok" : level === "warning" ? "**warning**" : "**failed**"}: ${item.name}${item.detail ? ` (${item.detail})` : ""}`);
    if (!item.ok) {
      failures += 1;
      annotate(level, `${heading}: ${item.name}. ${item.detail}`);
    }
  }
  summary.push("");
  return failures;
}

async function main() {
  const options = parseOptions(process.argv.slice(2));
  const published = readPublished(readFileSync(options.published, "utf8"));
  // Every address is read, whichever pages are checked: the site's demo cards
  // must link the demos' addresses.
  const urls = {};
  for (const key of PAGES) {
    const value = published[PAGE_KEYS[key]];
    if (!value) fail(`${PAGE_KEYS[key]} is missing from ${options.published}`);
    try {
      urls[key] = new URL(value).href;
    } catch {
      fail(`${PAGE_KEYS[key]} is not an address: '${value}'`);
    }
  }
  // The API's address is hidden in every message. The page's own API requests
  // are hidden as they happen (watchedContext), and the warm-up asks this one.
  if (published.HEALTHCARE_DEMO_PUBLIC_API_URL) hideOrigin(published.HEALTHCARE_DEMO_PUBLIC_API_URL);
  if (options.warmUp && !published.HEALTHCARE_DEMO_PUBLIC_API_URL) {
    fail(`--warm-up needs HEALTHCARE_DEMO_PUBLIC_API_URL in ${options.published}`);
  }

  const playwright = loadPlaywright();
  if (!playwright) {
    say("SKIP: playwright-core is not installed (run npm ci in site/)");
    process.exit(2);
  }
  const executablePath = findBrowser();
  if (!executablePath) {
    say("SKIP: no Chromium found; set CHROMIUM_PATH to run the check");
    process.exit(2);
  }

  const origins = new Set(Object.values(urls).map((url) => new URL(url).origin));
  const browser = await playwright.chromium.launch({ executablePath });
  const run = {
    browser,
    urls,
    origins,
    commit: options.commit,
    coldStartBudgetMs: options.only.includes("healthcare") ? coldStartBudgetMs() : 0,
    deadline: Date.now() + options.waitSeconds * 1000,
    delayMs: options.delaySeconds * 1000
  };
  say("Published pages in a browser");
  if (options.commit) say(`  commit: ${options.commit}`);
  if (options.only.includes("healthcare")) say(`  stroke demo build: ${options.healthcareMode}`);
  summary.push("### Published pages in a browser", "");

  let failures = 0;
  let warnings = 0;
  try {
    for (const key of PAGES.filter((item) => options.only.includes(item))) {
      if (key === "nl2sql") {
        const outcome = await untilPassing("NL2SQL", run, checkNl2sql);
        failures += report(`NL2SQL demo (${urls.nl2sql})`, outcome.results, "error");
        await outcome.watched.context.close();
      } else if (key === "site") {
        const outcome = await untilPassing("site", run, checkSite);
        failures += report(`Framework site (${urls.site})`, outcome.results, "error");
        await outcome.watched.context.close();
      } else if (options.healthcareMode === "replay") {
        const outcome = await untilPassing("stroke demo", run, checkHealthcareReplay);
        failures += report(`Stroke demo, replay build (${urls.healthcare})`, outcome.results, "error");
        await outcome.watched.context.close();
      } else {
        const apiLevel = options.apiProblems === "fail" ? "error" : "warning";
        const apiResults = [];
        if (options.warmUp) apiResults.push(await warmUp(published.HEALTHCARE_DEMO_PUBLIC_API_URL, run.coldStartBudgetMs));
        const outcome = await untilPassing("stroke demo", run, checkHealthcareLivePage);
        const pagePassed = outcome.results.every((item) => item.ok);
        if (pagePassed && outcome.page) {
          apiResults.push(...(await checkHealthcareLiveApi(outcome.page, outcome.watched.seen, run)));
          // A request the page sent while it showed the API's answers is the
          // page's own doing, so it fails the run whatever the API did.
          const refused = refusedCheck(outcome.watched.seen);
          outcome.results = outcome.results.map((item) => (item.name === refused.name ? refused : item));
        } else {
          apiResults.push(check("the live API part ran", false, "not checked, because the page itself failed"));
        }
        failures += report(`Stroke demo, live build (${urls.healthcare})`, outcome.results, "error");
        const apiFailures = report("Stroke demo, what the deployed API serves", apiResults, apiLevel);
        if (apiLevel === "error") failures += apiFailures;
        else warnings += apiFailures;
        if (apiFailures && apiLevel === "warning") {
          summary.push(
            "The deployed API does not fail a Pages run. Run the **Healthcare API check** workflow for a diagnosis.",
            ""
          );
        }
        await outcome.watched.context.close();
      }
    }
  } finally {
    await browser.close();
  }

  const verdict = failures
    ? `${failures} check(s) failed`
    : warnings
      ? `every page passed; ${warnings} warning(s) about the deployed API`
      : "every check passed";
  say(`\n${verdict}`);
  summary.push(`**${verdict}.**`, "");
  if (process.env.GITHUB_STEP_SUMMARY) {
    try {
      appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${redact(summary.join("\n"))}\n`);
    } catch (error) {
      say(`could not write the run summary: ${error.message}`);
    }
  }
  process.exit(failures ? 1 : 0);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    say(`FAIL: unexpected error: ${error.message}`);
    process.exit(1);
  });
}
