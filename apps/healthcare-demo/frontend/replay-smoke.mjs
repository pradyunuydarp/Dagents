/**
 * Browser check of the replay build: the demo that GitHub Pages publishes
 * when the Pages workflow cannot use the deployed API.
 *
 * The Pages workflow builds the demo with `VITE_DAGENTS_STATIC=1` and no API
 * address whenever its probe of the deployed API fails. That page answers
 * every request from `recording.json` and calls no backend. `smoke.mjs`
 * drives the live transport only, so without this check nobody opens the
 * replay before visitors do. This check serves a built copy the way Pages
 * does (`serve-dist.mjs`: under the base path, a missing file answers 404)
 * and checks, in a real browser:
 *
 *   1. the banner says the page is a replay and names the recording's commit
 *      and date; the hospitals table, the header and "Who does what" show the
 *      recorded overview and status;
 *   2. the Guard answers every combination the page offers: each hospital,
 *      both values of "Requester verified", each granularity in the list and
 *      each cohort size the recording holds (read from the recording, as the
 *      page does). Each answer comes from the chosen hospital's Guard, as the
 *      note under the decision and the "Dagents API calls" table name it, and
 *      shows the recorded decision, the recorded systolic_bp strategy and the
 *      recorded count of released records. None says "Not recorded". The
 *      drive changes one control between two asks, so a page that sends an
 *      old value of a control fails. The page matches a probe by its exact
 *      body, so a key added to or removed from the body the page sends fails
 *      here;
 *   3. a cohort size the recording lacks shows the "Not recorded" note with
 *      the sizes it holds;
 *   4. before the pilot, each hospital page and the consortium charts show
 *      the answers recorded before it. Each hospital page must name the
 *      hospital the drive opened, and its "Dagents API calls" strips must
 *      name only that hospital's LMA. No render from the click on may show
 *      one hospital's heading over another hospital's numbers;
 *   5. the pilot replays: every release gate, the decision, the dispatch rows
 *      of every round, the audit chains, and the approve panel only when the
 *      recorded gates allow a release, with its button turned off;
 *   6. after the pilot, the consortium charts, each hospital's dashboard and
 *      its audit log show the answers recorded after it, with the same
 *      checks of which hospital the page shows;
 *   7. each chart, on the consortium page and on every hospital page, draws
 *      the recorded SVG for the light theme, and the recorded dark SVG after
 *      a switch to the dark theme;
 *   8. the data-entry form and the panel to add a hospital are turned off and
 *      say why; each hospital page lists the entered records the recording
 *      holds and says the replay cannot store an outcome;
 *   9. the page sends no API request, to the server or to any other origin,
 *      loads nothing from another origin except the optional web fonts, and
 *      logs no console error and no failed request.
 *
 * When a check stops the run, the drive prints the console errors and the
 * requests it collected until then, which often name the cause.
 *
 * Usage:
 *   node replay-smoke.mjs <dist> [--base /Dagents/healthcare-demo/] [--hold-hashchange <ms>]
 *
 * `<dist>` is a build made with VITE_DAGENTS_STATIC=1, no
 * VITE_HEALTHCARE_API_BASE, and DEMO_BASE_PATH equal to `--base`. The base
 * defaults to the path Pages publishes the demo under.
 *
 * `--hold-hashchange <ms>` holds every `hashchange` event for that many
 * milliseconds before the page's own listeners receive it. The page then
 * keeps showing the previous page under the new address for that long. In
 * one CI run the drive's next command ran before the page's route change, and
 * the drive read the previous hospital's page; this option makes that happen
 * on every route change, to show that the drive waits for the page it asked
 * for.
 *
 * Exits 0 on success, 1 on failure, and 2 with a line starting "SKIP" when no
 * browser is available, like smoke.mjs. CI treats 2 as a failure.
 */
import { createHash } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import { resolve } from "node:path";

import { normaliseBase, serveDist } from "./serve-dist.mjs";

const args = process.argv.slice(2);
const baseIndex = args.indexOf("--base");
const holdIndex = args.indexOf("--hold-hashchange");
const base = normaliseBase(baseIndex === -1 ? "/Dagents/healthcare-demo/" : args[baseIndex + 1]);
const holdHashchange = holdIndex === -1 ? 0 : Number(args[holdIndex + 1]);
const valueIndexes = new Set([baseIndex, holdIndex].filter((i) => i !== -1).map((i) => i + 1));
const dist = args.find((arg, i) => !arg.startsWith("--") && !valueIndexes.has(i));

if (!dist || !Number.isInteger(holdHashchange) || holdHashchange < 0) {
  console.log("usage: node replay-smoke.mjs <dist> [--base /Dagents/healthcare-demo/] [--hold-hashchange <ms>]");
  process.exit(1);
}
if (!existsSync(dist) || !statSync(dist).isDirectory() || !existsSync(resolve(dist, "index.html"))) {
  console.log(`FAIL  ${dist} is not a built copy of the demo: it has no index.html`);
  process.exit(1);
}

/** Locate a Chromium the driver can launch, preferring the preinstalled one. */
function findBrowser() {
  const candidates = [
    process.env.CHROMIUM_PATH,
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
  ].filter(Boolean);
  return candidates.find((p) => existsSync(p)) ?? null;
}

let chromium;
try {
  ({ chromium } = await import("playwright-core"));
} catch {
  console.log("SKIP: playwright-core is not installed (npm install to enable the replay check)");
  process.exit(2);
}
const executablePath = findBrowser();
if (!executablePath) {
  console.log("SKIP: no Chromium found; set CHROMIUM_PATH to run the replay check");
  process.exit(2);
}

const failures = [];
let passed = 0;
// A passing line reads "ok  <name>", with two spaces, as in smoke.mjs.
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (ok) passed += 1;
  else failures.push(name);
};

