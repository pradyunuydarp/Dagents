/**
 * Serve a built copy of the demo the way GitHub Pages serves it.
 *
 * The Pages workflow publishes the demo under a sub-path such as
 * `/Dagents/healthcare-demo/`. Pages serves only the files in the published
 * tree: a missing file answers 404, never the index page, and nothing answers
 * an API request. `vite preview` answers a missing file with the index page,
 * so a build that lost a file would still look fine there. This server
 * follows the Pages rules instead:
 *
 * - a file under the base path is served with its content type;
 * - a folder serves its `index.html`, and a folder path without its last `/`
 *   is redirected to the path with it, as Pages does;
 * - anything else is a 404;
 * - every request whose path contains `/api/` is counted and its connection
 *   is closed without an answer. A replay build must make no API request, so
 *   `replay-smoke.mjs` fails when the count is not zero.
 *
 * Used by `replay-smoke.mjs`, or on its own to look at a build:
 *
 *   node serve-dist.mjs <dist> [--base /Dagents/healthcare-demo/] [--port 11500]
 *
 * The port defaults to 0, which lets the system choose a free one. The script
 * prints the address and, when it stops, each API request it refused.
 */
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".webmanifest": "application/manifest+json",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

/** A base path with one leading and one trailing slash: "/Dagents/healthcare-demo/". */
export function normaliseBase(base) {
  const trimmed = String(base ?? "/").replace(/^\/+|\/+$/g, "");
  return trimmed === "" ? "/" : `/${trimmed}/`;
}

/** True when an absolute path has an API segment, under the base path or not. */
export function isApiPath(pathname) {
  return /(^|\/)api\//.test(pathname);
}

/**
 * Start the server.
 *
 * Returns `{ url, origin, base, apiRequests, close }`. `url` is the address of
 * the demo's index page. `apiRequests` lists each refused API request as
 * "METHOD /path?query", in order.
 */
export async function serveDist({ dist, base = "/", host = "127.0.0.1", port = 0 }) {
  const root = resolve(dist);
  const prefix = normaliseBase(base);
  const apiRequests = [];

  const send = (res, status, body, headers = {}) => {
    res.writeHead(status, { "Cache-Control": "no-store", ...headers });
    res.end(body);
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    let pathname;
    try {
      pathname = decodeURIComponent(url.pathname);
    } catch {
      return send(res, 400, "bad request", { "Content-Type": "text/plain; charset=utf-8" });
    }

    if (isApiPath(pathname)) {
      apiRequests.push(`${req.method} ${url.pathname}${url.search}`);
      // No answer at all: Pages has no API, and an error page could look like
      // an answer to code that only checks the status.
      req.socket.destroy();
      return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      return send(res, 405, "method not allowed", { "Content-Type": "text/plain; charset=utf-8", Allow: "GET, HEAD" });
    }
    if (`${pathname}/` === prefix) {
      return send(res, 301, "", { Location: prefix });
    }
    if (!pathname.startsWith(prefix)) {
      return send(res, 404, "404: not in the published tree", { "Content-Type": "text/plain; charset=utf-8" });
    }

    let file = resolve(root, `.${sep}${pathname.slice(prefix.length)}`);
    if (file !== root && !file.startsWith(root + sep)) {
      return send(res, 404, "404: not in the published tree", { "Content-Type": "text/plain; charset=utf-8" });
    }
    try {
      let info = await stat(file);
      if (info.isDirectory()) {
        if (!pathname.endsWith("/")) return send(res, 301, "", { Location: `${url.pathname}/${url.search}` });
        file = join(file, "index.html");
        info = await stat(file);
      }
      if (!info.isFile()) throw new Error("not a file");
      const body = await readFile(file);
      const type = TYPES[extname(file).toLowerCase()] ?? "application/octet-stream";
      return send(res, 200, req.method === "HEAD" ? undefined : body, {
        "Content-Type": type,
        "Content-Length": String(body.length),
      });
    } catch {
      return send(res, 404, "404: not in the published tree", { "Content-Type": "text/plain; charset=utf-8" });
    }
  });

  await new Promise((done, fail) => {
    server.once("error", fail);
    server.listen(port, host, () => {
      server.off("error", fail);
      done();
    });
  });
  const origin = `http://${host}:${server.address().port}`;
  return {
    url: `${origin}${prefix}`,
    origin,
    base: prefix,
    apiRequests,
    close: () =>
      new Promise((done) => {
        server.closeAllConnections?.();
        server.close(() => done());
      }),
  };
}

/** Command line: serve until Ctrl-C, then list the refused API requests. */
async function main(argv) {
  const value = (name, fallback) => {
    const index = argv.indexOf(name);
    return index === -1 ? fallback : argv[index + 1];
  };
  const valueIndexes = new Set(
    ["--base", "--port"]
      .map((name) => argv.indexOf(name))
      .filter((i) => i !== -1)
      .map((i) => i + 1),
  );
  const dist = argv.find((arg, i) => !arg.startsWith("--") && !valueIndexes.has(i));
  if (!dist) {
    console.log("usage: node serve-dist.mjs <dist> [--base /Dagents/healthcare-demo/] [--port 0]");
    return 1;
  }
  const running = await serveDist({
    dist,
    base: value("--base", "/Dagents/healthcare-demo/"),
    port: Number(value("--port", "0")),
  });
  console.log(`Serving ${resolve(dist)} at ${running.url}`);
  console.log("A missing file answers 404, and every /api/ request is refused. Ctrl-C stops the server.");
  await new Promise((done) => process.once("SIGINT", done));
  await running.close();
  console.log(`${running.apiRequests.length} API requests refused`);
  for (const request of running.apiRequests) console.log(`  ${request}`);
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main(process.argv.slice(2));
}
