/**
 * Browser check of the NL2SQL demo.
 *
 * A typecheck and a bundle only prove that the app compiles. This check opens
 * the built UI in Chromium and uses it, in one of two modes.
 *
 * Live (the default), against a running backend and the framework services,
 * as `scripts/run_local_demo.sh --check` starts them:
 *
 *   1. the status strip reads 5/5 reachable;
 *   2. the sample list holds every sample the backend offers;
 *   3. for each sample, Generate SQL shows SQL that starts with SELECT, the DDL
 *      with a CREATE TABLE for each table, the prompt with the question, and
 *      a trace of 22 steps with no warning, whose payload opens;
 *   4. an edited question reaches the backend: the prompt and the SQL change;
 *   5. schema JSON that does not parse shows an error and sends nothing, and
 *      the page stays;
 *   6. the theme toggle switches the page between light and dark;
 *   7. with --pipeline-service-pid, after every other check, it stops
 *      pipeline-service: the strip reads 4/5, the trace shows exactly one
 *      warning, for "pipeline-service register and run", and SQL still shows;
 *   8. nothing logs a console error or fails a request.
 *
 * Replay (--replay <dist>), on the static build GitHub Pages publishes
 * (built with VITE_DAGENTS_STATIC=1 and its recording.json). It serves <dist>
 * under --base, as Pages does: a missing file is a 404, never the index page.
 *
 *   1. the banner names the commit the recording came from, and the button
 *      says it replays a recorded run;
 *   2. the status strip shows the status the recording holds;
 *   3. every sample with every model shows its recorded SQL and its trace; a
 *      warning step is allowed only when it calls a service the recording's
 *      note lists as not running during the capture;
 *   4. an edited question shows "Not recorded" and no SQL;
 *   5. no request goes to an /api/ path or to another origin (web fonts
 *      aside), and nothing logs a console error.
 *
 * Usage:
 *   node smoke.mjs [baseUrl] [--pipeline-service-pid <pid>]
 *   node smoke.mjs --replay <dist> [--base /Dagents/nl2sql-demo/]
 *
 * A passing line reads "ok  <name>", with two spaces. CI requires the line
 * "ok  with pipeline-service stopped ..." from a live run.
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "broken".
 */
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";

const args = process.argv.slice(2);
const valueIndexes = new Set();
const option = (name) => {
  const index = args.indexOf(name);
  if (index === -1) return null;
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) {
    console.log(`${name} needs a value`);
    process.exit(1);
  }
  valueIndexes.add(index + 1);
  return value;
};
const replayDir = option("--replay");
const basePath = `/${(option("--base") ?? "/").replace(/^\/+|\/+$/g, "")}/`.replace(/^\/\/$/, "/");
const pipelinePid = option("--pipeline-service-pid");
if (pipelinePid !== null && !/^\d+$/.test(pipelinePid)) {
  console.log("--pipeline-service-pid needs a process id");
  process.exit(1);
}
const positional = args.filter((a, i) => !a.startsWith("--") && !valueIndexes.has(i));

/** The trace of one generate request has this many steps. */
const TRACE_STEPS = 22;
/** The services the backend's status check calls. */
const SERVICE_COUNT = 5;
const PIPELINE_STEP = "pipeline-service register and run";

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
const problems = [];
// A passing line reads "ok  <name>", with two spaces: CI greps for some lines.
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(name);
};

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