/** Raised by `must` to end the run: the checks after it would only wait and fail. */
class Stop extends Error {}

/** A check the rest depends on. When it fails, the run stops. */
const must = (name, ok, detail = "") => {
  check(name, ok, detail);
  if (!ok) throw new Stop(name);
};

/** After this many wrong Guard answers the loop stops, so a broken build fails in seconds. */
const MAX_WRONG_ANSWERS = 5;

/** The probe and pilot paths, as the page sends them. */
const PROBE_PATH = "/api/v1/governance:probe";
const PILOT_PATH = "/api/v1/pilot:run";

// Web fonts are optional: the design tokens list fallback fonts, and some
// sandboxes block the font host. The check never loads them.
const FONT_HOSTS = /^https:\/\/fonts\.(googleapis|gstatic)\.com\//;
const optional = (text) => /fonts\.(googleapis|gstatic)\.com/.test(text);

const server = await serveDist({ dist, base });
let browser = null;

// What the page logs and requests. The last checks read these lists, and a
// run that stops early prints them, because a 404 or a console error often
// names the cause of the failed check.
const consoleErrors = [];
const apiRequests = [];
const foreignRequests = [];

/** Print the first entries of each list collected so far. */
const printCollected = () => {
  const lists = [
    ["console errors and failed requests", consoleErrors],
    ["API requests", [...apiRequests, ...server.apiRequests]],
    ["requests to another origin", foreignRequests],
  ];
  for (const [what, list] of lists) {
    if (list.length === 0) continue;
    console.log(`  ${what} so far (${list.length}):`);
    for (const line of list.slice(0, 3)) console.log(`    ${line}`);
  }
};

/** The SHA-256 of a text, as hex, to compare an SVG without moving it around. */
const digest = (text) => createHash("sha256").update(text, "utf8").digest("hex");

