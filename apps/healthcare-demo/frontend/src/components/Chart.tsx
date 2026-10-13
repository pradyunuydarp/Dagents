import { useEffect, useRef, useState } from "react";

import { getSvg } from "../api";
import type { ChartInfo } from "../types";
import { useColorScheme } from "../useColorScheme";
import { ErrorNote } from "./Basics";
import { DataTable } from "./DataTable";

/**
 * One chart drawn by the backend.
 *
 * The backend draws each chart with matplotlib as SVG, in a light and a dark
 * version that use the design system's tokens. The page fetches the SVG text
 * through the transport (so a cold start is waited for and a replay works),
 * and shows it as an image from a `blob:` URL. An image cannot run scripts, so
 * the SVG needs no sanitising. When the theme changes, the page fetches the
 * other version.
 */
export function ChartFigure({ chart }: { chart: ChartInfo }) {
  const scheme = useColorScheme();
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const shown = useRef<string | null>(null);

  useEffect(() => {
    if (!chart.available) return;
    let current = true;
    getSvg(`${chart.svg_path}?theme=${scheme}`)
      .then((text) => {
        if (!current) return;
        const url = URL.createObjectURL(new Blob([text], { type: "image/svg+xml" }));
        // Keep the old image until the new one is ready, then free it.
        const previous = shown.current;
        shown.current = url;
        setSrc(url);
        setError(null);
        if (previous) URL.revokeObjectURL(previous);
      })
      .catch((exc: unknown) => {
        if (current) setError(exc);
      });
    return () => {
      current = false;
    };
  }, [chart.svg_path, chart.available, chart.version, scheme]);

  useEffect(
    () => () => {
      if (shown.current) URL.revokeObjectURL(shown.current);
    },
    []
  );

  return (
    <figure className="chart" data-chart={chart.chart_id} data-available={chart.available ? "yes" : "no"}>
      <h3>{chart.title}</h3>
      <p className="ds-note">{chart.description}</p>
      {chart.available ? (
        <>
          {error ? (
            <ErrorNote error={error} />
          ) : (
            // On a narrow screen the chart keeps its drawn size and scrolls
            // inside this box, so its text stays readable and the page itself
            // does not scroll sideways.
            <div className="chart-scroll">
              {src ? (
                <img className="chart-image" src={src} alt={chart.alt} width={480} height={316} />
              ) : (
                <div className="chart-placeholder" aria-hidden="true" />
              )}
            </div>
          )}
          <details className="ds-details">
            <summary>Show the data</summary>
            <DataTable table={chart.table} caption={chart.title} />
          </details>
        </>
      ) : (
        <p className="chart-unavailable">{chart.reason ?? "This chart has no data yet."}</p>
      )}
    </figure>
  );
}

export function ChartGrid({ charts }: { charts: ChartInfo[] }) {
  return (
    <div className="chart-grid">
      {charts.map((chart) => (
        <ChartFigure key={chart.chart_id} chart={chart} />
      ))}
    </div>
  );
}
