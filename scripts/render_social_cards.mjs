#!/usr/bin/env node
/**
 * Render the link preview cards and the icons of the three published sites.
 *
 * A link shared on LinkedIn, WhatsApp, Slack or X shows the image named by the
 * page's `og:image` tag. Each site has its own card, drawn as an HTML page in
 * `design/social/` with the design system's tokens. This script opens each
 * card in Chromium with the real fonts and saves a 1200 x 630 PNG into that
 * site's `public/` folder, which Vite copies into the build. It also renders
 * the logo mark into the icons each site links.
 *
 * The PNGs are committed, so publishing needs no browser and no fonts. The
 * record `design/social/rendered.json` keeps the SHA-256 of every file each
 * output was made from. `tests/test_social_previews.py` fails when a source
 * changes and this script was not run again.
 *
 * Outputs, for each of `site/`, `apps/healthcare-demo/frontend/` and
 * `services/nl2sql-demo/frontend/`:
 *
 *   public/social-preview.png   that site's card (framework, healthcare, nl2sql.html)
 *   public/favicon.svg          a copy of design/social/favicon.svg
 *   public/favicon-32.png       design/social/mark-32.svg at 32 px
 *   public/apple-touch-icon.png design/social/mark.svg at 180 px, square
 *   public/icon-512.png         design/social/mark.svg at 512 px
 *
 * The icons are the same in all three folders. The healthcare demo keeps its
 * own copies because `apps/healthcare-demo/` must stay extractable.
 *
 * Usage:
 *
 *   node scripts/render_social_cards.mjs           # render what changed
 *   node scripts/render_social_cards.mjs --force   # render everything again
 *
 * It needs `playwright-core`, `@fontsource/archivo` and
 * `@fontsource/ibm-plex-mono`, which are devDependencies of the framework site
 * (`cd site && npm install`), and a Chromium (`CHROMIUM_PATH`). The fonts come
 * from those packages, never from the network: the script aborts every request
 * that is not a file in this repository.
 *
 * Exits 0 on success, 1 on failure, and 2 (`SKIP`) when no browser or no
 * package is available.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url));
const SOCIAL = "design/social";
const RECORD = `${SOCIAL}/rendered.json`;
const PUBLISHED = "env/.env.published";
const SCRIPT = "scripts/render_social_cards.mjs";
const SITE_PACKAGES = "site/node_modules";

const CARD = { width: 1200, height: 630 };
/** WhatsApp drops a preview image above about 300 KB. */
const MAX_BYTES = 300 * 1024;
const AIM_BYTES = 200 * 1024;

/** Each published site: its public folder and its card. */
const SITES = [
  { public: "site/public", card: "framework.html" },
  { public: "apps/healthcare-demo/frontend/public", card: "healthcare.html" },
  { public: "services/nl2sql-demo/frontend/public", card: "nl2sql.html" }
];

/** The icons every site gets. `square` fills the corners: iOS rounds the icon itself. */
const ICONS = [
  { file: "favicon.svg", copy: "favicon.svg" },
  { file: "favicon-32.png", from: "mark-32.svg", size: 32 },
  { file: "apple-touch-icon.png", from: "mark.svg", size: 180, square: true },
  { file: "icon-512.png", from: "mark.svg", size: 512 }
];

/** The faces the cards use, from the @fontsource packages (latin subset). */
const FONTS = [
  { family: "Archivo", package: "@fontsource/archivo", file: "archivo", weights: [400, 500, 600, 700] },
  { family: "IBM Plex Mono", package: "@fontsource/ibm-plex-mono", file: "ibm-plex-mono", weights: [400, 500] }
];

const repoPath = (path) => join(REPO_ROOT, path);
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const fileDigest = (path) => sha256(readFileSync(repoPath(path)));

/** Find a Chromium the driver can launch, preferring CHROMIUM_PATH. */
function findBrowser() {
  return (
    [
      process.env.CHROMIUM_PATH,
      "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
      "/opt/pw-browsers/chromium/chrome-linux/chrome",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/usr/bin/google-chrome"
    ]
      .filter(Boolean)
      .find((path) => existsSync(path)) ?? null
  );
}

