import { useState } from "react";

import { Diagram } from "./Diagram";
import { ROLE_ORDER, inventory, servicesWithRole, type ServiceEntry } from "./inventory";

/**
 * The Dagents framework site.
 *
 * It documents the framework and points at the apps built on it. Two rules keep
 * it from becoming the usual decorative project page:
 *
 * - The API reference is generated. It comes from
 *   `docs/reference/service-inventory.json`, which is extracted from the live
 *   routing tables and drift-checked in CI, so this page cannot advertise an
 *   endpoint that does not exist.
 * - The limits are stated where a reader will see them, not buried. A framework
 *   page that only lists capabilities is asking to be trusted.
 */

const REPO = "https://github.com/pradyunuydarp/Dagents";

/** Path to a demo, resolved against whatever base the site is served from. */
function demoHref(slug: string): string {
  return `${import.meta.env.BASE_URL}${slug}/`;
}

const LAYERS = [
  {
    layer: "OCaml",
    path: "bindings/ocaml/",
    owns: "Pure planning: validate, compile, route, render",
    never: "DB sockets, training loops, API servers"
  },
  {
    layer: "Python",
    path: "agents/, services/",
    owns: "FastAPI surfaces, ML training and inference, source I/O, runtime state",
    never: "Deterministic planning rules"
  },
  {
    layer: "Spring Boot",
    path: "services/spring-services/",
    owns: "Orchestration APIs, policy entrypoints, external integration",
    never: "ML execution"
  }
];

const PLANNERS = [
  { module: "common_ir", decides: "The shared typed IR and its JSON codecs. Every new type starts here." },
  {
    module: "dataset_compiler",
    decides: "Source validation, profiling, schema contracts, quality rules, transforms"
  },
  { module: "pipeline_compiler", decides: "DAG validation and topological ordering" },
  { module: "model_router", decides: "Dataset profile plus task → model family and packaging mode" },
  { module: "manifest_compiler", decides: "Typed workload spec → Kubernetes YAML" },
  {
    module: "governance_compiler",
    decides: "GRAILS Ethical-Restriction Rails: what protection a given request needs"
  },
  {
    module: "federation_compiler",
    decides: "Federated round manifests, eligibility, quorum, aggregation readiness, release gates"
  }
];

const DEMOS = [
  {
    slug: "healthcare-demo",
    name: "Stroke triage across three hospitals",
    owns: "Its clinical feature contract, scoring rule, FHIR mapping and intended-use statement",
    framework: "Governance, federation, and everything generic",
    proves:
      "That the governance and federation layers really decide things, and what they cost. Three levers change the guard's strategy; a candidate beats its baseline on AUC and is still refused release because a fairness gate fails.",
    caveat: "Synthetic patients. Not a medical device, not clinically validated."
  },
  {
    slug: "nl2sql-demo",
    name: "Natural language to SQL",
    owns: "Its UI and its SQL generation",
    framework: "Validation, planning, service checks and workload compilation",
    proves:
      "That an ordinary app can consume the framework end to end: a trace of SourceSpec validation, extraction planning, schema contracts, quality rules, DAG planning and model routing, then the service calls behind them.",
    caveat: "The published run uses the deterministic fallback adapter, not a GPU model."
  }
];

