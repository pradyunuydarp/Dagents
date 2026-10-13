/**
 * Frontend smoke test.
 *
 * A typecheck and a bundle only prove that the app compiles. They do not show
 * that it renders, that the proxy reaches the backend, or that the governance
 * controls change anything. This test drives the real UI against a live
 * backend, in a real browser:
 *
 *   1. the consortium page lists the hospitals the API reports;
 *   2. each of the guard's three controls changes the systolic_bp strategy,
 *      the cohort minimum denies only column, table and model-update requests,
 *      and the coordinator's page shows what the Guard would release without
 *      any patient value, from an answer that holds no patient row;
 *   3. a full pilot runs, every release gate is listed, a decision is shown,
 *      the rounds and audit chains match what the backend reports, and the
 *      values each hospital sent are named in words;
 *   4. the charts draw, on the consortium page and on a hospital page, and
 *      small text has at least 4.5:1 contrast in both themes;
 *   5. a record entered on the data-entry page is stored, appears on the
 *      hospital page, raises the entered count by one, and takes an outcome;
 *      keyboard focus moves to the first refused field and to the result;
 *   6. switching to the dark theme draws the charts again, a stored theme
 *      applies before the app's script runs, and the theme toggle's focus
 *      ring is drawn in full;
 *   7. a 503 without the app's X-Dagents-App header (the platform, during a
 *      cold start) shows the connecting banner and is retried, and the app's
 *      own 503 is shown at once, once per page, and not retried;
 *   8. empty hospital stores show one banner and a plain sentence in place of
 *      zeros and empty tables; a failed reload is reported above the last
 *      numbers; each reason the backend gives (a missing rate, a hospital left
 *      out of a round) is shown in words;
 *   9. at 375 px the charts keep their drawn size inside their own scroll box
 *      and the page does not scroll sideways;
 *  10. nothing logs a console error or fails a request on the way.
 *
 * Usage:
 *   node smoke.mjs [baseUrl] [--screenshots <dir>]
 *
 * `--screenshots` also writes the README screenshots (consortium, guard-denied,
 * federated-pilot, hospital, data-entry) to <dir>.
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "broken".
 */
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";

const args = process.argv.slice(2);
const shotIndex = args.indexOf("--screenshots");
const shotDir = shotIndex === -1 ? null : args[shotIndex + 1];
const positional = args.filter((a, i) => !a.startsWith("--") && (shotIndex === -1 || i !== shotIndex + 1));
const baseUrl = (positional[0] ?? "http://127.0.0.1:5174/").replace(/\/?$/, "/");

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
  console.log("SKIP: playwright-core is not installed (npm install to enable the smoke test)");
  process.exit(2);
}

const executablePath = findBrowser();
if (!executablePath) {
  console.log("SKIP: no Chromium found; set CHROMIUM_PATH to run the smoke test");
  process.exit(2);
}

const failures = [];
const consoleErrors = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(name);
};

if (shotDir) await mkdir(shotDir, { recursive: true });

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });

// Web fonts are optional: the design tokens list fallback fonts, and some
// sandboxes block the font host. Everything else must load.
const optional = (text) => /fonts\.(googleapis|gstatic)\.com/.test(text);
// The test sends one empty form on purpose, to see the backend's field errors.
// Only that one 422 is expected; any other failed request fails the test.
let expectRefusal = false;
const refused = (text) => expectRefusal && /422/.test(text) && /\/encounters/.test(text);

/** Count every console error and failed request on a page that must stay clean. */
const watch = (target) => {
  target.on("console", (m) => {
    const text = `${m.text()} ${m.location()?.url ?? ""}`;
    if (m.type() === "error" && !optional(text) && !refused(text)) consoleErrors.push(m.text());
  });
  target.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
  target.on("response", (r) => {
    const text = `HTTP ${r.status()} ${r.url()}`;
    if (r.status() >= 400 && !optional(r.url()) && !refused(text)) consoleErrors.push(text);
  });
  target.on("requestfailed", (r) => {
    if (!optional(r.url())) consoleErrors.push(`request failed: ${r.url()} ${r.failure()?.errorText ?? ""}`);
  });
};
watch(page);

