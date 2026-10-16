import { useLayoutEffect } from "react";

import { Diagram } from "../Mermaid";
import { StepArt, type StepArtKind } from "../StepArt";
import { Topology } from "../Topology";
import { DEMOS, REPO_URL, demoHref } from "../config";
import { BUILD_GUIDE } from "../guide";
import { GUIDE, firstDiagram } from "../learn";
import { href } from "../router";

const STEPS: { title: string; art: StepArtKind; text: string }[] = [
  {
    title: "Local agents",
    art: "agents",
    text: "An LMA runs next to each data source. It profiles the data, runs local jobs, and checks every request before data is read, used for training or sent out."
  },
  {
    title: "A coordinator",
    art: "coordinator",
    text: "The GMA registers the local agents, plans federated rounds and combines their results. A candidate model must pass its release gates before it can be used."
  },
  {
    title: "Typed planners",
    art: "planners",
    text: "Decisions such as which protection a request needs, or whether enough sites can join a round, are made by pure OCaml functions. The services call them through a command-line tool and apply the result."
  }
];

const PARTS = [
  {
    part: "Agents",
    folder: "dagents/",
    purpose: "The LMA and GMA services, and the client and tokens they call each other with"
  },
  { part: "Planners", folder: "bindings/ocaml/", purpose: "The OCaml decision modules and the dagentsc tool" },
  { part: "Framework services", folder: "services/", purpose: "Core, pipeline and model services, and their Spring Boot versions" },
  { part: "Stroke triage demo", folder: "apps/healthcare-demo/", purpose: "Example application on the LMA and GMA APIs: per-hospital stores, governance and federated training" },
  { part: "NL2SQL demo", folder: "services/nl2sql-demo/", purpose: "Example application: validation and planning" },
  { part: "Documentation", folder: "docs/", purpose: "The learning guide, architecture notes and the endpoint list" }
];

const STATUS = [
  "The agents deliver messages in memory, and the GMA keeps its enrolments and rounds in memory. With a database, an LMA keeps its audit chain and job log in Postgres, and the GMA keeps its model registry and audit chain there. With a database, the stroke demo keeps each hospital's records in its own Postgres schema; without one, in memory.",
  "The GMA can send each round's jobs to the LMAs over their HTTP APIs and combine training updates through a model kind the application contributes. It is not a secure-aggregation protocol: the GMA sees each site's clipped update. The in-process engine is a simulator for tests and the getting-started example.",
  "The LMA and GMA accept bearer tokens with fixed scopes. The framework services check no identity, so they belong on a private network.",
  "The stroke demo calls one LMA per hospital and a GMA only through their HTTP APIs. On the published demo, and locally by default, the agents run inside the demo's server and are called without a socket. With the network option of the local start script, in Docker Compose and in one CI test they run as separate processes, called by the same client code. Such a run on one machine can add a fourth hospital while it runs: core-service renders the new LMA's Compose file, the app starts it, and the GMA accepts only the address it was configured to accept.",
  "Kubernetes manifests and Docker Compose files are generated and tested, but CI deploys neither.",
  "The stroke demo's store and isolation tests run in CI against PostgreSQL 16. The tests that need the pagila sample database are skipped in CI."
];

export function Home({ anchor }: { anchor: string | null }) {
  useLayoutEffect(() => {
    if (anchor) document.getElementById(anchor)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [anchor]);

  const layers = firstDiagram("03-architecture.md");
  const steps = GUIDE.filter((page) => page.slug !== "" && page.slug !== "references");
  const buildSteps = BUILD_GUIDE.filter((page) => page.slug !== "");

  return (
    <div className="home">
      <section className="hero">
        <div className="hero-text">
          <p className="eyebrow">Open-source framework</p>
          <h1>Dagents</h1>
          <p className="lead">
            A framework for running analytics and machine learning across many data sources without
            moving the data.
          </p>
          <p className="ds-note ds-measure">
            Each data source runs a local agent. A coordinator combines their results. A typed planner
            decides what each request may see, and every decision is recorded.
          </p>
          <div className="actions">
            <a className="ds-btn ds-btn--primary" href={href("learn")}>
              Start learning
            </a>
            <a className="ds-btn" href={href("guide")}>
              Build an application
            </a>
            <a className="ds-btn" href={demoHref("healthcare-demo")}>
              Try the stroke demo
            </a>
            <a className="ds-btn" href={REPO_URL}>
              View on GitHub
            </a>
          </div>
        </div>
        <Topology />
      </section>

      <section id="how-it-works" className="block">
        <h2>How it works</h2>
        <ol className="steps">
          {STEPS.map((step, index) => (
            <li key={step.title}>
              <span className="step-index" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <StepArt kind={step.art} />
              <h3>{step.title}</h3>
              <p>{step.text}</p>
            </li>
          ))}
        </ol>
        {layers && (
          <Diagram
            source={layers}
            caption="Applications call the runtime over HTTP. The runtime asks the planners for decisions."
          />
        )}
        <p>
          <a href={href("learn/03-architecture")}>Read how Dagents is built</a>
        </p>
      </section>

      <section id="demos" className="block">
        <h2>Demos</h2>
        <div className="cards">
          {DEMOS.map((demo) => (
            <article className="ds-panel card" key={demo.slug}>
              <div className="ds-panel-body">
                <h3>{demo.name}</h3>
                <p>{demo.summary}</p>
                <ul className="plain-list">
                  {demo.tryThis.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
                <p className="ds-note">{demo.data}</p>
                <div className="actions">
                  <a className="ds-btn ds-btn--primary" href={demoHref(demo.slug)}>
                    Open the demo
                  </a>
                  <a className="ds-btn" href={demo.source}>
                    Source
                  </a>
                </div>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section id="learn" className="block">
        <h2>Learn</h2>
        <p className="ds-note ds-measure">
          A step-by-step guide to federated learning, data governance and the Dagents APIs, with
          videos, papers and exercises.
        </p>
        <ol className="guide-list">
          {steps.map((page) => (
            <li key={page.slug}>
              <a href={href(`learn/${page.slug}`)}>{page.title}</a>
            </li>
          ))}
        </ol>
        <p>
          <a href={href("learn/references")}>All references</a>
        </p>
        <p>
          <a href={href("resources")}>Slide decks and reports</a> we used to teach the framework, as
          PowerPoint files and PDFs.
        </p>
      </section>

      <section id="build" className="block">
        <h2>Build an application</h2>
        <p className="ds-note ds-measure">
          A step-by-step method for using Dagents in your own application, with a small worked
          example of two stores. Each step has the code, the command to run and the output to expect.
        </p>
        <ol className="guide-list">
          {buildSteps.map((page) => (
            <li key={page.slug}>
              <a href={href(`guide/${page.slug}`)}>{page.title}</a>
            </li>
          ))}
        </ol>
        <p>
          <a href={href("guide")}>Choose one of the three ways to build on Dagents</a>
        </p>
      </section>

      <section id="repository" className="block">
        <h2>What is in the repository</h2>
        <div className="ds-table-scroll">
          <table className="ds-table">
            <thead>
              <tr>
                <th>Part</th>
                <th>Folder</th>
                <th>Purpose</th>
              </tr>
            </thead>
            <tbody>
              {PARTS.map((row) => (
                <tr key={row.folder}>
                  <td>{row.part}</td>
                  <td>
                    <a className="ds-mono" href={`${REPO_URL}/tree/main/${row.folder}`}>
                      {row.folder}
                    </a>
                  </td>
                  <td>{row.purpose}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section id="status" className="block">
        <h2>Current status</h2>
        <ul className="plain-list">
          {STATUS.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}
