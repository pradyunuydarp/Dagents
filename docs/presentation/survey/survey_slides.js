// Slides whose content comes from the survey, and the closing slides.
// The family findings mirror section 4 of the paper; keep the two in step.

const FAMILIES = require("./families.js");

async function rules(k) {
  const { content, table, text, notes, M, W, HEX } = k;
  const s = content("The question", "The rules", "The rules behind the answers apply per request or per release");
  table(s, ["Requirement", "Where it is written", "When it is checked"], [
    ["Purpose limitation and data minimisation", "GDPR, Article 5(1)(b) and (c)", "each use of personal data"],
    ["Minimum necessary use and disclosure", "HIPAA Privacy Rule, 45 CFR 164.502(b) and 164.514(d)", "each use or disclosure"],
    ["Data protection by design and by default", "GDPR, Article 25", "design, and each processing default"],
    ["Automatic logs of a high-risk AI system", "EU AI Act, Article 12", "over the system's lifetime"],
    ["Human oversight of a high-risk AI system", "EU AI Act, Article 14", "each use the system supports"],
    ["Planned, controlled changes to a learning device", "FDA guidance on Predetermined Change Control Plans (December 2024)", "each model change"],
  ], M, 1.95, W - 2 * M, [4.1, 5.0, W - 2 * M - 9.1], { fontSize: 13, rowH: 0.55 });
  text(s, "Design-time methods and audits help, but a rule checked per request needs a decision made per request, and a record of it.", M, 5.95, W - 2 * M, 0.6, { fontSize: 14, color: HEX.grey });
  notes(s, `Exact provisions are in the paper's references. These are the requirements a governance layer has to turn
    into checks. Most of them apply each time data is used or a model changes, not once at design time.`);
}

