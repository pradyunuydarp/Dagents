/**
 * The framework's mechanism, drawn once.
 *
 * The thing worth a picture here is not the box list, it is the split: where a
 * decision is computed versus where a side effect happens, and the subprocess
 * boundary between them. So the drawing is a sandwich — consumers on top, the
 * Python and Spring runtime in the middle, the pure OCaml planners underneath —
 * and the dashed rule across it is the `dagentsc` JSON boundary that every
 * deterministic decision crosses, in both directions: a request down, a plan
 * back.
 *
 * Geometry is hand-placed, so it is laid out on a coarse grid with every label
 * kept inside its box and clear of every arrow. Colours come from the design
 * tokens, so it reads in both themes. Nothing here is decorative: every box is
 * a real directory in the repository.
 */

const COMPILERS = [
  "common_ir",
  "dataset",
  "pipeline",
  "model_router",
  "manifest",
  "governance",
  "federation"
];

const SOURCES = ["tenant database", "event stream", "service cluster"];
const SERVICES = ["core-service", "pipeline-service", "model-service"];

/** Row baselines for the three source/agent pairs. */
const ROWS = [142, 190, 238];
const GMA_MID = 209;

export function Diagram() {
  return (
    <figure className="diagram">
      <div className="diagram-scroll">
        <svg viewBox="0 0 900 470" role="img" aria-labelledby="diagram-title diagram-desc">
          <title id="diagram-title">
            Where a Dagents decision is computed, and where its side effects happen
          </title>
          <desc id="diagram-desc">
            Consumer backends call the framework's APIs. The Python and Spring runtime holds one
            Local Monitoring Agent per data source, a Global Monitoring Agent that assimilates
            their output, and the core, pipeline and model services. Every deterministic decision
            crosses a JSON subprocess boundary into seven pure OCaml compiler modules, which
            return a plan.
          </desc>

          {/* --- consumers ------------------------------------------------- */}
          <g className="d-layer">
            <rect x="8" y="8" width="884" height="50" rx="4" />
            <text className="d-layer-label" x="22" y="29">
              CONSUMER BACKEND
            </text>
            <text className="d-note" x="22" y="47">
              Watchdog · Datalytics · the demo apps — they call the framework, not the other way
              round
            </text>
          </g>

          <g className="d-flow">
            <line x1="450" y1="58" x2="450" y2="78" markerEnd="url(#d-arrow)" />
          </g>

          {/* --- runtime --------------------------------------------------- */}
          <g className="d-layer">
            <rect x="8" y="82" width="884" height="236" rx="4" />
            <text className="d-layer-label" x="22" y="103">
              PYTHON + SPRING RUNTIME
            </text>
            <text className="d-note" x="22" y="121">
              source I/O, ML execution, runtime state, audit records — everything with a side effect
            </text>
          </g>

          {/* one agent per source boundary */}
          {SOURCES.map((source, index) => {
            const y = ROWS[index];
            return (
              <g key={source}>
                <g className="d-box d-box--source">
                  <rect x="22" y={y} width="130" height="38" rx="3" />
                  <text x="32" y={y + 24}>
                    {source}
                  </text>
                </g>
                <g className="d-flow">
                  <line x1="152" y1={y + 19} x2="172" y2={y + 19} markerEnd="url(#d-arrow)" />
                </g>
                <g className="d-box d-box--agent">
                  <rect x="176" y={y} width="118" height="38" rx="3" />
                  <text x="212" y={y + 24}>
                    LMA
                  </text>
                </g>
                <g className="d-flow">
                  <line x1="294" y1={y + 19} x2="336" y2={GMA_MID} markerEnd="url(#d-arrow)" />
                </g>
              </g>
            );
          })}

          <text className="d-note" x="176" y="300">
            one per source boundary · profiles, partitions, runs models, enforces three of the four
            guard boundaries
          </text>

          {/* the coordinator */}
          <g className="d-box d-box--agent">
            <rect x="340" y="142" width="190" height="134" rx="3" />
            <text x="352" y="166">
              GMA
            </text>
            <text className="d-sub" x="352" y="190">
              registers agents
            </text>
            <text className="d-sub" x="352" y="208">
              assimilates outputs
            </text>
            <text className="d-sub" x="352" y="226">
              round control
            </text>
            <text className="d-sub" x="352" y="244">
              release governance
            </text>
            <text className="d-sub" x="352" y="262">
              4th guard boundary
            </text>
          </g>

          {/* framework services */}
          <g className="d-box">
            <rect x="556" y="142" width="326" height="134" rx="3" />
            <text x="568" y="166">
              Framework services
            </text>
            {SERVICES.map((service, index) => (
              <g className="d-chip" key={service}>
                <rect x="568" y={180 + index * 30} width="140" height="24" rx="2" />
                <text x="576" y={196 + index * 30}>
                  {service}
                </text>
              </g>
            ))}
            <text className="d-sub" x="722" y="196">
              stable APIs, so a
            </text>
            <text className="d-sub" x="722" y="214">
              consumer calls them
            </text>
            <text className="d-sub" x="722" y="232">
              instead of rebuilding
            </text>
            <text className="d-sub" x="722" y="250">
              profiling and planning
            </text>
          </g>

          {/* --- the boundary ---------------------------------------------- */}
          <g className="d-boundary">
            <text x="8" y="332">
              JSON OVER A SUBPROCESS BOUNDARY — NEVER FFI
            </text>
            <line x1="8" y1="340" x2="892" y2="340" />
            <text className="d-note d-boundary-note" x="892" y="358">
              failure isolation · upgrade independence · one binary to ship
            </text>
          </g>

          {/* One line, arrowed at both ends: a request goes down, a plan comes
              back. Placed clear of both boundary labels. */}
          <g className="d-flow">
            <line
              x1="450"
              y1="318"
              x2="450"
              y2="366"
              markerStart="url(#d-arrow)"
              markerEnd="url(#d-arrow)"
            />
          </g>

          {/* --- planners -------------------------------------------------- */}
          <g className="d-layer d-layer--planner">
            <rect x="8" y="372" width="884" height="86" rx="4" />
            <text className="d-layer-label" x="22" y="395">
              OCAML — PURE TYPED PLANNERS (dagentsc)
            </text>
          </g>
          {COMPILERS.map((compiler, index) => (
            <g className="d-chip d-chip--planner" key={compiler}>
              <rect x={20 + index * 124} y="410" width="116" height="28" rx="2" />
              <text x={28 + index * 124} y="428">
                {compiler}
              </text>
            </g>
          ))}

          <defs>
            <marker
              id="d-arrow"
              viewBox="0 0 8 8"
              refX="6"
              refY="4"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 7 4 L 0 7 z" />
            </marker>
          </defs>
        </svg>
      </div>
      <figcaption>
        Every box is a directory in the repository. Deciding happens below the dashed rule and is
        pure; everything with a side effect happens above it. A rule that belongs below and gets
        reimplemented above is the one mistake this architecture is arranged to prevent.
      </figcaption>
    </figure>
  );
}
