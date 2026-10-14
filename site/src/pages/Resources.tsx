import { useEffect, useLayoutEffect } from "react";

import { REPO_URL, sitePath } from "../config";
import {
  RESOURCES,
  formatBytes,
  formatDate,
  publishedFiles,
  type ResourceGroup,
  type ResourceItem
} from "../resources";
import { href } from "../router";

/** The teaching decks, notes and reports, grouped. The list is `docs/presentation/resources.json`. */
export function Resources({ anchor }: { anchor: string | null }) {
  useEffect(() => {
    document.title = "Resources · Dagents";
  }, []);

  useLayoutEffect(() => {
    if (anchor) document.getElementById(anchor)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [anchor]);

  return (
    <div className="block resources">
      <h1>Resources</h1>
      <p className="ds-measure">
        The slide decks, notes and reports we used to teach Dagents. Download a deck as a PowerPoint
        file (.pptx) to present or change it, or open its PDF in the browser. For a step-by-step
        introduction, read <a href={href("learn")}>the learning guide</a>.
      </p>
      <p className="ds-note">
        LibreOffice made these PDFs from the decks. It does not have the decks' fonts (Aptos and
        Georgia) and uses fonts of similar width instead, so some lines break in different places than
        in PowerPoint, and a few headings run into the text below them. "Updated" is the date of the
        last change to the file in the repository.
      </p>

      <nav className="resource-toc" aria-label="Groups on this page">
        {RESOURCES.groups.map((group) => (
          <a key={group.id} href={href("resources", group.id)}>
            {group.title}
          </a>
        ))}
      </nav>

      {RESOURCES.groups.map((group) => (
        <Group key={group.id} group={group} />
      ))}
    </div>
  );
}

function Group({ group }: { group: ResourceGroup }) {
  return (
    <section className="resource-group" id={group.id}>
      <h2>{group.title}</h2>
      <p className="ds-measure">{group.intro}</p>
      {group.note && (
        <p className="ds-note">
          {group.note}
          {group.noteLink && (
            <>
              {" "}
              <a href={`${REPO_URL}/blob/main/${group.noteLink.path}`}>{group.noteLink.label}</a>.
            </>
          )}
        </p>
      )}
      <ul className="resource-list">
        {group.items.map((item) => (
          <Item key={item.id} item={item} />
        ))}
      </ul>
    </section>
  );
}

function Item({ item }: { item: ResourceItem }) {
  const files = publishedFiles(item);
  const { facts } = item;
  const preview = files.preview && facts.preview;
  const count =
    item.kind === "deck"
      ? { label: "Slides", value: facts.slides }
      : item.kind === "pdf"
        ? { label: "Pages", value: facts.pages }
        : null;

  return (
    <li className={`resource${preview ? " has-preview" : ""}`} id={item.id}>
      {files.preview && facts.preview && (
        <img
          className="resource-preview"
          src={sitePath(files.preview.path)}
          width={facts.preview.width}
          height={facts.preview.height}
          alt={`Slide ${facts.preview.slide} of ${item.title}`}
          loading="lazy"
        />
      )}
      <div className="resource-body">
        <h3>{item.title}</h3>
        <p>{item.description}</p>
        {item.note && <p className="ds-note">{item.note}</p>}
        <dl className="resource-facts">
          <div>
            <dt>Audience</dt>
            <dd>{item.audience}</dd>
          </div>
          {count?.value !== undefined && (
            <div>
              <dt>{count.label}</dt>
              <dd className="ds-mono">{count.value}</dd>
            </div>
          )}
          <div>
            <dt>Updated</dt>
            <dd>{formatDate(facts.updated)}</dd>
          </div>
        </dl>
        <div className="actions">
          {files.pdf && (
            <a
              className="ds-btn"
              href={sitePath(files.pdf.path)}
              target="_blank"
              rel="noopener"
              aria-label={`Open the PDF of ${item.title}, ${formatBytes(files.pdf.bytes)}`}
            >
              <span className="resource-action">Open PDF</span>
              <span className="resource-size">{formatBytes(files.pdf.bytes)}</span>
            </a>
          )}
          {files.pptx && (
            <a
              className="ds-btn"
              href={sitePath(files.pptx.path)}
              download
              aria-label={`Download ${item.title} as PowerPoint, ${formatBytes(files.pptx.bytes)}`}
            >
              <span className="resource-action">Download .pptx</span>
              <span className="resource-size">{formatBytes(files.pptx.bytes)}</span>
            </a>
          )}
          {item.kind === "markdown" && (
            <a
              className="ds-btn"
              href={`${REPO_URL}/blob/main/${item.source}`}
              aria-label={`Read ${item.title} on GitHub, Markdown, ${formatBytes(facts.bytes)}`}
            >
              <span className="resource-action">Read on GitHub</span>
              <span className="resource-size">Markdown, {formatBytes(facts.bytes)}</span>
            </a>
          )}
        </div>
      </div>
    </li>
  );
}
