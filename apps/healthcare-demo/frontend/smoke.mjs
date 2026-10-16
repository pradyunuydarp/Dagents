/**
 * Frontend smoke test.
 *
 * A typecheck and a bundle only prove that the app compiles. They do not show
 * that it renders, that the proxy reaches the backend, or that the governance
 * controls change anything. This test drives the real UI against a live
 * backend, in a real browser:
 *
 *   1. the consortium page lists the hospitals the API reports, and the
 *      panel to add a hospital lists the one that can join, defines LMA and
 *      GMA where it first uses them, and says why it offers no button;
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
 *      and the page does not scroll sideways; on rewritten answers, the
 *      button that adds a hospital is fully visible at 375 px, the last
 *      attempt is hidden while a new one runs, focus moves to the attempt
 *      that returns, each step's status is a word, the next-round line does
 *      not repeat the planner's reason, and a joined hospital whose LMA
 *      stopped can be started again, also while the overview fails;
 *  10. the framework is visible: the "Who does what" table shows the agent
 *      mode the backend reports, every "Dagents API calls" strip names its
 *      agent and route and matches the calls the backend reports, no strip
 *      shows a host, each pilot round lists how the GMA dispatched its job
 *      (a refused job with its reason), and the hospital page groups its
 *      sections under the hospital's LMA and the app;
 *  11. with --expect-join, after every other check, the hospital joins from
 *      the panel: every step is done and shows its word, the panel shows the
 *      Compose file with <port> and <env folder> and no path of this machine,
 *      the next-round line and the attempt's "Dagents API calls" strip, focus
 *      is on the attempt, the table and the header list the hospital, its
 *      page draws its charts and names no count of hospitals, and a pilot
 *      includes it;
 *  12. nothing logs a console error or fails a request on the way.
 *
 * Usage:
 *   node smoke.mjs [baseUrl] [--screenshots <dir>] [--expect-join <site_id>]
 *
 * `--screenshots` also writes the README screenshots (consortium, guard-denied,
 * federated-pilot, hospital, data-entry) to <dir>.
 *
 * `--expect-join` is for a network-mode run started with --deploy, which can
 * add a hospital. Without it, the panel must offer no button. With it, the
 * panel must offer one for that hospital, and the test adds the hospital after
 * every other check, because the earlier checks count the hospitals the run
 * started with.
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "broken".
 */
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";

const args = process.argv.slice(2);
const shotIndex = args.indexOf("--screenshots");
const shotDir = shotIndex === -1 ? null : args[shotIndex + 1];
const joinIndex = args.indexOf("--expect-join");
const expectJoin = joinIndex === -1 ? null : args[joinIndex + 1] ?? "";
if (expectJoin === "" || expectJoin?.startsWith("--")) {
  console.log("--expect-join needs a site id, such as pl-290");
  process.exit(1);
}
const valueIndexes = new Set([shotIndex, joinIndex].filter((i) => i !== -1).map((i) => i + 1));
const positional = args.filter((a, i) => !a.startsWith("--") && !valueIndexes.has(i));
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
// A passing line reads "ok  <name>", with two spaces: CI greps for some
// lines exactly, such as "ok  deploy: hospital pl-290 joined".
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
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

/**
 * Every "Dagents API calls" strip inside `scope`: its count, its label, its
 * whole text (link text included, never an href) and one entry per row.
 */
const readStrips = (target, scope) =>
  target.locator(`${scope} [data-api-calls]`).evaluateAll((strips) =>
    strips.map((strip) => ({
      count: Number(strip.getAttribute("data-api-calls-count")),
      label: strip.querySelector(".api-calls-label")?.textContent?.trim() ?? "",
      text: strip.textContent ?? "",
      rows: [...strip.querySelectorAll("tr[data-call-path]")].map((row) => ({
        service: row.getAttribute("data-call-service"),
        site: row.getAttribute("data-call-site"),
        via: row.getAttribute("data-call-via"),
        path: row.getAttribute("data-call-path"),
        agent: row.querySelector("[data-call-agent]")?.textContent?.trim() ?? "",
        method: row.querySelector("[data-call-method]")?.textContent?.trim() ?? "",
        route: row.querySelector("[data-call-route]")?.textContent?.trim() ?? "",
      })),
    }))
  );

/** The agent a call names, as a strip must print it. */
const agentName = (call) => (call.service === "gma" ? "GMA" : `LMA ${call.site_id ?? call.site}`);

/** Why a strip is wrong, or null: each row names its agent, method and route, and no host appears. */
const stripProblem = (strip) => {
  if (/https?:|\.internal\b|bearer/i.test(strip.text)) return `shows an address or a token: ${strip.text.trim().slice(0, 120)}`;
  if (strip.count === 0) return /none/i.test(strip.text) ? null : "an empty strip does not say none";
  if (strip.rows.length === 0) return `${strip.count} calls and no rows`;
  for (const row of strip.rows) {
    if (row.agent !== agentName(row)) return `row names "${row.agent}" for ${row.service} ${row.site}`;
    if (!/^(GET|POST|PUT|DELETE|PATCH)$/.test(row.method)) return `row method "${row.method}"`;
    if (!row.path?.startsWith("/api/v1/") || row.route !== row.path) return `row route "${row.route}" for ${row.path}`;
  }
  return null;
};

/** `service site method path via` for each distinct call, so lists compare without order or counts. */
const callKeys = (calls) =>
  [...new Set((calls ?? []).map((c) => `${c.service} ${c.site_id ?? c.site ?? ""} ${c.method} ${c.path} ${c.via}`))].sort();

