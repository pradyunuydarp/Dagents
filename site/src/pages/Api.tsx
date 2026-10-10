import { useEffect, useMemo, useState } from "react";

import { REPO_LINKS } from "../config";
import { ROLE_ORDER, inventory, type ServiceEntry } from "../inventory";
import { href } from "../router";

export function Api() {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();

  useEffect(() => {
    document.title = "API reference · Dagents";
    window.scrollTo(0, 0);
  }, []);

  const filtered = useMemo(() => {
    if (!needle) return inventory.services;
    return inventory.services
      .map((service) => ({
        ...service,
        endpoints: service.endpoints.filter((endpoint) =>
          `${endpoint.method} ${endpoint.path} ${endpoint.handler ?? ""}`.toLowerCase().includes(needle)
        )
      }))
      .filter((service) => service.endpoints.length > 0);
  }, [needle]);

  const matches = filtered.reduce((total, service) => total + service.endpoints.length, 0);

  return (
    <div className="block">
      <h1>API reference</h1>
      <p className="ds-measure">
        {inventory.endpoint_count} endpoints across {inventory.services.length} services. This list is
        generated from the services' code, so it matches what they serve. Each Python service also
        has interactive documentation at <span className="ds-mono">/docs</span>.
      </p>
      <p className="ds-measure">
        New to the APIs? Start with <a href={href("learn/04-apis")}>Using the APIs</a>, which walks
        through real requests. The same list is in{" "}
        <a href={REPO_LINKS.serviceInventory}>the repository</a>.
      </p>

      <div className="api-filter">
        <label className="ds-field">
          <span>Search by path, method or handler</span>
          <input
            className="ds-input"
            type="search"
            value={query}
            placeholder="governance, federation, POST, :plan"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <p className="ds-mono api-count">
          {needle ? `${matches} of ${inventory.endpoint_count}` : `${inventory.endpoint_count} endpoints`}
        </p>
      </div>

      {needle && filtered.length === 0 && (
        <p>
          No endpoint matches <span className="ds-mono">{query.trim()}</span>.
        </p>
      )}

      {ROLE_ORDER.map((group) => {
        const services = filtered.filter((service) => service.role === group.role);
        if (services.length === 0) return null;
        return (
          <section className="role" key={group.role}>
            <h2>{group.label}</h2>
            <p className="ds-note ds-measure">{group.blurb}</p>
            {services.map((service) => (
              <ServiceTable key={service.name} service={service} forceOpen={needle.length > 0} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

/** One service's endpoints. Closed by default; opened while searching. */
function ServiceTable({ service, forceOpen }: { service: ServiceEntry; forceOpen: boolean }) {
  const [open, setOpen] = useState(false);
  const expanded = open || forceOpen;
  return (
    <div className="ds-panel service">
      <div className="ds-panel-head">
        <h3 className="ds-mono">{service.name}</h3>
        <div className="ds-row service-meta">
          <span className="ds-chip">{service.language}</span>
          <span className="ds-chip">port {service.port}</span>
          <span className="ds-mono count">{service.endpoints.length} endpoints</span>
          <button className="ds-btn" onClick={() => setOpen(!expanded)} aria-expanded={expanded}>
            {expanded ? "Hide" : "Show"}
          </button>
        </div>
      </div>
      {expanded && (
        <div className="ds-panel-body">
          <div className="ds-table-scroll">
            <table className="ds-table">
              <thead>
                <tr>
                  <th>Method</th>
                  <th>Path</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                {service.endpoints.map((endpoint) => (
                  <tr key={`${endpoint.method} ${endpoint.path}`}>
                    <td className="ds-mono method">{endpoint.method}</td>
                    <td className="ds-mono">{endpoint.path}</td>
                    <td>
                      {endpoint.alias_of && (
                        <span className="ds-chip">same as {endpoint.alias_of.path}</span>
                      )}{" "}
                      {endpoint.summary}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