async function slides(k) {
  const { pres, content, card, text, bullets, notes, table, M, W, HEX } = k;
  pres.addSection({ title: "The survey" });
  {
    const s = pres.addSlide({ masterName: "SECTION_DARK", sectionTitle: "The survey" });
    s.addText("Part 1", { placeholder: "kicker" });
    s.addText("Where do existing approaches decide, and where do they enforce?", { placeholder: "title" });
    s.addText(`${FAMILIES.total} works and industry systems in seven families, each described by the same four questions.`, { placeholder: "body" });
    notes(s, `A structured narrative review, not a systematic review. Every academic reference was checked against
      Crossref or arXiv; products are described from their documentation as of November 2026.`);
  }
  {
    const s = content("The survey", "Method", "One lens: four questions about every approach");
    const qs = [
      ["Where is it decided?", "In application code, configuration, a policy language, a person, a learned model, or a typed function."],
      ["What does it decide?", "Permit or deny, a transformation, a privacy mechanism, who may take part, or whether to release."],
      ["Where is it enforced?", "At a central store, at each source, where data leaves, or at model release."],
      ["What evidence remains?", "Audit logs, attestations, documentation, provenance. Can the decision be replayed?"],
    ];
    const cw = (W - 2 * M - 0.9) / 4;
    qs.forEach(([h, b], i) => {
      const x = M + i * (cw + 0.3);
      card(s, x, 1.95, cw, 3.2);
      text(s, String(i + 1), x + 0.22, 2.05, 0.6, 0.7, { fontSize: 36, fontFace: "Cambria", bold: true });
      text(s, h, x + 0.22, 2.8, cw - 0.4, 0.7, { fontSize: 16, bold: true });
      text(s, b, x + 0.22, 3.5, cw - 0.4, 1.5, { fontSize: 13, color: HEX.grey });
    });
    text(s, "We also record what happens when the decision point cannot answer (deny or allow), and what the approach is tied to: a domain, application code, or a vendor.", M, 5.5, W - 2 * M, 0.8, { fontSize: 14 });
    notes(s, `These are dimensions D1 to D7 in the paper. They let us compare a fairness toolkit, a policy engine,
      a federated learning framework and a regulation on the same terms.`);
  }
  {
    const s = content("The survey", "Families", "Seven families of related work");
    const cw = (W - 2 * M - 0.9) / 4;
    FAMILIES.list.forEach((f, i) => {
      const col = i % 4, row = Math.floor(i / 4);
      const x = M + col * (cw + 0.3), y = 1.95 + row * 2.45;
      card(s, x, y, cw, 2.2);
      text(s, `F${i + 1}`, x + 0.2, y + 0.12, 0.8, 0.4, { fontSize: 14, bold: true, color: HEX.grey });
      text(s, f.name, x + 0.2, y + 0.45, cw - 0.4, 0.7, { fontSize: 15, bold: true });
      text(s, f.examples, x + 0.2, y + 1.15, cw - 0.4, 1.0, { fontSize: 12, color: HEX.grey });
    });
    const x = M + 3 * (cw + 0.3), y = 1.95 + 2.45;
    text(s, `${FAMILIES.total}`, x + 0.2, y + 0.1, cw - 0.4, 1.0, { fontSize: 44, fontFace: "Cambria", bold: true });
    text(s, "works and systems, each checked against Crossref, arXiv or the vendor's documentation", x + 0.2, y + 1.1, cw - 0.4, 1.0, { fontSize: 13, color: HEX.grey });
    notes(s, `The families are grouped by the problem each was built to solve. The next seven slides give one finding
      per family: what it decides, where it enforces, what it does well, and what it leaves to others.`);
  }
  for (const f of FAMILIES.list) {
    const s = content("The survey", `F${FAMILIES.list.indexOf(f) + 1} · ${f.short}`, f.finding);
    card(s, M, 1.95, 7.4, 4.75);
    text(s, "What we found", M + 0.22, 2.1, 7.0, 0.4, { fontSize: 16, bold: true });
    bullets(s, f.points, M + 0.22, 2.6, 7.0, 4.0, { fontSize: 16 });
    card(s, 8.33, 1.95, 4.4, 2.25, { fill: HEX.white });
    text(s, "Closest to Dagents", 8.55, 2.1, 4.0, 0.4, { fontSize: 15, bold: true });
    text(s, f.closest, 8.55, 2.5, 4.0, 1.65, { fontSize: 14 });
    card(s, 8.33, 4.45, 4.4, 2.25, { fill: HEX.white });
    text(s, "Stronger than Dagents at", 8.55, 4.6, 4.0, 0.4, { fontSize: 15, bold: true });
    text(s, f.stronger, 8.55, 5.0, 4.0, 1.65, { fontSize: 14 });
    notes(s, f.notes);
  }
  {
    const s = content("The survey", "Synthesis", "Where each family decides and enforces");
    const stages = ["Read", "Compute or train", "Send a result", "Release a model", "Leaves evidence"];
    const rows = FAMILIES.matrix.map((r) => [
      { text: r[0], options: { bold: r[0] === "Dagents" || r[0] === "GRAILS" } },
      ...r.slice(1).map((c) => ({ text: c, options: { align: "center", fontSize: 12, color: c === "—" ? HEX.grey : HEX.ink } })),
    ]);
    table(s, ["Family", ...stages], rows, M, 1.9, W - 2 * M, [3.4, ...stages.map(() => (W - 2 * M - 3.4) / stages.length)], { fontSize: 12, rowH: 0.42 });
    text(s, "by hand: a person decides · central: in one engine that holds the data · —: the family does not usually cover the stage", M, 6.35, W - 2 * M, 0.4, { fontSize: 12, color: HEX.grey });
    notes(s, FAMILIES.matrixNotes);
  }
  {
    const s = content("The survey", "Synthesis", "Five gaps across the families");
    FAMILIES.gaps.forEach(([id, h, b], i) => {
      const y = 1.95 + i * 0.95;
      card(s, M, y, W - 2 * M, 0.82);
      text(s, id, M + 0.2, y + 0.18, 0.7, 0.45, { fontSize: 18, bold: true, fontFace: "Cambria" });
      text(s, h, M + 0.95, y + 0.1, 3.6, 0.62, { fontSize: 15, bold: true, valign: "middle" });
      text(s, b, M + 4.7, y + 0.1, W - 2 * M - 4.9, 0.62, { fontSize: 13, valign: "middle" });
    });
    notes(s, FAMILIES.gapNotes);
  }
}