export default function App() {
  return (
    <>
      <header className="masthead">
        <div className="ds-shell masthead-inner">
          <div className="wordmark">
            <span className="wordmark-name">Dagents</span>
            <span className="ds-eyebrow">agentic framework</span>
          </div>
          <nav aria-label="Sections">
            <a href="#architecture">Architecture</a>
            <a href="#governance">Governance</a>
            <a href="#demos">Demos</a>
            <a href="#api">API</a>
            <a href={REPO}>Repository</a>
          </nav>
        </div>
      </header>

      <main className="ds-shell page">
        <section className="hero">
          <p className="ds-eyebrow">A framework, not a product backend</p>
          <h1>
            Deploy a computation agent per data source, combine their outputs, and emit workload
            plans a real backend can deploy.
          </h1>
          <p className="hero-model ds-mono">
            compute locally · combine globally · plan with typed functional modules · deploy through
            services
          </p>
          <p className="ds-note hero-note">
            Dagents exists so a consumer backend does not rebuild profiling, orchestration, model
            routing, manifest generation, governance and federated round control. It is neither
            OCaml-first nor Python-first: it is planner-first where planning matters and
            runtime-first where side effects matter.
          </p>
          <dl className="ds-facts hero-facts">
            <div>
              <dt>Services</dt>
              <dd>{inventory.services.length}</dd>
            </div>
            <div>
              <dt>Endpoints</dt>
              <dd>{inventory.endpoint_count}</dd>
            </div>
            <div>
              <dt>Planner modules</dt>
              <dd>{PLANNERS.length}</dd>
            </div>
            <div>
              <dt>Languages</dt>
              <dd>OCaml · Python · Java</dd>
            </div>
          </dl>
        </section>

        <section id="architecture" className="ds-panel">
          <div className="ds-panel-head">
            <h2>Polyglot by layer</h2>
            <p className="ds-eyebrow">the central rule</p>
          </div>
          <div className="ds-panel-body">
            <p className="ds-note">
              Each language owns what it is best at, and the <em>never owns</em> column is the part
              that does the work. A deterministic rule belongs in a planner; applying it to real
              data, with an audit record, belongs in the runtime.
            </p>
            <div className="ds-table-scroll">
              <table className="ds-table">
                <thead>
                  <tr>
                    <th>Layer</th>
                    <th>Lives in</th>
                    <th>Owns</th>
                    <th>Never owns</th>
                  </tr>
                </thead>
                <tbody>
                  {LAYERS.map((layer) => (
                    <tr key={layer.layer}>
                      <td>
                        <strong>{layer.layer}</strong>
                      </td>
                      <td className="id">{layer.path}</td>
                      <td>{layer.owns}</td>
                      <td className="never">{layer.never}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Diagram />
          </div>
        </section>

        <section className="ds-panel">
          <div className="ds-panel-head">
            <h2>Two agents</h2>
            <p className="ds-eyebrow">local execution · global coordination</p>
          </div>
          <div className="ds-panel-body agent-pair">
            <article>
              <h3>
                LMA <span className="ds-chip">local monitoring agent</span>
              </h3>
              <p className="ds-note">
                One per source boundary — a tenant, a database, a service, an event stream, an
                environment. It profiles the data, partitions the work, runs source-level models,
                publishes summaries, and enforces governance at three of the four guard boundaries.
              </p>
            </article>
            <article>
              <h3>
                GMA <span className="ds-chip">global monitoring agent</span>
              </h3>
              <p className="ds-note">
                Registers the agents, assimilates what they publish, runs aggregate models across
                sources, coordinates dispatch, and owns federated round control and release
                governance — the fourth guard boundary.
              </p>
            </article>
            <p className="ds-note agent-note">
              Both follow the same layering: <span className="ds-mono">domain/</span> is pure typed
              models with no I/O, <span className="ds-mono">application/</span> holds use cases,
              <span className="ds-mono"> adapters/</span> faces technology, and
              <span className="ds-mono"> infrastructure/</span> holds messaging and persistence
              behind interfaces — so the repository can move from in-memory delivery to
              broker-backed deployment without a structural rewrite.
            </p>
          </div>
        </section>

        <section className="ds-panel">
          <div className="ds-panel-head">
            <h2>The planners</h2>
            <p className="ds-eyebrow">pure, typed, exhaustively matched</p>
          </div>
          <div className="ds-panel-body">
            <p className="ds-note">
              Choices are algebraic data types rather than string conditionals, which is the whole
              reason this layer is in OCaml: adding a sensitivity level or a model family fails to
              compile until every combination is handled. The modules return reports and plans
              rather than raising. Most of their tests need no Docker, database or GPU.
            </p>
            <div className="ds-table-scroll">
              <table className="ds-table">
                <thead>
                  <tr>
                    <th>Module</th>
                    <th>Decides</th>
                  </tr>
                </thead>
                <tbody>
                  {PLANNERS.map((planner) => (
                    <tr key={planner.module}>
                      <td className="id">{planner.module}</td>
                      <td>{planner.decides}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <section id="governance" className="ds-panel">
          <div className="ds-panel-head">
            <h2>Governance</h2>
            <p className="ds-eyebrow">GRAILS ethical-restriction rails</p>
          </div>
          <div className="ds-panel-body">
            <div className="split">
              <article>
                <p className="ds-eyebrow">Deciding</p>
                <p className="ds-note">
                  Pure, so it lives in the planner. Sensitivity, trust, granularity and strategy are
                  algebraic data types, and the strategy is selected by matching the full triple
                  exhaustively.
                </p>
              </article>
              <article>
                <p className="ds-eyebrow">Enforcing</p>
                <p className="ds-note">
                  Needs real data and an audit sink, so it lives in the runtime. The Guard applies
                  the plan, projects away anything the request did not ask for, and writes a
                  digest-chained audit record.
                </p>
              </article>
              <article>
                <p className="ds-eyebrow">Policy</p>
                <p className="ds-note">
                  Configuration, not code. A data classification is contributed by an extension or
                  registered at runtime, so changing a field's sensitivity changes no code path.
                </p>
              </article>
            </div>
            <div className="ds-verdict ds-verdict--deny">
              <strong>FAILS CLOSED</strong>
              <span>
                If the planner cannot be reached, the request is denied and the denial is recorded.
                An enforcement layer whose absence grants access is not one.
              </span>
            </div>
            <p className="ds-note">
              A site's local runner is handed the guarded payload, not its raw records, even though
              that data never leaves the site — the code running a round is the coordinator's, so
              what it observes is what the coordinator observes. That costs measurable accuracy, and
              the healthcare demo quantifies it rather than claiming the cost away.
            </p>
          </div>
        </section>

        <section className="ds-panel">
          <div className="ds-panel-head">
            <h2>Federated rounds</h2>
            <p className="ds-eyebrow">two invariants, both tested</p>
          </div>
          <div className="ds-panel-body">
            <ul className="ds-reasons invariants">
              <li>
                <strong>Aggregation produces a candidate, never a release.</strong> A release needs
                its gates passed and a human to approve.
              </li>
              <li>
                <strong>A release gate whose metric is absent blocks.</strong> It never passes by
                default.
              </li>
            </ul>
            <p className="ds-note">
              Eligibility, quorum, aggregation readiness and the release gates are all decided in
              the planner. The runtime owns state and side effects and delegates the rest. The
              in-process federated engine is a simulator for tests and demos; the protocol itself
              belongs to a specialist runtime behind an interface, and the simulator is deliberately
              not grown into an optimizer.
            </p>
          </div>
        </section>

        <section className="ds-panel">
          <div className="ds-panel-head">
            <h2>Extending it</h2>
            <p className="ds-eyebrow">one interface, explicit registration</p>
          </div>
          <div className="ds-panel-body">
            <p className="ds-note">
              A consumer app contributes feature contracts, data classifications, condition packs,
              named pipeline steps and named model adapters through one extension interface — never
              by patching the framework. Registration is explicit and id conflicts are errors,
              because two extensions claiming one id would make a guard decision depend on import
              order.
            </p>
            <p className="ds-note">
              The test for whether something belongs in the framework at all: if a capability would
              need a domain word in the agent layer, it belongs in the consumer. Nothing in{" "}
              <span className="ds-mono">agents/</span> knows what a stroke is, and nothing in the
              healthcare app re-implements a quorum rule.
            </p>
          </div>
        </section>

        <section id="demos" className="demos">
          <div className="section-head">
            <h2>Built on it</h2>
            <p className="ds-note">
              Both demos are published below. Neither has a backend on this site, so each replays a
              capture of a real run: the responses were recorded from the live backends with the
              OCaml planner built, and each page names the commit it was captured from. Ask one for
              something the capture does not hold and it says so rather than inventing an answer.
            </p>
          </div>
          <div className="demo-grid">
            {DEMOS.map((demo) => (
              <article className="ds-panel demo" key={demo.slug}>
                <div className="ds-panel-head">
                  <h3>{demo.name}</h3>
                  <p className="ds-eyebrow">{demo.slug}</p>
                </div>
                <div className="ds-panel-body">
                  <p>{demo.proves}</p>
                  <dl className="ds-facts">
                    <div>
                      <dt>The app owns</dt>
                      <dd className="prose">{demo.owns}</dd>
                    </div>
                    <div>
                      <dt>Dagents owns</dt>
                      <dd className="prose">{demo.framework}</dd>
                    </div>
                  </dl>
                  <p className="ds-note caveat">{demo.caveat}</p>
                  <a className="ds-btn ds-btn--primary" href={demoHref(demo.slug)}>
                    Open the demo
                  </a>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section id="api" className="api">
          <div className="section-head">
            <h2>API reference</h2>
            <p className="ds-note">
              Every endpoint the framework and its demo apps expose:{" "}
              <strong>{inventory.endpoint_count}</strong> across{" "}
              <strong>{inventory.services.length}</strong> services. This table is generated from
              the live FastAPI routing tables and the Spring controllers, and a test fails when it
              drifts from the code — so it cannot describe an endpoint that is not served. Ports come
              from the environment files; nothing hardcodes them.
            </p>
          </div>
          {ROLE_ORDER.map((group) => (
            <div className="role" key={group.role}>
              <h3>{group.label}</h3>
              <p className="ds-note">{group.blurb}</p>
              {servicesWithRole(group.role).map((service) => (
                <ServiceTable key={service.name} service={service} />
              ))}
            </div>
          ))}
        </section>

        <section className="ds-panel limits">
          <div className="ds-panel-head">
            <h2>Where it is not finished</h2>
            <p className="ds-eyebrow">stated, not buried</p>
          </div>
          <div className="ds-panel-body">
            <ul className="ds-reasons">
              <li>
                <strong>Delivery is in-memory across the agent layer.</strong> Messaging and
                persistence sit behind interfaces; broker-backed infrastructure is deferred.
              </li>
              <li>
                <strong>Kubernetes validation on Minikube is outstanding</strong>, blocked on local
                disk capacity. Manifests are generated and asserted, but not yet applied to a live
                cluster in CI.
              </li>
              <li>
                <strong>The Postgres-backed suites skip where no database is provisioned</strong>,
                including in CI. A change to source adapters is not fully covered by a green run.
              </li>
              <li>
                <strong>The federated engine is a simulator.</strong> It is there for tests and
                demos, and is not a federated optimizer.
              </li>
            </ul>
          </div>
        </section>
      </main>

      <footer className="footer">
        <div className="ds-shell footer-inner">
          <p className="ds-note">
            Dagents is MIT-licensed. The architecture notes, the contributor guide and the
            reference docs live in the repository.
          </p>
          <nav aria-label="Repository links">
            <a href={REPO}>Source</a>
            <a href={`${REPO}/blob/main/AGENTS.md`}>Contributor guide</a>
            <a href={`${REPO}/blob/main/docs/reference/service-inventory.md`}>Service inventory</a>
            <a href={`${REPO}/tree/main/bindings/ocaml`}>Planner layer</a>
          </nav>
        </div>
      </footer>
    </>
  );
}

/** One service's endpoints, collapsed by default so the page stays scannable. */
function ServiceTable({ service }: { service: ServiceEntry }) {
  const [open, setOpen] = useState(false);
  const aliases = service.endpoints.filter((endpoint) => endpoint.alias_of !== null).length;
  return (
    <div className="ds-panel service">
      <div className="ds-panel-head">
        <h4>
          <span className="ds-mono">{service.name}</span>
        </h4>
        <div className="ds-row service-meta">
          <span className="ds-chip">{service.language}</span>
          <span className="ds-chip">:{service.port}</span>
          <span className="ds-mono count">{service.endpoints.length} endpoints</span>
          {aliases > 0 && <span className="ds-mono count">{aliases} versioned aliases</span>}
          <button className="ds-btn" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "Hide" : "Show"}
          </button>
        </div>
      </div>
      {open && (
        <div className="ds-panel-body">
          <div className="ds-table-scroll">
            <table className="ds-table">
              <thead>
                <tr>
                  <th>Method</th>
                  <th>Path</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {service.endpoints.map((endpoint) => (
                  <tr key={`${endpoint.method} ${endpoint.path}`}>
                    <td className="id method">{endpoint.method}</td>
                    <td className="id">{endpoint.path}</td>
                    <td>
                      {endpoint.alias_of && (
                        <span className="ds-chip alias">
                          alias of {endpoint.alias_of.method} {endpoint.alias_of.path}
                        </span>
                      )}
                      {endpoint.status_code && (
                        <span className="ds-chip">returns {endpoint.status_code}</span>
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