const api = async (path) => {
  const response = await page.request.get(new URL(path.replace(/^\//, ""), baseUrl).toString());
  if (!response.ok()) throw new Error(`${path} returned ${response.status()}`);
  return response.json();
};

/** Wait until an image has loaded and report its natural width. */
const imageWidth = async (locator) => {
  await locator.waitFor({ timeout: 30000 });
  await page.waitForFunction((img) => img.complete, await locator.elementHandle(), { timeout: 30000 });
  return locator.evaluate((img) => img.naturalWidth);
};

/** The page-coordinate bottom of an element, for clipping a screenshot to it. */
const bottomOf = (selector) =>
  page.evaluate((sel) => {
    const box = document.querySelector(sel)?.getBoundingClientRect();
    return Math.ceil((box ? box.bottom : 1000) + window.scrollY + 24);
  }, selector);

/** Screenshots for the README: the sticky header would cover tall elements. */
const staticHeader = async () => {
  await page.addStyleTag({ content: ".masthead { position: static !important; }" });
};

/** Text that names a value by its code identifier, such as `record_count`. */
const identifier = /\b[a-z]+_[a-z0-9_]+\b/;

/**
 * The contrast of each matching element's text against the background behind
 * it, using the WCAG relative luminance formula. Returns the lowest ratio.
 */
const lowestContrast = (target, selector) =>
  target.evaluate((sel) => {
    const rgb = (text) => (text.match(/[\d.]+/g) ?? []).map(Number);
    const luminance = ([r, g, b]) => {
      const channel = (v) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const background = (element) => {
      for (let node = element; node; node = node.parentElement) {
        const value = rgb(getComputedStyle(node).backgroundColor);
        if (value.length === 3 || (value.length === 4 && value[3] > 0)) return value.slice(0, 3);
      }
      return rgb(getComputedStyle(document.body).backgroundColor).slice(0, 3);
    };
    let lowest = Infinity;
    const elements = [...document.querySelectorAll(sel)].filter((el) => el.getClientRects().length > 0);
    for (const element of elements) {
      const a = luminance(rgb(getComputedStyle(element).color).slice(0, 3));
      const b = luminance(background(element));
      lowest = Math.min(lowest, (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05));
    }
    return { lowest: Number(lowest.toFixed(2)), count: elements.length };
  }, selector);

try {
  console.log(`Dagents healthcare demo — frontend smoke test against ${baseUrl}\n`);
  const overview = await api("/api/v1/overview");
  const firstSite = overview.hospitals[0];
  const labels = overview.field_labels ?? {};

  await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60000 });

  // Panels are located by their `data-panel` hook rather than by position, so
  // reordering or restyling the page cannot silently retarget an assertion.
  const consortiumPanel = page.locator('[data-panel="consortium"]');
  const guardPanel = page.locator('[data-panel="guard"]');
  const pilotPanel = page.locator('[data-panel="pilot"]');
  const chartsPanel = page.locator('[data-panel="charts"]');

  // 1. The page renders and lists the hospitals the API reports.
  await consortiumPanel.locator("table tbody tr").first().waitFor({ timeout: 30000 });
  const heading = (await page.locator("h1").first().textContent())?.trim() ?? "";
  check("page renders its heading", heading.length > 0, heading);
  const hospitalRows = await consortiumPanel.locator("table tbody tr").count();
  check(
    "consortium table lists the three hospitals the API reports",
    hospitalRows === 3 && hospitalRows === overview.hospitals.length,
    `${hospitalRows} rows, ${overview.hospitals.length} in the overview`
  );
  const navLinks = await page.locator('nav[aria-label="Pages"] a').count();
  check("the header links to every hospital", navLinks === overview.hospitals.length + 1, `${navLinks} links`);
  const notReady = await page.locator('[data-banner="not-ready"]').count();
  check("no not-loaded banner while every store holds records", notReady === 0, `${notReady} banners`);
  if (shotDir) {
    await staticHeader();
    await page.screenshot({
      path: `${shotDir}/consortium.png`,
      clip: { x: 0, y: 0, width: 1280, height: await bottomOf('[data-panel="consortium"]') },
      fullPage: true,
    });
  }

  // 2. The guard has three controls. Each is asserted separately, because each
  //    proves a different rule and any of them can regress on its own.
  const strategyFor = async (field) =>
    (await guardPanel.locator(`tbody tr:has(td:text-is("${field}")) td`).last().textContent())?.trim() ?? "";
  await guardPanel.getByRole("combobox", { name: "Hospital", exact: true }).selectOption(firstSite.site_id);
  let lastProbe = null;
  const askGuard = async ({ verified, granularity, cohort }) => {
    const box = guardPanel.getByLabel("Requester verified");
    if (verified) await box.check();
    else await box.uncheck();
    await guardPanel.getByLabel("Granularity").selectOption(granularity);
    await guardPanel.getByLabel("Cohort size").fill(String(cohort));
    const answered = page.waitForResponse((r) => r.url().includes("/api/v1/governance:probe"), { timeout: 30000 });
    await page.getByRole("button", { name: "Ask the guard" }).click();
    lastProbe = await (await answered).json().catch(() => null);
    await page.waitForTimeout(300);
    return (await guardPanel.locator(".verdict").first().textContent())?.trim() ?? "";
  };

  /**
   * The coordinator's page never shows a patient row (CLAUDE.md, the frontend
   * skill), and no row reaches it: the probe's answer counts the records the
   * Guard would release and names their fields. The page shows that count and
   * no table other than the strategy table.
   */
  const recordLists = (value) =>
    Array.isArray(value)
      ? value.some((item) => item && typeof item === "object" && !Array.isArray(item) && !("field" in item)) ||
        value.some(recordLists)
      : value && typeof value === "object"
        ? Object.values(value).some(recordLists)
        : false;
  const releaseShownWithoutRows = async (label) => {
    const extraTables = await guardPanel.locator("table:not(.restrictions)").count();
    const released = ((await guardPanel.locator('[data-released="records"]').textContent().catch(() => "")) ?? "").trim();
    const expected = String(lastProbe?.released_count ?? "missing");
    check(
      `the guard shows what it would release, not the rows (${label})`,
      extraTables === 0 && released === expected,
      `${extraTables} other tables; released ${released || "not shown"}, the backend released ${expected}`
    );
    check(
      `the probe's answer holds no patient rows (${label})`,
      lastProbe !== null && !("payload" in lastProbe) && !recordLists(lastProbe),
      lastProbe === null ? "no answer" : Object.keys(lastProbe).join(", ")
    );
  };

  //    Trust: the same request, generalized for a verified requester and
  //    redacted for an unverified one.
  const trusted = await askGuard({ verified: true, granularity: "row", cohort: 25 });
  const trustedStrategy = await strategyFor("systolic_bp");
  check("a verified row request is narrowed, not denied", /NARROW|PERMIT/.test(trusted), trusted);
  check("a trusted requester gets generalization", trustedStrategy.startsWith("generalize"), trustedStrategy);
  await releaseShownWithoutRows("verified row request");
  const grails = guardPanel.getByRole("link", { name: "GRAILS" });
  check(
    "the guard panel names GRAILS and links the paper",
    (await grails.count()) === 1 && /ojs\.aaai\.org/.test((await grails.getAttribute("href")) ?? ""),
    `${await grails.count()} links`
  );

  const untrusted = await askGuard({ verified: false, granularity: "row", cohort: 25 });
  const untrustedStrategy = await strategyFor("systolic_bp");
  check("an unverified requester gets a harder strategy", untrustedStrategy === "redact", untrustedStrategy);
  check("lowering trust raises the filtering score", untrusted !== trusted, `${trusted} -> ${untrusted}`);
  await releaseShownWithoutRows("unverified row request");

  //    Granularity: a table can only come back as an aggregate.
  await askGuard({ verified: true, granularity: "table", cohort: 25 });
  const tableStrategy = await strategyFor("systolic_bp");
  check("a table request is reduced to an aggregate", tableStrategy.startsWith("aggregate_only"), tableStrategy);

  //    The federated extension: an update is never released raw.
  await askGuard({ verified: true, granularity: "model_update", cohort: 25 });
  const updateStrategy = await strategyFor("systolic_bp");
  check("a model update is bounded, never raw", /clip_contribution|add_noise/.test(updateStrategy), updateStrategy);

  //    The cohort minimum applies to column, table and model-update requests
  //    only. Cell and row requests return individual records, so a small
  //    cohort does not deny them, and the panel must not say that it does.
  const smallRow = await askGuard({ verified: true, granularity: "row", cohort: 5 });
  check("the cohort minimum does not deny a row request", !smallRow.includes("DENY"), smallRow);
  const cohortNote =
    (await guardPanel.locator(".lever-notes li", { hasText: String(overview.minimum_cohort) }).first().textContent()) ?? "";
  check(
    "the cohort note names the requests it applies to",
    /column, table or model-update/.test(cohortNote) && /[Cc]ell and row requests/.test(cohortNote),
    cohortNote.trim()
  );

  //    The cohort minimum denies outright, however trusted the requester.
  const denied = await askGuard({ verified: true, granularity: "table", cohort: 5 });
  check("a cohort below the minimum is denied", denied.includes("DENY"), denied);
  const deniedText = (await guardPanel.locator(".outcome").innerText()) ?? "";
  check(
    "the denial reason is in plain words",
    !/cohort_size|minimum_cohort/.test(deniedText) && /fewer than the minimum/.test(deniedText),
    deniedText.split("\n").find((line) => /cohort|minimum/.test(line)) ?? "no reason shown"
  );
  if (shotDir) await guardPanel.screenshot({ path: `${shotDir}/guard-denied.png` });

  // 3. A full pilot runs and reports every gate, a decision and intact chains.
  //    The decision is whatever the gates decide; the test does not assume one.
  const pilotAnswer = page.waitForResponse((r) => r.url().includes("/api/v1/pilot:run"), { timeout: 180000 });
  await page.getByRole("button", { name: "Run the pilot" }).click();
  const pilot = await (await pilotAnswer).json();
  await pilotPanel.locator(".gates li").first().waitFor({ timeout: 180000 });
  await page.waitForTimeout(500);
  const gates = await pilotPanel.locator(".gates li").count();
  check(
    "every release gate is reported",
    gates === overview.release_gates.length && gates > 0,
    `${gates} listed, ${overview.release_gates.length} in the overview`
  );
  const decision = ((await pilotPanel.locator(".decision .ds-chip").first().textContent()) ?? "").trim();
  check("a release decision is shown with its chip", /^(release|reject|another round)$/i.test(decision), decision);
  const chains = await pilotPanel.locator("tr[data-chain]").count();
  const intact = await pilotPanel.locator('tr[data-chain][data-intact="yes"]').count();
  check(
    "every audit chain is intact",
    chains === overview.hospitals.length + 1 && intact === chains,
    `${intact} of ${chains} intact`
  );
  //    The report carries each chain's summary, never its records.
  const chainCounts = await pilotPanel.locator("tr[data-chain]").evaluateAll((rows) =>
    rows.map((row) => [row.getAttribute("data-chain"), row.querySelector("[data-records]")?.textContent?.trim() ?? ""])
  );
  const reported = (key) =>
    String((key === "coordinator" ? pilot.coordinator_audit : pilot.site_audits?.[key])?.record_count ?? "missing");
  const chainMismatch = chainCounts.filter(([key, shown]) => shown.replace(/,/g, "") !== reported(key));
  check(
    "each audit chain shows the decision count its keeper reports",
    chainCounts.length > 0 && chainMismatch.length === 0,
    chainCounts.map(([key, shown]) => `${key} ${shown} (reported ${reported(key)})`).join(", ")
  );
  const roundsRan =
    ["analytics", "evaluation", "candidate_validation"].filter((phase) => pilot.rounds?.[phase]).length +
    (pilot.rounds?.training?.length ?? 0);
  const roundsShown = ((await pilotPanel.locator('[data-summary="rounds"]').textContent().catch(() => "")) ?? "").trim();
  check("rounds run counts the rounds that ran", roundsShown === String(roundsRan), `${roundsShown} shown, ${roundsRan} ran`);
  const sent = await pilotPanel.locator("td.values-sent").evaluateAll((cells) => cells.map((cell) => cell.textContent ?? ""));
  const coded = sent.filter((text) => identifier.test(text));
  check("the values each hospital sent are named in words", sent.length > 0 && coded.length === 0, coded[0] ?? `${sent.length} cells`);
  const gateDefinitions = new Map(overview.release_gates.map((gate) => [gate.gate_id, gate]));
  const observed = await pilotPanel.locator(".gates li").evaluateAll((items) =>
    items.map((item) => [item.getAttribute("data-gate"), item.querySelector("[data-observed]")?.textContent?.trim() ?? ""])
  );
  const badGate = observed.find(([gateId, text]) => {
    const metric = gateDefinitions.get(gateId)?.metric;
    if (text === "—") return false;
    if (metric === "alerts_per_1000") return !/^[\d,]+$/.test(text);
    if (metric === "sensitivity" || metric === "specificity") return !/%$/.test(text);
    return !/^-?\d+\.\d{1,3}$/.test(text);
  });
  check("each gate's observed value uses its metric's format", observed.length > 0 && !badGate, badGate ? badGate.join(": ") : "");
  //    The audit note says what the check does, as the code does it: each
  //    record links to the one before it, and a full check runs only without a
  //    saved check point. It must not promise that no record is missing,
  //    because removing the newest record is not noticed.
  const auditNoteMatches = (text) =>
    !/changed or removed/.test(text) &&
    !/none is missing/.test(text) &&
    /links to the one before it/.test(text) &&
    /saved check point/.test(text);
  const pilotAuditNote = ((await pilotPanel.locator("[data-audit-note]").textContent().catch(() => "")) ?? "").trim();
  check("the pilot's audit note says what the chain check does", auditNoteMatches(pilotAuditNote), pilotAuditNote);
  const pilotText = (await pilotPanel.innerText()) ?? "";
  const explained = !/L2 norm/.test(pilotText) || /length of the update/.test(pilotText);
  check("the pilot panel says what the update size is", explained, explained ? "" : "mentions L2 norm without saying what it is");
  const curveWidth = await imageWidth(pilotPanel.locator('[data-chart="training-curve"] img.chart-image'));
  check("the training curve draws", curveWidth > 0, `${curveWidth}px`);
  if (shotDir) await pilotPanel.screenshot({ path: `${shotDir}/federated-pilot.png` });

  // 4. A consortium chart draws.
  const consortiumChart = chartsPanel.locator("img.chart-image").first();
  const consortiumWidth = await imageWidth(consortiumChart);
  check("a consortium chart draws", consortiumWidth > 0, `${consortiumWidth}px`);

  //    Small text keeps at least 4.5:1 contrast in both themes: the footer,
  //    the eyebrows (one on the guard panel's inset head) and the gate kinds.
  const contrastTargets = ".foot p, .page-head .ds-eyebrow, .panel.guard .ds-eyebrow, .gate-kind";
  const contrast = {};
  for (const theme of ["Light", "Dark"]) {
    await page.locator(`.theme-toggle button:has-text("${theme}")`).click();
    await page.waitForTimeout(200);
    contrast[theme] = await lowestContrast(page, contrastTargets);
  }
  await page.locator('.theme-toggle button:has-text("System")').click();
  check(
    "small text has at least 4.5:1 contrast in both themes",
    contrast.Light.count > 0 && contrast.Light.lowest >= 4.5 && contrast.Dark.lowest >= 4.5,
    `light ${contrast.Light.lowest}, dark ${contrast.Dark.lowest} over ${contrast.Light.count} elements`
  );

  // 5. A hospital page, reached through the header.
  await page.locator('nav[aria-label="Pages"] a', { hasText: firstSite.display_name }).click();
  const enteredCount = page.locator('[data-panel="hospital-store"] [data-count="entered"]');
  await enteredCount.waitFor({ timeout: 30000 });
  check(
    "the header marks the hospital page as current",
    (await page.locator('nav[aria-label="Pages"] a[aria-current="page"]').textContent())?.trim() ===
      firstSite.display_name
  );
  const enteredBefore = Number((await enteredCount.textContent())?.replace(/[^\d]/g, "") ?? "NaN");
  const profileRows = await page.locator('[data-panel="profile"] tbody tr').count();
  check("the running statistics are listed", profileRows > 0, `${profileRows} fields`);
  const profileLabels = await page
    .locator('[data-panel="profile"] tbody td[title]')
    .evaluateAll((cells) => cells.map((cell) => [cell.getAttribute("title"), cell.textContent?.trim() ?? ""]));
  const labelMismatch = profileLabels.filter(([field, text]) => labels[field] && labels[field] !== text);
  check(
    "the statistics name each field with the backend's label",
    profileLabels.length > 0 && labelMismatch.length === 0,
    labelMismatch.map(([field, text]) => `${field}: "${text}" not "${labels[field]}"`).join("; ")
  );
  await page.locator('[data-panel="worklist"] tbody tr').first().waitFor({ timeout: 30000 });
  const worklistRows = await page.locator('[data-panel="worklist"] tbody tr').count();
  check("the worklist loads", worklistRows > 0, `${worklistRows} rows`);
  const worklistText = (await page.locator('[data-panel="worklist"]').innerText()) ?? "";
  check(
    "the worklist says how many records it shows",
    new RegExp(`Showing the ${worklistRows} highest-priority of [\\d,]+ records`).test(worklistText),
    worklistText.split("\n").find((line) => /Showing/.test(line)) ?? "no count shown"
  );
  const sentHere = await page
    .locator('[data-panel="contributions"] td.values-sent')
    .evaluateAll((cells) => cells.map((cell) => cell.textContent ?? ""));
  const codedHere = sentHere.filter((text) => identifier.test(text));
  check(
    "the hospital's values sent are named in words",
    sentHere.length > 0 && codedHere.length === 0,
    codedHere[0] ?? `${sentHere.length} cells`
  );
  await page.locator('[data-panel="audit"] [data-audit-note]').waitFor({ timeout: 30000 });
  const hospitalAuditNote = ((await page.locator('[data-panel="audit"] [data-audit-note]').textContent()) ?? "").trim();
  check("the hospital's audit note says what the chain check does", auditNoteMatches(hospitalAuditNote), hospitalAuditNote);
  const storeKind = (await api(`/api/v1/hospitals/${firstSite.site_id}`)).store?.kind;
  if (storeKind === "postgres") {
    const storeText = (await page.locator('[data-panel="hospital-store"]').innerText()) ?? "";
    check(
      "the Postgres note states that the demo's one login can read every schema",
      /single login can read every schema/.test(storeText),
      storeText.split("\n")[2] ?? ""
    );
  } else {
    console.log(`  info  the Postgres storage note is not shown: the store is ${storeKind}`);
  }
  const hospitalChart = page.locator('[data-panel="hospital-charts"] img.chart-image').first();
  const hospitalWidth = await imageWidth(hospitalChart);
  check("a hospital chart draws", hospitalWidth > 0, `${hospitalWidth}px`);
  if (shotDir) {
    await staticHeader();
    await page.screenshot({
      path: `${shotDir}/hospital.png`,
      clip: { x: 0, y: 0, width: 1280, height: await bottomOf('[data-panel="model"]') },
      fullPage: true,
    });
  }

  // 6. Data entry: the backend refuses an empty form and names the fields,
  //    then stores a valid record.
  await page.locator(".page-head").getByRole("link", { name: "Add a record" }).click();
  const form = await api(`/api/v1/hospitals/${firstSite.site_id}/entry-form`);
  const entryPanel = page.locator('[data-panel="entry"]');
  await entryPanel.locator("form").waitFor({ timeout: 30000 });
  const repeatedUnit = [];
  for (const field of form.fields.filter((item) => item.unit)) {
    const text = ((await entryPanel.locator(`[data-field="${field.name}"] .field-label`).first().textContent()) ?? "").toLowerCase();
    if (text.split(field.unit.toLowerCase()).length > 2) repeatedUnit.push(text.trim());
  }
  check("no field label repeats its unit", repeatedUnit.length === 0, repeatedUnit.join("; "));

  expectRefusal = true;
  const refusal = page.waitForResponse((r) => r.url().endsWith("/encounters") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Save the record" }).click();
  check("an empty form is refused by the backend", (await refusal).status() === 422);
  await entryPanel.locator('[data-field="age"] .field-error').waitFor({ timeout: 10000 });
  const fieldErrors = await entryPanel.locator(".field-error").count();
  const required = form.fields.filter((field) => field.required).length;
  check("each refused field shows its reason next to it", fieldErrors === required, `${fieldErrors} of ${required}`);
  await page.waitForTimeout(300);
  const focusAfterRefusal = await page.evaluate(() => {
    const active = document.activeElement;
    const first = document.querySelector(".form-field.has-error");
    return {
      inFirst: Boolean(first && active && first.contains(active)),
      what: active ? `${active.tagName.toLowerCase()}${active.getAttribute("name") ? `[name=${active.getAttribute("name")}]` : ""}` : "none",
    };
  });
  check("focus moves to the first refused field", focusAfterRefusal.inFirst, `focus on ${focusAfterRefusal.what}`);
  expectRefusal = false;

  for (const field of form.fields) {
    const box = entryPanel.locator(`[data-field="${field.name}"]`);
    if (field.type === "boolean") {
      await box.locator('input[value="true"]').check();
    } else if (field.type === "choice") {
      await box.locator("select").selectOption(field.options[field.options.length > 1 ? 1 : 0]);
    } else {
      const middle = (field.min + field.max) / 2;
      await box.locator("input").fill(String(field.type === "integer" ? Math.round(middle) : middle));
    }
  }
  const staleErrors = await entryPanel.locator(".field-error").count();
  check("a corrected field loses its error", staleErrors === 0, `${staleErrors} left`);
  if (shotDir) {
    await staticHeader();
    await page.screenshot({
      path: `${shotDir}/data-entry.png`,
      clip: { x: 0, y: 0, width: 1280, height: await bottomOf('[data-panel="entry"]') },
      fullPage: true,
    });
  }
  const stored = page.waitForResponse((r) => r.url().endsWith("/encounters") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Save the record" }).click();
  const storedResponse = await stored;
  check("a valid record is stored", storedResponse.status() === 201, `HTTP ${storedResponse.status()}`);
  const savedPanel = page.locator('[data-panel="entry-result"]');
  const saved = savedPanel.locator("[data-encounter]");
  await saved.waitFor({ timeout: 30000 });
  const encounterId = (await saved.getAttribute("data-encounter")) ?? "";
  check("the page shows the stored record", encounterId.startsWith(firstSite.site_id), encounterId);
  await page.waitForTimeout(300);
  const focusInResult = await page.evaluate(
    () => Boolean(document.querySelector('[data-panel="entry-result"]')?.contains(document.activeElement))
  );
  check("focus moves to the saved record", focusInResult);
  const savedText = (await savedPanel.innerText()) ?? "";
  check("the saved page promises only what the hospital page shows", !/guard probe/i.test(savedText));
  const savedLabels = await savedPanel
    .locator("table.record th[data-field]")
    .evaluateAll((cells) => cells.map((cell) => [cell.getAttribute("data-field"), cell.textContent?.trim() ?? ""]));
  const savedMismatch = savedLabels.filter(([field, text]) => labels[field] && labels[field] !== text);
  check(
    "the saved record names each field with the backend's label",
    savedLabels.length > 0 && savedMismatch.length === 0,
    savedMismatch.map(([field, text]) => `${field}: "${text}" not "${labels[field]}"`).join("; ")
  );

  // 7. Back on the hospital page, the record is listed and counted, and it
  //    takes an outcome.
  await page.getByRole("link", { name: `Back to ${firstSite.display_name}` }).click();
  const row = page.locator(`[data-panel="entered"] tr[data-encounter="${encounterId}"]`);
  await row.waitFor({ timeout: 30000 });
  check("the entered record is listed at its hospital", (await row.count()) === 1, encounterId);
  await page.waitForFunction(
    (expected) => document.querySelector('[data-panel="hospital-store"] [data-count="entered"]')?.textContent?.trim() === expected,
    String(enteredBefore + 1),
    { timeout: 30000 }
  ).catch(() => {});
  const enteredAfter = Number((await enteredCount.textContent())?.replace(/[^\d]/g, "") ?? "NaN");
  check("the entered count rises by one", enteredAfter === enteredBefore + 1, `${enteredBefore} -> ${enteredAfter}`);
  await row.getByRole("button", { name: "Yes" }).click();
  await row.locator(".outcome-value").waitFor({ timeout: 30000 });
  check("the outcome is recorded", ((await row.locator(".outcome-value").textContent()) ?? "").trim() === "yes");

  // 8. The dark theme draws the charts again. Recording the outcome reloads
  //    the chart list, which also changes an image, so the test lets that
  //    settle and then requires the dark versions to be fetched.
  await page.waitForTimeout(1500);
  const chartImages = await page.locator('[data-panel="hospital-charts"] img.chart-image').count();
  const chartBefore = await hospitalChart.getAttribute("src");
  let darkCharts = 0;
  const countDark = (r) => {
    if (/\/hospitals\/[^/]+\/charts\/[^/]+\.svg\?theme=dark$/.test(r.url()) && r.status() < 400) darkCharts += 1;
  };
  page.on("response", countDark);
  await page.locator('.theme-toggle button:has-text("Dark")').click();
  await page
    .waitForFunction(
      (before) => {
        const img = document.querySelector('[data-panel="hospital-charts"] img.chart-image');
        return img && img.getAttribute("src") !== before && img.complete && img.naturalWidth > 0;
      },
      chartBefore,
      { timeout: 30000 }
    )
    .catch(() => {});
  // Each chart is drawn on first request, so the last one can take a moment.
  for (let waited = 0; waited < 60 && darkCharts < chartImages; waited += 1) await page.waitForTimeout(500);
  page.off("response", countDark);
  const chartAfter = await hospitalChart.getAttribute("src");
  const theme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  check(
    "the dark theme redraws the charts",
    theme === "dark" && chartAfter !== chartBefore && darkCharts === chartImages && chartImages > 0,
    `theme ${theme}, ${darkCharts} of ${chartImages} charts fetched in the dark version`
  );
  await page.locator('.theme-toggle button:has-text("System")').click();

  //    A keyboard user sees the whole focus ring on the theme toggle. The
  //    toggle clips its children, so the ring must be drawn inside the button.
  await page.locator("main h1").first().click();
  await page.evaluate(() => window.scrollTo(0, 0));
  let onDark = false;
  for (let presses = 0; presses < 20 && !onDark; presses += 1) {
    await page.keyboard.press("Tab");
    onDark = await page.evaluate(() => document.activeElement?.textContent?.trim() === "Dark");
  }
  const ring = await page.evaluate(() => {
    const button = document.activeElement;
    if (!button || button.textContent?.trim() !== "Dark") return null;
    const style = getComputedStyle(button);
    const holder = button.parentElement;
    const offset = parseFloat(style.outlineOffset) || 0;
    const width = parseFloat(style.outlineWidth) || 0;
    const box = button.getBoundingClientRect();
    const outer = holder.getBoundingClientRect();
    const reach = offset + width;
    const clips = getComputedStyle(holder).overflow !== "visible";
    const inside =
      box.left - reach >= outer.left - 0.5 &&
      box.top - reach >= outer.top - 0.5 &&
      box.right + reach <= outer.right + 0.5 &&
      box.bottom + reach <= outer.bottom + 0.5;
    return { visible: button.matches(":focus-visible") && style.outlineStyle !== "none" && width > 0, clips, inside, offset };
  });
  check(
    "the theme toggle's focus ring is drawn in full",
    Boolean(ring && ring.visible && (!ring.clips || ring.inside)),
    ring ? `offset ${ring.offset}px, ${ring.clips ? (ring.inside ? "inside the clipping box" : "clipped") : "not clipped"}` : "focus never reached the toggle"
  );

  //    A stored theme applies before the app's script runs, so a dark choice
  //    does not paint light first. The test blocks every script file, which
  //    leaves only the inline script in index.html.
  const firstPaint = await browser.newPage({ viewport: { width: 800, height: 600 } });
  try {
    await firstPaint.addInitScript(() => {
      try {
        localStorage.setItem("dagents-theme", "dark");
      } catch {}
    });
    await firstPaint.route("**/*", (route) =>
      route.request().resourceType() === "script" ? route.abort() : route.continue()
    );
    await firstPaint.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
    const before = await firstPaint.evaluate(() => document.documentElement.getAttribute("data-theme"));
    check("a stored dark theme applies before the app's script runs", before === "dark", `data-theme ${before}`);
  } finally {
    await firstPaint.close();
  }

  // 9. A cold start and an outage look different to the page. While the
  //    container starts, the platform answers 502, 503 or 504 without the
  //    app's X-Dagents-App header, and the page waits behind the connecting
  //    banner. The app's own 503 carries the header, and the page shows its
  //    message at once. Both are simulated in a second page, so these 503s are
  //    not counted as failed requests below.
  const coldPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    // The banner may come and go before a locator looks, so the page records
    // whether it ever appeared, and what it said.
    await coldPage.addInitScript(() => {
      new MutationObserver(() => {
        const banner = document.querySelector('[data-banner="connecting"]');
        if (banner) {
          window.__sawConnecting = true;
          window.__connectingText = banner.textContent;
        }
      }).observe(document, { childList: true, subtree: true });
    });
    const platformTotal = 4;
    let platformAnswers = platformTotal;
    await coldPage.route("**/api/v1/overview", (route) => {
      if (platformAnswers === 0) return route.continue();
      platformAnswers -= 1;
      return route.fulfill({ status: 503, contentType: "text/plain", body: "Service Unavailable" });
    });
    await coldPage.goto(baseUrl, { timeout: 60000 });
    const banner = coldPage.locator('[data-banner="connecting"]');
    await coldPage.locator('[data-panel="consortium"] table tbody tr').first().waitFor({ timeout: 60000 });
    const bannerShown = await coldPage.evaluate(() => window.__sawConnecting === true);
    check(
      "a 503 without the app's header shows the connecting banner, then the page",
      bannerShown && platformAnswers === 0 && (await banner.count()) === 0,
      `banner ${bannerShown ? "shown" : "not shown"}, ${platformTotal - platformAnswers} platform answers used`
    );
    const connectingText = (await coldPage.evaluate(() => window.__connectingText ?? "")).trim();
    check("the connecting banner gives the measured wait", /about 20 seconds/.test(connectingText), connectingText);

    await coldPage.unroute("**/api/v1/overview");
    let appAnswers = 0;
    const appOutage = (route) => {
      appAnswers += 1;
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "X-Dagents-App": "healthcare-demo" },
        body: JSON.stringify({ detail: "The hospital records cannot be reached right now. Try again in a minute." }),
      });
    };
    await coldPage.route("**/api/v1/overview", appOutage);
    const started = Date.now();
    await coldPage.reload({ timeout: 60000 });
    const outage = coldPage.locator('.banners [role="alert"]');
    const shown = await outage.waitFor({ timeout: 15000 }).then(
      () => true,
      () => false
    );
    const shownAfter = Date.now() - started;
    const atMessage = appAnswers;
    // A retry would come one second after the first answer.
    await coldPage.waitForTimeout(2500);
    const message = shown ? ((await outage.textContent()) ?? "").trim() : "";
    const bannerAgain = await coldPage.evaluate(() => window.__sawConnecting === true);
    check(
      "the app's own 503 is shown at once and not retried",
      shown && appAnswers === atMessage && !bannerAgain && /cannot be reached/.test(message),
      `${shown ? `shown after ${shownAfter} ms` : "not shown within 15 s"}; ${atMessage} request(s), ` +
        `${appAnswers - atMessage} more after it; banner ${bannerAgain ? "shown" : "not shown"}`
    );

    //    During an outage each page says so once, and no heading falls back
    //    to the raw site id.
    await coldPage.unroute("**/api/v1/overview");
    await coldPage.route("**/api/v1/**", appOutage);
    for (const [where, path] of [
      ["hospital page", `#/hospitals/${firstSite.site_id}`],
      ["data-entry page", `#/hospitals/${firstSite.site_id}/new`],
    ]) {
      await coldPage.goto(`${baseUrl}${path}`, { timeout: 60000 });
      await coldPage.locator('[role="alert"]').first().waitFor({ timeout: 15000 }).catch(() => {});
      await coldPage.waitForTimeout(1500);
      const repeats = await coldPage
        .locator('[role="alert"]')
        .evaluateAll((alerts) => alerts.filter((alert) => /cannot be reached/.test(alert.textContent ?? "")).length);
      const title = ((await coldPage.locator("main h1").first().textContent()) ?? "").trim();
      check(
        `an outage is reported once on the ${where}, under a readable heading`,
        repeats === 1 && !title.includes(firstSite.site_id),
        `${repeats} messages; heading "${title}"`
      );
    }
  } finally {
    await coldPage.close();
  }

  //    A dashboard reload that fails while older numbers are on screen says
  //    so. A record is entered through the API, the dashboard is made to
  //    answer the app's own 503, and recording the outcome reloads it.
  const stalePage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    const entry = Object.fromEntries(
      form.fields.map((field) => [
        field.name,
        field.type === "boolean"
          ? true
          : field.type === "choice"
            ? field.options[field.options.length > 1 ? 1 : 0]
            : field.type === "integer"
              ? Math.round((field.min + field.max) / 2)
              : (field.min + field.max) / 2,
      ])
    );
    await stalePage.goto(`${baseUrl}#/hospitals/${firstSite.site_id}`, { timeout: 60000 });
    const created = await stalePage.evaluate(
      async ({ path, body }) => {
        const response = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": `smoke-stale-${Date.now()}` },
          body: JSON.stringify(body),
        });
        return response.ok ? (await response.json()).record.encounter_id : `HTTP ${response.status}`;
      },
      { path: `/api/v1/hospitals/${firstSite.site_id}/encounters`, body: entry }
    );
    await stalePage.reload({ timeout: 60000 });
    const staleRow = stalePage.locator(`[data-panel="entered"] tr[data-encounter="${created}"]`);
    await staleRow.waitFor({ timeout: 30000 });
    const totalBefore = ((await stalePage.locator('[data-panel="hospital-store"] [data-count="total"]').textContent()) ?? "").trim();
    await stalePage.route(new RegExp(`/api/v1/hospitals/${firstSite.site_id}$`), (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "X-Dagents-App": "healthcare-demo" },
        body: JSON.stringify({ detail: "The hospital records cannot be reached right now. Try again in a minute." }),
      })
    );
    await staleRow.getByRole("button", { name: "No" }).click();
    const stale = stalePage.locator("[data-stale]");
    const staleShown = await stale.waitFor({ timeout: 15000 }).then(
      () => true,
      () => false
    );
    const staleText = staleShown ? ((await stale.innerText()) ?? "").trim() : "";
    const totalShown = ((await stalePage.locator('[data-panel="hospital-store"] [data-count="total"]').textContent()) ?? "").trim();
    check(
      "a failed reload is reported above the numbers from the last load",
      staleShown && /cannot be reached/.test(staleText) && /last successful load/.test(staleText) && totalShown === totalBefore,
      staleShown ? `${staleText.replace(/\s+/g, " ")}; records ${totalShown}` : "nothing shown"
    );
  } finally {
    await stalePage.close();
  }

  //    A hospital's reasons come from the backend and are shown in words: why
  //    a rate is not reported, why a round left a hospital out. The API's
  //    answers are rewritten in a separate page.
  const reasonPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    const rawReason = `cohort_size 0 is below minimum_cohort ${overview.minimum_cohort}`;
    const [withheldSite, smallSite] = overview.hospitals;
    const rewriteReason = (pattern, change) =>
      reasonPage.route(pattern, async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: change(await response.json()) });
      });
    await rewriteReason("**/api/v1/overview", (body) => ({
      ...body,
      hospitals: body.hospitals.map((item) =>
        item.site_id === withheldSite.site_id
          ? { ...item, outcome_rate: null, outcome_rate_status: "withheld" }
          : item.site_id === smallSite.site_id
            ? { ...item, outcome_rate: null, outcome_rate_status: "below_minimum_cohort" }
            : item
      ),
    }));
    await rewriteReason(new RegExp(`/api/v1/hospitals/${withheldSite.site_id}$`), (body) => ({
      ...body,
      outcome_rate: null,
      outcome_rate_status: "withheld",
      contributions: [
        {
          round_id: "ui-smoke-analytics",
          phase: "analytics",
          participation: "rejected",
          contributed_examples: 0,
          update_norm: null,
          metrics: {},
          reason: rawReason,
          submitted_at: new Date().toISOString(),
        },
        ...body.contributions,
      ],
    }));
    const leftOut = JSON.parse(JSON.stringify(pilot));
    if (leftOut.rounds?.analytics?.plan) {
      leftOut.rounds.analytics.plan.excluded_sites = [{ site_id: smallSite.site_id, reason: rawReason }];
    }
    await reasonPage.route("**/api/v1/pilot:run", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", headers: { "X-Dagents-App": "healthcare-demo" }, json: leftOut })
    );

    await reasonPage.goto(baseUrl, { timeout: 60000 });
    const rateCell = (siteId) =>
      reasonPage.locator(`[data-panel="consortium"] tr[data-site="${siteId}"] td[data-rate-status]`);
    await rateCell(withheldSite.site_id).waitFor({ timeout: 30000 });
    const withheldText = ((await rateCell(withheldSite.site_id).textContent()) ?? "").trim();
    const smallText = ((await rateCell(smallSite.site_id).textContent()) ?? "").trim();
    check(
      "the consortium table shows each hospital's own reason for a missing rate",
      /planner could not be asked/.test(withheldText) && !/fewer than/.test(withheldText) && /fewer than/.test(smallText),
      `${withheldSite.site_id}: ${withheldText}; ${smallSite.site_id}: ${smallText}`
    );

    await reasonPage.getByRole("button", { name: "Run the pilot" }).click();
    await reasonPage.locator('[data-panel="pilot"] .gates li').first().waitFor({ timeout: 30000 });
    const leftOutText = ((await reasonPage.locator('[data-panel="pilot"]').innerText()) ?? "").split("\n").find((line) => /left out/.test(line)) ?? "";
    check(
      "a hospital left out of a round is given the reason in words",
      /left out: The request is about 0 records, fewer than the minimum/.test(leftOutText) && !/cohort_size/.test(leftOutText),
      leftOutText || "no left-out line shown"
    );

    await reasonPage.goto(`${baseUrl}#/hospitals/${withheldSite.site_id}`, { timeout: 60000 });
    await reasonPage.locator('[data-panel="contributions"] tbody tr').first().waitFor({ timeout: 30000 });
    const roundText = ((await reasonPage.locator('[data-panel="contributions"] tbody tr').first().innerText()) ?? "").trim();
    const hospitalRate = ((await reasonPage.locator('[data-panel="hospital-store"] [data-rate-status]').textContent()) ?? "").trim();
    check(
      "the hospital page gives a refused round's reason and the rate's reason in words",
      /fewer than the minimum/.test(roundText) && !/cohort_size/.test(roundText) && /planner could not be asked/.test(hospitalRate),
      `${roundText.replace(/\s+/g, " ")}; rate: ${hospitalRate}`
    );
  } finally {
    await reasonPage.close();
  }

  // 10. Empty stores. The API is simulated in a separate page: it reports
  //     stores that hold no records, as an unseeded database would. Each page
  //     shows one banner and a plain sentence in place of zeros and empty
  //     tables, and a chart shows the backend's reason.
  const emptyPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const emptyReason = "This hospital's store holds no records yet.";
  try {
    const rewrite = (pattern, change) =>
      emptyPage.route(pattern, async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({ response, json: change(body) });
      });
    const zero = { total: 0, entered: 0, labelled: 0 };
    await rewrite("**/api/v1/framework/status", (body) => ({
      ...body,
      ready: false,
      hospitals: (body.hospitals ?? overview.hospitals).map((item) => ({
        site_id: item.site_id,
        record_count: 0,
        labelled_count: 0,
      })),
    }));
    await rewrite("**/api/v1/overview", (body) => ({
      ...body,
      hospitals: body.hospitals.map((item) => ({ ...item, record_count: 0, entered_count: 0, labelled_count: 0, outcome_rate: null })),
    }));
    await rewrite(/\/api\/v1\/hospitals\/[^/?]+$/, (body) => ({
      ...body,
      counts: zero,
      outcome_rate: null,
      profile: { ...body.profile, record_count: 0, fields: {} },
      local_metrics: { ...body.local_metrics, labelled_records: 0, metrics: {} },
      contributions: [],
    }));
    await rewrite(/\/api\/v1\/hospitals\/[^/]+\/worklist/, (body) => ({ ...body, worklist: [] }));
    await rewrite(/\/api\/v1\/hospitals\/[^/]+\/encounters/, (body) => ({ ...body, encounters: [], counts: zero }));
    await rewrite(/\/api\/v1\/hospitals\/[^/]+\/charts$/, (body) => ({
      ...body,
      charts: body.charts.map((chart) => ({ ...chart, available: false, reason: emptyReason })),
    }));

    await emptyPage.goto(baseUrl, { timeout: 60000 });
    await emptyPage.locator('[data-panel="consortium"] table tbody tr').first().waitFor({ timeout: 30000 });
    await emptyPage.waitForTimeout(500);
    const consortiumBanner = await emptyPage.locator('[data-banner="not-ready"]').count();
    const emptyRows = await emptyPage.locator('[data-panel="consortium"] tr[data-site] [data-empty-store]').count();
    const zeroCells = await emptyPage.locator('[data-panel="consortium"] tr[data-site] td.num').count();
    check(
      "empty stores: the consortium page shows one banner and no zero counts",
      consortiumBanner === 1 && emptyRows === overview.hospitals.length && zeroCells === 0,
      `${consortiumBanner} banners, ${emptyRows} rows say the store is empty, ${zeroCells} count cells`
    );

    await emptyPage.goto(`${baseUrl}#/hospitals/${firstSite.site_id}`, { timeout: 60000 });
    await emptyPage.locator('[data-panel="hospital-store"] [data-empty-store]').waitFor({ timeout: 30000 }).catch(() => {});
    await emptyPage.waitForTimeout(800);
    const hospitalBanner = await emptyPage.locator('[data-banner="not-ready"]').count();
    const storeEmpty = await emptyPage.locator('[data-panel="hospital-store"] [data-empty-store]').count();
    const emptyTables = await emptyPage.locator('[data-panel="profile"] table, [data-panel="worklist"] table').count();
    const worklistEmpty = ((await emptyPage.locator('[data-panel="worklist"]').innerText()) ?? "").includes("No records to rank");
    const reasons = await emptyPage.locator('[data-panel="hospital-charts"] .chart-unavailable').allTextContents();
    check(
      "empty stores: the hospital page says so instead of showing empty tables",
      hospitalBanner === 1 && storeEmpty === 1 && emptyTables === 0 && worklistEmpty,
      `${hospitalBanner} banners, store note ${storeEmpty}, ${emptyTables} empty tables, worklist note ${worklistEmpty}`
    );
    check(
      "empty stores: each chart shows the backend's reason",
      reasons.length > 0 && reasons.every((text) => text.trim() === emptyReason),
      reasons[0] ?? "no chart listed"
    );
  } finally {
    await emptyPage.close();
  }

  // 11. A phone-sized screen. The charts keep their drawn size and scroll
  //     inside their own box; the page itself never scrolls sideways.
  const phone = await browser.newPage({ viewport: { width: 375, height: 812 } });
  watch(phone);
  try {
    await phone.goto(`${baseUrl}#/hospitals/${firstSite.site_id}`, { timeout: 60000 });
    const image = phone.locator('[data-panel="hospital-charts"] img.chart-image').first();
    await image.waitFor({ timeout: 30000 });
    await phone.waitForFunction((img) => img.complete && img.naturalWidth > 0, await image.elementHandle(), { timeout: 30000 });
    const sizes = await phone.evaluate(() => ({
      chart: document.querySelector('[data-panel="hospital-charts"] img.chart-image')?.getBoundingClientRect().width ?? 0,
      page: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    check(
      "at 375 px a chart keeps its drawn size and the page does not scroll sideways",
      sizes.chart >= 480 && sizes.page <= sizes.viewport,
      `chart ${Math.round(sizes.chart)}px, page ${sizes.page}px, viewport ${sizes.viewport}px`
    );
  } finally {
    await phone.close();
  }

  // 12. Nothing failed quietly along the way.
  const unique = [...new Set(consoleErrors)];
  check("no console errors or failed requests", unique.length === 0, unique.join(" | "));
} catch (error) {
  console.log(`  FAIL  unexpected error — ${error.message}`);
  failures.push("unexpected error");
} finally {
  await browser.close();
}

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