async function caseStudy(k) {
  const { pres, content, card, text, bullets, notes, table, pill, M, W, HEX, C } = k;
  {
    const s = content("Case study and demo", "Release", "The release gates rejected a better model");
    const gates = [
      ["discrimination", "AUC 0.786", "at least 0.7", "pass"],
      ["improves on the model in use", "AUC 0.786", "at least 0.777 + 0.02", "fail"],
      ["subgroup fairness", "AUC gap 0.022", "at most 0.1", "pass"],
      ["sensitivity at the alert threshold", "0.922", "at least 0.8", "pass"],
      ["alert burden", "853 per 1,000", "at most 750", "fail"],
    ];
    const body = gates.map((g) => [g[0], g[1], g[2], { text: g[3], options: { bold: true, color: HEX.white, fill: { color: g[3] === "pass" ? HEX.permit : HEX.deny }, align: "center" } }]);
    table(s, ["Gate", "Candidate", "Required", "Result"], body, M, 1.95, 8.2, [3.4, 1.7, 2.0, 1.1], { fontSize: 13, rowH: 0.5 });
    card(s, 9.2, 1.95, 3.53, 3.1);
    text(s, "Decision", 9.42, 2.1, 3.1, 0.4, { fontSize: 15, bold: true });
    pill(s, "reject", "deny", 9.42, 2.6, 1.3, 0.4);
    text(s, "The hospitals keep the model in use. The candidate is better, but not by the 0.02 margin the consortium set.", 9.42, 3.15, 3.1, 1.8, { fontSize: 13 });
    text(s, "The run has no randomness, so it repeats exactly on the committed data. The candidate is trained and measured on the same hospitals, which raises its AUC by about 0.02 compared with unseen data.", M, 5.35, W - 2 * M, 0.9, { fontSize: 14, color: HEX.grey });
    notes(s, `Five gates; one blocking failure is enough to reject. A gate whose metric is missing would also block.
      The comments next to each gate in the demo's conditions.py give the reason for its threshold.`);
  }
  {
    const s = content("Case study and demo", "Cost", "In this study the Guard cost 0.0005 AUC");
    s.addChart(pres.charts.BAR, [
      { name: "Scored on exact records", labels: ["United Kingdom (524)", "Italy (292)", "Switzerland (198)"], values: [0.7594, 0.8117, 0.8185] },
      { name: "Scored on guarded records", labels: ["United Kingdom (524)", "Italy (292)", "Switzerland (198)"], values: [0.7593, 0.8115, 0.8184] },
    ], {
      x: M, y: 1.9, w: 7.6, h: 4.6, barDir: "col", barGapWidthPct: 60,
      chartColors: [HEX.ink, "8C959F"], showLegend: true, legendPos: "b", legendFontSize: 12, legendFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "0.0000", dataLabelFontSize: 11, dataLabelFontFace: "+mn-lt", dataLabelColor: HEX.ink,
      valAxisMinVal: 0.7, valAxisMaxVal: 0.85, valAxisLabelFormatCode: "0.00", valAxisLabelFontSize: 11, catAxisLabelFontSize: 12,
      valAxisLabelFontFace: "+mn-lt", catAxisLabelFontFace: "+mn-lt", catAxisLabelColor: HEX.grey, valAxisLabelColor: HEX.grey,
      valGridLine: { color: "D5D9DD", size: 0.5 }, catGridLine: { style: "none" },
      showTitle: true, title: "The same candidate at each hospital", titleFontSize: 14, titleFontFace: "+mn-lt", titleColor: HEX.ink,
    });
    card(s, 8.6, 1.95, 4.13, 2.3);
    text(s, "0.7864 → 0.7859", 8.82, 2.1, 3.7, 0.8, { fontSize: 28, fontFace: "Cambria", bold: true, valign: "middle" });
    text(s, "size-weighted AUC of a candidate trained on exact values → trained on guarded values", 8.82, 2.95, 3.7, 0.8, { fontSize: 13, color: HEX.grey });
    text(s, "The Guard generalises hours since onset and systolic blood pressure to the nearest 10 when a site reads its records, so the round's code never sees exact values. To change the cost, a consortium changes the classification, not the code.", 8.6, 4.5, 4.13, 2.0, { fontSize: 13 });
    notes(s, `Two measurements. Training: we ran the pilot twice, once as committed and once with the two fields lowered to
      medium sensitivity so the Guard returns them unchanged; the candidates reach 0.7864 and 0.7859. Scoring: the
      chart shows the guarded candidate scored on each hospital's exact and guarded records. This answers open
      point O5 for one study only: one model, one dataset, run by us. The value axis starts at 0.70 so the bars
      can be read; the differences are in the fourth decimal place.`);
  }
  {
    const s = content("Case study and demo", "Demo", "Demo: what to show");
    const steps = [
      ["Build the planner and start the demo", "cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe\napps/healthcare-demo/scripts/run_frontend_demo.sh"],
      ["Ask the Guard", "Choose a hospital, change Requester verified, the granularity and the Cohort size, then select Ask the guard. Each answer names the LMA route that decided."],
      ["Run the pilot", "Select Run the pilot: eight rounds, then the release gates and the decision."],
      ["Open a hospital view", "See that hospital's records, its charts and the counts it reports."],
      ["Read the API calls", "Each panel's Dagents API calls table names the agent and route behind the answer."],
    ];
    steps.forEach(([h, b], i) => {
      const y = 1.95 + i * 0.95;
      text(s, String(i + 1), M, y, 0.5, 0.6, { fontSize: 24, fontFace: "Cambria", bold: true });
      text(s, h, M + 0.6, y, 3.6, 0.8, { fontSize: 15, bold: true });
      text(s, b, M + 4.3, y, W - 2 * M - 4.3, 0.85, { fontSize: i === 0 ? 12 : 13, fontFace: i === 0 ? "Courier New" : "Calibri" });
    });
    text(s, "Published demo: pradyunuydarp.github.io/Dagents/healthcare-demo/ (the first request after an idle period takes about 20 seconds)", M, 6.55, W - 2 * M, 0.35, { fontSize: 12, color: HEX.grey, hyperlink: { url: "https://pradyunuydarp.github.io/Dagents/healthcare-demo/" } });
    notes(s, `A local run needs OCaml with opam, Node.js 22 and a Python virtual environment; step 5 of the learning guide
      covers the setup. Without the planner the Guard denies every request, which is correct but makes every
      panel a denial, so the script checks for it first.`);
  }

  pres.addSection({ title: "How Dagents compares" });
  {
    const s = content("How Dagents compares", "GRAILS", "Dagents next to GRAILS");
    table(s, ["", "GRAILS", "Dagents"], [
      ["Inputs", "sensitivity × trust × granularity", "the same, plus approved purpose and minimum cohort as request-level checks"],
      ["Granularities", "cell, row, column, table", "the same, plus model update"],
      ["Strategies", "one or more SDC techniques per case", "exactly one of seven strategies per case"],
      ["Trust", "fuzzy inference over user attributes", "0.7 × verified share + 0.3 × compliance history"],
      ["Protection profile", "language model fine-tuned on regulations", "a classification the application declares"],
      ["Where data is", "one Privacy Vault", "each source's own store"],
      ["Boundaries", "the response to a request", "before_train, before_read, before_send, before_release"],
      ["Planner", "Java, in process, design patterns", "OCaml, separate process, total match"],
      ["Planner fails", "not stated", "deny, and record the denial"],
      ["Evidence", "Filtering Score; plan stored in a rule base", "filtering score; hash-chained record naming the planner"],
      ["Evaluation", "open government data, Filtering Score", "1,014 trial records, AUC cost and release gates"],
    ], M, 1.85, W - 2 * M, [2.4, 4.4, W - 2 * M - 6.8], { fontSize: 11, rowH: 0.37 });
    notes(s, `Dagents keeps the GRAILS split and its three inputs. It replaces the learned parts with declared data and a
      fixed formula so that a plan can be computed again from its request, and it moves the Guard to each source
      and to every boundary.`);
  }
  {
    const s = content("How Dagents compares", "Families", "What Dagents takes from each family, and where each is stronger");
    table(s, ["Family", "Dagents takes", "Dagents differs", "The family is stronger at"], FAMILIES.compare, M, 1.85, W - 2 * M, [2.6, 3.3, 3.3, W - 2 * M - 9.2], { fontSize: 11, rowH: 0.6 });
    notes(s, FAMILIES.compareNotes);
  }
  {
    const s = content("How Dagents compares", "Limits", "What this work does not show");
    card(s, M, 1.95, 6.0, 4.75);
    text(s, "Limits of the design", M + 0.22, 2.1, 5.6, 0.4, { fontSize: 16, bold: true });
    bullets(s, [
      "A strategy is not a privacy proof: clipping and noise are not differential privacy without an accountant and a budget",
      "The table encodes a judgement; typing makes it complete, a consortium must still review it",
      "Noise is seeded from the request id, which the coordinator knows for an update, so it can remove the noise",
      "Approval records no named approver; decisions are made one request at a time, with exact-count thresholds",
      "Trust is computed from attributes the caller supplies",
      "Digests and the hash chain detect tampering; they are not signatures or an append-only store",
    ], M + 0.22, 2.6, 5.6, 4.0, { fontSize: 14 });
    card(s, 6.73, 1.95, 6.0, 4.75);
    text(s, "Limits of the evidence", 6.95, 2.1, 5.6, 0.4, { fontSize: 16, bold: true });
    bullets(s, [
      "One study, one model, one dataset, run by the authors; records from 1991 to 1996",
      "No measurement yet of planner latency at scale or of adoption effort",
      "No independent deployment; the comparison is an analysis of designs",
      "The survey is a narrative review by the authors of one of the systems it compares",
    ], 6.95, 2.6, 5.6, 4.0, { fontSize: 14 });
    notes(s, `These are stated once in the paper's limitations section. Architecture is not compliance: regulations also
      require administrative and physical safeguards and independent audits.`);
  }
  {
    const s = pres.addSlide({ masterName: "SECTION_DARK", sectionTitle: "How Dagents compares" });
    s.addText("Summary", { placeholder: "kicker" });
    s.addText("Decisions in one typed planner; enforcement at every boundary, at each source", { placeholder: "title" });
    s.addText([
      { text: "Next: a privacy accountant, a site secret in the noise seed, named approvers, signed records, a test of monotonicity in sensitivity, and a study with an independent team.", options: { breakLine: true } },
      { text: " ", options: { breakLine: true } },
      { text: "Paper: docs/reports/dagents_survey_paper.pdf · Framework site: pradyunuydarp.github.io/Dagents" },
    ], { placeholder: "body" });
    notes(s, `Three points to leave with. First, most approaches cover one part of a multi-source study: one decision,
      one boundary, or one stage. Second, GRAILS showed that the decision can be separated from the application;
      Dagents applies that separation to every boundary of a study and makes the decision a total, typed function
      that fails closed and leaves a record. Third, in one study the cost was small, and the limits are stated.`);
  }
}

module.exports = { rules, slides, caseStudy };