/** Serve `root` under `base` on a free loopback port. A missing file is a 404, as on Pages. */
function serveStatic(root, base) {
  const top = path.resolve(root);
  const server = http.createServer(async (request, response) => {
    const pathname = decodeURIComponent(new URL(request.url ?? "/", "http://localhost").pathname);
    let relative = pathname.startsWith(base) ? pathname.slice(base.length) : null;
    if (relative !== null && (relative === "" || relative.endsWith("/"))) relative += "index.html";
    const file = relative === null ? null : path.resolve(top, relative);
    if (file === null || !file.startsWith(top + path.sep)) {
      response.writeHead(404, { "content-type": "text/plain" }).end("not found");
      return;
    }
    try {
      const body = await readFile(file);
      response.writeHead(200, { "content-type": CONTENT_TYPES[path.extname(file)] ?? "application/octet-stream" });
      response.end(body);
    } catch {
      response.writeHead(404, { "content-type": "text/plain" }).end("not found");
    }
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

let server = null;
let baseUrl;
let recording = null;
if (replayDir) {
  try {
    recording = JSON.parse(await readFile(path.join(replayDir, "recording.json"), "utf-8"));
  } catch (error) {
    console.log(`  FAIL  ${replayDir}/recording.json could not be read — ${error.message}`);
    process.exit(1);
  }
  server = await serveStatic(replayDir, basePath);
  baseUrl = `http://127.0.0.1:${server.address().port}${basePath}`;
} else {
  baseUrl = (positional[0] ?? "http://127.0.0.1:5173/").replace(/\/?$/, "/");
}
const origin = new URL(baseUrl).origin;
console.log(`${replayDir ? "replay" : "live"} check of ${baseUrl}`);

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });

// Web fonts are optional: the design tokens list fallback fonts, and some
// sandboxes block the font host. Everything else must load.
const optional = (text) => /fonts\.(googleapis|gstatic)\.com/.test(text);
const generateRequests = [];
page.on("console", (m) => {
  const text = `${m.text()} ${m.location()?.url ?? ""}`;
  if (m.type() === "error" && !optional(text)) problems.push(`console: ${m.text()}`);
});
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("request", (r) => {
  const url = new URL(r.url());
  if (url.pathname.endsWith("/api/v1/generate")) generateRequests.push(r.postData() ?? "");
  if (optional(r.url()) || url.protocol === "data:") return;
  if (replayDir && url.pathname.includes("/api/")) problems.push(`a replay sent a request to ${url.pathname}`);
  if (url.origin !== origin) problems.push(`request to another origin: ${url.origin}${url.pathname}`);
});
page.on("response", (r) => {
  if (r.status() >= 400 && !optional(r.url())) problems.push(`HTTP ${r.status()} ${r.url()}`);
});
page.on("requestfailed", (r) => {
  if (!optional(r.url())) problems.push(`request failed: ${r.url()} ${r.failure()?.errorText ?? ""}`);
});

/** A form control by the words of its label. */
const field = (label) =>
  page.locator("label.ds-field", { has: page.locator("span", { hasText: new RegExp(`^${label}$`) }) }).locator("select, textarea");
const sampleSelect = field("Sample");
const modelSelect = field("Model adapter");
const questionBox = field("Natural language question");
const schemaBox = field("Schema tables \\(JSON\\)");
const runButton = page.locator("button.run-btn");
const traceMeta = page.locator(".trace-meta");
const sqlBlock = page.locator("pre.sql");
const ddlBlock = page.locator("section.sub", { hasText: "DDL the adapter was given" }).locator("pre");
const promptBlock = page.locator("section.sub", { hasText: "Prompt sent to the adapter" }).locator("pre");
const notRecordedBanner = page.locator(".notice--warn", { hasText: "Not recorded" });
const errorBanner = page.locator(".ds-banner--deny");

const statusText = async () => {
  await page.waitForFunction(
    () => /\d+\/\d+ reachable|no status reported/.test(document.querySelector(".services-count")?.textContent ?? ""),
    null,
    { timeout: 45000 }
  );
  return (await page.locator(".services-count").textContent())?.trim() ?? "";
};

/** Load the page and wait until the samples and the status are shown. */
const open = async () => {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => (document.querySelector("label.ds-field select")?.options.length ?? 0) > 0, null, {
    timeout: 45000,
  });
  return statusText();
};

/** Choose a sample, which clears the last result, and optionally a model. */
const choose = async (sampleId, modelId) => {
  // Choosing the sample that is already selected changes nothing, so pick
  // another one first (or load the page again when there is only one).
  if ((await sampleSelect.inputValue()) === sampleId) {
    const values = await sampleSelect.locator("option").evaluateAll((items) => items.map((o) => o.value));
    const other = values.find((value) => value !== sampleId);
    if (other) await sampleSelect.selectOption(other);
    else await open();
  }
  await sampleSelect.selectOption(sampleId);
  await page.waitForFunction(() => /no run yet/.test(document.querySelector(".trace-meta")?.textContent ?? ""));
  if (modelId) await modelSelect.selectOption(modelId);
};

