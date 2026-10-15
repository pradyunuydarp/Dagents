/**
 * Small line drawings for the three "How it works" cards on the home page.
 * They are decoration, so screen readers skip them. They use ink and rule
 * tokens only, and move a little when the card is hovered (styles.css).
 */

export type StepArtKind = "agents" | "coordinator" | "planners";

function Agents() {
  return (
    <>
      {/* A data store: a cylinder with two rows of records. */}
      <path className="art-fill" d="M58 26 v42 a34 9 0 0 0 68 0 v-42" />
      <ellipse className="art-fill art-ink" cx="92" cy="26" rx="34" ry="9" />
      <path className="art-line" d="M58 26 v42 a34 9 0 0 0 68 0 v-42" />
      <path className="art-line" d="M58 40 a34 9 0 0 0 68 0 M58 54 a34 9 0 0 0 68 0" />
      <line className="art-scan" x1="60" y1="30" x2="124" y2="30" />
      {/* The local agent beside it, watching every read. */}
      <path className="art-link" d="M130 48 H154" />
      <line className="art-line" x1="174" y1="22" x2="174" y2="30" />
      <circle className="art-antenna" cx="174" cy="20" r="3" />
      <rect className="art-fill art-ink" x="156" y="30" width="36" height="34" rx="8" />
      <g className="art-eyes">
        <circle className="art-dot" cx="167" cy="45" r="2.5" />
        <circle className="art-dot" cx="181" cy="45" r="2.5" />
      </g>
      <path className="art-line" d="M168 55 q6 4 12 0" />
    </>
  );
}

function Coordinator() {
  const satellites = [0, 120, 240].map((deg) => {
    const rad = ((deg - 90) * Math.PI) / 180;
    return { x: 120 + 34 * Math.cos(rad), y: 48 + 34 * Math.sin(rad) };
  });
  return (
    <>
      <circle className="art-orbit" cx="120" cy="48" r="34" />
      <g className="art-spin">
        {satellites.map((s) => (
          <g key={`${s.x}-${s.y}`}>
            <line className="art-link" x1="120" y1="48" x2={s.x} y2={s.y} />
            <circle className="art-fill art-ink" cx={s.x} cy={s.y} r="7" />
          </g>
        ))}
      </g>
      <circle className="art-fill art-ink" cx="120" cy="48" r="14" />
      <circle className="art-dot" cx="120" cy="48" r="4" />
    </>
  );
}

function Planners() {
  return (
    <>
      {/* A typed request goes in, one branch is chosen, a plan comes out. */}
      <rect className="art-fill art-ink" x="26" y="36" width="44" height="24" rx="4" />
      <text className="art-text" x="48" y="52" textAnchor="middle">
        {"{ }"}
      </text>
      <path className="art-line" d="M70 48 H104" />
      <path className="art-fill art-ink" d="M120 32 L136 48 L120 64 L104 48 Z" />
      <path className="art-link" d="M136 48 C156 48 160 22 184 22" />
      <path className="art-line art-chosen" d="M136 48 H184" />
      <path className="art-link" d="M136 48 C156 48 160 74 184 74" />
      <circle className="art-fill art-line" cx="190" cy="22" r="6" />
      <circle className="art-fill art-ink" cx="190" cy="48" r="6" />
      <circle className="art-fill art-line" cx="190" cy="74" r="6" />
      <circle className="art-runner" cx="0" cy="0" r="3" />
    </>
  );
}

export function StepArt({ kind }: { kind: StepArtKind }) {
  return (
    <svg className={`step-art step-art--${kind}`} viewBox="0 0 240 96" aria-hidden="true" focusable="false">
      {kind === "agents" && <Agents />}
      {kind === "coordinator" && <Coordinator />}
      {kind === "planners" && <Planners />}
    </svg>
  );
}
