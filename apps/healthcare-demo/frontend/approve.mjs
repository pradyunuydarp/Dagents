/**
 * Browser check of two people approving at once.
 *
 * Two browser contexts stand for two people on the consortium page. Each runs
 * a pilot, so the GMA holds two candidates, both checked against the model in
 * use. The first person approves their candidate. The second person's approval
 * is then refused with a 409, because the model their candidate was checked
 * against is no longer in use. The check requires that:
 *
 *   1. both pilots reach a release decision, so both pages offer
 *      "Approve release";
 *   2. after the first approval, the first page's "Model in use" shows the
 *      released version;
 *   3. the second page shows the backend's reason for the 409, and its
 *      "Model in use" changes to the released version without a reload;
 *   4. after a reload, the second page still shows the released version, and
 *      so does GET /api/v1/models;
 *   5. nothing logs a console error or fails a request, except that one 409.
 *
 * The check changes the model in use and needs gates that let a candidate
 * through, so it runs against a demo of its own. Use tests/run_approval_check.sh,
 * which starts one.
 *
 * Usage:
 *   node approve.mjs <baseUrl>
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "broken".
 */
import { existsSync } from "node:fs";

const target = process.argv[2];
if (!target || target.startsWith("--")) {
  console.log("Usage: node approve.mjs <baseUrl>. tests/run_approval_check.sh starts a demo for it.");
  process.exit(1);
}
const baseUrl = target.replace(/\/?$/, "/");

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
  console.log("SKIP: playwright-core is not installed (npm install to enable the approval check)");
  process.exit(2);
}

const executablePath = findBrowser();
if (!executablePath) {
  console.log("SKIP: no Chromium found; set CHROMIUM_PATH to run the approval check");
  process.exit(2);
}

const failures = [];
const consoleErrors = [];
// A passing line reads "ok  <name>", with two spaces, as in smoke.mjs.
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(name);
};

// Web fonts are optional: the design tokens list fallback fonts, and some
// sandboxes block the font host. Everything else must load.
const optional = (text) => /fonts\.(googleapis|gstatic)\.com/.test(text);
const isApproval = (url) => /\/api\/v1\/models\/[^/]+:approve$/.test(new URL(url).pathname);

/**
 * Count every console error and failed request on a page. The second page
 * expects one refused approval: its 409 response and the console line Chromium
 * logs for it are counted in `refusals` instead.
 */
const watch = (page, name, refusals = null) => {
  const refused = (status, url) => refusals !== null && status === 409 && isApproval(url);
  page.on("console", (m) => {
    const url = m.location()?.url ?? "";
    if (m.type() !== "error" || optional(`${m.text()} ${url}`)) return;
    if (/status of 409/.test(m.text()) && refused(409, url)) return;
    consoleErrors.push(`${name}: ${m.text()}`);
  });
  page.on("pageerror", (e) => consoleErrors.push(`${name}: pageerror: ${e.message}`));
  page.on("response", (r) => {
    if (r.status() < 400 || optional(r.url())) return;
    if (refused(r.status(), r.url())) refusals.push(r.url());
    else consoleErrors.push(`${name}: HTTP ${r.status()} ${r.url()}`);
  });
  page.on("requestfailed", (r) => {
    if (!optional(r.url())) consoleErrors.push(`${name}: request failed: ${r.url()} ${r.failure()?.errorText ?? ""}`);
  });
};

/** The value of a fact in a panel's facts list, such as "Model in use" on the consortium panel. */
const fact = (page, panel, term) =>
  page
    .locator(`[data-panel="${panel}"] .ds-facts > div`)
    .filter({ has: page.locator(`dt:text-is("${term}")`) })
    .locator("dd");

/**
 * Wait until a locator's text is `expected`, for up to `timeout` ms, and
 * return the text it shows at the end, so a failure names what the page showed.
 */