try {
  console.log(
    `Dagents healthcare demo — replay check of ${resolve(dist)} at ${server.url}` +
      (holdHashchange > 0 ? `, with each hashchange held ${holdHashchange} ms` : "") +
      "\n",
  );

  // 0. The build carries its recording, served at the address the page reads.
  const recordingResponse = await fetch(`${server.url}recording.json`);
  must(
    "the build serves recording.json under its base path",
    recordingResponse.ok,
    `GET ${base}recording.json answered ${recordingResponse.status}`,
  );
  const recording = await recordingResponse.json();
  const entries = Array.isArray(recording.entries) ? recording.entries : [];

  /**
   * The recorded answer to a GET. Once the pilot has run, the page shows the
   * answer recorded after it when there is one (api.ts, `findEntry`).
   */
  const recorded = (path, afterPilot = false) => {
    const matches = entries.filter((e) => e.method === "GET" && e.path === path);
    const after = afterPilot ? matches.find((e) => e.phase === "after_pilot") : undefined;
    return (after ?? matches.find((e) => e.phase !== "after_pilot"))?.response;
  };
  const svgDigest = (path, theme, afterPilot) => {
    const response = recorded(`${path}?theme=${theme}`, afterPilot);
    return typeof response?.text === "string" ? digest(response.text) : null;
  };

  const overview = recorded("/api/v1/overview");
  const status = recorded("/api/v1/framework/status");
  const deployments = recorded("/api/v1/deployments");
  const pilot = entries.find((e) => e.method === "POST" && e.path === PILOT_PATH)?.response;
  const probes = entries.filter((e) => e.method === "POST" && e.path === PROBE_PATH);
  must(
    "the recording holds the overview, the status, the deployments, the probes and the pilot",
    Boolean(overview && status && deployments && pilot && probes.length > 0),
    overview && status && deployments && pilot && probes.length > 0
      ? `${probes.length} probes`
      : "scripts/check_demo_recordings.py healthcare says what is missing",
  );
  const sites = overview.hospitals.map((hospital) => hospital.site_id);
  // The cohort sizes come from the recording, as GuardProbe.tsx reads them.
  const cohorts = [...new Set(probes.map((e) => e.request?.cohort_size).filter((n) => typeof n === "number"))].sort(
    (a, b) => a - b,
  );

  browser = await chromium.launch({ executablePath });
  const context = await browser.newContext({ viewport: { width: 1280, height: 1100 }, colorScheme: "light" });
  if (holdHashchange > 0) {
    // Runs before the page's scripts, so this listener comes first. It stops
    // the event and sends a copy once the time is up.
    await context.addInitScript((ms) => {
      window.addEventListener(
        "hashchange",
        (event) => {
          if (event.held) return;
          event.stopImmediatePropagation();
          setTimeout(() => {
            const late = new HashChangeEvent("hashchange", { oldURL: event.oldURL, newURL: event.newURL });
            Object.defineProperty(late, "held", { value: true });
            window.dispatchEvent(late);
          }, ms);
        },
        { capture: true },
      );
    }, holdHashchange);
  }

  // Everything the page loads must come from the published tree. A request to
  // any other origin is refused and counted, except the optional fonts.
  await context.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(`${server.origin}/`)) return route.continue();
    if (!FONT_HOSTS.test(url)) foreignRequests.push(`${route.request().method()} ${url}`);
    return route.abort();
  });
  const page = await context.newPage();
  // Every answer comes from memory, so 15 seconds is a wide margin on a slow runner.
  page.setDefaultTimeout(15000);
  page.on("request", (request) => {
    try {
      const url = new URL(request.url());
      if (/(^|\/)api\//.test(url.pathname)) apiRequests.push(`${request.method()} ${url.pathname}${url.search}`);
    } catch {
      // blob: and data: addresses are not requests to a server.
    }
  });
  page.on("console", (message) => {
    const text = `${message.text()} ${message.location()?.url ?? ""}`;
    if (message.type() === "error" && !optional(text)) consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400 && !optional(response.url()))
      consoleErrors.push(`HTTP ${response.status()} ${response.url()}`);
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    if (!optional(url) && !foreignRequests.some((line) => line.endsWith(url))) {
      consoleErrors.push(`request failed: ${url} ${request.failure()?.errorText ?? ""}`);
    }
  });

  /** The text of an element, or "" when it is not there. */
  const textOf = async (locator) =>
    (
      (await locator
        .first()
        .textContent({ timeout: 2000 })
        .catch(() => "")) ?? ""
    ).trim();

  /**
   * Click a button the page disables while it works, and wait until it is
   * enabled again. Two answers in a row can look the same, so the check waits
   * for the button rather than for a change on the page.
   */
  const clickAndSettle = (button, timeout = 15000) =>
    button.evaluate(
      (element, ms) =>
        new Promise((done, fail) => {
          const timer = setTimeout(() => {
            observer.disconnect();
            fail(new Error(`"${element.textContent}" was still busy after ${ms} ms`));
          }, ms);
          const observer = new MutationObserver((records) => {
            // A record with an old value means the attribute was removed: the
            // button was disabled and is enabled again.
            if (records.some((record) => record.oldValue !== null) && !element.disabled) {
              clearTimeout(timer);
              observer.disconnect();
              done();
            }
          });
          observer.observe(element, { attributes: true, attributeFilter: ["disabled"], attributeOldValue: true });
          element.click();
        }),
      timeout,
    );

  /** Error banners on the page: an unrecorded request shows one. */
  const errorBanners = () =>
    page.locator(".banner.error, .not-loaded").evaluateAll((nodes) => nodes.map((node) => node.textContent.trim()));

  /** Switch the theme with the toggle in the header. */
  const chooseTheme = async (label) => {
    await page.getByRole("group", { name: "Colour theme" }).getByRole("button", { name: label, exact: true }).click();
  };

  /**
   * Wait until every available chart in `scope` shows the recorded SVG for
   * `theme`, drawn at a real size. Returns the problems, or [] when all match.
   */
  const chartsMatch = async (scope, listing, theme, afterPilot, timeout = 10000) => {
    const expected = Object.fromEntries(
      listing.charts
        .filter((chart) => chart.available)
        .map((chart) => [chart.chart_id, svgDigest(chart.svg_path, theme, afterPilot)]),
    );
    const deadline = Date.now() + timeout;
    let problems = [];
    do {
      const shown = await page
        .locator(scope)
        .evaluate(async (root) => {
          const hex = async (text) =>
            [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))]
              .map((byte) => byte.toString(16).padStart(2, "0"))
              .join("");
          const out = {};
          for (const figure of root.querySelectorAll("figure[data-chart]")) {
            const image = figure.querySelector("img.chart-image");
            if (!image || !image.complete || image.naturalWidth === 0 || !image.src.startsWith("blob:")) {
              out[figure.dataset.chart] = { available: figure.dataset.available, drawn: false };
              continue;
            }
            const text = await (await fetch(image.src)).text();
            out[figure.dataset.chart] = { available: figure.dataset.available, drawn: true, digest: await hex(text) };
          }
          return out;
        })
        .catch(() => null);
      problems = shown === null ? [`${scope} is not on the page`] : [];
      for (const chart of shown === null ? [] : listing.charts) {
        const item = shown[chart.chart_id];
        if (!item) problems.push(`${chart.chart_id} is not on the page`);
        else if (item.available !== (chart.available ? "yes" : "no"))
          problems.push(`${chart.chart_id} shows available=${item.available}`);
        else if (chart.available && expected[chart.chart_id] === null)
          problems.push(`${chart.chart_id} has no recorded ${theme} SVG`);
        else if (chart.available && !item.drawn) problems.push(`${chart.chart_id} is not drawn`);
        else if (chart.available && item.digest !== expected[chart.chart_id])
          problems.push(`${chart.chart_id} is not the recorded ${theme} SVG`);
      }
      for (const id of Object.keys(shown ?? {})) {
        if (!listing.charts.some((chart) => chart.chart_id === id))
          problems.push(`${id} is on the page but not in the recorded list`);
      }
      if (problems.length === 0) return [];
      await page.waitForTimeout(150);
    } while (Date.now() < deadline);
    return problems;
  };

  await page.goto(server.url, { waitUntil: "load", timeout: 60000 });

  // 1. The banner, the hospitals and the status.
  const banner = page.locator('[data-banner="recorded"]');
  await banner.waitFor({ timeout: 15000 }).catch(() => {});
  await page
    .locator('[data-banner="recorded"] .mono')
    .waitFor({ timeout: 15000 })
    .catch(() => {});
  const bannerText = await textOf(banner);
  // Without the banner this is not a replay build (or it never loaded), and
  // every check below would wait for answers that cannot come.
  must(
    "the banner says the page replays a recording and names its commit and date",
    bannerText.includes("Recorded run") &&
      bannerText.includes(String(recording.commit).slice(0, 10)) &&
      bannerText.includes(String(recording.captured_at).slice(0, 10)),
    bannerText.slice(0, 160) || "no banner",
  );

  const consortium = page.locator('[data-panel="consortium"]');
  await consortium
    .locator("table.hospitals tbody tr")
    .first()
    .waitFor({ timeout: 15000 })
    .catch(() => {});
  const rows = await consortium.locator("table.hospitals tbody tr[data-site]").evaluateAll((trs) =>
    trs.map((tr) => ({
      site: tr.getAttribute("data-site"),
      records: tr.querySelectorAll("td")[1]?.innerText.trim() ?? "",
    })),
  );
  const expectedRows = overview.hospitals.map((hospital) => ({
    site: hospital.site_id,
    records: hospital.record_count === 0 ? null : hospital.record_count.toLocaleString("en-GB"),
  }));
  must(
    "the hospitals table lists the recorded hospitals and their record counts, in order",
    rows.length === expectedRows.length &&
      rows.every(
        (row, i) =>
          row.site === expectedRows[i].site &&
          (expectedRows[i].records === null || row.records === expectedRows[i].records),
      ),
    `page ${rows.map((row) => `${row.site}:${row.records}`).join(", ")}; recorded ${expectedRows.map((row) => `${row.site}:${row.records}`).join(", ")}`,
  );
  const navSites = await page
    .locator('nav[aria-label="Pages"] a[href^="#/hospitals/"]')
    .evaluateAll((links) => links.map((link) => link.getAttribute("href").replace("#/hospitals/", "")));
  check("the header links to every recorded hospital", navSites.join(",") === sites.join(","), navSites.join(", "));
  const mode = await page
    .locator('[data-panel="who-does-what"] [data-agents-mode]')
    .first()
    .getAttribute("data-agents-mode", { timeout: 5000 })
    .catch(() => null);
  check(
    "'Who does what' shows the agent mode the recorded status reports",
    mode !== null && mode === status.agents?.mode,
    `page ${mode}, recorded ${status.agents?.mode}`,
  );

  // 2. The Guard answers every combination the page offers.
  const guard = page.locator('[data-panel="guard"]');
  const hospitalSelect = guard.getByRole("combobox", { name: "Hospital", exact: true });
  const verifiedBox = guard.getByLabel("Requester verified");
  const granularitySelect = guard.getByLabel("Granularity");
  const cohortInput = guard.getByLabel("Cohort size");
  const askButton = guard.getByRole("button", { name: "Ask the guard" });
  const optionValues = (select) =>
    select.locator("option").evaluateAll((options) => options.map((option) => option.value));

  const hospitalOptions = await optionValues(hospitalSelect);
  const granularities = await optionValues(granularitySelect);
  check(
    "the Guard's hospital list is the recorded hospitals",
    hospitalOptions.join(",") === sites.join(","),
    hospitalOptions.join(", "),
  );

  // The three hospitals' answers often hold the same decision, strategy and
  // count, so these alone cannot show which Guard answered. The page names it
  // twice: in the note under the decision ("Answer from <name>'s Guard") and
  // in the LMA rows of its "Dagents API calls" table.
  const readOutcome = () =>
    guard.evaluate((panel) => {
      const outcome = panel.querySelector(".outcome");
      const row = [...outcome.querySelectorAll("table.restrictions tbody tr")].find(
        (tr) => tr.querySelector("td")?.textContent.trim() === "systolic_bp",
      );
      const note = [...outcome.querySelectorAll("p")]
        .map((p) => p.textContent.replace(/\s+/g, " ").trim())
        .find((text) => text.startsWith("Answer from "));
      return {
        decision: outcome.querySelector(".verdict strong")?.textContent.trim() ?? null,
        strategy: row ? (row.querySelector("td.strategy")?.textContent.trim() ?? null) : null,
        released: outcome.querySelector('[data-released="records"]')?.textContent.trim() ?? null,
        answeredBy: note?.match(/^Answer from (.+?)['’]s Guard\./)?.[1] ?? null,
        lmaSites: [...outcome.querySelectorAll('[data-api-calls] tr[data-call-service="lma"]')].map((tr) =>
          tr.getAttribute("data-call-site"),
        ),
        notRecorded: outcome.querySelector(".banner.recorded")?.textContent.trim() ?? null,
        error: outcome.querySelector(".banner.error")?.textContent.trim() ?? null,
      };
    });
  /** The site id of a hospital the page names. The page shows the id itself for a hospital it does not list. */
  const siteNamed = (name) => overview.hospitals.find((hospital) => hospital.display_name === name)?.site_id ?? name;

  const probeFor = (site, verified, granularity, cohort) =>
    probes.find(
      (e) =>
        e.request.site_id === site &&
        e.request.verified === verified &&
        e.request.granularity === granularity &&
        e.request.cohort_size === cohort,
    );

  /**
   * Every combination of the controls, in an order where each combination
   * differs from the one before it in exactly one control. `lists` runs from
   * the control that changes least often to the one that changes most often.
   *
   * The panel builds its request in a React callback. If a control were
   * missing from that callback's list of dependencies, the page would send
   * the control's old value, but only when it is the one control changed
   * since the last ask. Changing one control at a time shows that mistake.
   */
  const oneChangeAtATime = (lists) => {
    if (lists.length === 0) return [[]];
    const [first, ...rest] = lists;
    const tail = oneChangeAtATime(rest);
    return first.flatMap((value, i) => (i % 2 === 0 ? tail : [...tail].reverse()).map((combo) => [value, ...combo]));
  };

  let asked = 0;
  const wrong = [];
  let current = { site: null, verified: null, granularity: null, cohort: null };
  // How often each control was the only one changed before an ask.
  const alone = { hospital: 0, verified: 0, granularity: 0, cohort: 0 };
  for (const [granularity, cohort, verified, site] of oneChangeAtATime([
    granularities,
    cohorts,
    [true, false],
    hospitalOptions,
  ])) {
    const changed = [];
    if (current.site !== site) {
      await hospitalSelect.selectOption(site);
      changed.push("hospital");
    }
    if (current.verified !== verified) {
      await (verified ? verifiedBox.check() : verifiedBox.uncheck());
      changed.push("verified");
    }
    if (current.granularity !== granularity) {
      await granularitySelect.selectOption(granularity);
      changed.push("granularity");
    }
    if (current.cohort !== cohort) {
      await cohortInput.fill(String(cohort));
      changed.push("cohort");
    }
    if (changed.length === 1) alone[changed[0]] += 1;
    current = { site, verified, granularity, cohort };
    const label = `${site} verified=${verified} ${granularity} cohort=${cohort} (changed: ${changed.join(", ") || "nothing"})`;
    try {
      await clickAndSettle(askButton);
    } catch (error) {
      wrong.push(`${label}: ${error.message}`);
      if (wrong.length >= MAX_WRONG_ANSWERS) break;
      continue;
    }
    asked += 1;
    const shown = await readOutcome();
    const answer = probeFor(site, verified, granularity, cohort)?.response;
    if (!answer) {
      wrong.push(`${label}: the recording has no answer for a combination the page offers`);
      if (wrong.length >= MAX_WRONG_ANSWERS) break;
      continue;
    }
    const decision = String(answer.plan?.decision ?? "").toUpperCase();
    const strategy = answer.plan?.field_restrictions?.find((item) => item.field === "systolic_bp")?.strategy ?? null;
    const released = String(answer.released_count);
    const answeredBy = shown.answeredBy === null ? null : siteNamed(shown.answeredBy);
    if (shown.notRecorded) wrong.push(`${label}: the page says "Not recorded"`);
    else if (shown.error) wrong.push(`${label}: ${shown.error}`);
    else if (
      answeredBy !== site ||
      answer.site_id !== site ||
      shown.lmaSites.length === 0 ||
      shown.lmaSites.some((called) => called !== site)
    )
      wrong.push(
        `${label}: answer from ${answeredBy ?? "no hospital"}, ` +
          `LMA calls to ${shown.lmaSites.join(", ") || "none"}, recorded answer from ${answer.site_id}`,
      );
    else if (shown.decision !== decision) wrong.push(`${label}: decision ${shown.decision}, recorded ${decision}`);
    else if (shown.strategy !== strategy) wrong.push(`${label}: systolic_bp ${shown.strategy}, recorded ${strategy}`);
    else if (shown.released !== released) wrong.push(`${label}: released ${shown.released}, recorded ${released}`);
    if (wrong.length >= MAX_WRONG_ANSWERS) break;
  }
  const combinations = hospitalOptions.length * 2 * granularities.length * cohorts.length;
  check(
    `the chosen hospital's Guard answers all ${combinations} combinations, with the recorded decision, systolic_bp strategy and released count`,
    wrong.length === 0 && asked === combinations && combinations > 0,
    wrong.length
      ? `${wrong.length} wrong, first: ${wrong.slice(0, 3).join(" | ")}`
      : `${asked} answers, cohort sizes ${cohorts.join(", ")}; asks after a change of one control only: ` +
          `hospital ${alone.hospital}, "Requester verified" ${alone.verified}, cohort size ${alone.cohort}, granularity ${alone.granularity}`,
  );

  // 3. A cohort size the recording lacks.
  let missing = 1;
  while (cohorts.includes(missing)) missing += 1;
  await cohortInput.fill(String(missing));
  await clickAndSettle(askButton).catch(() => {});
  const unrecorded = await readOutcome();
  check(
    `a cohort size the recording lacks (${missing}) shows the "Not recorded" note with the recorded sizes`,
    Boolean(unrecorded.notRecorded?.startsWith("Not recorded.")) &&
      unrecorded.notRecorded.includes(cohorts.join(", ")) &&
      unrecorded.decision === null,
    unrecorded.notRecorded ?? unrecorded.error ?? `decision ${unrecorded.decision}`,
  );

  // 4. Before the pilot: the consortium charts and each hospital page show
  //    the answers recorded before it.
  const chartsBefore = recorded("/api/v1/charts");
  const chartsAfter = recorded("/api/v1/charts", true);
  const consortiumCharts = '[data-panel="charts"]';
  const beforeProblems = await chartsMatch(consortiumCharts, chartsBefore, "light", false);
  check(
    "before the pilot, the consortium charts are the ones recorded before it",
    beforeProblems.length === 0,
    beforeProblems.slice(0, 3).join("; ") || `${chartsBefore.charts.filter((c) => c.available).length} drawn`,
  );

  /**
   * Open a hospital's page from the header and wait until the page shows that
   * hospital, with its dashboard and audit log loaded.
   *
   * The click changes the address at once, but the page changes its route
   * only when the browser delivers the `hashchange` event. Until then it
   * still shows the previous page, which has the same panels. Waiting for a
   * panel alone once read the previous hospital's numbers in CI. So the drive
   * waits until the address, the heading and the LMA heading all name the
   * hospital, and reads everything in that same pass.
   *
   * A hospital's numbers and its "Dagents API calls" strips come from the
   * same answers, and a strip names the LMA each call went to. So the strips
   * show whose answers the page displays. A watcher records the page after
   * every change while the route changes. Each recorded state must show one
   * hospital only: its heading, its LMA heading, its record count and the LMA
   * rows of every strip. A page that shows one hospital's numbers under
   * another hospital's name fails, even when only for one render.
   */
  const openHospital = async (site) => {
    await page.evaluate(() => {
      const view = () => {
        const text = (selector) => document.querySelector(selector)?.textContent.trim() ?? null;
        const lmaRows = (scope) =>
          scope
            ? [...scope.querySelectorAll('[data-api-calls] tr[data-call-service="lma"]')].map((tr) =>
                tr.getAttribute("data-call-site"),
              )
            : [];
        const audit = document.querySelector('[data-panel="audit"]');
        const total = text('[data-panel="hospital-store"] [data-count="total"]');
        return {
          hash: location.hash,
          heading: text(".page-head h1"),
          site: text("#group-lma .ds-mono"),
          total,
          // The rounds table comes from the same answer as the record count.
          rounds:
            total === null
              ? null
              : document.querySelectorAll('[data-panel="contributions"] table.contributions tbody tr').length,
          audit: audit?.querySelector("[data-audit-note]") ? audit.querySelectorAll(".audit-list li").length : null,
          dashboardCalls: lmaRows(document.querySelector('[data-group="lma"] > .group-head')),
          auditCalls: lmaRows(audit),
          lmaCalls: lmaRows(document.querySelector("main")),
        };
      };
      const states = [];
      let last = "";
      const record = () => {
        const state = view();
        const key = JSON.stringify(state);
        if (key !== last) {
          last = key;
          states.push(state);
        }
      };
      const observer = new MutationObserver(record);
      observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
      record();
      window.__replayHospitalWatch = {
        view,
        stop: () => {
          observer.disconnect();
          record();
          return states;
        },
      };
    });
    await page.locator(`nav[aria-label="Pages"] a[href="#/hospitals/${site}"]`).click();
    const shown = await page
      .waitForFunction(
        ({ site, name }) => {
          const view = window.__replayHospitalWatch.view();
          return (
            view.hash === `#/hospitals/${site}` &&
            view.heading === name &&
            view.site === site &&
            view.total !== null &&
            view.audit !== null
          );
        },
        { site, name: nameOf(site) },
        { timeout: 15000 },
      )
      .then(
        () => true,
        () => false,
      );
    const states = await page.evaluate(() => window.__replayHospitalWatch.stop());
    return { shown, states, view: states[states.length - 1] };
  };
  const nameOf = (site) => overview.hospitals.find((hospital) => hospital.site_id === site)?.display_name ?? site;
  const expectedCounts = (site, afterPilot) => {
    const dashboard = recorded(`/api/v1/hospitals/${site}`, afterPilot);
    const audit = recorded(`/api/v1/hospitals/${site}/audit?limit=50`, afterPilot);
    return {
      total: dashboard?.counts?.total?.toLocaleString("en-GB") ?? null,
      rounds: dashboard?.contributions?.length ?? -1,
      audit: audit?.records?.length ?? -1,
      // Whether the recorded audit answer lists a call to the hospital's LMA,
      // which the audit panel's strip then shows.
      auditCalls: (audit?.framework_calls ?? []).some((call) => call.service === "lma" && call.site_id === site),
    };
  };
  const describe = (counts) => `${counts.total} records, ${counts.rounds} rounds, ${counts.audit} audit records`;
  const listed = (sites) => [...new Set(sites)].join(", ") || "none";

  /**
   * What is wrong with the page `openHospital(site)` opened, or [] when it
   * shows `site` with the answers recorded for it.
   */
  const hospitalProblems = (site, { shown, states, view }, afterPilot) => {
    const problems = [];
    // Every state the page passed through shows one hospital, whichever it is.
    // Records are the same before and after the pilot; the capture enters none.
    for (const state of states) {
      if (state.site === null) continue;
      const totals = [expectedCounts(state.site, false).total, expectedCounts(state.site, true).total];
      if (
        siteNamed(state.heading) !== state.site ||
        state.lmaCalls.some((called) => called !== state.site) ||
        (state.total !== null && !totals.includes(state.total))
      ) {
        problems.push(
          `while the route changed to ${site}, the page showed "${state.heading}" over the LMA of ${state.site}, ` +
            `with ${state.total ?? "no"} records and calls to the LMA of ${listed(state.lmaCalls)}`,
        );
        break;
      }
    }
    if (!shown) {
      problems.push(
        `the page did not show ${site} with its dashboard and audit log within 15 s; it shows "${view.heading}" ` +
          `over the LMA of ${view.site ?? "no hospital"} at ${view.hash || "#/"}`,
      );
      return problems;
    }
    const expected = expectedCounts(site, afterPilot);
    if (view.total !== expected.total || view.rounds !== expected.rounds || view.audit !== expected.audit) {
      problems.push(`page ${describe(view)}; recorded ${describe(expected)}`);
    }
    if (view.dashboardCalls.length === 0 || view.dashboardCalls.some((called) => called !== site)) {
      problems.push(`the dashboard's strip lists calls to the LMA of ${listed(view.dashboardCalls)}`);
    }
    if ((expected.auditCalls && view.auditCalls.length === 0) || view.auditCalls.some((called) => called !== site)) {
      problems.push(`the audit log's strip lists calls to the LMA of ${listed(view.auditCalls)}`);
    }
    return problems;
  };
  const hospitalDetail = (site, { states, view }, problems) =>
    problems.length > 0
      ? problems.join("; ")
      : `${view.heading}: ${describe(view)}; strips call the LMA of ${site} only; ` +
        `${states.length} page states recorded from the click on, none mixing two hospitals`;

  for (const site of sites) {
    const opened = await openHospital(site);
    const problems = hospitalProblems(site, opened, false);
    const errors = await errorBanners();
    check(
      `before the pilot, ${site}'s page shows its own dashboard and audit log, the ones recorded before it`,
      problems.length === 0 && errors.length === 0,
      errors[0] ?? hospitalDetail(site, opened, problems),
    );
  }

  // 5. The pilot replays.
  await page.locator('nav[aria-label="Pages"] a[href="#/"]').click();
  const pilotPanel = page.locator('[data-panel="pilot"]');
  const runButton = pilotPanel.getByRole("button", { name: "Run the pilot" });
  await runButton.waitFor({ timeout: 15000 });
  await clickAndSettle(runButton, 30000).catch((error) => check("the pilot replays", false, error.message));
  await pilotPanel
    .locator(".gates li")
    .first()
    .waitFor({ timeout: 15000 })
    .catch(() => {});

  const gates = pilot.release?.gates;
  const shownGates = await pilotPanel
    .locator(".gates li[data-gate]")
    .evaluateAll((items) => items.map((item) => item.getAttribute("data-gate")));
  const recordedGates = (gates?.gate_results ?? []).map((gate) => gate.gate_id);
  check(
    "the pilot lists every recorded release gate",
    recordedGates.length > 0 && shownGates.join(",") === recordedGates.join(","),
    `page ${shownGates.join(", ") || "none"}; recorded ${recordedGates.join(", ") || "none"}`,
  );
  const decision = await pilotPanel
    .locator("[data-decision]")
    .getAttribute("data-decision", { timeout: 5000 })
    .catch(() => null);
  const decisionChip = await textOf(pilotPanel.locator("[data-decision] .ds-chip"));
  check(
    "the pilot shows the recorded release decision",
    decision !== null && decision === gates?.action && decisionChip.length > 0,
    `page ${decision} ("${decisionChip}"), recorded ${gates?.action}`,
  );
  const { analytics, evaluation, candidate_validation: validation, training } = pilot.rounds;
  const roundsRun = [analytics, evaluation, validation].filter(Boolean).length + training.length;
  const shownRounds = await textOf(pilotPanel.locator('[data-summary="rounds"]'));
  check(
    "the pilot summary counts the recorded rounds",
    shownRounds === String(roundsRun),
    `page ${shownRounds}, recorded ${roundsRun}`,
  );

  const dispatchShown = await pilotPanel
    .locator("tr[data-dispatch-round]")
    .evaluateAll((trs) =>
      trs.map(
        (tr) =>
          `${tr.getAttribute("data-dispatch-round")} ${tr.getAttribute("data-dispatch-site")} ${tr.getAttribute("data-offer")}`,
      ),
    );
  const dispatchExpected = [analytics, evaluation, ...training, validation]
    .filter((round) => round && Array.isArray(round.dispatch))
    .flatMap((round) =>
      [...new Set(round.dispatch.map((call) => call.site_id))].map((site) => `${round.round_id} ${site}`),
    );
  const dispatchKeys = dispatchShown.map((line) => line.split(" ").slice(0, 2).join(" "));
  check(
    "the pilot shows a dispatch row for every hospital in every recorded round, each with its answer",
    dispatchExpected.length > 0 &&
      [...dispatchKeys].sort().join("|") === [...dispatchExpected].sort().join("|") &&
      dispatchShown.every((line) => line.split(" ")[2]),
    `${dispatchShown.length} rows on the page, ${dispatchExpected.length} in the recording`,
  );

  const chains = await pilotPanel
    .locator("tr[data-chain]")
    .evaluateAll((trs) =>
      trs.map(
        (tr) =>
          `${tr.getAttribute("data-chain")} ${tr.getAttribute("data-intact")} ${tr.querySelector("[data-records]")?.textContent.trim()}`,
      ),
    );
  const decisions = (audit) => audit.record_count ?? audit.records?.length ?? null;
  const chainsExpected = [
    ...Object.entries(pilot.site_audits).map(
      ([site, audit]) => `${site} ${audit.chain_intact ? "yes" : "no"} ${decisions(audit)?.toLocaleString("en-GB")}`,
    ),
    `coordinator ${pilot.coordinator_audit.chain_intact ? "yes" : "no"} ${decisions(pilot.coordinator_audit)?.toLocaleString("en-GB")}`,
  ];
  check(
    "the pilot shows each recorded audit chain with its state and length",
    chains.join("|") === chainsExpected.join("|"),
    `page ${chains.join(", ")}; recorded ${chainsExpected.join(", ")}`,
  );

  const approve = pilotPanel.locator('[data-panel="approve"]');
  if (gates?.action === "release") {
    const approveButton = approve.getByRole("button", { name: "Approve release" });
    const approveText = await textOf(approve);
    check(
      "the approve button is turned off and says the replay cannot approve a model",
      (await approveButton.count()) === 1 &&
        (await approveButton.isDisabled()) &&
        approveText.includes("This page replays a recorded run, so it cannot approve a model."),
      approveText.slice(0, 160),
    );
  } else {
    check(
      `no approve button, because the recorded gates decided "${gates?.action}"`,
      (await approve.count()) === 0,
      `${await approve.count()} approve panels`,
    );
  }

  // 6 and 7. After the pilot: the consortium charts and the hospital pages
  //    show the answers recorded after it, in both themes.
  const differs =
    JSON.stringify(chartsBefore.charts.map((c) => [c.chart_id, c.available])) !==
      JSON.stringify(chartsAfter.charts.map((c) => [c.chart_id, c.available])) &&
    sites.some((site) => expectedCounts(site, true).rounds > expectedCounts(site, false).rounds);
  check(
    "the recording's answers after the pilot differ from those before it, so the switch can be seen",
    differs,
    differs ? "chart list and rounds differ" : "the answers after the pilot equal those before it",
  );
  const trainingChart = chartsAfter.charts.find((chart) => chart.chart_id === "training-curve");
  const gridAfter = { charts: chartsAfter.charts.filter((chart) => chart !== trainingChart) };
  for (const theme of ["light", "dark"]) {
    if (theme === "dark") await chooseTheme("Dark");
    const gridProblems = await chartsMatch(consortiumCharts, gridAfter, theme, true);
    check(
      `after the pilot, the consortium charts are the ones recorded after it (${theme})`,
      gridProblems.length === 0,
      gridProblems.slice(0, 3).join("; ") || `${gridAfter.charts.filter((c) => c.available).length} drawn`,
    );
    if (trainingChart) {
      const curveProblems = await chartsMatch('[data-panel="pilot"]', { charts: [trainingChart] }, theme, true);
      check(
        `the pilot panel draws the recorded training curve (${theme})`,
        curveProblems.length === 0,
        curveProblems.join("; "),
      );
    }
  }
  await chooseTheme("Light");

  for (const site of sites) {
    const opened = await openHospital(site);
    const problems = hospitalProblems(site, opened, true);
    check(
      `after the pilot, ${site}'s page shows its own dashboard and audit log, the ones recorded after it`,
      problems.length === 0,
      hospitalDetail(site, opened, problems),
    );
    const listing = recorded(`/api/v1/hospitals/${site}/charts`, true);
    for (const theme of ["light", "dark"]) {
      await chooseTheme(theme === "light" ? "Light" : "Dark");
      const problems = await chartsMatch('[data-panel="hospital-charts"]', listing, theme, true);
      check(
        `${site}'s page draws the recorded ${theme} charts`,
        problems.length === 0 && listing.charts.some((chart) => chart.available),
        problems.slice(0, 3).join("; ") || `${listing.charts.filter((c) => c.available).length} drawn`,
      );
    }
    await chooseTheme("Light");

    // 8. What a replay cannot do is turned off, with the reason. A record
    //    shows its two outcome buttons only while its outcome is unknown, so
    //    the buttons can be checked only when the recording holds such a
    //    record. The capture enters none, so the detail line says when there
    //    was no button to check.
    const enteredRecorded = recorded(`/api/v1/hospitals/${site}/encounters?origin=entered&limit=20`, true)?.encounters;
    const awaiting = (enteredRecorded ?? []).filter((entry) => typeof entry.poor_outcome_6m !== "boolean").length;
    const entered = await page
      .locator('[data-panel="entered"]')
      .evaluate((panel) => ({
        note: panel.querySelector(".banner.recorded")?.textContent.trim() ?? "",
        rows: panel.querySelectorAll("table.entered tbody tr[data-encounter]").length,
        buttons: [...panel.querySelectorAll(".outcome-control button")].map((button) => button.disabled),
      }))
      .catch(() => ({ note: "", rows: -1, buttons: [] }));
    check(
      `${site}'s entered records are the recorded ones, and the page says the replay cannot store an outcome`,
      entered.note.includes("cannot store an outcome") &&
        Array.isArray(enteredRecorded) &&
        entered.rows === enteredRecorded.length &&
        entered.buttons.length === 2 * awaiting &&
        entered.buttons.every(Boolean),
      `${entered.note || "no note"}; page ${entered.rows} entered records, recorded ${enteredRecorded?.length ?? "none"}; ` +
        (awaiting === 0
          ? "no recorded record awaits an outcome, so there is no outcome button to check"
          : `${entered.buttons.filter(Boolean).length} of ${entered.buttons.length} outcome buttons turned off, ${2 * awaiting} expected`),
    );
    await page.locator(`.page-head a[href="#/hospitals/${site}/new"]`).click();
    const form = page.locator('[data-panel="entry"] form.entry-form');
    await form.waitFor({ timeout: 15000 }).catch(() => {});
    const formState = await form
      .evaluate((element) => ({
        note: element.querySelector(".banner.recorded")?.textContent.trim() ?? "",
        controls: element.querySelectorAll("input, select, textarea").length,
        // `:disabled` also matches a radio button inside a disabled fieldset,
        // whose own `disabled` property stays false.
        enabled: [...element.querySelectorAll("input, select, textarea")].filter(
          (control) => !control.matches(":disabled"),
        ).length,
        save: element.querySelector('button[type="submit"]')?.disabled ?? null,
      }))
      .catch(() => ({ note: "", controls: 0, enabled: -1, save: null }));
    check(
      `${site}'s data-entry form is turned off and says why`,
      formState.note.includes("A recording cannot store new records, so the form is turned off.") &&
        formState.controls > 0 &&
        formState.enabled === 0 &&
        formState.save === true,
      `${formState.note.slice(0, 80) || "no note"}; ${formState.enabled} of ${formState.controls} controls enabled; save disabled: ${formState.save}`,
    );
    const errors = await errorBanners();
    check(`${site}'s pages show no error`, errors.length === 0, errors[0] ?? "");
  }

  await page.locator('nav[aria-label="Pages"] a[href="#/"]').click();
  const deploy = page.locator('[data-panel="deploy"]');
  await deploy
    .locator('[data-role="reason"]')
    .waitFor({ timeout: 15000 })
    .catch(() => {});
  const deployReason = await textOf(deploy.locator('[data-role="reason"]'));
  const joinable = await deploy
    .locator("tr[data-joinable]")
    .evaluateAll((trs) => trs.map((tr) => tr.getAttribute("data-joinable")));
  const deployButtons = await deploy.locator('[data-action="deploy"]').count();
  const recordedJoinable = (deployments.hospitals ?? []).map((hospital) => hospital.site_id);
  check(
    "the panel to add a hospital lists the recorded hospitals, offers no button and says why",
    deployReason.startsWith("This page replays a recorded run.") &&
      deployButtons === 0 &&
      joinable.join(",") === recordedJoinable.join(","),
    `${deployButtons} buttons; ${joinable.join(", ")}; ${deployReason.slice(0, 80) || "no reason"}`,
  );
  const pageErrors = await errorBanners();
  check("the consortium page shows no error", pageErrors.length === 0, pageErrors[0] ?? "");

  // 9. Nothing but the published tree, and nothing logged.
  check(
    "the page sent no API request",
    apiRequests.length === 0 && server.apiRequests.length === 0,
    [...apiRequests, ...server.apiRequests].slice(0, 3).join("; "),
  );
  check(
    "the page loaded nothing from another origin except the optional fonts",
    foreignRequests.length === 0,
    foreignRequests.slice(0, 3).join("; "),
  );
  check("no console errors or failed requests", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
} catch (error) {
  if (error instanceof Stop) console.log("  Stopped: the checks after this one need it.");
  else check("the replay check ran to the end", false, error.message.split("\n")[0]);
  printCollected();
} finally {
  await browser?.close().catch(() => {});
  await server.close();
}

console.log(`\n${passed} checks passed, ${failures.length} failed.`);
if (failures.length === 0 && passed === 0) {
  console.log("No check ran.");
  process.exit(1);
}
process.exit(failures.length ? 1 : 0);
