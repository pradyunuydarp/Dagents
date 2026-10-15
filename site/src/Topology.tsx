/**
 * The hero figure on the home page: three local agents send results to the
 * coordinator, which asks the planner for decisions. Drawn in ink tokens only,
 * because colour on this site marks decisions. The moving pulses are CSS
 * animations of one dash per line, and they stop when the reader asks for
 * reduced motion.
 */

const SITES = [
  { y: 16, name: "site A" },
  { y: 98, name: "site B" },
  { y: 180, name: "site C" }
];

const NODE_H = 44;
const GMA = { x: 168, y: 98, w: 104 };
const PLANNER = { x: 316, y: 98, w: 96 };
const MID = GMA.y + NODE_H / 2;

function Node({ x, y, w, title, sub }: { x: number; y: number; w: number; title: string; sub: string }) {
  return (
    <g className="topo-node">
      <rect x={x} y={y} width={w} height={NODE_H} rx={6} />
      <text className="topo-title" x={x + 12} y={y + 19}>
        {title}
      </text>
      <text className="topo-sub" x={x + 12} y={y + 34}>
        {sub}
      </text>
    </g>
  );
}

export function Topology() {
  const links = SITES.map((site) => {
    const y = site.y + NODE_H / 2;
    return `M116 ${y} C146 ${y} 146 ${MID} ${GMA.x} ${MID}`;
  });
  const planLink = `M${GMA.x + GMA.w} ${MID} H${PLANNER.x}`;

  return (
    <figure className="topology">
      <svg
        viewBox="0 0 420 240"
        role="img"
        aria-label="Three local agents send results to a coordinator, which asks a typed planner for decisions."
      >
        {links.map((d) => (
          <path key={d} className="topo-link" d={d} />
        ))}
        <path className="topo-link topo-link--plan" d={planLink} />
        {links.map((d, i) => (
          <path
            key={`pulse-${d}`}
            className="topo-pulse"
            d={d}
            pathLength={100}
            style={{ animationDelay: `${i * 0.9}s` }}
          />
        ))}
        <path className="topo-pulse topo-pulse--plan" d={planLink} pathLength={100} />
        {SITES.map((site) => (
          <Node key={site.name} x={8} y={site.y} w={108} title="LMA" sub={site.name} />
        ))}
        <g className="topo-hub">
          <Node x={GMA.x} y={GMA.y} w={GMA.w} title="GMA" sub="coordinator" />
        </g>
        <Node x={PLANNER.x} y={PLANNER.y} w={PLANNER.w} title="Planner" sub="OCaml" />
      </svg>
      <figcaption>Each site sends results to the coordinator. Its records stay where they are.</figcaption>
    </figure>
  );
}