const waitForText = async (locator, expected, timeout = 10000) => {
  const deadline = Date.now() + timeout;
  for (;;) {
    const shown = ((await locator.first().textContent({ timeout: 1000 }).catch(() => "")) ?? "").trim();
    if (shown === expected || Date.now() >= deadline) return shown;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
};

/** Run the pilot from a consortium page, wait until its gates are shown, and return the report. */
const runPilot = async (page) => {
  const answer = page.waitForResponse((r) => r.url().includes("/api/v1/pilot:run"), { timeout: 180000 });
  await page.getByRole("button", { name: "Run the pilot" }).click();
  const report = await (await answer).json();
  await page.locator('[data-panel="pilot"] .gates li').first().waitFor({ timeout: 180000 });
  return report;
};

/** Press "Approve release" and return the approval's response. */
const approve = async (page) => {
  const answer = page.waitForResponse((r) => isApproval(r.url()), { timeout: 60000 });
  await page.locator('[data-panel="approve"]').getByRole("button", { name: "Approve release" }).click();
  return answer;
};

const browser = await chromium.launch({ executablePath });
try {
  console.log(`Dagents healthcare demo — approval check against ${baseUrl}\n`);
  const refusals = [];
  // Two contexts share nothing, like two people on two machines.
  const first = await (await browser.newContext({ viewport: { width: 1280, height: 1100 } })).newPage();
  const second = await (await browser.newContext({ viewport: { width: 1280, height: 1100 } })).newPage();
  watch(first, "first page");
  watch(second, "second page", refusals);

  const models = async () => {
    const response = await first.request.get(new URL("api/v1/models", baseUrl).toString());
    return response.ok() ? response.json() : null;
  };
  const before = (await models())?.model_in_use?.version ?? "";

  for (const page of [first, second]) {
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60000 });
    await page.locator('[data-panel="consortium"] table.hospitals tbody tr').first().waitFor({ timeout: 30000 });
  }
  const shownBefore = await waitForText(fact(second, "consortium", "Model in use"), before);
  check("both pages start with the model in use", shownBefore === before && before !== "", `${shownBefore}, API ${before}`);

  // 1. Each person runs a pilot. Both candidates are checked against the same model.
  const pilots = [];
  for (const page of [first, second]) pilots.push(await runPilot(page));
  const decisions = pilots.map((pilot) => pilot.release?.gates?.action ?? "none");
  const baselines = pilots.map((pilot) => pilot.model_in_use?.version ?? "none");
  check(
    "both pilots reach a release decision against the same model",
    decisions.every((action) => action === "release") && baselines.every((version) => version === before),
    `decisions ${decisions.join(", ")}; checked against ${baselines.join(", ")}`
  );
  const offered = [];
  for (const page of [first, second]) {
    offered.push(await page.locator('[data-panel="approve"]').getByRole("button", { name: "Approve release" }).count());
  }
  check("both pages offer Approve release", offered.every((n) => n === 1), offered.join(", "));
  if (failures.length > 0) throw new Error("the pilots did not give two candidates to approve");
  const [released, refusedVersion] = pilots.map((pilot) => pilot.candidate.candidate_version);

  // 2. The first person approves.
  const firstAnswer = await approve(first);
  check("the first approval succeeds", firstAnswer.status() === 200, `HTTP ${firstAnswer.status()}`);
  const firstShown = await waitForText(fact(first, "consortium", "Model in use"), released);
  check("the first page shows the released model in use", firstShown === released, `shown ${firstShown}, released ${released}`);

  // 3. The second person approves the other candidate and is refused.
  const secondAnswer = await approve(second);
  const body = await secondAnswer.json().catch(() => ({}));
  const detail = typeof body.detail === "string" ? body.detail : "";
  check(
    "the second approval is refused because another model is now in use",
    secondAnswer.status() === 409 && detail.includes(refusedVersion) && detail.includes(released),
    `HTTP ${secondAnswer.status()}: ${detail || "no reason"}`
  );
  const alert = second.locator('[data-panel="approve"] [role="alert"]');
  await alert.waitFor({ timeout: 10000 }).catch(() => {});
  const shownReason = ((await alert.textContent().catch(() => "")) ?? "").trim();
  check("the second page shows the backend's reason", detail !== "" && shownReason === detail, shownReason || "no message shown");
  const refreshed = await waitForText(fact(second, "consortium", "Model in use"), released);
  check(
    "the refused page shows the released model in use without a reload",
    refreshed === released,
    `shown ${refreshed}, released ${released}`
  );

  // 4. A reload shows the same, and so does the API.
  await second.reload({ waitUntil: "networkidle", timeout: 60000 });
  const reloaded = await waitForText(fact(second, "consortium", "Model in use"), released);
  check("after a reload the refused page shows the released model in use", reloaded === released, `shown ${reloaded}`);
  const after = (await models())?.model_in_use?.version ?? "none";
  check("GET /api/v1/models names the released model", after === released, `${after}, released ${released}`);

  // 5. The one refusal, and nothing else.
  check("the second page got exactly one refused approval", refusals.length === 1, `${refusals.length} refusals`);
  check("no console errors or failed requests", consoleErrors.length === 0, consoleErrors.slice(0, 5).join(" | "));
} catch (error) {
  check("the approval check ran to the end", false, error.message);
} finally {
  await browser.close();
}

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall approval checks passed");
process.exit(failures.length ? 1 : 0);