/** Select Generate SQL and wait for a trace, the "Not recorded" banner or an error. */
const generate = async () => {
  await runButton.click();
  await page.waitForFunction(
    () =>
      /steps/.test(document.querySelector(".trace-meta")?.textContent ?? "") ||
      document.querySelector(".notice--warn") !== null ||
      document.querySelector(".ds-banner--deny") !== null,
    null,
    { timeout: 120000 }
  );
  await page.waitForFunction(() => !document.querySelector("button.run-btn")?.disabled, null, { timeout: 120000 });
};

/** The trace as the page shows it. */
const traceRows = () =>
  page.locator("ol.trace > li.step").evaluateAll((rows) =>
    rows.map((row) => ({
      name: row.querySelector(".step-name")?.textContent?.trim() ?? "",
      status: row.classList.contains("step--warning") ? "warning" : row.classList.contains("step--ok") ? "ok" : "other",
    }))
  );

const traceSummary = (steps, warnings) => `${steps} steps · ${warnings} warning${warnings === 1 ? "" : "s"}`;

/** The trace step's service: its name starts with the service's name, as the status check reports it. */
const stepService = (stepName, serviceNames) =>
  serviceNames.find((name) => stepName.toLowerCase().startsWith(`${name.toLowerCase()} `)) ?? null;

