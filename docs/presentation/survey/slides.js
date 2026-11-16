// The slides of the survey deck. build_deck.js holds the theme, layouts and helpers.
// Sections follow the paper: the question, GRAILS, the survey, the Dagents design,
// the case study and demo, the comparison, the limits.

const survey = require("./survey_slides.js");

async function build(k) {
  const { pres, HEX, M, W, chip, content, card, text, bullets, pill, iconRow, table, notes } = k;

  // ------------------------------------------------------------ opening
  pres.addSection({ title: "The question" });
  {
    const s = pres.addSlide({ masterName: "TITLE_DARK", sectionTitle: "The question" });
    chip(s, "Survey and design", { dark: true, y: 1.4, w: 2.0 });
    s.addText("Where governance decisions live", { placeholder: "title" });
    s.addText("A survey of governed analytics and learning across data sources, and the design of Dagents", { placeholder: "body" });
    s.addText("Pradyun Devarakonda · International Institute of Information Technology Bangalore · November 2026", { placeholder: "byline" });
    notes(s, `This talk has two parts. First, a survey: how existing research and industry systems decide what a
      request may see, what may leave a site, who may take part in a study, and whether a model may be released.
      Second, the design idea behind Dagents, which extends the GRAILS framework from one application to studies
      that run across several organisations. The full argument and every reference are in the paper,
      docs/reports/dagents_survey_paper.pdf.`);
  }
  {
    const s = content("The question", "The problem", "Four questions every multi-source system answers");
    const qs = [
      ["LuEye", "What may this requester see, and in what form?", "A count, a generalised value, a row, or nothing."],
      ["LuLogOut", "May this result or model update leave the site?", "Metrics and updates are derived from records."],
      ["LuUsers", "May this site take part in this study?", "Enrolment, contract version, cohort size, approval."],
      ["LuBadgeCheck", "May this new model replace the one in use?", "Release gates, approval and a record of why."],
    ];
    let y = 2.0;
    for (const [ic, h, b] of qs) { await iconRow(s, ic, h, b, M, y, 7.4); y += 1.15; }
    card(s, 8.6, 2.0, 4.1, 3.0);
    text(s, "Who asks them", 8.85, 2.2, 3.6, 0.4, { fontSize: 16, bold: true });
    bullets(s, ["Hospitals that may not pool patient records", "Banks that keep transactions apart", "Public agencies with census and survey data", "A service that keeps each tenant's data separate"], 8.85, 2.7, 3.6, 3.4, { fontSize: 14 });
    notes(s, `These four questions come up many times a day in any system that computes across data it may not pool.
      The rest of the talk asks, for each approach we surveyed, where the answer to each question is decided and
      where it is enforced.`);
  }
  await survey.rules(k);

  // ------------------------------------------------------------ GRAILS
  pres.addSection({ title: "GRAILS" });
  {
    const s = content("GRAILS", "Starting point", "GRAILS separates deciding from enforcing");
    // Guard
    card(s, M, 2.0, 5.2, 3.9, { fill: HEX.white });
    text(s, "Ethical Guard (runtime): enforces", M + 0.25, 2.15, 4.8, 0.4, { fontSize: 15, bold: true });
    const g = [["Strategy Engine", "turns the restriction plan into an execution plan"], ["Execution Engine", "applies it to data in the Privacy Vault"], ["Plug-ins", "masking, anonymisation, encryption, consent"]];
    let y = 2.7;
    for (const [h, b] of g) { card(s, M + 0.25, y, 4.7, 0.9, { fill: HEX.paper }); text(s, h, M + 0.4, y + 0.08, 4.4, 0.3, { fontSize: 14, bold: true }); text(s, b, M + 0.4, y + 0.42, 4.4, 0.4, { fontSize: 13, color: HEX.grey }); y += 1.0; }
    // Rails
    card(s, 7.5, 2.0, 5.2, 3.9, { fill: HEX.white });
    text(s, "Ethical-Restriction Rails: decide", 7.75, 2.15, 4.8, 0.4, { fontSize: 15, bold: true });
    const r = [["Know Your User service", "user attributes to a trust score (fuzzy inference)"], ["Data Protection service", "metadata and regulations to a protection profile"], ["Restriction Planner", "like a query planner: profiles to strategies"]];
    y = 2.7;
    for (const [h, b] of r) { card(s, 7.75, y, 4.7, 0.9, { fill: HEX.paper }); text(s, h, 7.9, y + 0.08, 4.4, 0.3, { fontSize: 14, bold: true }); text(s, b, 7.9, y + 0.42, 4.4, 0.4, { fontSize: 13, color: HEX.grey }); y += 1.0; }
    s.addShape(pres.shapes.LINE, { x: 5.9, y: 3.4, w: 1.5, h: 0, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
    text(s, "request", 6.0, 3.0, 1.3, 0.3, { fontSize: 12, color: HEX.grey, align: "center" });
    s.addShape(pres.shapes.LINE, { x: 5.9, y: 4.4, w: 1.5, h: 0, line: { color: HEX.ink, width: 1.25, beginArrowType: "triangle" } });
    text(s, "restriction plan", 5.85, 4.5, 1.6, 0.3, { fontSize: 12, color: HEX.grey, align: "center" });
    text(s, "Kulkarni and Ramanathan, AIES 2025. Proof of concept in Java on the Karnataka At a Glance open government dataset: 30 districts, 17 domains, more than 1,200 attributes.", M, 6.2, W - 2 * M, 0.6, { fontSize: 13, color: HEX.grey });
    notes(s, `GRAILS, by Apurva Kulkarni and Chandrashekar Ramanathan at IIIT Bangalore, AIES 2025. Its main move is to
      keep ethical constraints out of the application's functional code. The Rails decide; the Guard enforces.
      The paper compares the Restriction Planner to a database query planner. This split is the starting point
      for Dagents.`);
  }
  {
    const s = content("GRAILS", "Starting point", "GRAILS picks a transformation from three inputs");
    const inputs = [["Sensitivity", "low · moderate · high", "from metadata and regulations"], ["Trust (KYU score)", "low · moderate · high", "from verified identity, purpose, history"], ["Granularity", "cell · row · column · table", "how much is requested"]];
    let x = M;
    for (const [h, v, b] of inputs) {
      card(s, x, 2.0, 3.7, 1.7);
      text(s, h, x + 0.2, 2.15, 3.3, 0.35, { fontSize: 16, bold: true });
      text(s, v, x + 0.2, 2.6, 3.3, 0.35, { fontSize: 15 });
      text(s, b, x + 0.2, 3.05, 3.3, 0.5, { fontSize: 13, color: HEX.grey });
      x += 4.05;
    }
    text(s, "3 × 3 × 4 = 36 combinations, each mapped to statistical disclosure control techniques:", M, 4.05, W - 2 * M, 0.4, { fontSize: 16, bold: true });
    const techs = ["cell suppression", "top and bottom coding", "microaggregation", "generalisation", "binning", "full and partial masking", "noise injection", "differential privacy"];
    x = M; let y = 4.6;
    for (const t of techs) {
      const w = 0.12 * t.length + 0.5;
      if (x + w > W - M) { x = M; y += 0.55; }
      s.addText(t, { x, y, w, h: 0.4, fontFace: "Calibri", fontSize: 13, color: HEX.ink, fill: { color: HEX.white }, line: { color: HEX.hair, width: 0.75 }, align: "center", valign: "middle", margin: 0, isTextBox: true });
      x += w + 0.2;
    }
    text(s, "A Filtering Score, the mean change per cell between the original and the filtered data, measures how much was filtered.", M, 5.9, W - 2 * M, 0.6, { fontSize: 14, color: HEX.grey });
    notes(s, `The decision is a lookup over three inputs. Trust comes from a Know Your User score, computed with fuzzy
      inference over user attributes. The data protection profile comes from metadata and a knowledge base of
      regulations; the proof of concept uses a fine-tuned language model for it. Strategies come from statistical
      disclosure control.`);
  }
  {
    const s = content("GRAILS", "Starting point", "What GRAILS leaves open for multi-source studies");
    const open = [
      ["O1", "One boundary", "The Guard filters a response. A study also sends metrics, counts and model updates, and releases models."],
      ["O2", "One store", "The Guard reads one Privacy Vault. When each source keeps its records, the Guard must run at each source."],
      ["O3", "Replay", "A profile from a language model can differ between runs or model versions, which is harder to audit."],
      ["O4", "Failure", "The paper does not say what the Guard does when the Rails cannot answer."],
      ["O5", "Utility", "The Filtering Score measures change, not what an analysis or model loses."],
    ];
    let y = 1.95;
    for (const [id, h, b] of open) {
      card(s, M, y, W - 2 * M, 0.85);
      text(s, id, M + 0.2, y + 0.2, 0.6, 0.45, { fontSize: 18, bold: true, fontFace: "Cambria" });
      text(s, h, M + 0.9, y + 0.12, 2.6, 0.6, { fontSize: 16, bold: true, valign: "middle" });
      text(s, b, M + 3.6, y + 0.12, W - 2 * M - 3.8, 0.6, { fontSize: 14, valign: "middle" });
      y += 0.97;
    }
    notes(s, `These are not defects of GRAILS, which addresses one application and one store. GRAILS lists its own
      next steps: other ethical dimensions, learned models for trust and protection levels, and retrieval over
      regulations. These five points are what changes when data sits at several sources and models are trained
      across them. Dagents addresses O1 to O4; the case study measures O5 for one study.`);
  }

  // ------------------------------------------------------------ survey
  await survey.slides(k);

  // ------------------------------------------------------------ design
  pres.addSection({ title: "The Dagents design" });
  {
    const s = pres.addSlide({ masterName: "SECTION_DARK", sectionTitle: "The Dagents design" });
    s.addText("Part 2", { placeholder: "kicker" });
    s.addText("The design idea behind Dagents", { placeholder: "title" });
    s.addText("Decide with pure, typed planners. Enforce at every boundary, at each source. Deny when there is no decision. Leave a record of every decision.", { placeholder: "body" });
    notes(s, `Dagents is an open-source framework; the Python package dagents is on PyPI as an alpha, 0.1.0a1. This part
      states its design as ten principles, each tied to code in the repository.`);
  }
  {
    const s = content("The Dagents design", "Architecture", "A local agent at each source, one coordinator, one planner");
    const sites = [["Hospital A", "524 records"], ["Hospital B", "292 records"], ["Hospital C", "198 records"]];
    let y = 1.95;
    for (const [n, c] of sites) {
      card(s, M, y, 5.4, 1.35);
      text(s, `${n}: LMA and Guard`, M + 0.2, y + 0.12, 3.6, 0.35, { fontSize: 15, bold: true });
      text(s, `own store, own database role (${c})`, M + 0.2, y + 0.5, 3.6, 0.35, { fontSize: 13, color: HEX.grey });
      const gates = ["before_train", "before_read", "before_send"];
      gates.forEach((gname, i) => s.addText(gname, { x: M + 0.2 + i * 1.7, y: y + 0.9, w: 1.55, h: 0.3, fontFace: "Courier New", fontSize: 10, color: HEX.ink, fill: { color: HEX.paper }, line: { color: HEX.hair, width: 0.5 }, align: "center", valign: "middle", margin: 0, isTextBox: true }));
      y += 1.5;
    }
    card(s, 7.3, 1.95, 5.4, 2.4);
    text(s, "GMA: the coordinator", 7.5, 2.07, 5.0, 0.35, { fontSize: 15, bold: true });
    bullets(s, ["Plans rounds: eligibility, quorum", "Sends jobs over each LMA's API", "Combines clipped updates into a candidate", "Releases only after gates, the Guard and a person"], 7.5, 2.5, 5.0, 1.6, { fontSize: 13 });
    s.addText("before_release", { x: 7.5, y: 3.95, w: 1.7, h: 0.3, fontFace: "Courier New", fontSize: 10, color: HEX.ink, fill: { color: HEX.paper }, line: { color: HEX.hair, width: 0.5 }, align: "center", valign: "middle", margin: 0, isTextBox: true });
    card(s, 7.3, 4.6, 5.4, 1.7, { fill: HEX.soft });
    text(s, "dagentsc: the OCaml planners", 7.5, 4.72, 5.0, 0.35, { fontSize: 15, bold: true });
    text(s, "Pure functions over algebraic data types. Called as a separate process with JSON on standard input. No plan means deny.", 7.5, 5.12, 5.0, 1.1, { fontSize: 13 });
    s.addShape(pres.shapes.LINE, { x: 6.1, y: 3.1, w: 1.15, h: 0, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
    text(s, "counts, metrics,\nupdate norm", 6.0, 2.45, 1.35, 0.6, { fontSize: 11, color: HEX.grey, align: "center" });
    notes(s, `One LMA per data source; each keeps its records in its own store and reads it as its own database role.
      The GMA plans rounds and sends jobs through the LMAs' HTTP APIs. Every Guard, at the sites and at the
      coordinator, asks the same planner. The record counts are from the stroke demo's three hospitals.`);
  }
  {
    const s = content("The Dagents design", "Principles", "Ten principles in three groups");
    const groups = [
      ["Where computation runs", [["P1", "Compute at the source"], ["P2", "Delegate the learning protocol"]]],
      ["How decisions are made", [["P3", "Separate deciding from doing"], ["P4", "Make every decision a total, typed function"], ["P5", "Put the planner behind a process boundary"], ["P6", "Deny when there is no decision"], ["P7", "Keep policy as declared data"]]],
      ["What decisions cover and leave behind", [["P8", "Govern every boundary, with one decision model"], ["P9", "Separate a candidate from a release"], ["P10", "Leave evidence with every decision"]]],
    ];
    let x = M;
    const cw = (W - 2 * M - 0.6) / 3;
    for (const [h, ps] of groups) {
      card(s, x, 1.95, cw, 4.75);
      text(s, h, x + 0.22, 2.1, cw - 0.4, 0.7, { fontSize: 16, bold: true, fontFace: "Cambria" });
      let y = 2.95;
      for (const [id, p] of ps) {
        text(s, id, x + 0.22, y, 0.6, 0.35, { fontSize: 14, bold: true });
        text(s, p, x + 0.8, y, cw - 1.0, 0.65, { fontSize: 14 });
        y += 0.72;
      }
      x += cw + 0.3;
    }
    notes(s, `The ten principles. The paper ties each to code. P3 is the GRAILS split. P4 to P7 are about how the
      decision is made: total, typed, isolated, failing closed, with policy as data. P8 to P10 extend GRAILS
      from one boundary to the whole study.`);
  }
  {
    const s = content("The Dagents design", "P8", "Four boundaries, one decision model");
    const steps = [["Accept the job", "before_train", "LMA"], ["Read local data", "before_read", "LMA"], ["Train or compute", "", ""], ["Send the result", "before_send", "LMA"], ["Release a candidate", "before_release", "GMA"]];
    const bw = 2.2; const gap = (W - 2 * M - 5 * bw) / 4;
    steps.forEach(([h, gname, who], i) => {
      const x = M + i * (bw + gap);
      card(s, x, 2.1, bw, 0.9, { fill: HEX.white });
      text(s, h, x, 2.1, bw, 0.9, { fontSize: 15, bold: true, align: "center", valign: "middle" });
      if (i < 4) s.addShape(pres.shapes.LINE, { x: x + bw + 0.05, y: 2.55, w: gap - 0.1, h: 0, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
      if (gname) {
        s.addText(gname, { x, y: 3.2, w: bw, h: 0.36, fontFace: "Courier New", fontSize: 12, bold: true, color: HEX.white, fill: { color: HEX.ink }, align: "center", valign: "middle", margin: 0, isTextBox: true });
        text(s, who, x, 3.62, bw, 0.3, { fontSize: 12, color: HEX.grey, align: "center" });
      }
    });
    card(s, M, 4.25, 5.9, 2.35);
    text(s, "A fifth granularity: the model update", M + 0.22, 4.4, 5.5, 0.4, { fontSize: 16, bold: true });
    text(s, "GRAILS covers cell, row, column and table. Dagents adds the model update, whose strategies are clipping to a norm bound and adding noise. An update always gets some protection, because it is derived from the records.", M + 0.22, 4.85, 5.5, 1.7, { fontSize: 14 });
    card(s, 6.83, 4.25, 5.9, 2.35);
    text(s, "Two checks before any field is looked up", 7.05, 4.4, 5.5, 0.4, { fontSize: 16, bold: true });
    bullets(s, ["The declared purpose must be an approved one", "Column, table and model-update requests must cover at least the minimum cohort (20 in the demo)", "If either fails, every field is refused"], 7.05, 4.85, 5.5, 1.7, { fontSize: 14 });
    notes(s, `The same planner and Guard serve four boundaries. The job at a site runs on the guarded data, not the raw
      records. The update vector never appears in a site's result: before_send clips it and sends it to a
      write-only store in the GMA; the result carries only the update's norm and digest.`);
  }
  {
    const s = content("The Dagents design", "P4", "The rule table is a total function");
    const code = [
      "let select_strategy sensitivity trust granularity minimum_cohort =",
      "  match (sensitivity, trust, granularity) with",
      "  | HighSensitivity, LowTrust,",
      "      (CellGrain | RowGrain | ColumnGrain",
      "       | TableGrain | ModelUpdateGrain) -> Refuse",
      "  | HighSensitivity, ModerateTrust,",
      "      (CellGrain | RowGrain) -> Redact",
      "  | HighSensitivity, ModerateTrust,",
      "      (ColumnGrain | TableGrain) -> AggregateOnly minimum_cohort",
      "  (* ... 23 more arms ... *)",
      "  | LowSensitivity, HighTrust, ModelUpdateGrain ->",
      "      ClipContribution clip_bound",
    ].join("\n");
    s.addText(code, { x: M, y: 2.0, w: 7.4, h: 3.4, fontFace: "Courier New", fontSize: 11, color: HEX.ink, fill: { color: HEX.white }, line: { color: HEX.hair, width: 0.75 }, valign: "top", margin: 10, isTextBox: true });
    const stats = [["45", "combinations of sensitivity, trust and granularity"], ["27", "match arms"], ["0", "wildcards: a new level fails to compile until handled"]];
    let y = 2.0;
    for (const [n, l] of stats) {
      text(s, n, 8.4, y, 1.4, 0.9, { fontSize: 44, fontFace: "Cambria", bold: true, valign: "middle" });
      text(s, l, 9.85, y + 0.08, 2.9, 0.8, { fontSize: 14, color: HEX.grey, valign: "middle" });
      y += 1.15;
    }
    text(s, "Tested: more trust never means stricter protection; a model update is never released raw; high sensitivity with low trust is refused at every granularity. No input or output, clock or randomness, so a plan can be computed again from its request during an audit.", M, 5.65, W - 2 * M, 1.0, { fontSize: 14 });
    notes(s, `This is the governance planner's strategy table, in OCaml. Because there is no wildcard, adding a
      sensitivity level or a granularity makes the match non-exhaustive, and the development build that CI runs on
      every change treats that warning as an error, so the change cannot pass until every combination has a strategy. The table
      is also monotone in sensitivity; we checked that with a copy of the table, and the repository has no test
      for that direction yet.`);
  }
  {
    const s = content("The Dagents design", "P4 and P8", "The whole table: one strategy per case");
    const sens = [["High", "H"], ["Medium", "M"], ["Low", "L"]];
    const trusts = [["Low", "L"], ["Moderate", "M"], ["High", "H"]];
    const grains = ["cell", "row", "column", "table", "update"];
    const T = {
      HL: ["refuse", "refuse", "refuse", "refuse", "refuse"], HM: ["redact", "redact", "agg", "agg", "noise"], HH: ["gen", "gen", "agg", "agg", "clip"],
      ML: ["redact", "redact", "agg", "refuse", "refuse"], MM: ["gen", "gen", "agg", "agg", "clip"], MH: ["allow", "allow", "gen", "agg", "clip"],
      LL: ["allow", "allow", "gen", "agg", "noise"], LM: ["allow", "allow", "allow", "gen", "clip"], LH: ["allow", "allow", "allow", "allow", "clip"],
    };
    const kind = (v) => (v === "allow" ? "permit" : v === "refuse" ? "deny" : "narrow");
    const cw = 0.66, ch = 0.36, x0 = M + 1.25, y0 = 2.35;
    trusts.forEach(([t], ti) => {
      const bx = x0 + ti * (5 * cw + 0.3);
      text(s, `${t} trust`, bx, 1.85, 5 * cw, 0.3, { fontSize: 14, bold: true, align: "center" });
      grains.forEach((g, gi) => text(s, g, bx + gi * cw, 2.12, cw, 0.22, { fontSize: 10, color: HEX.grey, align: "center" }));
      sens.forEach(([sn, sk], si) => {
        if (ti === 0) text(s, `${sn}`, M, y0 + si * (ch + 0.08), 1.2, ch, { fontSize: 13, bold: true, valign: "middle" });
        T[sk + trusts[ti][1]].forEach((v, gi) => {
          s.addText(v, { x: bx + gi * cw + 0.02, y: y0 + si * (ch + 0.08), w: cw - 0.04, h: ch, fontFace: "Calibri", fontSize: 11, bold: true, color: HEX.white, fill: { color: k.HEX[kind(v)] }, align: "center", valign: "middle", margin: 0, isTextBox: true });
        });
      });
    });
    text(s, "Rows: sensitivity of the field. Colour is the decision class only: green returns the field unchanged, amber narrows it, red withholds it.", M, 3.85, W - 2 * M, 0.5, { fontSize: 13, color: HEX.grey });
    table(s, ["Strategy", "Effect"], [
      ["allow", "returns the field unchanged"], ["gen", "generalises it (level 1 rounds a number to the nearest 10)"], ["redact", "removes the field"],
      ["agg", "returns only aggregates over at least the minimum cohort"], ["clip / noise", "bounds an update's norm to 1.0 / adds Gaussian noise of scale 0.5"], ["refuse", "withholds the field"],
    ], M, 4.4, W - 2 * M, [2.0, W - 2 * M - 2.0], { fontSize: 12, rowH: 0.3 });
    notes(s, `All 45 cases. Two differences from GRAILS: high sensitivity with low trust is refused at every
      granularity, where GRAILS returns masked data; and column and table requests return only aggregates over
      the minimum cohort. The update column is new. The paper's appendix sets the two tables side by side.`);
  }
  {
    const s = content("The Dagents design", "P5, P6, P10", "No plan means deny, and every decision leaves a record");
    card(s, M, 1.95, 5.6, 4.7);
    text(s, "The Guard denies, and records the denial, when the planner", M + 0.22, 2.1, 5.2, 0.7, { fontSize: 16, bold: true });
    const fails = ["cannot be started", "runs past 10 seconds or 16 MiB of input or output", "prints something that is not a plan", "returns a strategy the Guard cannot apply", "gives a plan that fails on the records"];
    let y = 2.95;
    for (const f of fails) { pill(s, "deny", "deny", M + 0.22, y, 0.8, 0.32); text(s, f, M + 1.2, y, 4.2, 0.6, { fontSize: 14 }); y += 0.65; }
    card(s, 6.53, 1.95, 6.2, 4.7);
    text(s, "One audit record per decision", 6.75, 2.1, 5.8, 0.4, { fontSize: 16, bold: true });
    table(s, ["Field", "Holds"], [
      ["request, boundary", "who asked, for what, at which boundary"],
      ["decision, strategies", "permit, narrow or deny; one strategy per field"],
      ["obligations", "what the Guard must do, such as record accounting"],
      ["planner_version, planner_sha256", "the planner that decided"],
      ["previous_digest, digest", "SHA-256 chain: an edit breaks every later digest"],
    ], 6.75, 2.6, 5.8, [2.4, 3.4], { fontSize: 12, rowH: 0.55 });
    text(s, "Noise is seeded from the request so an auditor can reproduce it. The coordinator can too; see the limits.", 6.75, 5.95, 5.8, 0.5, { fontSize: 13, color: HEX.grey });
    notes(s, `The planner runs as a separate process, dagentsc, with fixed limits; the runner kills it with its process
      group if it exceeds them. An operator can pin the planner binary's SHA-256. Whatever goes wrong, the answer
      is a recorded denial. The chain is tamper-evident, not tamper-proof: production needs append-only storage
      and signing.`);
  }
  {
    const s = content("The Dagents design", "P9", "A candidate is not a release");
    const st = [["candidate", "narrow"], ["released", "permit"], ["rejected", "deny"], ["superseded", null]];
    const sx = [M, 4.9, 4.9, 9.2];
    const sy = [2.6, 2.0, 3.6, 2.0];
    st.forEach(([n, kd], i) => {
      s.addText(n, { x: sx[i], y: sy[i], w: 2.6, h: 0.7, fontFace: "Calibri", fontSize: 16, bold: true, color: kd ? HEX.white : HEX.ink, fill: { color: kd ? k.HEX[kd] : HEX.white }, line: { color: kd ? k.HEX[kd] : HEX.hair, width: 0.75 }, align: "center", valign: "middle", margin: 0, isTextBox: true });
    });
    s.addShape(pres.shapes.LINE, { x: M + 2.65, y: 2.8, w: 1.6, h: -0.4, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
    s.addShape(pres.shapes.LINE, { x: M + 2.65, y: 3.1, w: 1.6, h: 0.75, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
    s.addShape(pres.shapes.LINE, { x: 7.55, y: 2.35, w: 1.6, h: 0, line: { color: HEX.ink, width: 1.25, endArrowType: "triangle" } });
    text(s, "the previous release, when a new one is approved", 7.4, 2.75, 3.0, 0.6, { fontSize: 12, color: HEX.grey });
    bullets(s, [
      "Aggregation produces a candidate, never a release",
      "A release gate whose metric is missing blocks the release",
      "Approval releases a candidate only if the stored decision says release, the Guard permitted it, its metrics came from the study's rounds, and the model it was compared with is still in use",
      "Every status change comes from the planner; at most one version is released",
    ], M, 4.65, W - 2 * M, 2.1, { fontSize: 14 });
    notes(s, `The model registry applies the planner's lifecycle answer and keeps no status rule of its own. The round
      controller also refuses a reused round id, a result from a site the plan did not select, and a second
      aggregation of the same round.`);
  }

  // ------------------------------------------------------------ case study
  pres.addSection({ title: "Case study and demo" });
  {
    const s = pres.addSlide({ masterName: "SECTION_DARK", sectionTitle: "Case study and demo" });
    s.addText("Part 3", { placeholder: "kicker" });
    s.addText("Stroke triage across three hospitals", { placeholder: "title" });
    s.addText("Real, de-identified records from the International Stroke Trial, one store per hospital, one LMA per hospital and one GMA. The models are demonstrations, not validated clinical models.", { placeholder: "body" });
    notes(s, `The records are from the International Stroke Trial (IST-1, 1991 to 1996), made available under the Open Data
      Commons Attribution License. Credit: the International Stroke Trial Collaborative Group; Sandercock,
      Niewada and Czlonkowska, Trials 2011.`);
  }
  {
    const s = content("Case study and demo", "Setting", "Three hospitals keep their records; one study runs across them");
    const stats = [["1,014", "patients"], ["524 · 292 · 198", "United Kingdom · Italy · Switzerland"], ["13", "agreed fields, plus the six-month outcome"], ["20", "minimum cohort for any aggregate"]];
    let x = M;
    const sw = (W - 2 * M - 0.9) / 4;
    for (const [n, l] of stats) {
      card(s, x, 1.95, sw, 1.65);
      text(s, n, x + 0.2, 2.05, sw - 0.4, 0.85, { fontSize: n.length > 6 ? 26 : 40, fontFace: "Cambria", bold: true, valign: "middle" });
      text(s, l, x + 0.2, 2.9, sw - 0.4, 0.65, { fontSize: 13, color: HEX.grey });
      x += sw + 0.3;
    }
    text(s, "Eight rounds, each sent by the GMA to every hospital's LMA", M, 3.95, W - 2 * M, 0.4, { fontSize: 16, bold: true });
    const rounds = ["analytics", "evaluate the model in use", "train 1", "train 2", "train 3", "train 4", "train 5", "validate the candidate"];
    const rw = (W - 2 * M - 7 * 0.12) / 8;
    rounds.forEach((r, i) => {
      s.addText(r, { x: M + i * (rw + 0.12), y: 4.5, w: rw, h: 0.75, fontFace: "Calibri", fontSize: 12, color: HEX.ink, fill: { color: HEX.white }, line: { color: HEX.hair, width: 0.75 }, align: "center", valign: "middle", margin: 2, isTextBox: true });
    });
    text(s, "Five training rounds lower the candidate's loss from 0.693 to 0.496. The application contributes one 136-line extension file: feature contract, classification, condition pack, two model kinds.", M, 5.55, W - 2 * M, 0.9, { fontSize: 14 });
    notes(s, `The task is to flag, at arrival, patients at high risk of death or dependency six months later. The
      classification marks four fields high (hours since onset, systolic blood pressure, atrial fibrillation and
      the outcome), nine medium and one low, and the model update high.`);
  }
  {
    const s = content("Case study and demo", "Governance", "The Guard's decisions at one hospital");
    const rows = [
      ["Verified, row", "generalize:1", "allow_full", "allow_full", "narrow"],
      ["Unverified, row", "redact", "generalize:1", "allow_full", "narrow"],
      ["Verified, table, 25 subjects", "aggregate_only:20", "aggregate_only:20", "allow_full", "narrow"],
      ["Verified, table, 5 subjects", "refuse", "refuse", "refuse", "deny"],
      ["Verified, model update", "clip_contribution:1", "clip_contribution:1", "clip_contribution:1", "narrow"],
    ];
    const head = ["Request", "systolic_bp (high)", "age_band (medium)", "onset_on_waking (low)", "Decision"];
    const body = rows.map((r) => r.map((c, i) => (i === 4 ? { text: c, options: { bold: true, color: HEX.white, fill: { color: k.HEX[c] }, align: "center" } } : i === 0 ? c : { text: c, options: { fontFace: "Courier New", fontSize: 12 } })));
    table(s, head, body, M, 2.0, W - 2 * M, [3.1, 2.55, 2.55, 2.55, 1.383], { fontSize: 13, rowH: 0.5 });
    text(s, "The verified requester scores 0.97 on the Know Your User scale (high trust); the unverified one scores 0.445 (moderate). The table request for 5 subjects is denied because 5 is below the minimum cohort of 20.", M, 5.3, W - 2 * M, 0.9, { fontSize: 14 });
    notes(s, `These are real answers from the demo's governance endpoint at one hospital, which asks that hospital's LMA
      to run its Guard. In the live demo, the Ethical Guard panel has controls for Requester verified, the
      granularity and the Cohort size; select Ask the guard to see each decision change.`);
  }
  await survey.caseStudy(k);
}

module.exports = { build };
