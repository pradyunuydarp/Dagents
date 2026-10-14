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
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const distDir = resolve(process.argv[2] ?? "dist");
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

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
  ".json": "application/json",
  ".webp": "image/webp",
  ".pdf": "application/pdf",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
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
  // API pages load their data after the first paint; wait for it, so anchors exist.
  if (hash.startsWith("#/api")) await page.waitForSelector("[data-api-ready]", { timeout: 30000 });
  return (await page.textContent("main h1"))?.trim() ?? "";
}

/** The first bytes each kind of file on the Resources page must start with. */
const SIGNATURES = {
  ".pptx": (bytes) => bytes.subarray(0, 4).toString("latin1") === "PK\x03\x04",
  ".pdf": (bytes) => bytes.subarray(0, 5).toString("latin1") === "%PDF-",
  ".webp": (bytes) =>
    bytes.subarray(0, 4).toString("latin1") === "RIFF" && bytes.subarray(8, 12).toString("latin1") === "WEBP"
};

/** Fetch each address from the built site; return the ones that are missing or the wrong kind of file. */
async function wrongFiles(urls) {
  const wrong = [];
  for (const url of urls) {
    const response = await page.request.get(url);
    const body = await response.body();
    const { pathname } = new URL(url);
    if (response.status() !== 200 || !SIGNATURES[extname(pathname)]?.(body)) {
      wrong.push(`${pathname} (${response.status()}, starts ${JSON.stringify(body.subarray(0, 5).toString("latin1"))})`);
    }
  }
  return wrong;
}

/** The id the site gives a heading (site/src/markdown.ts). */
function headingId(text) {
  return text
    .replace(/[`*_]/g, "")
    .toLowerCase()
    .trim()
    .replace(/<[^>]+>/g, "")
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

/**
 * Where docs/reference/api/<module>.md places each endpoint: "service METHOD path"
 * (or "dagentsc <words>") to { module, interface }. The tests read the same
 * blocks; this check proves the page shows each endpoint in that place.
 */
function apiPlacements() {
  const placements = new Map();
  const dir = join(repoRoot, "docs/reference/api");
  for (const file of readdirSync(dir).filter((name) => name.endsWith(".md") && !["README.md", "overview.md"].includes(name))) {
    const module = file.replace(/\.md$/, "");
    const lines = readFileSync(join(dir, file), "utf8").split("\n");
    let heading = null;
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const match = line.match(/^##\s+(.+?)\s*$/);
      if (match) heading = match[1];
      if (line.trim() !== "```api-interface") continue;
      const block = [];
      for (index += 1; index < lines.length && !lines[index].startsWith("```"); index += 1) block.push(lines[index].trim());
      const service = block.find((entry) => entry.startsWith("service:"))?.slice(8).trim();
      const start = block.indexOf("endpoints:");
      for (const entry of block.slice(start + 1)) {
        if (!entry.startsWith("- ")) break;
        const endpoint = entry.slice(2).trim();
        const key = endpoint.startsWith("dagentsc ") ? endpoint : `${service} ${endpoint}`;
        placements.set(key, { module, interface: headingId(heading ?? "") });
      }
    }
  }
  return placements;
}