/** `KEY=value` lines of env/.env.published. */
function publishedAddresses() {
  const values = {};
  for (const line of readFileSync(repoPath(PUBLISHED), "utf8").split("\n")) {
    const match = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (match) values[match[1]] = match[2].trim();
  }
  return values;
}

/** The installed version of each font package, and an @font-face block for each face. */
function loadFonts() {
  const versions = {};
  const faces = [];
  for (const font of FONTS) {
    const dir = repoPath(join(SITE_PACKAGES, font.package));
    if (!existsSync(join(dir, "package.json"))) return null;
    versions[font.package] = JSON.parse(readFileSync(join(dir, "package.json"), "utf8")).version;
    for (const weight of font.weights) {
      const file = join(dir, "files", `${font.file}-latin-${weight}-normal.woff2`);
      if (!existsSync(file)) throw new Error(`${file} is missing from ${font.package}`);
      // A data URL, because Chromium refuses a font from another file:// path.
      const data = readFileSync(file).toString("base64");
      faces.push({
        family: font.family,
        weight,
        css: `@font-face { font-family: "${font.family}"; font-style: normal; font-weight: ${weight}; font-display: block; src: url(data:font/woff2;base64,${data}) format("woff2"); }`
      });
    }
  }
  return { versions, faces };
}

/** Width and height from a PNG's IHDR chunk. */
function pngSize(bytes) {
  if (bytes.subarray(1, 4).toString("latin1") !== "PNG") throw new Error("not a PNG");
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

function readRecord() {
  if (!existsSync(repoPath(RECORD))) return { outputs: {} };
  return JSON.parse(readFileSync(repoPath(RECORD), "utf8"));
}

/** True when the output is on disk and every input it was made from is unchanged. */
function isCurrent(entry, fontVersions, published) {
  if (!entry || !existsSync(repoPath(entry.output))) return false;
  if (fileDigest(entry.output) !== entry.sha256) return false;
  for (const [path, digest] of Object.entries(entry.sources)) {
    if (!existsSync(repoPath(path)) || fileDigest(path) !== digest) return false;
  }
  for (const [name, version] of Object.entries(entry.fonts ?? {})) {
    if (fontVersions[name] !== version) return false;
  }
  for (const [key, value] of Object.entries(entry.published ?? {})) {
    if (published[key] !== value) return false;
  }
  return true;
}

function write(path, bytes) {
  mkdirSync(dirname(repoPath(path)), { recursive: true });
  writeFileSync(repoPath(path), bytes);
}

const force = process.argv.includes("--force");
const unknown = process.argv.slice(2).filter((arg) => arg !== "--force");
if (unknown.length) {
  console.error(`unknown argument: ${unknown.join(" ")}\nusage: node ${SCRIPT} [--force]`);
  process.exit(1);
}

let chromium;
try {
  ({ chromium } = createRequire(repoPath("site/package.json"))("playwright-core"));
} catch {
  console.log("SKIP: playwright-core is not installed (cd site && npm install)");
  process.exit(2);
}
const fonts = loadFonts();
if (!fonts) {
  console.log("SKIP: the @fontsource packages are not installed (cd site && npm install)");
  process.exit(2);
}
const executablePath = findBrowser();
if (!executablePath) {
  console.log("SKIP: no Chromium found; set CHROMIUM_PATH");
  process.exit(2);
}

const published = publishedAddresses();
const previous = readRecord().outputs ?? {};
const outputs = {};
const failures = [];
let rendered = 0;

const browser = await chromium.launch({ executablePath });

/**
 * Open a page that may load files from this repository and nothing else.
 * Returns the page and the set of repository files it loaded.
 */
async function isolatedPage(viewport, options = {}) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, colorScheme: "light", ...options });
  const loaded = new Set();
  const problems = [];
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith("data:")) return route.continue();
    if (url.startsWith("file:")) {
      const path = relative(REPO_ROOT, fileURLToPath(url)).split(sep).join("/");
      if (!path.startsWith("..") && !path.includes("node_modules/")) {
        loaded.add(path);
        return route.continue();
      }
    }
    problems.push(`blocked a request for ${url}`);
    return route.abort();
  });
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
  page.on("requestfailed", (request) => {
    if (!request.url().startsWith("data:")) problems.push(`failed to load ${request.url()}`);
  });
  return { page, loaded, problems };
}

