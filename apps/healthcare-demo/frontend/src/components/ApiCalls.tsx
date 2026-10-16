import { Fragment, createContext, useContext, useState } from "react";

import { AGENTS_API_URL } from "../config";
import { agentName, count } from "../format";
import type { FrameworkCall } from "../types";

/**
 * The hospitals' site ids in display order, from `/api/v1/overview`.
 *
 * The GMA calls the LMAs at the same time, so the order of their calls changes
 * from run to run. The strips list the LMAs in this order instead.
 */
export const SiteOrder = createContext<string[]>([]);

/** Calls to one route, merged: the same agent, caller, method and route. */
interface CallRow {
  key: string;
  route: string;
  service: string;
  siteId: string | null;
  method: string;
  path: string;
  via: "app" | "gma";
  calls: number;
  statuses: (number | null)[];
}

/** Where a site goes in a list: the GMA first, then the hospitals in display order, then any other site. */
function siteRank(siteId: string | null, order: string[]): number {
  if (siteId === null) return -1;
  const index = order.indexOf(siteId);
  return index === -1 ? order.length : index;
}

/**
 * Merge the calls by agent and route. Routes keep the order they were first
 * called in; the agents of one route follow `SiteOrder`.
 */
function rows(calls: FrameworkCall[], order: string[]): CallRow[] {
  const merged = new Map<string, CallRow>();
  const firstCall = new Map<string, number>();
  for (const call of calls) {
    const route = [call.via, call.method, call.path].join(" ");
    if (!firstCall.has(route)) firstCall.set(route, firstCall.size);
    const key = [route, call.service, call.site_id ?? ""].join(" ");
    const row = merged.get(key);
    if (row) {
      row.calls += 1;
      if (!row.statuses.includes(call.status)) row.statuses.push(call.status);
    } else {
      merged.set(key, {
        key,
        route,
        service: call.service,
        siteId: call.site_id,
        method: call.method,
        path: call.path,
        via: call.via,
        calls: 1,
        statuses: [call.status]
      });
    }
  }
  return [...merged.values()].sort(
    (a, b) =>
      (firstCall.get(a.route) ?? 0) - (firstCall.get(b.route) ?? 0) ||
      siteRank(a.siteId, order) - siteRank(b.siteId, order) ||
      (a.siteId ?? "").localeCompare(b.siteId ?? "")
  );
}

/** How many calls went to each agent: "GMA: 35 calls · LMA uk-134: 16 calls". */
function perAgent(calls: FrameworkCall[], order: string[]): string {
  const totals = new Map<string, { siteId: string | null; name: string; total: number }>();
  for (const call of calls) {
    const name = agentName(call.service, call.site_id);
    const entry = totals.get(name) ?? { siteId: call.service === "gma" ? null : call.site_id, name, total: 0 };
    entry.total += 1;
    totals.set(name, entry);
  }
  return [...totals.values()]
    .sort((a, b) => siteRank(a.siteId, order) - siteRank(b.siteId, order) || a.name.localeCompare(b.name))
    .map(({ name, total }) => `${name}: ${count(total)} ${total === 1 ? "call" : "calls"}`)
    .join(" · ");
}

function statusText(statuses: (number | null)[]): string {
  return statuses.map((status) => (status === null ? "no answer" : String(status))).join(", ");
}

/** A route with a line-break chance after each `/`, so a narrow cell breaks between its parts. */
function Route({ path }: { path: string }) {
  const parts = path.split("/");
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {part}
          {index < parts.length - 1 && (
            <>
              /<wbr />
            </>
          )}
        </Fragment>
      ))}
    </>
  );
}

/**
 * The "Dagents API calls" strip under a panel.
 *
 * It lists the calls to the Dagents LMAs and GMA behind the answer the panel
 * shows, as the backend reports them in `framework_calls`: the agent, the
 * method and the route, with a count. The backend never puts a host, a token or
 * a body in that list, and this shows nothing else.
 *
 * `calls` undefined (an answer recorded before the agents existed) shows
 * nothing. An empty list says the app answered without calling an agent.
 */
export function ApiCalls({ calls, label = "Dagents API calls" }: { calls: FrameworkCall[] | undefined; label?: string }) {
  const order = useContext(SiteOrder);
  const list = calls ?? [];
  // Short lists start open, so the routes show without a click; a pilot's long
  // list starts closed behind its per-agent counts.
  const [open] = useState(() => rows(list, order).length <= 6);
  if (calls === undefined) return null;

  if (list.length === 0) {
    return (
      <p className="api-calls api-calls-none" data-api-calls data-api-calls-count="0">
        <span className="api-calls-label">{label}</span>: none. The app answered without calling a Dagents agent.
      </p>
    );
  }

  const merged = rows(list, order);
  const byGma = merged.some((row) => row.via === "gma");
  return (
    <details className="api-calls" data-api-calls data-api-calls-count={list.length} open={open}>
      <summary>
        <span className="api-calls-label">{label}</span>
        <span className="api-calls-agents">{perAgent(list, order)}</span>
      </summary>
      <div className="ds-table-scroll">
        <table className="ds-table api-calls-table">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              {byGma && <th scope="col">Called by</th>}
              <th scope="col">Method</th>
              <th scope="col">Route</th>
              <th scope="col" className="wide-only">
                Status
              </th>
              <th scope="col" className="num">
                Calls
              </th>
            </tr>
          </thead>
          <tbody>
            {merged.map((row) => (
              <tr
                key={row.key}
                data-call-service={row.service}
                data-call-site={row.siteId ?? ""}
                data-call-via={row.via}
                data-call-path={row.path}
              >
                <td className="mono api-agent" data-call-agent>
                  {agentName(row.service, row.siteId)}
                </td>
                {byGma && <td>{row.via === "gma" ? "the GMA" : "the app"}</td>}
                <td className="mono" data-call-method>
                  {row.method}
                </td>
                <td className="mono api-route" data-call-route>
                  <Route path={row.path} />
                </td>
                <td className="mono wide-only">{statusText(row.statuses)}</td>
                <td className="num">{count(row.calls)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="api-calls-note">
        {byGma && "Rows called by the GMA are the jobs the GMA sent to each hospital's LMA during the rounds: one call per hospital and round, in which the LMA checks the job and runs it. "}
        <a href={AGENTS_API_URL} rel="noreferrer">
          What each route does
        </a>
      </p>
    </details>
  );
}

/** Whether a call went to this hospital's LMA. */
function toSite(call: FrameworkCall, siteId: string): boolean {
  return call.service === "lma" && call.site_id === siteId;
}

/**
 * The strips on a hospital's page.
 *
 * The first strip lists the calls to this hospital's LMA. Any other call gets
 * a strip of its own, so a strip under the LMA never lists another agent. The
 * app registers its model versions with the GMA before the first answer that
 * needs one, so the first hospital page after the demo server starts can
 * include those calls to the GMA.
 */
export function SiteCalls({
  calls,
  siteId,
  label = "Dagents API calls"
}: {
  calls: FrameworkCall[] | undefined;
  siteId: string;
  label?: string;
}) {
  if (calls === undefined) return null;
  const own = calls.filter((call) => toSite(call, siteId));
  const other = calls.filter((call) => !toSite(call, siteId));
  return (
    <>
      {(own.length > 0 || other.length === 0) && <ApiCalls calls={own} label={label} />}
      {other.length > 0 && <ApiCalls calls={other} label="Dagents API calls to other agents" />}
    </>
  );
}
