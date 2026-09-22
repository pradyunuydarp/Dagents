/**
 * Frontend smoke test.
 *
 * A typecheck and a bundle prove the app compiles. They prove nothing about
 * whether it renders, whether the proxy reaches the backend, or whether the
 * governance controls actually change anything — which is the whole point of
 * this UI. This drives the three interactions that carry the demo's argument
 * and fails if any of them stops working.
 *
 * Usage:
 *   node smoke.mjs [baseUrl] [--screenshots <dir>]
 *
 * Exits 0 on success, 1 on failure, and 2 when no browser is available, so a
 * machine without Chromium reports "skipped" rather than "broken".
 */
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";

const args = process.argv.slice(2);
const baseUrl = args.find((a) => !a.startsWith("--")) ?? "http://127.0.0.1:5174/";
const shotIndex = args.indexOf("--screenshots");
const shotDir = shotIndex === -1 ? null : args[shotIndex + 1];

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
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
page.on("response", (r) => { if (r.status() >= 400) consoleErrors.push(`HTTP ${r.status()} ${r.url()}`); });

try {
  console.log(`Dagents healthcare demo — frontend smoke test against ${baseUrl}\n`);
  await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1200);

  // 1. The page renders and the backend answered.
  const heading = (await page.locator("h1").first().textContent())?.trim() ?? "";
  check("page renders its heading", heading.length > 0, heading);
  const hospitalRows = await page.locator("section.card").first().locator("table tbody tr").count();
  check("consortium table is populated from the API", hospitalRows === 3, `${hospitalRows} hospitals`);
  if (shotDir) await page.screenshot({ path: `${shotDir}/01-landing.png`, fullPage: true });

  // The guard panel has three levers. Each is asserted separately, because
  // each proves a different rule and any of them can regress on its own.
  const guardPanel = page.locator("section.card").nth(2);
  const strategyFor = async (field) =>
    (await guardPanel.locator(`tbody tr:has(td:text-is("${field}")) td`).last().textContent())?.trim() ?? "";
  const askGuard = async ({ verified, granularity, cohort }) => {
    const box = page.getByLabel("requester verified");
    if (verified) await box.check();
    else await box.uncheck();
    await page.locator("select").first().selectOption(granularity);
    await page.getByLabel("cohort").fill(String(cohort));
    await page.getByRole("button", { name: "Ask the guard" }).click();
    await page.waitForTimeout(1000);
    return (await page.locator(".verdict").first().textContent())?.trim() ?? "";
  };

  // 2. Trust: the same request, generalized for a verified requester and
  //    redacted for an unverified one.
  const trusted = await askGuard({ verified: true, granularity: "row", cohort: 25 });
  const trustedStrategy = await strategyFor("nihss_total");
  check("a verified row request is narrowed, not denied", /NARROW|PERMIT/.test(trusted), trusted);
  check("a trusted requester gets generalization", trustedStrategy.startsWith("generalize"), trustedStrategy);
  if (shotDir) await guardPanel.screenshot({ path: `${shotDir}/03-guard-permit.png` });

  const untrusted = await askGuard({ verified: false, granularity: "row", cohort: 25 });
  const untrustedStrategy = await strategyFor("nihss_total");
  check("an unverified requester gets a harder strategy", untrustedStrategy === "redact", untrustedStrategy);
  check("lowering trust raises the filtering score", untrusted !== trusted, `${trusted} -> ${untrusted}`);

  // 3. Granularity: a table can only come back as an aggregate.
  await askGuard({ verified: true, granularity: "table", cohort: 25 });
  const tableStrategy = await strategyFor("nihss_total");
  check("a table request is reduced to an aggregate", tableStrategy.startsWith("aggregate_only"), tableStrategy);

  // 4. The federated extension: an update is never released raw.
  await askGuard({ verified: true, granularity: "model_update", cohort: 25 });
  const updateStrategy = await strategyFor("nihss_total");
  check(
    "a model update is bounded, never raw",
    /clip_contribution|add_noise/.test(updateStrategy),
    updateStrategy
  );

  // 5. The cohort floor denies outright, however trusted the requester.
  const denied = await askGuard({ verified: true, granularity: "table", cohort: 5 });
  check("a cohort below the floor is denied", denied.includes("DENY"), denied);
  if (shotDir) await guardPanel.screenshot({ path: `${shotDir}/02-guard-deny.png` });

  // 6. A full pilot runs and the release gates reject the candidate.
  await page.getByRole("button", { name: "Run governed pilot" }).click();
  await page.waitForSelector(".gates li", { timeout: 120000 });
  await page.waitForTimeout(600);
  const gates = await page.locator(".gates li").count();
  check("every release gate is reported", gates === 5, `${gates} gates`);
  const verdict = (await page.locator("section.card").nth(1).locator(".verdict").first().textContent()) ?? "";
  check("a candidate failing a safety gate is rejected", verdict.toLowerCase().includes("reject"), verdict.trim());
  const chains = await page.locator("section.card").nth(1).locator("table").last().textContent();
  check("every site's audit chain verifies", !(chains ?? "").includes("BROKEN"));
  if (shotDir) {
    await page.locator("section.card").nth(1).screenshot({ path: `${shotDir}/04-pilot.png` });
    await page.screenshot({ path: `${shotDir}/05-full.png`, fullPage: true });
  }

  // 7. Nothing failed quietly along the way.
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