/** Render one card; return its PNG, the files it was made from and the addresses it shows. */
async function renderCard(card) {
  const { page, loaded, problems } = await isolatedPage(CARD);
  try {
    await page.goto(pathToFileURL(repoPath(`${SOCIAL}/${card}`)).href, { waitUntil: "load" });
    await page.addStyleTag({ content: fonts.faces.map((face) => face.css).join("\n") });

    // Fill in each published address from env/.env.published, without the scheme.
    const keys = await page.$$eval("[data-published-url]", (nodes) => nodes.map((node) => node.dataset.publishedUrl));
    const shown = {};
    for (const key of keys) {
      const value = published[key] ?? "";
      if (!/^https:\/\/[^\s?#@]+\/$/.test(value)) {
        throw new Error(`${card}: ${key} in ${PUBLISHED} must be an https address ending in /, not ${JSON.stringify(value)}`);
      }
      shown[key] = value;
    }
    await page.$$eval(
      "[data-published-url]",
      (nodes, values) => {
        for (const node of nodes) node.textContent = values[node.dataset.publishedUrl].replace(/^https:\/\//, "").replace(/\/$/, "");
      },
      shown
    );

    // Every face must load from the package, or the PNG would show a fallback font.
    const missing = await page.evaluate(async (faces) => {
      const absent = [];
      for (const face of faces) {
        const found = await document.fonts.load(`${face.weight} 20px "${face.family}"`);
        if (!found.some((item) => item.status === "loaded")) absent.push(`${face.family} ${face.weight}`);
      }
      await document.fonts.ready;
      return absent;
    }, fonts.faces.map(({ family, weight }) => ({ family, weight })));
    if (missing.length) throw new Error(`${card}: these fonts did not load: ${missing.join(", ")}`);

    // An SVG that is not valid XML loads without an error but draws nothing.
    const broken = await page.$$eval("img", (images) => images.filter((image) => !image.naturalWidth).map((image) => image.getAttribute("src")));
    if (broken.length) throw new Error(`${card}: these images did not draw: ${broken.join(", ")}`);

    // Nothing may spill outside the card: a longer line would be cut off.
    const outside = await page.evaluate(({ width, height }) => {
      const card = document.querySelector(".card");
      const box = card.getBoundingClientRect();
      const spilled = [];
      if (Math.round(box.width) !== width || Math.round(box.height) !== height) spilled.push(`.card is ${box.width} x ${box.height}`);
      for (const node of card.querySelectorAll("*")) {
        const rect = node.getBoundingClientRect();
        if (rect.left < box.left - 0.5 || rect.top < box.top - 0.5 || rect.right > box.right + 0.5 || rect.bottom > box.bottom + 0.5) {
          spilled.push(`${node.tagName.toLowerCase()}.${node.className} "${(node.textContent ?? "").trim().slice(0, 40)}"`);
        }
        if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflow !== "visible") {
          spilled.push(`${node.tagName.toLowerCase()}.${node.className} scrolls`);
        }
      }
      return spilled;
    }, CARD);
    if (outside.length) throw new Error(`${card}: content spills outside the card: ${outside.join("; ")}`);
    if (problems.length) throw new Error(`${card}: ${problems.join("; ")}`);

    const png = await page.locator(".card").screenshot({ type: "png", animations: "disabled" });
    return { png, loaded, shown };
  } finally {
    await page.close();
  }
}

/** Render one SVG at a square size. `square` paints the corners in the tile's colour. */
async function renderIcon(icon) {
  const { page, problems } = await isolatedPage({ width: icon.size, height: icon.size });
  try {
    const svg = readFileSync(repoPath(`${SOCIAL}/${icon.from}`), "utf8");
    await page.setContent(
      `<!doctype html><html><head><style>html,body{margin:0;background:transparent}` +
        `#icon,#icon svg{display:block;width:${icon.size}px;height:${icon.size}px}</style></head>` +
        `<body><div id="icon">${svg}</div></body></html>`
    );
    if (icon.square) {
      await page.evaluate(() => {
        document.body.style.background = document.querySelector("#icon svg rect").getAttribute("fill");
      });
    }
    if (problems.length) throw new Error(`${icon.from}: ${problems.join("; ")}`);
    return await page.locator("#icon").screenshot({ type: "png", omitBackground: !icon.square });
  } finally {
    await page.close();
  }
}

/** Record one output. `sources` are repository paths; the script itself is always one. */
function record(output, bytes, from, sources, extra = {}) {
  const digests = {};
  for (const path of [...new Set([...sources, SCRIPT])].sort()) digests[path] = fileDigest(path);
  outputs[output] = { output, from, bytes: bytes.length, sha256: sha256(bytes), ...extra, sources: digests };
}

try {
  console.log(`Rendering link previews and icons into ${SITES.length} sites\n`);

  for (const site of SITES) {
    const output = `${site.public}/social-preview.png`;
    if (!force && isCurrent(previous[output], fonts.versions, published)) {
      outputs[output] = previous[output];
      console.log(`  unchanged  ${output}`);
      continue;
    }
    const { png, loaded, shown } = await renderCard(site.card);
    const { width, height } = pngSize(png);
    if (width !== CARD.width || height !== CARD.height) failures.push(`${output} is ${width} x ${height}, not ${CARD.width} x ${CARD.height}`);
    if (png.length > MAX_BYTES) failures.push(`${output} is ${png.length} bytes; WhatsApp drops previews over about ${MAX_BYTES} bytes`);
    write(output, png);
    record(output, png, `${SOCIAL}/${site.card}`, [...loaded], { width, height, fonts: fonts.versions, published: shown });
    rendered += 1;
    const note = png.length > AIM_BYTES ? `  (over the ${AIM_BYTES / 1024} KB aim)` : "";
    console.log(`  rendered   ${output}  ${width} x ${height}, ${(png.length / 1024).toFixed(1)} KB${note}`);
  }

  for (const icon of ICONS) {
    const source = `${SOCIAL}/${icon.copy ?? icon.from}`;
    const targets = SITES.map((site) => `${site.public}/${icon.file}`);
    if (!force && targets.every((target) => isCurrent(previous[target], fonts.versions, published))) {
      for (const target of targets) outputs[target] = previous[target];
      console.log(`  unchanged  ${icon.file} (${targets.length} copies)`);
      continue;
    }
    const bytes = icon.copy ? readFileSync(repoPath(source)) : await renderIcon(icon);
    const extra = icon.copy ? { copy: true } : { width: icon.size, height: icon.size };
    if (!icon.copy) {
      const { width, height } = pngSize(bytes);
      if (width !== icon.size || height !== icon.size) failures.push(`${icon.file} is ${width} x ${height}, not ${icon.size} px`);
    }
    for (const target of targets) {
      write(target, bytes);
      record(target, bytes, source, [source], extra);
    }
    rendered += 1;
    console.log(`  ${icon.copy ? "copied  " : "rendered"}   ${icon.file} into ${targets.length} sites, ${(bytes.length / 1024).toFixed(1)} KB`);
  }

  const sorted = Object.fromEntries(Object.keys(outputs).sort().map((key) => [key, outputs[key]]));
  write(
    RECORD,
    JSON.stringify(
      {
        about:
          "Written by scripts/render_social_cards.mjs. The SHA-256 of each output and of every file it was made from. tests/test_social_previews.py fails when a source changes without a new render. Do not edit.",
        outputs: sorted
      },
      null,
      2
    ) + "\n"
  );
} catch (error) {
  failures.push(error.message);
} finally {
  await browser.close();
}

if (failures.length) {
  console.log(`\nFAIL\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
console.log(`\n${rendered ? `rendered ${rendered} item(s)` : "everything was current"}; wrote ${RECORD}`);
console.log("Look at every changed PNG before you commit it.");
process.exit(0);