try {
  console.log(`Dagents healthcare demo — frontend smoke test against ${baseUrl}\n`);
  const overview = await api("/api/v1/overview");
  const status = await api("/api/v1/framework/status");
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
  await consortiumPanel.locator("table.hospitals tbody tr").first().waitFor({ timeout: 30000 });
  const heading = (await page.locator("h1").first().textContent())?.trim() ?? "";
  check("page renders its heading", heading.length > 0, heading);
  const hospitalRows = await consortiumPanel.locator("table.hospitals tbody tr").count();
  check(
    "consortium table lists the three hospitals the API reports",
    hospitalRows === 3 && hospitalRows === overview.hospitals.length,
    `${hospitalRows} rows, ${overview.hospitals.length} in the overview`
  );
  const navLinks = await page.locator('nav[aria-label="Pages"] a').count();
  check("the header links to every hospital", navLinks === overview.hospitals.length + 1, `${navLinks} links`);
  const notReady = await page.locator('[data-banner="not-ready"]').count();
  check("no not-loaded banner while every store holds records", notReady === 0, `${notReady} banners`);

  //    Who does what: the parts of the demo, and where the agents run, as the
  //    backend reports it. The page never guesses the mode.
  const who = page.locator('[data-panel="who-does-what"]');
  const reportedMode = status.agents?.mode ?? "missing";
  await who.locator("[data-agents-mode]").first().waitFor({ timeout: 30000 }).catch(() => {});
  const shownMode = (await who.locator("[data-agents-mode]").first().getAttribute("data-agents-mode").catch(() => null)) ?? "not shown";
  const modeText = ((await who.locator("[data-agents-mode]").first().textContent().catch(() => "")) ?? "").trim();
  const modeWords = { "in-process": /inside the demo server/i, network: /own process/i }[reportedMode];
  check(
    "the 'Who does what' table shows the agent mode the backend reports",
    shownMode === reportedMode && Boolean(modeWords?.test(modeText)),
    `backend ${reportedMode}, page ${shownMode}: "${modeText}"`
  );
  const agentRows = await who
    .locator("tr[data-agent]")
    .evaluateAll((rows) => rows.map((row) => [row.getAttribute("data-agent"), row.getAttribute("data-site") ?? ""]));
  const lmaRows = agentRows.filter(([agent]) => agent === "lma").map(([, site]) => site);
  check(
    "the 'Who does what' table lists the app, the GMA and each hospital's LMA",
    agentRows.filter(([agent]) => agent === "app").length === 1 &&
      agentRows.filter(([agent]) => agent === "gma").length === 1 &&
      lmaRows.join(",") === overview.hospitals.map((item) => item.site_id).join(","),
    agentRows.map(([agent, site]) => `${agent}${site ? ` ${site}` : ""}`).join(", ") || "no rows"
  );

  //    The strips on arrival: the hospitals table and the "Who does what"
  //    panel each list the agent calls behind their answer.
  for (const [hook, path] of [
    ["consortium", "/api/v1/overview"],
    ["who-does-what", "/api/v1/framework/status"],
  ]) {
    const strips = await readStrips(page, `[data-panel="${hook}"]`);
    const problem = strips.map(stripProblem).find(Boolean);
    check(
      `the ${hook} panel has a "Dagents API calls" strip that names each agent and route`,
      strips.length > 0 && strips[0].count > 0 && !problem,
      problem ?? `${strips.length} strips, ${strips[0]?.count ?? 0} calls, from ${path}`
    );
  }

  //    The panel to add a hospital lists the hospitals that can join. A run
  //    that cannot add one (any run but --deploy) shows the backend's reason
  //    and no button. A run that can offers a button for the hospital.
  const deployPanel = page.locator('[data-panel="deploy"]');
  await deployPanel.locator('[data-role="reason"], [data-action="deploy"]').first().waitFor({ timeout: 30000 }).catch(() => {});
  const deployReason = ((await deployPanel.locator('[data-role="reason"]').textContent().catch(() => "")) ?? "").trim();
  const deployButtons = await deployPanel.locator('[data-action="deploy"]').count();
  const joinable = await deployPanel.locator("tr[data-joinable]").count();
  if (expectJoin === null) {
    check(
      "deploy panel shows why adding a hospital is not offered",
      deployReason.length > 0 && deployButtons === 0 && joinable > 0,
      `${deployButtons} buttons, ${joinable} hospitals listed; ${deployReason || "no reason shown"}`
    );
  } else {
    const offered = deployPanel.locator(`[data-action="deploy"][data-site="${expectJoin}"]`);
    check(
      `deploy panel offers to add ${expectJoin}`,
      (await offered.count()) === 1 && (await offered.isEnabled()) && deployReason === "",
      `${deployButtons} buttons; ${deployReason || "no reason shown"}`
    );
  }
  //    The panel is shown to every visitor, so it defines LMA and GMA where it
  //    first uses them.
  const deployText = (await deployPanel.innerText().catch(() => "")) ?? "";
  const definedAtFirstUse = (abbreviation) => {
    const first = deployText.indexOf(abbreviation);
    return first > 0 && deployText[first - 1] === "(" && deployText[first + abbreviation.length] === ")";
  };
  check(
    "deploy panel defines LMA and GMA where it first uses them",
    definedAtFirstUse("LMA") && definedAtFirstUse("GMA"),
    deployText.split("\n").find((line) => /LMA|GMA/.test(line))?.slice(0, 160) ?? "no LMA or GMA in the panel"
  );
  //    Its strip lists the agent calls behind GET /api/v1/deployments: the
  //    GMA's site-addresses route, or none when the answer needed no agent.
  const deployStrips = await readStrips(page, '[data-panel="deploy"] [data-role="view-calls"]');
  const deployStripProblem = deployStrips.map(stripProblem).find(Boolean);
  const deployStripRows = deployStrips.flatMap((strip) => strip.rows);
  check(
    'the deploy panel has a "Dagents API calls" strip that names each agent and route',
    deployStrips.length === 1 &&
      !deployStripProblem &&
      deployStripRows.every((row) => row.service === "gma" && row.path === "/api/v1/federation/site-addresses"),
    deployStripProblem ?? `${deployStrips.length} strips, ${deployStripRows.length} rows`
  );
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
  //    `framework_calls` lists the agent calls behind the answer. It is a list
  //    of objects too, so it is checked on its own: each entry may hold only a
  //    call's seven keys, never a record value, a host or a body.
  const CALL_KEYS = ["method", "ms", "path", "service", "site_id", "status", "via"].join(",");
  const withoutCalls = (answer) => {
    if (!answer || typeof answer !== "object") return answer;
    const { framework_calls: _calls, ...rest } = answer;
    return rest;
  };
  const callsAreCallsOnly = (answer) =>
    Array.isArray(answer?.framework_calls) &&
    answer.framework_calls.every((call) => Object.keys(call).sort().join(",") === CALL_KEYS);
  const releaseShownWithoutRows = async (label) => {
    // The "Dagents API calls" table lists routes, and its own check below
    // requires every one of its rows to be a call. Any other table fails.
    const extraTables = await guardPanel.locator("table:not(.restrictions):not(.api-calls-table)").count();
    const nonCallRows = await guardPanel.locator("table.api-calls-table tbody tr:not([data-call-path])").count();
    const released = ((await guardPanel.locator('[data-released="records"]').textContent().catch(() => "")) ?? "").trim();
    const expected = String(lastProbe?.released_count ?? "missing");
    check(
      `the guard shows what it would release, not the rows (${label})`,
      extraTables === 0 && nonCallRows === 0 && released === expected,
      `${extraTables} other tables, ${nonCallRows} rows that are not calls; released ${released || "not shown"}, the backend released ${expected}`
    );
    check(
      `the probe's answer holds no patient rows (${label})`,
      lastProbe !== null && !("payload" in lastProbe) && !recordLists(withoutCalls(lastProbe)) && callsAreCallsOnly(lastProbe),
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
  const probeStrips = await readStrips(page, '[data-panel="guard"]');
  const probeShown = callKeys(probeStrips.flatMap((strip) => strip.rows));
  const probeReported = callKeys(lastProbe?.framework_calls);
  check(
    "the guard's strip lists the LMA call behind the answer",
    probeReported.length > 0 &&
      probeReported.every((key) => key.startsWith(`lma ${firstSite.site_id} `)) &&
      probeShown.join("|") === probeReported.join("|") &&
      !probeStrips.map(stripProblem).find(Boolean),
    `shown [${probeShown.join("; ")}], reported [${probeReported.join("; ")}]`
  );
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
  /** Run the pilot from the consortium page, wait until its gates are shown, and return the report. */
  const runPilot = async () => {
    const pilotAnswer = page.waitForResponse((r) => r.url().includes("/api/v1/pilot:run"), { timeout: 180000 });
    await page.getByRole("button", { name: "Run the pilot" }).click();
    const report = await (await pilotAnswer).json();
    await pilotPanel.locator(".gates li").first().waitFor({ timeout: 180000 });
    await page.waitForTimeout(500);
    return report;
  };
  const pilot = await runPilot();
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

  //    The pilot's strip: the app called only the GMA, and the GMA's calls to
  //    each hospital's LMA are listed as its own.
  const pilotStrips = await readStrips(page, '[data-panel="pilot"]');
  const pilotRows = pilotStrips.flatMap((strip) => strip.rows);
  const pilotProblem = pilotStrips.map(stripProblem).find(Boolean);
  //    The GMA sends each hospital one jobs:run call per round: the offer
  //    and the execution in one call.
  const executedAt = new Set(
    pilotRows.filter((row) => row.via === "gma" && row.path === "/api/v1/federation/jobs:run").map((row) => row.site)
  );
  check(
    "the pilot's strip lists the GMA's round calls and the GMA's calls to every LMA",
    pilotRows.some((row) => row.service === "gma" && row.via === "app" && row.method === "POST" && row.path === "/api/v1/federation/rounds") &&
      pilotRows.filter((row) => row.via === "app").every((row) => row.service === "gma") &&
      overview.hospitals.every((item) => executedAt.has(item.site_id)) &&
      callKeys(pilotRows).join("|") === callKeys(pilot.framework_calls).join("|") &&
      !pilotProblem,
    pilotProblem ?? `${pilotRows.length} rows; run calls at ${[...executedAt].join(", ") || "no hospital"}`
  );

  //    Dispatch rows: each round lists, per hospital, the job the GMA offered
  //    and whether the hospital's LMA ran it, as the round report says.
  const roundReports = [
    ["analytics", pilot.rounds?.analytics],
    ["evaluation", pilot.rounds?.evaluation],
    ["candidate_validation", pilot.rounds?.candidate_validation],
    ...(pilot.rounds?.training ?? []).map((round) => ["training", round]),
  ].filter(([, round]) => round && Array.isArray(round.dispatch));
  const dispatchShown = await pilotPanel.locator("tr[data-dispatch-site]").evaluateAll((rows) =>
    rows.map((row) => ({
      round: row.getAttribute("data-dispatch-round"),
      site: row.getAttribute("data-dispatch-site"),
      offer: row.getAttribute("data-offer"),
      run: row.getAttribute("data-run"),
    }))
  );
  //    A run row holds both steps: refused or failed at the offer, or the
  //    participation of the job the hospital ran.
  const dispatchReported = (round, siteId) => {
    const runRow = round.dispatch.find((call) => call.site_id === siteId && call.step === "run");
    if (runRow) {
      const offer = runRow.outcome === "refused" || runRow.outcome === "failed" ? runRow.outcome : "accepted";
      return { offer, run: offer === "accepted" ? runRow.outcome : "" };
    }
    return {
      offer: round.dispatch.find((call) => call.site_id === siteId && call.step === "offer")?.outcome ?? "",
      run: round.dispatch.find((call) => call.site_id === siteId && call.step === "execute")?.outcome ?? "",
    };
  };
  const dispatchMismatch = [];
  for (const [, round] of roundReports) {
    for (const siteId of new Set(round.dispatch.map((call) => call.site_id))) {
      const { offer, run } = dispatchReported(round, siteId);
      const row = dispatchShown.find((item) => item.round === round.round_id && item.site === siteId);
      if (!row || row.offer !== offer || row.run !== run) {
        dispatchMismatch.push(`${round.round_id} ${siteId}: shown ${row ? `${row.offer}/${row.run}` : "no row"}, reported ${offer}/${run}`);
      }
    }
  }
  const runEverywhere = roundReports.every(([, round]) =>
    overview.hospitals.every((item) => dispatchShown.some((row) => row.round === round.round_id && row.site === item.site_id && row.run))
  );
  check(
    "each round lists how the GMA dispatched its job to every hospital",
    roundReports.length > 0 && dispatchMismatch.length === 0 && runEverywhere,
    dispatchMismatch[0] ?? `${dispatchShown.length} rows over ${roundReports.length} rounds`
  );
  const curveWidth = await imageWidth(pilotPanel.locator('[data-chart="training-curve"] img.chart-image'));
  check("the training curve draws", curveWidth > 0, `${curveWidth}px`);
  if (shotDir) await pilotPanel.screenshot({ path: `${shotDir}/federated-pilot.png` });

  // 4. A consortium chart draws.
  const consortiumChart = chartsPanel.locator("img.chart-image").first();
  const consortiumWidth = await imageWidth(consortiumChart);
  check("a consortium chart draws", consortiumWidth > 0, `${consortiumWidth}px`);
  const chartList = await api("/api/v1/charts");
  const chartStrips = await readStrips(page, '[data-panel="charts"]');
  const chartProblem = chartStrips.map(stripProblem).find(Boolean);
  check(
    "the charts panel's strip names the GMA call behind the chart data",
    chartStrips.length === 1 &&
      chartStrips[0].rows.some((row) => row.service === "gma") &&
      callKeys(chartList.framework_calls).every((key) => callKeys(chartStrips[0].rows).includes(key)) &&
      !chartProblem,
    chartProblem ?? `${chartStrips.length} strips, ${chartStrips[0]?.rows.length ?? 0} rows`
  );
  const chartSources = await chartsPanel.locator("figure[data-chart]").evaluateAll((figures) =>
    figures.map((figure) => [
      figure.getAttribute("data-chart"),
      figure.querySelector("[data-drawn-by]")?.textContent?.trim() ?? "",
      figure.querySelector("[data-data-from]")?.textContent?.trim() ?? "",
    ])
  );
  const sourceMismatch = chartSources.filter(([id, drawn, from]) => {
    const info = chartList.charts.find((chart) => chart.chart_id === id);
    return !info || drawn !== info.drawn_by || from !== info.data_from;
  });
  check(
    "each consortium chart says who drew it and where its numbers come from",
    chartSources.length > 0 && sourceMismatch.length === 0,
    sourceMismatch.map(([id, drawn, from]) => `${id}: "${drawn}" / "${from}"`).join("; ") || `${chartSources.length} charts`
  );

  //    Small text keeps at least 4.5:1 contrast in both themes: the footer,
  //    the eyebrows (one on the guard panel's inset head), the gate kinds, the
  //    "Dagents API calls" labels and the chart source labels.
  const contrastTargets =
    ".foot p, .page-head .ds-eyebrow, .panel.guard .ds-eyebrow, .gate-kind, .api-calls-label, .chart-source dt, .chart-source dd";
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
  const profileRows = await page.locator('[data-panel="profile"] table.profile tbody tr').count();
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
  await page.locator('[data-panel="worklist"] table.worklist tbody tr').first().waitFor({ timeout: 30000 });
  const worklistRows = await page.locator('[data-panel="worklist"] table.worklist tbody tr').count();
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
  const codedHere = sentHere.filter((text) => identifier.test(text) || /label rate/.test(text));
  check(
    "the hospital's values sent are named in words",
    sentHere.length > 0 && codedHere.length === 0,
    codedHere[0] ?? `${sentHere.length} cells`
  );

  //    The hospital page in two groups: what the hospital's Dagents LMA holds,
  //    and what the app does at the hospital.
  const groups = await page.locator("main [data-group]").evaluateAll((sections) =>
    sections.map((section) => ({
      group: section.getAttribute("data-group"),
      heading: section.querySelector("h2")?.textContent?.replace(/\s+/g, " ").trim() ?? "",
      panels: [...section.querySelectorAll("[data-panel]")].map((panel) => panel.getAttribute("data-panel")),
    }))
  );
  const lmaGroup = groups.find((item) => item.group === "lma");
  const appGroup = groups.find((item) => item.group === "app");
  const hasAll = (group, hooks) => Boolean(group) && hooks.every((hook) => group.panels.includes(hook));
  check(
    "the hospital page groups its sections under the hospital's LMA and the app",
    groups.length === 2 &&
      lmaGroup?.heading === `Dagents LMA ${firstSite.site_id}` &&
      appGroup?.heading === "App at the hospital" &&
      hasAll(lmaGroup, ["profile", "model", "audit", "contributions"]) &&
      hasAll(appGroup, ["hospital-store", "worklist", "entered", "hospital-charts"]),
    groups.map((item) => `${item.heading}: ${item.panels.join(", ")}`).join(" | ") || "no groups"
  );

  //    Every strip on the hospital page names this hospital's LMA, and the
  //    LMA group's strip lists the calls the dashboard answer reports.
  await page.locator('[data-panel="audit"] [data-api-calls]').waitFor({ timeout: 30000 }).catch(() => {});
  const dashboardNow = await api(`/api/v1/hospitals/${firstSite.site_id}`);
  const hospitalStrips = await readStrips(page, "main");
  const hospitalProblem = hospitalStrips.map(stripProblem).find(Boolean);
  const lmaStripKeys = callKeys((await readStrips(page, '[data-group="lma"] > .group-head')).flatMap((strip) => strip.rows));
  const dashboardKeys = callKeys(dashboardNow.framework_calls);
  const otherSite = hospitalStrips
    .flatMap((strip) => strip.rows)
    .find((row) => row.service === "lma" && row.site !== firstSite.site_id);
  check(
    "the hospital page's strips name only this hospital's LMA and list the dashboard's calls",
    hospitalStrips.length >= 5 &&
      dashboardKeys.length > 0 &&
      dashboardKeys.every((key) => lmaStripKeys.includes(key)) &&
      !otherSite &&
      !hospitalProblem,
    hospitalProblem ??
      (otherSite ? `a strip names LMA ${otherSite.site}` : `${hospitalStrips.length} strips; LMA group [${lmaStripKeys.join("; ")}]`)
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
  const formStrips = await readStrips(page, '[data-panel="entry"]');
  const formCalls = form.framework_calls ?? [];
  check(
    "the entry form's strip matches the calls the backend reports, and says none when there are none",
    formStrips.length === 1 &&
      formStrips[0].count === formCalls.length &&
      callKeys(formStrips[0].rows).join("|") === callKeys(formCalls).join("|") &&
      !stripProblem(formStrips[0]),
    formStrips[0] ? `${formStrips[0].count} shown, ${formCalls.length} reported: ${formStrips[0].text.trim().slice(0, 80)}` : "no strip"
  );

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
  const savedBody = await storedResponse.json().catch(() => null);
  const savedStrips = await readStrips(page, '[data-panel="entry-result"]');
  const savedKeys = callKeys(savedStrips.flatMap((strip) => strip.rows));
  check(
    "the saved record's strip shows the hospital's LMA reading the store again",
    savedKeys.some((key) => key.startsWith(`lma ${firstSite.site_id} POST /api/v1/site/source:refresh`)) &&
      callKeys(savedBody?.framework_calls).join("|") === savedKeys.join("|") &&
      !savedStrips.map(stripProblem).find(Boolean),
    `[${savedKeys.join("; ")}]`
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
  // Start from the wordmark, the first link in the header, so the number of
  // focusable controls on the page does not decide whether the toggle is reached.
  await page.locator(".masthead .wordmark").focus();
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
    await coldPage.locator('[data-panel="consortium"] table.hospitals tbody tr').first().waitFor({ timeout: 60000 });
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

  //    The first hospital page after the demo server starts: the app registers
  //    its model versions with the GMA before the dashboard answers, so the
  //    dashboard lists those GMA calls too. The strip under the LMA lists only
  //    this hospital's LMA; the GMA's calls get a strip of their own.
  const firstPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    const registration = [1, 2].map((n) => ({
      service: "gma",
      site_id: null,
      method: "PUT",
      path: "/api/v1/model-versions/{version}",
      status: 201,
      ms: n,
      via: "app",
    }));
    await firstPage.route(new RegExp(`/api/v1/hospitals/${firstSite.site_id}$`), async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, framework_calls: [...registration, ...(body.framework_calls ?? [])] } });
    });
    await firstPage.goto(`${baseUrl}#/hospitals/${firstSite.site_id}`, { timeout: 60000 });
    await firstPage.locator('[data-group="lma"] > .group-head [data-api-calls]').first().waitFor({ timeout: 30000 });
    const headStrips = await readStrips(firstPage, '[data-group="lma"] > .group-head');
    const lmaRows = headStrips[0]?.rows ?? [];
    const gmaStrip = headStrips.find((strip) => strip.rows.some((row) => row.service === "gma"));
    check(
      "after a start, the LMA strip lists only the LMA and the GMA's calls have their own strip",
      lmaRows.length > 0 &&
        lmaRows.every((row) => row.service === "lma" && row.site === firstSite.site_id) &&
        Boolean(gmaStrip) &&
        gmaStrip !== headStrips[0] &&
        /other agents/.test(gmaStrip.label) &&
        gmaStrip.rows.every((row) => row.service === "gma" && row.path === "/api/v1/model-versions/{version}") &&
        !headStrips.map(stripProblem).find(Boolean),
      headStrips.map((strip) => `${strip.label}: ${strip.rows.map((row) => `${row.agent} ${row.method} ${row.path}`).join(", ")}`).join(" | ")
    );
  } finally {
    await firstPage.close();
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
    //    A hospital's LMA refuses the evaluation job: the GMA's dispatch row
    //    says so, with the LMA's reason, and the job does not run there.
    const refusedReason = "the job's model digest does not match the model the site received";
    const evaluationRound = leftOut.rounds?.evaluation;
    if (evaluationRound?.dispatch) {
      evaluationRound.dispatch = evaluationRound.dispatch
        .filter((call) => !(call.site_id === withheldSite.site_id && call.step === "execute"))
        .map((call) => (call.site_id === withheldSite.site_id ? { ...call, outcome: "refused" } : call));
      evaluationRound.results = (evaluationRound.results ?? []).map((item) =>
        item.site_id === withheldSite.site_id ? { ...item, participation: "rejected", refusal_reason: refusedReason } : item
      );
      evaluationRound.acceptances = (evaluationRound.acceptances ?? []).map((item) =>
        item.site_id === withheldSite.site_id ? { ...item, accepted: false, reason: refusedReason } : item
      );
    }
    //    In candidate validation, one LMA answers the job offer with an error
    //    and another does not answer at all. The table tells the two apart.
    const silentSite = overview.hospitals[2];
    const validationRound = leftOut.rounds?.candidate_validation;
    if (validationRound?.dispatch && silentSite) {
      const changed = { [smallSite.site_id]: 503, [silentSite.site_id]: null };
      validationRound.dispatch = validationRound.dispatch
        .filter((call) => !(call.site_id in changed && call.step === "execute"))
        .map((call) => (call.site_id in changed ? { ...call, outcome: "failed", status_code: changed[call.site_id] } : call));
    }
    await reasonPage.route("**/api/v1/pilot:run", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", headers: { "X-Dagents-App": "healthcare-demo" }, json: leftOut })
    );

    //    The status reports agents in their own processes, and one LMA that
    //    did not answer the GMA. The "Who does what" table says so.
    await rewriteReason("**/api/v1/framework/status", (body) => ({
      ...body,
      agents: {
        ...(body.agents ?? {}),
        mode: "network",
        lmas: (body.agents?.lmas ?? []).map((item) =>
          item.site_id === smallSite.site_id ? { ...item, reachable: false, source_kind: null } : item
        ),
      },
    }));

    await reasonPage.goto(baseUrl, { timeout: 60000 });
    const networkMode = reasonPage.locator('[data-panel="who-does-what"] [data-agents-mode]');
    await networkMode.waitFor({ timeout: 30000 }).catch(() => {});
    const networkText = ((await networkMode.textContent().catch(() => "")) ?? "").trim();
    const silentRow = (
      (await reasonPage.locator(`[data-panel="who-does-what"] tr[data-agent="lma"][data-site="${smallSite.site_id}"]`).textContent().catch(() => "")) ?? ""
    ).trim();
    check(
      "the 'Who does what' table follows the mode and the LMA answers the backend reports",
      (await networkMode.getAttribute("data-agents-mode").catch(() => null)) === "network" &&
        /own process/i.test(networkText) &&
        /did not answer the GMA/i.test(silentRow) &&
        /Not reported/.test(silentRow) &&
        !/from not reported/i.test(silentRow),
      `"${networkText}"; ${smallSite.site_id}: "${silentRow.replace(/\s+/g, " ").slice(0, 120)}"`
    );
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
    const refusedRow = reasonPage.locator(
      `[data-panel="pilot"] tr[data-dispatch-round="${evaluationRound?.round_id}"][data-dispatch-site="${withheldSite.site_id}"]`
    );
    const refusedText = ((await refusedRow.textContent().catch(() => "")) ?? "").replace(/\s+/g, " ").trim();
    check(
      "a job a hospital refused is shown as refused, with the LMA's reason, and not run",
      (await refusedRow.getAttribute("data-offer").catch(() => null)) === "refused" &&
        (await refusedRow.getAttribute("data-run").catch(() => "x")) === "" &&
        /refused/i.test(refusedText) &&
        refusedText.includes(refusedReason),
      refusedText || "no dispatch row for the refusing hospital"
    );
    const offerCell = async (siteId) =>
      (
        (await reasonPage
          .locator(
            `[data-panel="pilot"] tr[data-dispatch-round="${validationRound?.round_id}"][data-dispatch-site="${siteId}"] td:has(.ds-chip)`
          )
          .textContent()
          .catch(() => "")) ?? ""
      )
        .replace(/\s+/g, " ")
        .trim();
    const errorOffer = await offerCell(smallSite.site_id);
    const silentOffer = silentSite ? await offerCell(silentSite.site_id) : "";
    check(
      "an offer the LMA answered with an error is shown as failed with its status, not as no answer",
      /failed/.test(errorOffer) && errorOffer.includes("503") && !/no answer/.test(errorOffer) && /no answer/.test(silentOffer),
      `${smallSite.site_id}: "${errorOffer}"; ${silentSite?.site_id}: "${silentOffer}"`
    );

    await reasonPage.goto(`${baseUrl}#/hospitals/${withheldSite.site_id}`, { timeout: 60000 });
    await reasonPage.locator('[data-panel="contributions"] table.contributions tbody tr').first().waitFor({ timeout: 30000 });
    const roundText = ((await reasonPage.locator('[data-panel="contributions"] table.contributions tbody tr').first().innerText()) ?? "").trim();
    const hospitalRate = ((await reasonPage.locator('[data-panel="hospital-store"] [data-rate-status]').textContent()) ?? "").trim();
    check(
      "the hospital page gives a refused round's reason and the rate's reason in words",
      /fewer than the minimum/.test(roundText) && !/cohort_size/.test(roundText) && /planner could not be asked/.test(hospitalRate),
      `${roundText.replace(/\s+/g, " ")}; rate: ${hospitalRate}`
    );
  } finally {
    // The app may still have a request in a rewrite handler when the page
    // closes. That request fails, and its error is not a finding.
    await reasonPage.unrouteAll({ behavior: "ignoreErrors" });
    await reasonPage.close();
  }

  //    The Add a hospital panel, with GET /api/v1/deployments rewritten to
  //    offer the action, so every run checks it at phone width. The page
  //    never sends the request that adds a hospital.
  /** Steps whose visible status word is not the word for their `data-status`. */
  const STATUS_WORDS = { done: "Done", skipped: "Skipped", failed: "Failed", undone: "Undone", not_run: "Not run" };
  const wrongStatusWords = async (attemptLocator) =>
    (
      await attemptLocator
        .locator("[data-step]")
        .evaluateAll((items) =>
          items.map((item) => [item.getAttribute("data-step"), item.getAttribute("data-status"), item.querySelector(".step-status")?.textContent?.trim() ?? ""])
        )
    ).filter(([, status, word]) => STATUS_WORDS[status] !== word);

  const deployPage = await browser.newPage({ viewport: { width: 375, height: 812 } });
  watch(deployPage);
  try {
    const realView = await api("/api/v1/deployments");
    const offeredView = {
      ...realView,
      available: true,
      reason: null,
      applier: "process",
      hospitals: realView.hospitals.map((item) => ({ ...item, in_consortium: false, service: "not_started" })),
      deployments: [],
      framework_calls: [],
    };
    const appJson = (json) => ({ status: 200, contentType: "application/json", headers: { "X-Dagents-App": "healthcare-demo" }, json });
    const joining = offeredView.hospitals[0];
    /** An attempt in the shape the backend returns, for the rewritten answers. */
    const attempt = (id, failedStep, detail, nextRound = null) => {
      const order = ["plan", "start", "answer", "attach", "enrol", "join", "check"];
      const failedAt = order.indexOf(failedStep);
      const now = new Date().toISOString();
      return {
        deployment_id: id, kind: "lma", site_id: joining.site_id, service: `lma-${joining.site_id}`,
        applier: "process", status: "failed", adopted: false, compose_yaml: null, issues: [],
        steps: order.map((step, index) => ({
          step,
          status: index < failedAt ? "done" : index === failedAt ? "failed" : "not_run",
          detail: index === failedAt ? detail : index < failedAt ? "Done in the rewritten answer." : "",
          seconds: 0.01,
        })),
        next_round: nextRound, started_at: now, finished_at: now, framework_calls: [],
      };
    };
    const previous = attempt("dep-smoke-previous", "start", "Port 8085 is in use.");
    const second = attempt("dep-smoke-second", "start", "Port 8085 is still in use.");
    // The planner's own wording, which the page shows as it comes.
    const plannerSays = "site cohort 0 is below the per-site minimum 20";
    const notSelected = {
      ...attempt("dep-smoke-third", "check", `The next round would not select ${joining.display_name}: ${plannerSays}.`,
        { selected: false, reason: plannerSays }),
      status: "joined",
    };
    const answers = [second, notSelected];
    offeredView.deployments = [previous];
    let releasePost = () => {};
    const postHeld = new Promise((resolve) => {
      releasePost = resolve;
    });
    await deployPage.route("**/api/v1/deployments", async (route) => {
      if (route.request().method() === "GET") return route.fulfill(appJson(offeredView));
      // The request that adds a hospital never reaches the backend here.
      await postHeld;
      const answer = answers.shift();
      offeredView.deployments = [answer, ...offeredView.deployments];
      return route.fulfill(appJson(answer));
    });
    await deployPage.goto(baseUrl, { timeout: 60000 });
    const phoneButton = deployPage.locator('[data-panel="deploy"] [data-action="deploy"]').first();
    await phoneButton.waitFor({ timeout: 30000 });
    // Measured as a reader first sees it: no box scrolled sideways. (A
    // scrollIntoView would scroll the table's own scroll box to the button.)
    const fit = await phoneButton.evaluate((button) => {
      let clipped = false;
      const boxes = [];
      for (let node = button.parentElement; node; node = node.parentElement) {
        if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(node).overflowX)) {
          node.scrollLeft = 0;
          boxes.push(node);
        }
      }
      const box = button.getBoundingClientRect();
      for (const node of boxes) {
        const outer = node.getBoundingClientRect();
        if (box.left < outer.left - 0.5 || box.right > outer.right + 0.5) clipped = true;
      }
      return { left: Math.round(box.left), right: Math.round(box.right), clipped, viewport: window.innerWidth };
    });
    check(
      "at 375 px the button that adds a hospital is fully visible",
      !fit.clipped && fit.left >= 0 && fit.right <= fit.viewport,
      `button from ${fit.left} to ${fit.right} px of ${fit.viewport}${fit.clipped ? ", clipped by a scroll box" : ""}`
    );

    //    While a second attempt runs, the first one's steps are not shown
    //    under the "Adding" note, where they would read as the new attempt.
    const attemptShown = (id) => deployPage.locator(`[data-panel="deploy"] [data-deployment="${id}"]`);
    await attemptShown(previous.deployment_id).waitFor({ timeout: 30000 });
    await phoneButton.click();
    await deployPage.locator("[data-deploy-running]").waitFor({ timeout: 15000 });
    const shownWhileAdding = await deployPage.locator('[data-panel="deploy"] [data-deployment]').count();
    releasePost();
    await attemptShown(second.deployment_id).waitFor({ timeout: 30000 });
    check(
      "while a hospital is being added, the last attempt is not shown",
      shownWhileAdding === 0,
      `${shownWhileAdding} earlier attempt(s) shown under the running note`
    );
    const wrongWords = await wrongStatusWords(attemptShown(second.deployment_id));
    check(
      "each step of an attempt shows its status as a word",
      wrongWords.length === 0,
      wrongWords.length ? wrongWords.map((item) => item.join(" ")).join(", ") : "Done, Failed and Not run shown"
    );
    //    The button is disabled while the app works, so focus would fall to
    //    the page. It moves to the new attempt's heading, and the live region,
    //    which stays on the page, says how the attempt ended.
    await deployPage.waitForTimeout(300);
    const focused = await deployPage.evaluate(() => document.activeElement?.id ?? document.activeElement?.tagName ?? "");
    const announced = ((await deployPage.locator('[data-panel="deploy"] [data-role="deploy-status"]').textContent().catch(() => "")) ?? "").trim();
    check(
      "focus moves to the attempt that returned, and its end is announced",
      focused === `attempt-${second.deployment_id}` && announced.includes(`${joining.display_name} did not join`),
      `focus on ${focused || "nothing"}; live region: ${announced || "empty"}`
    );

    //    When the planner does not select the hospital, the check step gives
    //    its reason. The last line says what that means, without repeating it.
    await phoneButton.click();
    await attemptShown(notSelected.deployment_id).waitFor({ timeout: 30000 });
    const nextRoundLine = (
      (await attemptShown(notSelected.deployment_id).locator('[data-role="next-round"]').textContent().catch(() => "")) ?? ""
    ).trim();
    const attemptText = (await attemptShown(notSelected.deployment_id).innerText()) ?? "";
    const reasonCount = attemptText.split(plannerSays).length - 1;
    check(
      "the next-round line ends with a full stop and does not repeat the planner's reason",
      nextRoundLine.length > 0 && nextRoundLine.endsWith(".") && reasonCount === 1,
      `"${nextRoundLine || "no line"}"; the reason appears ${reasonCount} time(s) in the attempt`
    );
  } finally {
    await deployPage.unrouteAll({ behavior: "ignoreErrors" });
    await deployPage.close();
  }

  //    A joined hospital whose LMA has stopped makes the overview fail until
  //    it runs again. The panel offers to start it again, with the overview
  //    and while the overview answers the app's 503. The answers are
  //    rewritten in a page whose 503s are not counted as failures, and the
  //    page never sends the request.
  const restartPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    const realView = await api("/api/v1/deployments");
    const stoppedView = {
      ...realView,
      available: true,
      reason: null,
      applier: "process",
      hospitals: realView.hospitals.map((item) => ({ ...item, in_consortium: true, service: "stopped" })),
      deployments: [],
      framework_calls: [],
    };
    const stopped = stoppedView.hospitals[0];
    await restartPage.route("**/api/v1/deployments", (route) =>
      route.request().method() === "GET"
        ? route.fulfill({ status: 200, contentType: "application/json", headers: { "X-Dagents-App": "healthcare-demo" }, json: stoppedView })
        : route.abort()
    );
    const restartButton = restartPage.locator(`[data-panel="deploy"] [data-action="deploy"][data-site="${stopped.site_id}"]`);
    const buttonText = async () =>
      (await restartButton.waitFor({ timeout: 30000 }).then(
        async () => ((await restartButton.textContent()) ?? "").trim(),
        () => "no button"
      ));
    await restartPage.goto(baseUrl, { timeout: 60000 });
    const withOverview = await buttonText();
    await restartPage.route("**/api/v1/overview", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "X-Dagents-App": "healthcare-demo" },
        body: JSON.stringify({ detail: `${stopped.display_name} cannot be reached right now. Try again in a minute.` }),
      })
    );
    await restartPage.reload({ timeout: 60000 });
    const withoutOverview = await buttonText();
    const expected = `Start ${stopped.display_name} again`;
    check(
      "a joined hospital whose LMA stopped can be started again, also while the overview fails",
      withOverview === expected && withoutOverview === expected,
      `with the overview: ${withOverview}; while it answers 503: ${withoutOverview}`
    );
  } finally {
    await restartPage.unrouteAll({ behavior: "ignoreErrors" });
    await restartPage.close();
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
    await emptyPage.locator('[data-panel="consortium"] table.hospitals tbody tr').first().waitFor({ timeout: 30000 });
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
    // The strips' tables list routes, not records, so they are not counted.
    const emptyTables = await emptyPage
      .locator('[data-panel="profile"] table:not(.api-calls-table), [data-panel="worklist"] table:not(.api-calls-table)')
      .count();
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
    // The app may still have a request in a rewrite handler when the page
    // closes. That request fails, and its error is not a finding.
    await emptyPage.unrouteAll({ behavior: "ignoreErrors" });
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

  // 12. With --expect-join, the hospital joins from the panel, after every
  //     other check, because they count the hospitals the run started with.
  if (expectJoin !== null) {
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60000 });
    const rows = consortiumPanel.locator("table.hospitals tbody tr");
    await rows.first().waitFor({ timeout: 30000 });
    const rowsBefore = await rows.count();
    const linksBefore = await page.locator('nav[aria-label="Pages"] a').count();
    const button = deployPanel.locator(`[data-action="deploy"][data-site="${expectJoin}"]`);
    await button.waitFor({ timeout: 30000 });
    const answered = page.waitForResponse(
      (r) => r.url().endsWith("/api/v1/deployments") && r.request().method() === "POST",
      { timeout: 180000 }
    );
    await button.click();
    // Starting the hospital's LMA takes at least a second, so the button is
    // still on the page, disabled, right after the click.
    const disabled = await button.isDisabled({ timeout: 2000 }).catch(() => null);
    const deployment = await (await answered).json();
    await deployPanel.locator('[data-step="check"]').waitFor({ timeout: 60000 });
    const steps = await deployPanel
      .locator("[data-step]")
      .evaluateAll((items) => items.map((item) => [item.getAttribute("data-step"), item.getAttribute("data-status")]));
    const unfinished = steps.filter(([, status]) => status !== "done" && status !== "skipped");
    const composeText = (await deployPanel.locator('[data-role="compose-file"] pre').textContent().catch(() => "")) ?? "";
    // The page fetches the overview and the panel's list again after a join.
    const grown = await page
      .waitForFunction(
        ([rowsWanted, linksWanted]) =>
          document.querySelectorAll('[data-panel="consortium"] table.hospitals tbody tr').length === rowsWanted &&
          document.querySelectorAll('nav[aria-label="Pages"] a').length === linksWanted &&
          document.querySelectorAll('[data-panel="deploy"] [data-action="deploy"]').length === 0,
        [rowsBefore + 1, linksBefore + 1],
        { timeout: 30000 }
      )
      .then(
        () => true,
        () => false
      );
    const rowsAfter = await rows.count();
    const linksAfter = await page.locator('nav[aria-label="Pages"] a').count();
    const listed = await consortiumPanel.locator(`tr[data-site="${expectJoin}"]`).count();
    const buttonsLeft = await deployPanel.locator('[data-action="deploy"]').count();
    check(
      `deploy: hospital ${expectJoin} joined`,
      disabled === true &&
        deployment.status === "joined" &&
        steps.length === 7 &&
        unfinished.length === 0 &&
        /(^|\n)services:/.test(composeText) &&
        composeText.includes(`lma-${expectJoin}`) &&
        grown &&
        listed === 1 &&
        buttonsLeft === 0,
      `button ${disabled === true ? "disabled" : "not disabled"} while it ran; status ${deployment.status}; ` +
        `${steps.length} steps${unfinished.length ? `, not finished: ${unfinished.map((item) => item.join(" ")).join(", ")}` : ""}; ` +
        `compose file ${composeText.includes(`lma-${expectJoin}`) ? "shown" : "missing"}; ` +
        `rows ${rowsBefore} -> ${rowsAfter}, links ${linksBefore} -> ${linksAfter}; ${buttonsLeft} buttons left`
    );

    //     The page and the answer show <port> and <env folder> in place of
    //     the new LMA's port and the folder of its env file, and no path of
    //     this machine, because any page can read the deployments view.
    const placeholderNote =
      ((await deployPanel.locator('[data-role="compose-placeholders"]').textContent().catch(() => "")) ?? "").trim();
    const machinePath = /(^|[\s"'(\[=])\/(home|tmp|root|Users|srv|var|opt|usr|private)\//;
    const detailsText = deployment.steps.map((step) => step.detail).join(" ");
    const leaked =
      machinePath.exec(composeText)?.[0] ??
      machinePath.exec(deployment.compose_yaml ?? "")?.[0] ??
      machinePath.exec(detailsText)?.[0] ??
      /127\.0\.0\.1:\d+/.exec(JSON.stringify(deployment))?.[0] ??
      null;
    check(
      "deploy: the Compose file shows <port> and <env folder>, and no address or path of this machine",
      composeText.includes('"--port", "<port>"') &&
        composeText.includes(`"<env folder>/lma-${expectJoin}.env"`) &&
        placeholderNote.includes("<port>") &&
        leaked === null,
      leaked ? `shows "${leaked.trim()}"` : `placeholders ${composeText.includes("<port>") ? "shown" : "missing"}; note "${placeholderNote}"`
    );

    //     The steps show their status in words, the line under them says
    //     what the planner's answer means, and focus is on the attempt.
    const joinedAttempt = deployPanel.locator(`[data-deployment="${deployment.deployment_id}"]`);
    const joinedWords = await wrongStatusWords(joinedAttempt);
    const nextRoundLine = ((await joinedAttempt.locator('[data-role="next-round"]').textContent().catch(() => "")) ?? "").trim();
    const focusedId = await page.evaluate(() => document.activeElement?.id ?? "");
    const joinedDisplay =
      (await api("/api/v1/deployments")).hospitals.find((item) => item.site_id === expectJoin)?.display_name ?? expectJoin;
    check(
      `deploy: the attempt shows its status words, the next-round line and takes focus`,
      joinedWords.length === 0 &&
        nextRoundLine.endsWith(".") &&
        nextRoundLine.includes(joinedDisplay) &&
        (deployment.next_round?.selected ? /^Run the pilot/.test(nextRoundLine) : /leave it out/.test(nextRoundLine)) &&
        focusedId === `attempt-${deployment.deployment_id}`,
      `${joinedWords.length ? `wrong words: ${joinedWords.map((item) => item.join(" ")).join(", ")}; ` : ""}` +
        `next round: "${nextRoundLine || "no line"}"; focus on ${focusedId || "nothing"}`
    );

    //     The attempt's strip lists the calls the POST reports: the new LMA's
    //     health and site routes and the GMA's address, refresh and plan
    //     routes. It names each agent and route and holds no address.
    const attemptStrips = await readStrips(page, `[data-panel="deploy"] [data-deployment="${deployment.deployment_id}"]`);
    const attemptRows = attemptStrips.flatMap((strip) => strip.rows);
    const attemptProblem = attemptStrips.map(stripProblem).find(Boolean);
    check(
      "deploy: the attempt's strip lists the agent calls the answer reports",
      attemptStrips.length === 1 &&
        !attemptProblem &&
        attemptRows.some((row) => row.service === "lma" && row.site === expectJoin) &&
        attemptRows.some((row) => row.service === "gma") &&
        callKeys(attemptRows).join("|") === callKeys(deployment.framework_calls).join("|"),
      attemptProblem ?? `${attemptRows.length} rows; ${callKeys(deployment.framework_calls).length} distinct calls reported`
    );

    //     Its page comes from its own LMA, and its charts draw.
    const name =
      (await api("/api/v1/overview")).hospitals.find((item) => item.site_id === expectJoin)?.display_name ?? expectJoin;
    await page.locator(`nav[aria-label="Pages"] a[href$="hospitals/${expectJoin}"]`).click();
    const joinedChart = page.locator('[data-panel="hospital-charts"] img.chart-image').first();
    const joinedWidth = await imageWidth(joinedChart).catch(() => 0);
    const joinedHeading = ((await page.locator("main h1").first().textContent()) ?? "").trim();
    check(
      `deploy: hospital ${expectJoin} page draws its charts`,
      joinedWidth > 0 && joinedHeading.includes(name),
      `${joinedWidth}px; heading "${joinedHeading}"`
    );

    //     A hospital can join while the demo runs, so its page names no count
    //     of hospitals.
    const joinedText = (await page.locator("main").innerText().catch(() => "")) ?? "";
    const counted = /\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+hospitals\b/i.exec(joinedText);
    check(
      `deploy: hospital ${expectJoin} page names no count of hospitals`,
      joinedText.length > 0 && counted === null,
      counted ? `says "${counted[0]}"` : `${joinedText.length} characters of text`
    );

    //     The next pilot includes it.
    await page.locator('nav[aria-label="Pages"] a', { hasText: "Consortium" }).click();
    await pilotPanel.waitFor({ timeout: 30000 });
    const joinedPilot = await runPilot();
    const rounds = [
      joinedPilot.rounds?.analytics,
      joinedPilot.rounds?.evaluation,
      ...(joinedPilot.rounds?.training ?? []),
      joinedPilot.rounds?.candidate_validation,
    ].filter(Boolean);
    const without = rounds.filter((round) => !round.results.some((result) => result.site_id === expectJoin));
    const chainShown = await pilotPanel.locator(`tr[data-chain="${expectJoin}"]`).count();
    check(
      `deploy: the pilot includes ${expectJoin}`,
      rounds.length > 0 && without.length === 0 && chainShown === 1,
      `${rounds.length - without.length} of ${rounds.length} rounds include it; audit chain row ${chainShown ? "shown" : "missing"}`
    );
  }

  // 13. Nothing failed quietly along the way.
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