async function live() {
  const api = async (pathname) => {
    const response = await page.request.get(new URL(pathname.replace(/^\//, ""), baseUrl).toString());
    if (!response.ok()) throw new Error(`${pathname} returned ${response.status()}`);
    return response.json();
  };

  // 1. Every framework service answers the backend's status check.
  const strip = await open();
  check(`the status strip reads ${SERVICE_COUNT}/${SERVICE_COUNT} reachable`, strip === `${SERVICE_COUNT}/${SERVICE_COUNT} reachable`, strip);

  // 2. The sample list is the backend's list.
  const samples = await api("/api/v1/samples");
  const options = await sampleSelect.locator("option").evaluateAll((items) => items.map((o) => [o.value, o.textContent]));
  check(
    "the sample list holds every sample the backend offers",
    samples.length > 0 && JSON.stringify(options) === JSON.stringify(samples.map((s) => [s.sample_id, s.title])),
    `${options.length} options, ${samples.length} samples`
  );

  // 3. Each sample runs through the framework and shows its SQL, DDL, prompt and trace.
  for (const sample of samples) {
    await choose(sample.sample_id);
    await generate();
    const sql = (await sqlBlock.textContent())?.trim() ?? "";
    const ddl = (await ddlBlock.textContent()) ?? "";
    const prompt = (await promptBlock.textContent()) ?? "";
    const rows = await traceRows();
    const warnings = rows.filter((row) => row.status !== "ok");
    const meta = (await traceMeta.textContent())?.trim() ?? "";
    const missingTables = sample.tables.filter((table) => !ddl.includes(`CREATE TABLE ${table.name} (`)).map((t) => t.name);
    check(`${sample.sample_id}: SQL that starts with SELECT`, sql.startsWith("SELECT"), sql);
    check(`${sample.sample_id}: the DDL has a CREATE TABLE for each table`, missingTables.length === 0, missingTables.join(", "));
    check(`${sample.sample_id}: the prompt holds the question`, prompt.includes(sample.question), prompt.slice(0, 80));
    check(
      `${sample.sample_id}: ${traceSummary(TRACE_STEPS, 0)}`,
      meta === traceSummary(TRACE_STEPS, 0) && rows.length === TRACE_STEPS && warnings.length === 0,
      warnings.length ? warnings.map((w) => w.name).join(", ") : meta
    );
  }
  const payload = page.locator("details.step-payload").first();
  await payload.locator("summary").click();
  let parsed = null;
  try {
    parsed = JSON.parse((await payload.locator("pre").textContent()) ?? "");
  } catch {
    parsed = null;
  }
  check("a trace step's payload opens and holds JSON", parsed !== null && typeof parsed === "object");

  // 4. An edited question reaches the backend.
  const first = samples[0];
  const edited = `How many ${first.tables[0].name} are there?`;
  await choose(first.sample_id);
  await questionBox.fill(edited);
  const sent = generateRequests.length;
  await generate();
  const editedSql = (await sqlBlock.textContent())?.trim() ?? "";
  const editedPrompt = (await promptBlock.textContent()) ?? "";
  check(
    "an edited question reaches the backend",
    generateRequests.length === sent + 1 &&
      generateRequests.at(-1).includes(edited) &&
      editedPrompt.includes(edited) &&
      editedSql.startsWith("SELECT COUNT(*)"),
    editedSql
  );

  // 5. Schema JSON that does not parse shows an error and sends nothing.
  await choose(first.sample_id);
  await schemaBox.fill("[{ not json");
  const before = generateRequests.length;
  await runButton.click();
  // The JSON is parsed in the browser, so the error shows at once or not at all.
  const shown = await errorBanner.waitFor({ timeout: 10000 }).then(
    () => true,
    () => false
  );
  const errorText = shown ? ((await errorBanner.textContent()) ?? "") : "no error banner";
  const heading = await page.locator("h1", { hasText: "Natural language to SQL" }).count();
  check(
    "schema JSON that does not parse shows an error, and the page stays",
    generateRequests.length === before && /JSON|SyntaxError/.test(errorText) && heading === 1,
    errorText.slice(0, 120)
  );

  // 6. The theme toggle switches the page.
  const themes = page.getByRole("group", { name: "Colour theme" });
  const ground = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await themes.getByRole("button", { name: "Light" }).click();
  const light = [await page.evaluate(() => document.documentElement.dataset.theme), await ground()];
  await themes.getByRole("button", { name: "Dark" }).click();
  const dark = [await page.evaluate(() => document.documentElement.dataset.theme), await ground()];
  await themes.getByRole("button", { name: "System" }).click();
  const system = await page.evaluate(() => document.documentElement.hasAttribute("data-theme"));
  check(
    "the theme toggle switches between light and dark",
    light[0] === "light" && dark[0] === "dark" && light[1] !== dark[1] && system === false,
    `light ${light[1]}, dark ${dark[1]}`
  );

  // 7. With pipeline-service stopped, only its step turns into a warning.
  if (pipelinePid) {
    try {
      process.kill(Number(pipelinePid), "SIGTERM");
    } catch (error) {
      check("pipeline-service can be stopped", false, error.message);
    }
    const deadline = Date.now() + 15000;
    let stopped = false;
    while (!stopped && Date.now() < deadline) {
      const status = await api("/api/v1/dagents/status");
      stopped = status.services.some((s) => s.name === "pipeline-service" && s.status !== "ok");
      if (!stopped) await page.waitForTimeout(250);
    }
    const degraded = await open();
    const chip = (await page.locator("li.service", { hasText: "pipeline-service" }).locator(".service-state").textContent())?.trim();
    await choose(first.sample_id);
    await generate();
    const rows = await traceRows();
    const warned = rows.filter((row) => row.status === "warning").map((row) => row.name);
    const meta = (await traceMeta.textContent())?.trim() ?? "";
    const sql = (await sqlBlock.textContent())?.trim() ?? "";
    check(
      `with pipeline-service stopped: ${SERVICE_COUNT - 1}/${SERVICE_COUNT} reachable, one warning step, and SQL`,
      degraded === `${SERVICE_COUNT - 1}/${SERVICE_COUNT} reachable` &&
        chip === "unavailable" &&
        meta === traceSummary(TRACE_STEPS, 1) &&
        JSON.stringify(warned) === JSON.stringify([PIPELINE_STEP]) &&
        sql.startsWith("SELECT"),
      `${degraded}; chip "${chip}"; ${meta}; warnings: ${warned.join(", ") || "none"}`
    );
  }
}

async function replay() {
  const entries = recording.entries ?? [];
  const recorded = (method, pathname) => entries.filter((e) => e.method === method && e.path === pathname);
  const status = recorded("GET", "/api/v1/dagents/status")[0]?.response?.services ?? [];
  const serviceNames = status.map((s) => s.name);
  const note = String(recording.planner ?? "");
  const marker = "not running during capture:";
  const notRunning = note.includes(marker)
    ? note.split(marker)[1].split(",").map((s) => s.trim()).filter(Boolean)
    : [];

  // 1. The page says it is a replay and names the commit.
  const strip = await open();
  const banner = (await page.locator(".notice", { hasText: "Recorded run." }).textContent()) ?? "";
  const runNote = (await page.locator(".run-note").textContent())?.trim() ?? "";
  check(
    "the banner names the recording's commit",
    String(recording.commit ?? "").length >= 10 && banner.includes(String(recording.commit).slice(0, 10)),
    banner.slice(0, 160)
  );
  check("the button says it replays a recorded run", runNote === "replays a recorded run", runNote);

  // 2. The status strip shows the recorded status.
  const reachable = status.filter((s) => s.status === "ok").length;
  check(
    "the status strip shows the recorded status",
    status.length > 0 && strip === `${reachable}/${status.length} reachable`,
    `${strip}; not running during the capture: ${notRunning.join(", ") || "none"}`
  );

  // 3. Every sample with every model replays its SQL and its trace.
  const samples = await sampleSelect.locator("option").evaluateAll((items) => items.map((o) => o.value));
  const models = await modelSelect.locator("option").evaluateAll((items) => items.map((o) => o.value));
  const recordedSamples = recorded("GET", "/api/v1/samples")[0]?.response ?? [];
  check(
    "the sample list holds every recorded sample",
    samples.length > 0 && JSON.stringify(samples) === JSON.stringify(recordedSamples.map((s) => s.sample_id)),
    `${samples.length} samples, ${models.length} models`
  );
  let replayed = 0;
  const wrong = [];
  for (const sampleId of samples) {
    const sample = recordedSamples.find((s) => s.sample_id === sampleId);
    for (const modelId of models) {
      await choose(sampleId, modelId);
      await generate();
      const entry = recorded("POST", "/api/v1/generate").find(
        (e) => e.request?.model_id === modelId && e.request?.question === sample?.question
      );
      const sql = (await sqlBlock.count()) ? ((await sqlBlock.textContent())?.trim() ?? "") : "";
      const rows = await traceRows();
      const warned = rows.filter((row) => row.status !== "ok").map((row) => row.name);
      const unexplained = warned.filter((name) => !notRunning.includes(stepService(name, serviceNames)));
      const meta = (await traceMeta.textContent())?.trim() ?? "";
      const trace = entry?.response?.dagents_trace ?? [];
      const ok =
        entry !== undefined &&
        sql.startsWith("SELECT") &&
        sql === entry.response.sql &&
        rows.length === trace.length &&
        rows.length > 0 &&
        meta === traceSummary(rows.length, warned.length) &&
        unexplained.length === 0;
      if (ok) replayed += 1;
      else wrong.push(`${sampleId} with ${modelId}: ${entry ? `${meta}; unexplained: ${unexplained.join(", ") || "none"}` : "not in the recording"}`);
    }
  }
  check(
    "every sample with every model replays its SQL and its trace",
    replayed === samples.length * models.length && replayed > 0,
    wrong.length ? wrong.join(" | ") : `${replayed} replayed`
  );

  // 4. An edited question was not recorded, and the page says so.
  await choose(samples[0], models[0]);
  await questionBox.fill("Which question was never recorded?");
  await generate();
  const shown = await notRecordedBanner.count();
  const sqlShown = await sqlBlock.count();
  const placeholder = await page.locator(".sql-panel .placeholder", { hasText: "No SQL yet" }).count();
  check("an edited question shows Not recorded and no SQL", shown === 1 && sqlShown === 0 && placeholder === 1);
}

try {
  if (replayDir) await replay();
  else await live();
  const unique = [...new Set(problems)];
  check("no console errors or failed requests", unique.length === 0, unique.join(" | "));
} catch (error) {
  console.log(`  FAIL  unexpected error — ${error.message.split("\n")[0]}`);
  // A page that never loads usually shows why in its failed requests.
  const unique = [...new Set(problems)];
  if (unique.length) console.log(`        on the way: ${unique.join(" | ")}`);
  failures.push("unexpected error");
} finally {
  await browser.close();
  server?.close();
}

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
