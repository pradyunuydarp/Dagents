import { useLayoutEffect } from "react";

import { Diagram } from "../Mermaid";
import { DEMOS, REPO_URL, demoHref } from "../config";
import { GUIDE, firstDiagram } from "../learn";
import { href } from "../router";

const STEPS = [
  {
    title: "Local agents",
    text: "An LMA runs next to each data source. It profiles the data, runs local jobs, and checks every request before data is read, used for training or sent out."
  },
  {
    title: "A coordinator",
    text: "The GMA registers the local agents, plans federated rounds and combines their results. A candidate model must pass its release gates before it can be used."
  },
  {
    title: "Typed planners",
    text: "Decisions such as which protection a request needs, or whether enough sites can join a round, are made by pure OCaml functions. The services call them through a command-line tool and apply the result."
  }
];

const PARTS = [
  { part: "Agents", folder: "agents/", purpose: "The LMA and GMA services" },
  { part: "Planners", folder: "bindings/ocaml/", purpose: "The OCaml decision modules and the dagentsc tool" },
  { part: "Framework services", folder: "services/", purpose: "Core, pipeline and model services, and their Spring Boot versions" },
  { part: "Stroke triage demo", folder: "apps/healthcare-demo/", purpose: "Example application: governance and federated learning" },
  { part: "NL2SQL demo", folder: "services/nl2sql-demo/", purpose: "Example application: validation and planning" },
  { part: "Documentation", folder: "docs/", purpose: "The learning guide, architecture notes and the endpoint list" }
];

const STATUS = [
  "The agents keep their state in memory. Storage and messaging sit behind interfaces so they can be replaced later.",
  "The federation engine is a simulator for tests and demos. Production training would use a dedicated federated learning runtime.",
  "Kubernetes manifests are generated and tested, but are not yet deployed to a cluster in CI.",
  "Tests that need a sample Postgres database are skipped in CI."
];

export function Home({ anchor }: { anchor: string | null }) {
  useLayoutEffect(() => {
    if (anchor) document.getElementById(anchor)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [anchor]);

  const layers = firstDiagram("03-architecture.md");
  const steps = GUIDE.filter((page) => page.slug !== "" && page.slug !== "references");

  return (
    <>
      <section className="hero">
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
            Start the guide
          </a>
          <a className="ds-btn" href={demoHref("healthcare-demo")}>
            Try the stroke demo
          </a>
          <a className="ds-btn" href={REPO_URL}>
            View on GitHub
          </a>
        </div>
      </section>

      <section id="how-it-works" className="block">
        <h2>How it works</h2>
        <ol className="steps">
          {STEPS.map((step) => (
            <li key={step.title}>
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
    </>
  );
}