async function checkApi() {
  const inventory = JSON.parse(readFileSync(join(repoRoot, "docs/reference/service-inventory.json"), "utf8"));
  const reference = JSON.parse(readFileSync(join(repoRoot, "docs/reference/api-reference.json"), "utf8"));
  const expected = new Set([
    ...inventory.services.flatMap((service) =>
      service.endpoints.map((endpoint) => `${service.name} ${endpoint.method} ${endpoint.path}`)
    ),
    ...reference.planner.commands.map((command) => `dagentsc ${command.words.join(" ")}`)
  ]);
  const placements = apiPlacements();

  const overview = await open("#/api");
  check("the API overview renders", overview === "API reference", overview);
  const modules = await page.$$eval(".api-nav .api-tree > li > a", (links) => links.map((a) => a.getAttribute("href")));
  check("the API navigation lists the overview and five modules", modules.length === 6, modules.join(" "));

  const shown = new Map();
  const links = new Set();
  for (const hash of modules) {
    const title = await open(hash);
    check(`${hash} renders`, title.length > 0 && !/not found/i.test(title), title);
    const broken = await diagrams();
    check(`${hash} diagrams draw`, broken.length === 0, broken.join(" | "));
    const failedLoad = await page.$(".api-main .ds-banner--deny");
    check(`${hash} loads its data`, failedLoad === null);
    const text = await page.evaluate(() => document.querySelector("main").innerText);
    const leaked = ["```"].filter((marker) => text.includes(marker));
    check(`${hash} has no raw Markdown`, leaked.length === 0, leaked.join(" "));
    const ids = await page.$$eval("main [id]", (nodes) => nodes.map((node) => node.id));
    const repeated = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
    check(`${hash} has no repeated ids`, repeated.length === 0, repeated.slice(0, 5).join(", "));
    const cards = await page.$$eval("[data-endpoint]", (nodes) =>
      nodes.map((node) => ({
        keys: [node.getAttribute("data-endpoint"), node.getAttribute("data-alias")].filter(Boolean),
        interface: node.closest("[data-interface]")?.getAttribute("data-interface") ?? null
      }))
    );
    const module = hash.split("/")[2] ?? "";
    for (const card of cards) {
      for (const key of card.keys) shown.set(key, [...(shown.get(key) ?? []), { module, interface: card.interface }]);
    }
    for (const href of await page.$$eval("main a, .api-nav a", (anchors) => anchors.map((a) => a.getAttribute("href")))) {
      if (href?.startsWith("#/")) links.add(href);
    }
    // A success response whose route declares only "a dict" must show the fields
    // of its recorded answer, not a bare "A JSON object." with no table.
    const bare = await page.$$eval(".api-response", (nodes) =>
      nodes
        .filter((node) => /^2/.test(node.querySelector(".api-status")?.textContent ?? ""))
        .filter((node) => /A JSON (object|array of object|map of \w+)\./.test(node.innerText) && !node.querySelector("table"))
        .map((node) => node.closest("[data-endpoint]")?.getAttribute("data-endpoint"))
    );
    check(`${hash} shows a field table for every success body`, bare.length === 0, bare.slice(0, 5).join(", "));
  }

  const missing = [...expected].filter((key) => !shown.has(key));
  const twice = [...shown].filter(([, places]) => places.length > 1).map(([key]) => key);
  const misplaced = [...shown]
    .filter(([key, places]) => {
      const want = placements.get(key);
      return !want || places[0].module !== want.module || places[0].interface !== want.interface;
    })
    .map(([key]) => key);
  check(
    "every endpoint and planner command appears once, where the hierarchy places it",
    missing.length === 0 && twice.length === 0 && misplaced.length === 0,
    [
      missing.length && `missing ${missing.slice(0, 5).join(", ")}`,
      twice.length && `twice ${twice.slice(0, 5).join(", ")}`,
      misplaced.length && `misplaced ${misplaced.slice(0, 5).join(", ")}`
    ]
      .filter(Boolean)
      .join("; ") || `${shown.size} of ${expected.size}`
  );

  // Every link on the API pages lands on a page and an element it names. Each
  // page is opened once and all of its anchors are looked up there.
  const byPage = new Map();
  for (const link of links) {
    const [, path, anchor] = link.split("#");
    byPage.set(path, [...(byPage.get(path) ?? []), anchor].filter(Boolean));
  }
  const unresolved = [];
  for (const [path, anchors] of byPage) {
    const title = await open(`#${path}`);
    if (/not found/i.test(title)) unresolved.push(`#${path}`);
    const absent = await page.evaluate((ids) => ids.filter((id) => document.getElementById(id) === null), [...new Set(anchors)]);
    unresolved.push(...absent.map((id) => `#${path}#${id}`));
  }
  check("every link on the API pages resolves", unresolved.length === 0, unresolved.slice(0, 8).join(", ") || `${links.size} links`);

  // The code tabs switch between curl, Python and JavaScript.
  await open("#/api/framework-services#core-service-post-api-v1-workloads-compile");
  const tabs = page.locator("#core-service-post-api-v1-workloads-compile .api-code").first();
  const before = await tabs.locator("pre").textContent();
  await tabs.locator('button[role="tab"]', { hasText: "Python" }).click();
  const python = await tabs.locator("pre").textContent();
  await tabs.locator('button[role="tab"]', { hasText: "JavaScript" }).click();
  const javascript = await tabs.locator("pre").textContent();
  const selected = await tabs.locator('button[aria-selected="true"]').textContent();
  check(
    "the code tabs switch",
    before?.startsWith("curl ") && python?.includes("import httpx") && javascript?.includes("await fetch(") && selected === "JavaScript",
    `${before?.slice(0, 12)} | ${python?.slice(0, 12)} | ${javascript?.slice(0, 12)}`
  );

  // The code samples read each answer the way it comes back: text for an SVG,
  // nothing for a 304, and the return code and stderr for a refused command.
  const sampleCode = async (hash, exampleId, tab) => {
    await open(hash);
    const tabs = page.locator(`[id$="--${exampleId}"] .api-code`).first();
    await tabs.locator('button[role="tab"]', { hasText: tab }).click();
    return (await tabs.locator("pre").textContent()) ?? "";
  };
  const svgPython = await sampleCode("#/api/demo-apps#healthcare-demo-backend-get-api-v1-charts-chart-id-svg", "chart-svg", "Python");
  const svgScript = await sampleCode("#/api/demo-apps#healthcare-demo-backend-get-api-v1-charts-chart-id-svg", "chart-svg", "JavaScript");
  const notModified = await sampleCode("#/api/demo-apps#healthcare-demo-backend-get-api-v1-charts-chart-id-svg", "chart-svg-not-modified", "Python");
  const refused = await sampleCode("#/api/dagentsc#dagentsc-governance-restrict", "restrict-missing-field", "Python");
  const yaml = await sampleCode("#/api/dagentsc#dagentsc-manifest-compile", "manifest-from-flags", "Python");
  const stderr = await page
    .locator('[id$="--restrict-missing-field"]')
    .first()
    .innerText()
    .catch(() => "");
  check(
    "the code samples read text, empty and refused answers without failing",
    svgPython.includes("response.text") &&
      !svgPython.includes("response.json()") &&
      svgScript.includes("response.text()") &&
      !notModified.includes("response.json()") &&
      refused.includes("result.returncode") &&
      !refused.includes("check=True") &&
      !yaml.includes("json.loads") &&
      stderr.includes("Missing JSON field: boundary"),
    [svgPython, svgScript, notModified, refused, yaml].map((code) => code.split("\n").pop()).join(" | ")
  );

  // The search finds endpoints, classes and objects, and its results open them.
  await open("#/api");
  await page.fill(".api-search input", "ManifestService");
  const kinds = await page.$$eval(".api-results .ds-chip", (chips) => chips.map((chip) => chip.textContent));
  await page.fill(".api-search input", "WorkloadComponent");
  const objects = await page.$$eval(".api-results .ds-chip", (chips) => chips.map((chip) => chip.textContent));
  await page.fill(".api-search input", "workloads:compile");
  await page.locator(".api-results a").first().click();
  await page.waitForSelector("#core-service-post-api-v1-workloads-compile");
  const landed = await page.evaluate(() => window.location.hash);
  check(
    "the search finds classes, objects and endpoints",
    kinds.includes("Class") && objects.includes("Object") && landed.endsWith("#core-service-post-api-v1-workloads-compile"),
    `${kinds.join(",")} | ${objects.join(",")} | ${landed}`
  );

  // At phone width the pages do not scroll sideways, and the menu holds the tree.
  await page.setViewportSize({ width: 375, height: 800 });
  const wide = [];
  for (const hash of modules) {
    await open(hash);
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    if (width > 375) wide.push(`${hash} ${width}px`);
  }
  await open("#/api/framework-services");
  await page.click(".api-menu summary");
  const menu = await page.$$eval(".api-menu .api-tree a", (anchors) => anchors.length);
  check("the API pages fit a 375 px screen and the menu holds the tree", wide.length === 0 && menu > 6, wide.join(", ") || `${menu} menu links`);
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function checkResources() {
  const list = JSON.parse(readFileSync(join(repoRoot, "docs/presentation/resources.json"), "utf8"));
  const items = list.groups.flatMap((group) => group.items);

  const title = await open("#/resources");
  check("the Resources page renders", title === "Resources", title);
  const current = await page.textContent('.masthead nav a[aria-current="page"]').catch(() => null);
  check("the nav marks Resources as the current page", current?.trim() === "Resources", `${current}`);

  const listed = await page.$$eval("main .resource", (nodes) => nodes.map((node) => node.id));
  const unlisted = items.map((item) => item.id).filter((id) => !listed.includes(id));
  check(
    "the Resources page lists every item in resources.json",
    unlisted.length === 0 && listed.length === items.length,
    unlisted.join(", ") || `${listed.length} items`
  );

  // A deck offers a .pptx and a PDF; a report offers a PDF; Markdown opens on GitHub.
  const links = await page.$$eval("main .resource a.ds-btn", (anchors) => anchors.map((a) => a.href));
  const downloads = links.filter((url) => url.startsWith(base));
  const expected = items.reduce((total, item) => total + ({ deck: 2, pdf: 1 }[item.kind] ?? 0), 0);
  const wrong = await wrongFiles(downloads);
  check(
    "every download answers 200 with the right file signature",
    wrong.length === 0 && downloads.length === expected,
    wrong.join(" | ") || `${downloads.length} of ${expected} files (.pptx starts PK, .pdf starts %PDF)`
  );

  const github = links.filter((url) => !url.startsWith(base));
  const absent = github.filter((url) => {
    const path = url.match(/^https:\/\/github\.com\/[^/]+\/[^/]+\/blob\/main\/(.+)$/)?.[1];
    return !path || !existsSync(join(repoRoot, decodeURIComponent(path)));
  });
  check(
    "every GitHub link names a file in the repository",
    absent.length === 0 && github.length === items.filter((item) => item.kind === "markdown").length,
    absent.join(" | ") || `${github.length} links`
  );

  const previews = await page.evaluate(async () => {
    const found = [];
    for (const image of Array.from(document.querySelectorAll("main .resource img"))) {
      // Previews load lazily, so bring each one into view first.
      image.scrollIntoView();
      await new Promise((done) => {
        if (image.complete) return done();
        image.addEventListener("load", done, { once: true });
        image.addEventListener("error", done, { once: true });
        setTimeout(done, 10000);
      });
      found.push({ src: image.currentSrc || image.src, loaded: image.naturalWidth > 0 });
    }
    return found;
  });
  const decks = items.filter((item) => item.kind === "deck").length;
  const unloaded = previews.filter((image) => !image.loaded).map((image) => image.src);
  const wrongPreviews = await wrongFiles(previews.map((image) => image.src));
  check(
    "every preview image loads",
    unloaded.length === 0 && wrongPreviews.length === 0 && previews.length === decks,
    [...unloaded, ...wrongPreviews].join(" | ") || `${previews.length} of ${decks} previews`
  );

  const groupsMissing = [];
  for (const group of list.groups) {
    await open(`#/resources#${group.id}`);
    if (!(await page.evaluate((id) => document.getElementById(id) !== null, group.id))) groupsMissing.push(group.id);
  }
  check("every group has its anchor", groupsMissing.length === 0, groupsMissing.join(", ") || `${list.groups.length} groups`);

  // At phone width the page must not scroll sideways.
  await page.setViewportSize({ width: 375, height: 800 });
  await open("#/resources");
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  check("the Resources page fits a 375 px screen", width <= 375, `${width} px wide`);
  await page.setViewportSize({ width: 1280, height: 900 });
}

try {
  console.log(`Dagents framework site — browser check against ${distDir}\n`);

  // 1. The home page renders, with its diagram.
  const home = await open("#/");
  check("the home page renders", home === "Dagents", home);
  check("the home page diagram draws", (await diagrams()).length === 0);

  // 2. Every page of the learning guide (#/learn) and of the guide to building
  //    an application (#/guide) renders, and every diagram on it draws. Each
  //    page list comes from that guide's own navigation, so a new page is
  //    checked too.
  const guide = [];
  for (const [route, name, minimum] of [
    ["learn", "Learn", 7],
    ["guide", "Guide", 11]
  ]) {
    await open(`#/${route}`);
    const pages = await page.$$eval(".learn-nav a", (links) => links.map((a) => a.getAttribute("href")));
    check(`#/${route} lists its pages`, pages.length >= minimum, `${pages.length} pages`);
    const current = await page.textContent('.masthead nav a[aria-current="page"]').catch(() => null);
    check(`the nav marks ${name} as the current page`, current?.trim() === name, `${current}`);
    guide.push(...pages);
  }

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

  // 3b. At phone width no page of either guide scrolls sideways: wide code,
  //     tables and diagrams scroll inside their own boxes, and long paths wrap.
  await page.setViewportSize({ width: 375, height: 800 });
  const wideGuide = [];
  for (const hash of guide) {
    await open(hash);
    await diagrams();
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    if (width > 375) wideGuide.push(`${hash} ${width}px`);
  }
  check(`all ${guide.length} guide pages fit a 375 px screen`, wideGuide.length === 0, wideGuide.join(", "));
  await page.setViewportSize({ width: 1280, height: 900 });

  // 4. The API reference: every page, every endpoint where the hierarchy puts
  //    it, its anchors, its diagrams, the code tabs and the search.
  await checkApi();

  // 5. Dark mode redraws the diagrams instead of breaking them.
  await open("#/learn/03-architecture");
  await page.click('.theme-toggle button:has-text("Dark")');
  await page.waitForTimeout(1000);
  check("diagrams draw in dark mode", (await diagrams()).length === 0);

  // 6. A stored theme applies before the app's script runs, so a dark choice
  //    does not paint light first. Blocking every script file leaves only the
  //    inline script in index.html.
  const firstPaint = await browser.newPage();
  await firstPaint.addInitScript(() => {
    try {
      localStorage.setItem("dagents-theme", "dark");
    } catch {}
  });
  await firstPaint.route("**/*", (route) =>
    route.request().resourceType() === "script" ? route.abort() : route.continue()
  );
  await firstPaint.goto(base, { waitUntil: "domcontentloaded" });
  const stored = await firstPaint.evaluate(() => document.documentElement.getAttribute("data-theme"));
  check("a stored dark theme applies before the app's script runs", stored === "dark", `data-theme ${stored}`);
  await firstPaint.close();

  // 6b. The Resources page lists every item in docs/presentation/resources.json,
  //     and every file it offers is in the built site. The static server above
  //     answers a missing file with index.html and status 200, so each download
  //     is checked by its first bytes, not only its status.
  await checkResources();

  // 7. Nothing failed quietly along the way.
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
