# Family `rai`: turning responsible-AI requirements into software practice

Scope: how ethics and responsible-AI requirements are put into practice in software,
and where that has fallen short. Searched 2026-11-16. Metadata of every academic work
was checked with Crossref or arXiv. Product pages could not be opened from this
machine (hosts blocked), so their content comes from web search results and is marked
as such. The web search budget ran out late in the session; items that needed a further
search are in **Unverified**.

## Synthesis

**Shared assumption.** Most of this literature treats ethics as a requirement that
people interpret and carry through an organisation. The principle surveys show
broad agreement on a few values: Jobin et al. found five principles that recur across
84 guideline documents. They also show wide disagreement about how to apply them.
Mittelstadt and Hagendorff note that principles have no enforcement.
Empirical studies from 2021 to 2026 report the same gap inside firms: launch pressure,
metrics that ethics cannot easily meet, and personal risk for the people who raise
issues (Ali et al. 2023). Developers value guidelines more than they follow them
(Ximenes et al. 2026).

**Where decisions live.** Most approaches leave the decision with a person, at a point
in time:

- Value Sensitive Design, Privacy by Design and Ethics by Design act at design time.
- Data protection by design (GDPR Art. 25) is a legal duty on the controller to choose
  measures.
- Model cards, datasheets, data statements and system cards record decisions after they
  are made.
- Audits (SMACTR, external audits) and impact assessments are human reviews before
  release or after the fact.
- Fairness toolkits (AIF360, Fairlearn, What-If Tool) compute metrics and offer
  mitigations during development. A data scientist decides which to apply.
- Industry governance platforms (Microsoft's standard, IBM watsonx.governance, Credo AI,
  Holistic AI, NIST AI RMF) organise all of the above as an inventory, risk tiers,
  evidence and approvals.

**Where enforcement happens.** Per-request enforcement appears only in the runtime
"guardrail" strand. Most guardrails sit at the input and output of a large language
model (LLM): NeMo Guardrails, Llama Guard, ShieldGemma, Granite Guardian, Guardrails
AI and IBM's HAP/PII detectors. Newer agent work enforces at tool-call boundaries:
CaMeL attaches capabilities to values and checks Python policy functions; AgentSpec
checks rules written in a small domain-specific language (DSL). GRAILS is the one
surveyed design that enforces per request on structured data. It chooses a
transformation per data item from sensitivity, user trust and granularity. Its
enforcement sits at one application's data-access layer, in front of a "Privacy Vault".

**What the family does well.** It covers what a runtime cannot:

- stakeholder values and harms (VSD, impact assessments);
- fairness measurement and mitigation, which neither GRAILS nor Dagents performs;
- documentation that travels with a model or dataset;
- regulation-mapped evidence (Credo AI policy packs, Holistic AI control mapping);
- independent scrutiny (external audits).

LLM guardrails also handle unstructured text, which rule tables cannot.

**What it leaves to others.** Few works say what happens when the checker fails:

- Guardrails AI's default `on_fail` action is `noop`: the output passes and the failure
  is only logged.
- CaMeL blocks a tool call that breaks a policy, and suggests asking the user in
  production.
- Documentation and governance products mostly record rather than block.
- None of the surveyed works places a deterministic check at each data source of a
  multi-site computation, or treats a model update or a model release as a guarded
  boundary.

Lu et al.'s catalogue lists "federated learner", "ethical black box" and
"continuous ethical validator" as separate patterns. Hash-chained
or immutable evidence is proposed (FactSheets on ledgers; the ethical black box) but
seldom built. No surveyed work makes the decision function formally verified or exhaustive
by construction; AgentSpec (a DSL) and CaMeL (Python policies) come closest.

**Prior art for Dagents.** Dagents shares design choices with:

- GRAILS: its strategy table;
- CaMeL and AgentSpec: deterministic allow/deny at a boundary, with a reason;
- FactSheets and the ethical black box: append-only evidence;
- Hoepman's strategies: minimise, separate, aggregate, hide, enforce and demonstrate
  all map onto Dagents's boundaries.

### When each approach is active, and whether it acts per request

| Approach | Active when | Decides per request? | Enforces per request? | Evidence produced |
|---|---|---|---|---|
| Principles and guidelines (Jobin, Mittelstadt, Hagendorff) | Before design | No | No | None |
| VSD, Privacy by Design, Ethics by Design, Hoepman strategies | Design time | No; choices are built into the architecture | Only structurally (architecture limits what can flow) | Design rationale |
| GDPR Art. 25 (by design and by default) | Design time and default settings | No (default settings limit data per purpose) | Through defaults and configuration | Accountability records (Art. 5(2), not in this survey) |
| Model cards, datasheets, data statements, system cards | At release or publication | No | No | Documents |
| FactSheets (IBM) | Across the lifecycle; automated population proposed | No | No | Versioned facts; ledger proposed |
| SMACTR internal audit; external audits | Before deployment (internal); after deployment (external) | Human go/no-go | No | Audit report and artefacts |
| PIAs and AIAs | Before deployment, sometimes repeated | Human judgement | No | Assessment report |
| AIF360, Fairlearn, What-If Tool | Development and evaluation | Metrics per dataset/model; post-processing mitigators (e.g. ThresholdOptimizer) act on each prediction | Only if a post-processor is deployed | Metric reports |
| Governance platforms (watsonx.governance, Credo AI, Holistic AI) | Whole lifecycle, as a registry | Mostly no; vendor-stated runtime modules exist (IBM guardrails; Holistic AI "Enforce") | IBM: pass/flag/block on LLM I/O; Holistic AI: inline controls (vendor claim) | Inventory, factsheets, evidence, audit trails |
| LLM guardrails (NeMo, Llama Guard, ShieldGemma, Granite Guardian, Guardrails AI) | Runtime, per prompt and response | Yes (classifier or flow) | Yes, at LLM input/output; Llama Guard family only classifies | Logs (Guardrails AI history); not stated elsewhere |
| Agent runtime enforcement (CaMeL, AgentSpec) | Runtime, per tool call or agent step | Yes (Python policy / DSL rule) | Yes (block, stop, ask the user) | Reason string (CaMeL); not stated (AgentSpec) |
| GRAILS | Runtime, per data request | Yes (restriction plan per data item) | Yes, at one application's data-access layer | Restriction plan stored in a rule base; no audit log described |
| Dagents (for comparison) | Runtime at four boundaries | Yes (pure OCaml function) | Yes, at each site and at release; fails closed | Hash-chained audit naming the planner's SHA-256 |

### How "guardrail" is used

- **Policy metaphor.** Corporate and policy texts use "guard rails" for principles or
  standards. Nothing executes.
- **Classifier.** Llama Guard, ShieldGemma and Granite Guardian label content as safe or
  unsafe (with categories). The application decides what to do with the label.
- **Proxy with actions.** NeMo Guardrails ("programmable rails" between user and LLM),
  Guardrails AI (validators with `on_fail` actions) and IBM watsonx (detectors with
  pass, flag or block) act on what they find.
- **General definition.** Dong et al. (ICML 2024): a guardrail is "an algorithm that
  takes as input a set of objects ... and determines if and how some enforcement
  actions can be taken."
- **User-dependent moderation.** Hu et al.: guardrail strictness depends on a user trust
  score.
- **Structured-data access filter.** GRAILS: "Guardrail Framework" in the AAAI abstract;
  "Ethical-Restriction Rails" plus an "Ethical Guard" in the body.
- **No such term.** CaMeL calls itself a "protective system layer". AgentSpec uses
  "runtime enforcement". A text search of the RAI pattern catalogue found no
  "guardrail" pattern.

Dagents's sense (a plan per field, enforced at a boundary) is closest to GRAILS. Its
deterministic allow/deny with a reason is closest to CaMeL and AgentSpec.

### Works that cite GRAILS

None found as of 2026-11-16:

- Crossref's `is-referenced-by-count` for 10.1609/aies.v8i2.36650 is 0.
- Two web searches (one "extended") returned only the paper itself.
- Two arXiv papers that the search ranked near GRAILS (2512.16873, 2601.13122) were
  downloaded and searched for "GRAILS", "Kulkarni" and "Ramanathan". Neither cites it.
- An arXiv metadata search for GRAILS returned nothing relevant.

Google Scholar and Semantic Scholar could not be queried from this machine. A citing
work may therefore exist that this check missed.

---

## Entries

### A. The principles-to-practice gap

#### jobin2019global
Jobin, Ienca, Vayena. "The global landscape of AI ethics guidelines." *Nature Machine Intelligence* 1(9):389–399, 2019.
- Verification: DOI 10.1038/s42256-019-0088-2 (Crossref); arXiv 1906.11668 (text read).
- What it is: a content analysis of AI ethics guidelines. It finds a global convergence
  on five principles and divergence on how to interpret and apply them.
- D1–D7: n/a. It decides and enforces nothing; it is a map of non-binding documents.
  D2 notes that guidelines are prose.
- Overlap with Dagents: none at the mechanism level. Useful as the baseline that
  principles alone leave implementation open.
- What it does that Dagents does not: shows which values recur, which tells a framework
  which dimensions it ignores. Fairness and transparency are near the top; Dagents's
  governance is mainly about privacy protection.
- Quotes and numbers (arXiv text): "Our search identified 84 documents containing
  ethical principles or guidelines for AI". "Eleven overarching ethical values and
  principles have emerged". Counts (Table 2): transparency 73/84, justice and fairness
  68/84, non-maleficence 60/84, responsibility 60/84, privacy 47/84, beneficence 41/84,
  freedom and autonomy 34/84, trust 28/84. Abstract: convergence on "transparency,
  justice and fairness, non-maleficence, responsibility and privacy".

#### mittelstadt2019principles
Mittelstadt. "Principles alone cannot guarantee ethical AI." *Nature Machine Intelligence* 1(11):501–507, 2019.
- Verification: DOI 10.1038/s42256-019-0114-4 (Crossref); arXiv 1906.06668.
- What it is: compares the principled approach in AI with medical ethics. It argues that
  AI lacks the institutions that make principles work in medicine.
- D1–D7: n/a. D6 in spirit: the critique is that nothing happens when principles are
  broken.
- Overlap with Dagents: Dagents is one answer to point (3) below, "methods to translate
  principles into practice", for privacy-type rules.
- What it does that Dagents does not: names the missing institutions: fiduciary duties,
  professional norms and accountability. Software cannot supply them.
- Quote (arXiv abstract): "Compared to medicine, AI development lacks (1) common aims and
  fiduciary duties, (2) professional history and norms, (3) proven methods to translate
  principles into practice, and (4) robust legal and professional accountability
  mechanisms." Also: "At least 84 public-private initiatives have produced statements".

#### morley2020what
Morley, Floridi, Kinsey, Elhalal. "From What to How: An Initial Review of Publicly Available AI Ethics Tools, Methods and Research to Translate Principles into Practices." *Science and Engineering Ethics* 26(4):2141–2168, 2020.
- Verification: DOI 10.1007/s11948-019-00165-5 (Crossref, with abstract).
- What it is: a typology that maps available tools and methods to five principles
  (beneficence, non-maleficence, autonomy, justice, explicability) and to the stages of
  the machine-learning pipeline.
- D1–D7: n/a (a review). D4: organised by lifecycle stage.
- Overlap with Dagents: the same per-stage framing. Dagents's four boundaries are points
  in the pipeline.
- What it does that Dagents does not: covers explicability and autonomy, and points to
  tools for each stage.
- Quote (abstract): the debate "has primarily focused on principles—the 'what' of AI
  ethics ... —rather than on practices, the 'how.'"

#### morley2023operationalising
Morley, Kinsey, Elhalal, Garcia, Ziosi, Floridi. "Operationalising AI ethics: barriers, enablers and next steps." *AI & Society* 38(1):411–423, 2023.
- Verification: DOI 10.1007/s00146-021-01308-8 (Crossref, with abstract).
- What it is: a mixed-methods qualitative study of practitioners. It asks what
  practitioners understand, what motivates them, what barriers they face and what help
  they want.
- D1–D7: n/a.
- Overlap with Dagents: none at the mechanism level.
- What it does that Dagents does not: evidence on why tools go unused.
- Number (abstract): "By mid-2019 there were more than 80 AI ethics guides available in
  the public domain."

#### hagendorff2020ethics
Hagendorff. "The Ethics of AI Ethics: An Evaluation of Guidelines." *Minds and Machines* 30(1):99–120, 2020.
- Verification: DOI 10.1007/s11023-020-09517-8 (Crossref, with abstract); arXiv 1903.03425 (text read).
- What it is: compares 22 guidelines and asks how far their principles are put into
  practice.
- D1–D7: n/a. D6 in spirit: no consequence for deviations.
- Overlap with Dagents: Dagents's fail-closed Guard is a technical "reinforcement
  mechanism" in the narrow sense Hagendorff finds missing. It applies only to rules that
  can be written as data-handling plans.
- What it does that Dagents does not: shows which topics guidelines omit, and argues
  that ethics should build practitioners' judgement, not only constrain them.
- Quotes: abstract (journal), "analyzes and compares 22 guidelines". arXiv v2, §5:
  "Currently, AI ethics is failing in many cases. Ethics lacks a reinforcement mechanism.
  Deviations from the various codes of ethics have no consequences." §3 (arXiv): "ethics
  has no enforcement mechanisms reaching beyond a voluntary and non-binding cooperation
  between ethicists and individuals working in research and industry." The journal
  wording may differ slightly.

#### rakova2021where
Rakova, Yang, Cramer, Chowdhury. "Where Responsible AI meets Reality." *PACM HCI* 5(CSCW1):1–23, 2021.
- Verification: DOI 10.1145/3449081 (Crossref); arXiv 2006.12358 (abstract).
- What it is: semi-structured interviews with industry practitioners on how
  organisational culture and structure help or block responsible-AI initiatives.
- D1–D7: n/a.
- What it does that Dagents does not: the organisational enablers (roles, incentives)
  without which a technical guard is not adopted.

#### ali2023walking
Ali, Christin, Smart, Katila. "Walking the Walk of AI Ethics." FAccT 2023, pp. 217–226.
- Verification: DOI 10.1145/3593013.3593990 (Crossref); arXiv 2305.09573 (abstract).
- What it is: a qualitative study of "ethics entrepreneurs" in technology companies.
  Policies, practices and outcomes are "decoupled".
- D1–D7: n/a. D6 in spirit: people raising issues carry personal risk.
- Overlap with Dagents: an enforced check removes some reliance on individuals for the
  rules it covers.
- What it does that Dagents does not: explains why. Ethics loses to launch schedules and
  to metric-driven goals, and frequent reorganisation breaks the relationships the work
  needs.
- Quote (abstract): ethics entrepreneurs "struggle to have ethics prioritized in an
  environment centered around software product launches."

#### sanderson2023ethics
Sanderson et al. "AI Ethics Principles in Practice: Perspectives of Designers and Developers." *IEEE Trans. Technology and Society* 4(2):171–187, 2023.
- Verification: DOI 10.1109/TTS.2023.3257303 (Crossref, 10 authors); arXiv 2112.07467.
- What it is: interviews with CSIRO researchers and engineers about Australia's eight AI
  ethics principles, including tensions and trade-offs between principles.
- D1–D7: n/a.
- What it does that Dagents does not: covers contestability and human, social and
  environmental wellbeing, which a data-handling planner does not address.

#### pant2024ethics
Pant, Hoda, Tantithamthavorn, Turhan. "Ethics in AI through the practitioner's view: a grounded theory literature review." *Empirical Software Engineering* 29(3):67, 2024.
- Verification: DOI 10.1007/s10664-024-10465-5 (Crossref, with abstract).
- What it is: a grounded-theory review of 38 empirical studies of practitioners' views.
  It derives five categories: awareness, perception, need, challenge and approach.
- D1–D7: n/a.

#### ximenes2026bridging
Ximenes, Santos, Ramalho. "Bridging principles and practice: ethical machine learning in production for developers." *AI and Ethics* 6(4):339, 2026.
- Verification: DOI 10.1007/s43681-026-01083-6 (Crossref, with abstract).
- What it is: focus groups, a survey of 132 ML developers and one year of action research
  in a company with 35 million monthly active users. The result is 18 concrete guidelines
  and 29 best practices.
- D1–D7: n/a; the guidelines are prose for developers.
- Overlap with Dagents: none at the mechanism level.
- What it does that Dagents does not: a recent (2026) measurement of the gap.
- Quote (abstract): insights on "the gap between the importance ML developers attribute
  to guidelines and their actual personal adherence" and on "how the system in which the
  ML model is embedded may hinder the efforts to make the ethical decisions reach the
  final user."

#### lu2024responsible
Lu, Zhu, Xu, Whittle, Zowghi, Jacquet. "Responsible AI Pattern Catalogue: A Collection of Best Practices for AI Governance and Engineering." *ACM Computing Surveys* 56(7):1–35, 2024.
- Verification: DOI 10.1145/3626234 (Crossref); arXiv 2209.04963 (text read).
- What it is: a multivocal literature review. It groups patterns as governance, process
  and "responsible-AI-by-design" product patterns.
- D1: varies by pattern. D2: patterns in prose. D3: product patterns place components in
  the system. D5: "ethical black box" and "global view auditor" are evidence patterns.
  D7: technology-neutral.
- Overlap with Dagents: the product patterns include §5.1.4 "Federated learner", §5.3.1
  "Continuous ethical validator", §5.3.6 "Ethical black box" (records decisions; storage
  may be "a blockchain-based immutable log") and §5.3.7 "Global view auditor". Dagents
  combines versions of these: federated rounds, guards at boundaries, a hash-chained
  audit and a coordinator that sees what sites report. The catalogue lists them
  separately.
- What it does that Dagents does not: covers governance (ethics committees, RAI
  certification, RAI software bill of materials) and process patterns (FMEA, ethical
  user stories, stakeholder engagement).
- Quote (abstract): "without further guidance on best practices, practitioners are left
  with nothing much beyond truisms."

### B. Design-time approaches

#### cavoukian2010privacy
Cavoukian. "Privacy by design: the definitive workshop. A foreword." *Identity in the Information Society* 3(2):247–251, 2010.
- Verification: DOI 10.1007/s12394-010-0062-y (Crossref). Text not read; see Unverified
  for the "7 foundational principles" document.
- What it is: the published statement of Privacy by Design (PbD). Privacy should be
  built into systems proactively and as the default, not added later.
- D1: design choices. D2: principles for people. D3: in the architecture. D4: whole
  lifecycle in principle. D5: not specified. D6: n/a. D7: technology-neutral.
- Overlap with Dagents: Dagents's per-site stores and its "deny when the planner is
  missing" behaviour are PbD's "privacy as the default" in software form.
- What it does that Dagents does not: covers the full product lifecycle and user-facing
  transparency.

#### friedman1996value / friedman2008value
Friedman. "Value-sensitive design." *Interactions* 3(6):16–23, 1996. Friedman, Kahn, Borning. "Value Sensitive Design and Information Systems." In *The Handbook of Information and Computer Ethics*, Wiley, 2008, pp. 69–101.
- Verification: DOIs 10.1145/242485.242493 and 10.1002/9780470281819.ch4 (Crossref).
  Text not read in this session.
- What it is: Value Sensitive Design (VSD) is a design method. It combines conceptual,
  empirical and technical investigations of stakeholders' values, both direct and
  indirect stakeholders.
- D1: which values the design supports. D2: human analysis. D3: the design itself.
  D4: design time. D5: design rationale. D6: n/a. D7: none.
- Overlap with Dagents: none at runtime. Dagents assumes that a person decided the field
  classifications. VSD is a method for making that decision.
- What it does that Dagents does not: elicits values from affected people, including
  those who never use the system.

#### dignum2018ethics
Dignum et al. (19 authors). "Ethics by Design: Necessity or Curse?" AIES 2018, pp. 60–66.
- Verification: DOI 10.1145/3278721.3278745 (Crossref; title deposited as "Ethics by
  Design"). Text not read in this session.
- What it is: a position paper from a multi-disciplinary group. It separates ethics *in*
  design (regulation and engineering methods), ethics *by* design (building ethical
  reasoning into the AI system itself) and ethics *for* design (codes of conduct and
  standards for developers). It asks whether machines should reason about ethics.
- D1: for "by design", the machine's own action choices. D2: varies (formal reasoning is
  discussed). D3: inside the agent. D4: runtime for "by design". D5–D7: not specified.
- Overlap with Dagents: the three-way distinction places Dagents. It is "ethics in
  design" made executable: an external planner, not ethical reasoning in the model.
- What it does that Dagents does not: considers agents that reason about ethics
  themselves.
- Note: the in/by/for distinction is given from prior knowledge of the paper; the text
  was not re-read.

#### gdpr2016regulation / bygrave2017data
GDPR Art. 25, "Data protection by design and by default" (Regulation (EU) 2016/679). Bygrave. "Data Protection by Design and by Default: Deciphering the EU's Legislative Requirements." *Oslo Law Review* 4(2):105–120, 2017.
- Verification: Bygrave DOI 10.18261/issn.2387-3299-2017-02-03 (Crossref). GDPR URL not
  re-fetched (see Unverified).
- What it is: Art. 25 makes Privacy by Design a legal duty of the controller. Measures
  must implement data-protection principles such as data minimisation at the time the
  processing means are chosen and at the time of processing. By default, only the data
  needed for each purpose may be processed. Bygrave analyses what the article requires
  and its likely effect.
- D1: which technical and organisational measures (pseudonymisation is the example);
  what data is processed by default. D2: legal text; interpreted by people and
  regulators. D3: unspecified (any part of processing). D4: both "at the time of the
  determination of the means" and "at the time of the processing itself". D5:
  accountability elsewhere in the GDPR. D6: n/a. D7: jurisdiction (EU/EEA).
- Overlap with Dagents: dropping every field a request did not ask for (per the
  repository's CLAUDE.md) and checking an approved purpose per request are
  "by default, only data necessary for each specific purpose" in executable form.
- What it does that Dagents does not: binds an accountable legal person, and covers
  retention, accessibility and organisational measures.

#### spiekermann2009engineering
Spiekermann, Cranor. "Engineering Privacy." *IEEE Trans. Software Engineering* 35(1):67–82, 2009.
- Verification: DOI 10.1109/TSE.2008.88 (Crossref). Text not read in this session.
- What it is: a framework for privacy-friendly system design. It distinguishes a
  "privacy-by-policy" approach (notice and choice) from a "privacy-by-architecture"
  approach (data minimisation, anonymity, client-side processing) and relates them to
  how identifiable the data is.
- D1: architecture choice. D2: engineering guidance. D3: architecture: where data is
  kept and processed. D4: design. D5: n/a. D6: n/a. D7: none.
- Overlap with Dagents: Dagents is privacy-by-architecture: data stays at each site and
  only guarded summaries and clipped updates leave. It adds a policy layer (the Guard)
  on top.
- What it does that Dagents does not: covers notice and choice towards data subjects.
- Note: the by-policy / by-architecture summary is from prior knowledge; the text was
  not re-read.

#### hoepman2014privacy
Hoepman. "Privacy Design Strategies." IFIP SEC 2014, IFIP AICT, pp. 446–459.
- Verification: DOI 10.1007/978-3-642-55415-5_38 (Crossref); arXiv 1210.6621 (abstract).
- What it is: derives eight privacy design strategies from data protection law, for use
  early in the software lifecycle.
- D1: which strategy the architecture follows. D2: a classification for architects.
  D3–D4: design time. D5: the "demonstrate" strategy. D6–D7: n/a.
- Overlap with Dagents: Dagents implements six of the eight in code:
  - *minimise*: unrequested fields dropped;
  - *separate*: one store per site;
  - *aggregate*: minimum cohort, model updates instead of rows;
  - *hide*: clipping and noise;
  - *enforce*: the Guard;
  - *demonstrate*: the hash-chained audit.
- What it does that Dagents does not: *inform* and *control*, the data subject's view
  and choices.
- Quote (arXiv abstract): "we derive the following eight privacy design strategies:
  minimise, hide, separate, aggregate, inform, control, enforce, and demonstrate."

### C. Documentation artefacts

#### mitchell2019model
Mitchell et al. "Model Cards for Model Reporting." FAT* 2019, pp. 220–229.
- Verification: DOI 10.1145/3287560.3287596 (Crossref); arXiv 1810.03993 (text read).
- What it is: a short document that accompanies a trained model. It reports evaluation
  results disaggregated by groups, intended use and caveats.
- D1: none; it informs whether to use a model. D2: prose and tables written by people.
  D3: published with the model. D4: model release. D5: the card itself. D6: n/a. D7:
  none.
- Overlap with Dagents: Dagents's release gate needs named metrics, and a missing metric
  blocks release. A model card reports such metrics but blocks nothing.
- What it does that Dagents does not: intended-use statements, disaggregated evaluation
  and ethical considerations, in a form people can read. The Dagents brief does not
  mention any generated model documentation.
- Sections (arXiv): Model Details, Intended Use, Factors, Metrics, Evaluation Data,
  Training Data, Quantitative Analyses, Ethical Considerations, Caveats and
  Recommendations.

#### gebru2021datasheets
Gebru et al. "Datasheets for datasets." *Communications of the ACM* 64(12):86–92, 2021.
- Verification: DOI 10.1145/3458723 (Crossref); arXiv 1803.09010 (text read).
- What it is: a set of questions dataset creators answer. They cover motivation,
  composition, collection process, preprocessing, cleaning and labelling, uses,
  distribution and maintenance.
- D1: none. D2: human-written answers. D3: published with the dataset. D4: data creation
  and distribution. D5: the datasheet. D6–D7: n/a.
- Overlap with Dagents: Dagents's field classifications (from extensions) record the
  sensitivity of each field, which is part of what a datasheet describes.
- What it does that Dagents does not: makes creators reflect on collection and consent,
  which configuration cannot capture.
- Quote (arXiv, §1): "the process of creating a datasheet is not intended to be
  automated. Although automated documentation processes are convenient, they run counter
  to our objective of encouraging dataset creators to carefully reflect".

#### arnold2019factsheets
Arnold et al. "FactSheets: Increasing trust in AI services through supplier's declarations of conformity." *IBM J. Res. Dev.* 63(4/5):6:1–6:13, 2019.
- Verification: DOI 10.1147/JRD.2019.2942288 (Crossref); arXiv 1808.07261 (text read).
- What it is: proposes a supplier's declaration of conformity (SDoC) for AI services. It
  is a voluntary, supplier-written document of tests, intended use and lineage.
- D1: none; it supports a consumer's decision. D2: supplier-provided facts. D3:
  attached to the service. D4: whole service lifecycle. D5: versioned facts. Results are
  added, never removed. A ledger is proposed. D6: n/a. D7: IBM origin, but
  vendor-neutral in concept.
- Overlap with Dagents: append-only, versioned evidence. Dagents's hash-chained audit
  and its model-version status are an implemented form of "results cannot be removed".
- What it does that Dagents does not: aimed at buyers and markets. It covers
  explainability, robustness and lineage; third-party certification is suggested.
- Quotes (arXiv): FactSheets "can continually be augmented, without removing previous
  information, i.e., results are added from more tests, but results cannot be removed."
  "We also envision the automation of nearly the entire FactSheet as part of the build
  and runtime environments of AI services ... automatically posted to distributed,
  immutable ledgers". Also: "SDoCs are often voluntary and tests reported in SDoCs are
  conducted by the supplier itself".

#### bender2018data
Bender, Friedman. "Data Statements for Natural Language Processing." *TACL* 6:587–604, 2018.
- Verification: DOI 10.1162/tacl_a_00041 (Crossref).
- What it is: documentation of a language dataset: speakers, annotators, curation
  rationale and language variety, so that bias and generalisation can be judged.
- D1–D7: as for datasheets (documentation; no decision or enforcement).
- What it does that Dagents does not: domain-specific provenance for text data.

#### openai2023gpt4
OpenAI. "GPT-4 Technical Report." arXiv 2303.08774, 2023 (includes the GPT-4 System Card).
- Verification: arXiv 2303.08774 (v6; text checked: "This report includes an extensive
  system card (after the Appendix)").
- What it is: a "system card" documents a deployed system, not only a model: risks found
  by red-teaming and the mitigations applied before release.
- D1: none in the card. D2: prose. D3: published with the release. D4: release. D5: the
  card. D6–D7: one vendor.
- Overlap with Dagents: the card describes release mitigations; Dagents enforces a
  release gate.
- What it does that Dagents does not: describes the whole deployed system, including
  red-teaming results.

#### liang2024systematic
Liang et al. "Systematic analysis of 32,111 AI model cards characterizes documentation practice in AI." *Nature Machine Intelligence* 6(7):744–753, 2024.
- Verification: DOI 10.1038/s42256-024-00857-z (Crossref); arXiv 2402.05160 (abstract).
- What it is: a large-scale study of model cards on Hugging Face. Most popular models
  have cards, but the cards are uneven.
- D1–D7: n/a (an empirical study).
- Why it matters here: evidence that voluntary documentation leaves out the sections
  closest to responsible AI.
- Quote (arXiv abstract): "sections addressing environmental impact, limitations, and
  evaluation exhibit the lowest filled-out rates, while the training section is the most
  consistently filled-out." An intervention added detailed cards to 42 models.

#### heger2022understanding
Heger, Marquis, Vorvoreanu, Wallach, Wortman Vaughan. "Understanding Machine Learning Practitioners' Data Documentation Perceptions, Needs, Challenges, and Desiderata." *PACM HCI* 6(CSCW2):1–29, 2022.
- Verification: DOI 10.1145/3555760 (Crossref, with abstract).
- What it is: interviews with 14 practitioners at one large company, who answered
  datasheet questions. They want documentation that is adaptable, integrated into their
  tools and "automated wherever possible". They often did not connect the questions to
  responsible-AI implications.
- D1–D7: n/a.
- Why it matters here: a tension with Gebru et al.'s "not intended to be automated".

### D. Auditing and impact assessment

#### raji2020closing
Raji et al. "Closing the AI Accountability Gap: Defining an End-to-End Framework for Internal Algorithmic Auditing." FAT* 2020, pp. 33–44.
- Verification: DOI 10.1145/3351095.3372873 (Crossref); arXiv 2001.00973 (text read).
- What it is: SMACTR is an internal audit run through the development lifecycle in five
  stages: Scoping, Mapping, Artifact Collection, Testing, Reflection. Each stage produces
  documents, among them a stakeholder map, an FMEA, adversarial tests and an ethical risk
  analysis chart. Together they form an audit report.
- D1: whether to deploy, and what to fix, judged against the organisation's principles.
  D2: human reviewers using structured artefacts. D3: within the organisation, before
  launch. D4: development through pre-deployment, with post-audit follow-up. D5: audit
  documents. D6: not discussed as a runtime property. D7: organisation-specific.
- Overlap with Dagents: release gating. Dagents's rule that a missing metric blocks
  release is an automated form of one Testing-to-Reflection check.
- What it does that Dagents does not: covers harms that need human judgement (use-related
  risk, stakeholder impact). It also states the limits of internal audits.
- Quote (§5): "Internal auditors necessarily share an organizational interest with the
  target of the audit." Internal audits are "only one important aspect of a broader
  system of required quality checks and balances."

#### raji2019actionable
Raji, Buolamwini. "Actionable Auditing: Investigating the Impact of Publicly Naming Biased Performance Results of Commercial AI Products." AIES 2019, pp. 429–435.
- Verification: DOI 10.1145/3306618.3314244 (Crossref). Text not read.
- What it is: a follow-up external audit of commercial facial-analysis APIs after the
  Gender Shades results were made public. It asks whether naming companies publicly led
  them to change their products.
- D1: none in the system. D2: external researchers. D3: outside the organisation,
  against a deployed API. D4: after deployment. D5: the published audit. D6–D7: n/a.
- What it does that Dagents does not: independence from the deployer. Dagents's audit
  chain is written by the system it audits. The chain is tamper-evident, but it is not an
  external audit.

#### birhane2024ai
Birhane, Steed, Ojewale, Vecchione, Raji. "AI auditing: The Broken Bus on the Road to AI Accountability." SaTML 2024, pp. 612–643.
- Verification: DOI 10.1109/SaTML59370.2024.00037 (Crossref); arXiv 2401.14462 (abstract).
- What it is: a taxonomy of AI audit practice (regulators, law firms, civil society,
  journalism, academia, consulting) and an assessment of which audits led to
  accountability.
- D1–D7: n/a (a review).
- Quote (abstract): "only a subset of AI audit studies translate to desired
  accountability outcomes."

#### metcalf2021algorithmic
Metcalf, Moss, Watkins, Singh, Elish. "Algorithmic Impact Assessments and Accountability: The Co-construction of Impacts." FAccT 2021, pp. 735–746.
- Verification: DOI 10.1145/3442188.3445935 (Crossref). Text not read in this session.
- What it is: analyses algorithmic impact assessments (AIAs) by comparison with impact
  assessment in other fields. It argues that "impacts" are constructed by the people who
  assess them, and that assessments can measure proxies rather than the harms people
  experience.
- D1: whether and how to deploy. D2: an assessment process. D3: before deployment.
  D4: design and pre-deployment. D5: the assessment. D6–D7: n/a.
- What it does that Dagents does not: brings affected communities and harms into scope.
  A planner only checks the rules it was given.

#### wright2012state
Wright. "The state of the art in privacy impact assessment." *Computer Law & Security Review* 28(1):54–61, 2012.
- Verification: DOI 10.1016/j.clsr.2011.11.007 (Crossref). Text not read.
- What it is: a comparative review of privacy impact assessment (PIA) policy and practice
  across several countries. A PIA is a process run before a system is deployed, not only
  a report.
- D1–D7: as for AIAs (a human assessment before deployment). D5: the PIA report.
- What it does that Dagents does not: a recognised legal and organisational instrument.
  The GDPR's data protection impact assessment (Art. 35) descends from it.

### E. Toolkits and their adoption

#### bellamy2019ai
Bellamy et al. "AI Fairness 360: An extensible toolkit for detecting and mitigating algorithmic bias." *IBM J. Res. Dev.* 63(4/5):4:1–4:15, 2019.
- Verification: DOI 10.1147/JRD.2019.2942287 (Crossref); arXiv 1810.01943 (text read).
- What it is: an open-source Python library of fairness metrics and bias-mitigation
  algorithms. Pre-processing algorithms change the data, in-processing algorithms change
  training, and post-processing algorithms change predictions (e.g. equalized odds
  post-processing).
- D1: a transformation of data, model or predictions. D2: library code chosen by a data
  scientist. D3: in the ML pipeline; post-processors at prediction output. D4: training
  and prediction. D5: metric reports. D6: n/a. D7: Python/ML stack.
- Overlap with Dagents: none directly. Dagents transforms data for privacy, AIF360
  for fairness.
- What it does that Dagents does not: fairness measurement and mitigation, which neither
  Dagents nor GRAILS performs.
- Numbers (arXiv 2018, §1): "over 71 bias detection metrics, 9 bias mitigation
  algorithms". The current release may differ.

#### weerts2023fairlearn
Weerts, Dudík, Edgar, Jalali, Lutz, Madaio. "Fairlearn: Assessing and Improving Fairness of AI Systems." arXiv 2303.16626, 2023.
- Verification: arXiv 2303.16626 (text read). Journal version: see Unverified.
- What it is: an open-source library and community project. `MetricFrame` gives
  disaggregated evaluation. Mitigators include `ExponentiatedGradient` (reductions) and
  `ThresholdOptimizer` (post-processing). The project includes learning material and
  treats fairness as a "sociotechnical challenge".
- D1: per-group thresholds or a constrained model. D2: library code. D3: training or
  prediction output. D4: training and prediction. D5: metric tables. D6: n/a. D7: the
  scikit-learn ecosystem.
- Overlap with Dagents: `ThresholdOptimizer` is a per-prediction transformation, the one
  toolkit component that acts on each request at runtime.
- What it does that Dagents does not: fairness constraints and disaggregated evaluation.
- Quote (§3, arXiv): ThresholdOptimizer "identifies a separate threshold for each group
  defined by a sensitive feature in order to optimize some specified objective ...
  subject to specified fairness constraints".

#### wexler2019what
Wexler et al. "The What-If Tool: Interactive Probing of Machine Learning Models." *IEEE TVCG*, 2019 (IEEE VIS/VAST 2019).
- Verification: DOI 10.1109/TVCG.2019.2934619 (Crossref, early-access record); arXiv
  1907.04135.
- What it is: an interactive visual tool for exploring a model's behaviour:
  counterfactuals, performance across subgroups and fairness criteria, without code.
- D1: none; it supports human analysis. D2: interactive exploration. D3: the analyst's
  notebook or TensorBoard. D4: development and evaluation. D5: none formal. D6–D7: Google
  TensorFlow ecosystem originally.
- What it does that Dagents does not: lets non-programmers explore model behaviour.

#### holstein2019improving / lee2021landscape / deng2022exploring
Holstein et al. CHI 2019 (DOI 10.1145/3290605.3300830); Lee, Singh. CHI 2021 (DOI 10.1145/3411764.3445261); Deng et al. FAccT 2022 (DOI 10.1145/3531146.3533113).
- Verification: all three by Crossref. Abstracts were read for Holstein (arXiv
  1812.05239), Lee and Singh (SSRN 10.2139/ssrn.3695002) and Deng (arXiv 2205.06922).
- What they are: three studies of how fairness tools fit industry practice.
  - Holstein et al.: 35 semi-structured interviews and a survey of 267 ML practitioners.
    They find gaps between what product teams need and what fair-ML research offers.
  - Lee and Singh: compare six open-source fairness toolkits and survey practitioners.
    They find gaps between what the toolkits can do and what practitioners need.
  - Deng et al.: think-aloud interviews and a survey on how practitioners "(try to)" use
    toolkits. Practitioners need support to put metrics in context, communicate them
    and collaborate.
- D1–D7: n/a (empirical).
- Why they matter here: a toolkit that exists is not a toolkit in use. A runtime guard
  that is on by default avoids part of this adoption problem, but only for the rules it
  encodes.

### F. Runtime guardrails

#### kulkarni2025grails
Kulkarni, Ramanathan. "GRAILS – A Framework for Embedding Ethical Safeguards in Software Applications for Responsible AI." *Proc. AIES* 8(2):1513–1523, 2025.
- Verification: DOI 10.1609/aies.v8i2.36650 (Crossref and the AAAI OJS page). The PDF
  from ojs.aaai.org was searched for specific facts.
- What it is: a framework that separates ethical constraints from application logic.
  - The Ethical-Restriction Rails have three parts. A Know-Your-User (KYU) service gives
    a trust score; the current version uses fuzzy inference and weighted aggregation. A
    Data Protection Service builds a profile from metadata and a regulations knowledge
    base; the current version uses an LLM fine-tuned on Indian regulatory frameworks and
    healthcare data. A Restriction Planner, "much like the database query processor",
    writes a Restriction Plan for each data item as a tuple ⟨User: KYU Profile, Data
    Protection Profile, Restriction Strategies⟩.
  - The Ethical Guard executes the plan. A Strategy Engine builds an execution plan and
    an Execution Engine applies it to data held in a "Privacy Vault".
  - Strategies come from a table indexed by granularity (cell, row, column, table),
    sensitivity and KYU level. Examples are cell suppression, full or partial masking,
    microaggregation, generalisation, binning, top/bottom coding, noise injection, a
    differential-privacy column and no transformation.
  - The evaluation is a proof of concept on Open Government Data. It reports high
    cohesion and low coupling.
- D1: a transformation per data item; consent verification and encryption are also
  listed as strategies.
- D2: configuration and a rule/strategy knowledge base. The trust and protection
  profiles may come from fuzzy logic or ML/LLM models. Not formally analysed.
- D3: at one application's data-access layer, in front of the Privacy Vault.
- D4: data read and response (egress). Training and model release are out of scope.
- D5: the plan is "stored in the rule base". A "Filtering Score (FS)" quantifies
  compliance. No audit log is described (the text search found none).
- D6: not stated.
- D7: domain-agnostic by configuration and plugs into applications. Java (consent
  management is "Java-based"). The current Data Protection Service is tied to Indian
  regulations.
- Overlap with Dagents: Dagents adopts GRAILS's sensitivity × trust × granularity
  decision and its separation of planner from guard.
- What it does that Dagents does not:
  - it computes trust (KYU) rather than taking it as an input; the Dagents brief does
    not say how trust is obtained;
  - it maps regulations to protection levels through a knowledge base;
  - it includes consent management and encryption strategies;
  - it has a broader table of anonymisation transforms;
  - it offers a compliance score (FS);
  - it is meant to be retrofitted into existing single-database applications.
- Quotes: abstract (OJS), "The framework allows ethical filtering of information on the
  basis of data sensitivity (Low to High), the trust score of the user (Low to High),
  and granularity of the data (Cell, Row, Column, or Table)." Body: "The Restriction
  Planner functions as a mapping system". Future work: extending it "to other ethical
  dimensions like fairness, transparency, inclusiveness, security".

#### rebedea2023nemo
Rebedea, Dinu, Sreedhar, Parisien, Cohen. "NeMo Guardrails: A Toolkit for Controllable and Safe LLM Applications with Programmable Rails." EMNLP 2023 System Demonstrations, pp. 431–445.
- Verification: DOI 10.18653/v1/2023.emnlp-demo.40 (Crossref); arXiv 2310.10501 (text
  read).
- What it is: an open-source runtime that "acts like a proxy between the user and the
  LLM".
  - *Topical rails* are dialogue flows written in Colang, a modelling language,
    interpreted with LLM-generated "canonical forms".
  - *Execution rails* are custom Python actions that monitor input and output, for
    example fact-checking, hallucination checks and moderation.
  - It contrasts "programmable rails" with "embedded rails" trained into the model.
- D1: whether to answer, how to steer, whether output passes a check. D2: Colang plus
  Python actions; the LLM interprets user intent, so the rails are only partly
  deterministic. D3: a proxy at LLM input and output. D4: inference. D5: not stated.
  D6: not stated. D7: LLM chat applications; NVIDIA.
- Overlap with Dagents: rails separated from the application and from the model, and
  configured, not hard-coded.
- What it does that Dagents does not: handles open-ended natural language and dialogue
  state.
- Quotes: §7.1, "our toolkit should not be used as a stand-alone solution, especially
  for safety-specific rails." §7.2, "the latency and costs required are about 3 times
  the latency and cost of a normal call to generate the bot message without using
  Guardrails."

#### inan2023llama (with zeng2024shieldgemma and padhi2024granite)
Inan et al. "Llama Guard: LLM-based Input-Output Safeguard for Human-AI Conversations." arXiv 2312.06674, 2023. Related: Zeng et al. "ShieldGemma" (arXiv 2407.21772, Google, 2024); Padhi et al. "Granite Guardian" (arXiv 2412.07724, IBM, 2024).
- Verification: arXiv records for all three; Llama Guard text read.
- What it is:
  - Llama Guard is a Llama2-7b model fine-tuned to classify prompts and responses
    against a safety taxonomy given in its instruction. The output is "safe" or "unsafe"
    plus the violated categories. The taxonomy can be changed by zero- or few-shot
    prompting.
  - ShieldGemma (on Gemma 2) covers sexually explicit content, dangerous content,
    harassment and hate speech.
  - Granite Guardian adds jailbreak and RAG-hallucination risks (groundedness, context
    and answer relevance).
- D1: a label. The caller decides permit or deny. D2: a learned model. Not analysable.
  The policy is partly given in the prompt. D3: LLM input and output. D4: inference.
  D5: none. D6: not stated. D7: model family; English-centric (Llama Guard §6).
- Overlap with Dagents: per-request decisions at a boundary.
- What it does that Dagents does not: judges unstructured content with no hand-written
  rules, and the taxonomy can be changed at inference time.
- Quotes and numbers: Llama Guard §6, "we don't claim that we have perfect coverage of
  our policy." ShieldGemma abstract: "+10.8% AU-PRC on public benchmarks" over Llama
  Guard. Granite Guardian abstract: AUC "0.871 and 0.854 on harmful content and
  RAG-hallucination-related benchmarks". Dong et al. on Llama Guard: it "lacks
  guaranteed reliability since the classification results depend on the LLM's
  understanding of the categories and the model's predictive accuracy."

#### guardrailsai2026onfail
Guardrails AI (open-source library and hub). Documentation: "Use On Fail Actions"; "Validator On Fail Actions".
- Verification: URLs from web search; page content seen only through search results.
- What it is: a `Guard` wraps LLM calls and runs validators on outputs (and inputs).
  Each validator has an `on_fail` action: for example raise an exception, fix the
  output, re-ask the LLM, or do nothing.
- D1: pass, fix, re-ask or raise. D2: Python validators; per-validator configuration.
  D3: around the LLM call in the application. D4: inference. D5: failures recorded in
  the guard's history/logs. D6: default is effectively fail-open; see quotes. D7: Python
  LLM apps.
- Overlap with Dagents: validators separated from the application, applied per call.
- What it does that Dagents does not: repair (fix or re-ask) instead of only permitting,
  narrowing or denying; a hub of reusable validators.
- Quotes (official docs, via search snippets): "the on_fail parameter does not have to
  be set, as the default is "noop"". For noop, "The failure will still be recorded in
  the logs, but no corrective action will be taken." Set "at the validator level, not
  the guard level." This contrasts directly with Dagents's fail-closed default.

#### dong2024building
Dong et al. "Building Guardrails for Large Language Models." ICML 2024 (PMLR 235), position paper.
- Verification: arXiv 2402.01822 (text read); venue from the arXiv comment.
- What it is: reviews Llama Guard, NeMo Guardrails and Guardrails AI. It argues for
  systematic, multi-disciplinary guardrail design, possibly neural-symbolic, with
  verification and testing across the software development life cycle.
- D1–D7: a position paper. Its D2 point: existing guardrails are "loosely coupled" and
  their reliability rests on the classifier's accuracy.
- Overlap with Dagents: argues for symbolic, verifiable components. Dagents's typed
  planner is one.
- What it does that Dagents does not: covers conflicting requirements (fairness,
  toxicity, hallucination) and how to resolve them.
- Quote (§1): "A guardrail is an algorithm that takes as input a set of objects (e.g.,
  the input and/or the output of LLMs) and determines if and how some enforcement
  actions can be taken to reduce the risks embedded in the objects."

#### hu2024trust
Hu, Dong, Huang. "Trust-Oriented Adaptive Guardrails for Large Language Models." arXiv 2408.08959, 2024 (v3, 2025; under review).
- Verification: arXiv 2408.08959 (abstract and introduction read).
- What it is: a guardrail whose strictness depends on a user trust score. The score
  combines "direct interaction trust" and "authority-verified trust", with RAG-based
  in-context learning over a knowledge base.
- D1: grant or withhold sensitive content by trust level. D2: trust model plus LLM.
  D3: LLM output. D4: inference. D5–D6: not stated. D7: LLM services.
- Overlap with Dagents: the trust dimension of GRAILS's grid, for unstructured text.
- What it does that Dagents does not: computes and updates trust from behaviour and
  credentials.
- Quote (abstract): "this work is the first to introduce trust-oriented concept into a
  guardrail system". GRAILS's KYU score is similar and later (2025).

#### debenedetti2026defeating
Debenedetti et al. "Defeating Prompt Injections by Design." SaTML 2026, pp. 587–618.
- Verification: DOI 10.1109/SaTML68715.2026.00040 (Crossref); arXiv 2503.18813 (text
  read).
- What it is: CaMeL works as follows.
  - A privileged LLM turns the trusted user query into code.
  - A custom Python interpreter runs that code. Untrusted data is handled by a
    quarantined LLM and cannot change control flow.
  - Every value carries capabilities: provenance and allowed readers.
  - Before each tool call, policies check the arguments and their dependencies.
- D1: allow or deny a tool call, with a reason.
- D2: policies "expressed as Python functions" returning `Allowed` or `Denied`. The
  interpreter is deterministic, which gives what the authors call "provable security".
- D3: at tool-call boundaries (egress to tools with side effects).
- D4: agent execution.
- D5: a reason per decision.
- D6: blocks on violation in the evaluation. The authors expect production to ask the
  user instead.
- D7: LLM agents; Google reference implementation.
- Overlap with Dagents: the closest runtime analogue. Labels on data, a deterministic
  policy at each boundary, deny with a reason, and the policy outside the model. CaMeL
  compares its data minimisation to federated learning (footnote 5).
- What it does that Dagents does not: tracks information flow through a computation
  (capabilities propagate through dependencies). Dagents classifies fields at
  boundaries.
- Quotes: abstract, "solving 77% of tasks with provable security (compared to 84% with
  an undefended system) in AgentDojo." §5.2, "Policies are expressed as Python
  functions ... returns a SecurityPolicyResult expressed as Allowed or Denied, along
  with a reason". "If a policy violation is detected ..., the tool's execution is
  blocked. In a real-world application, executions that violate security policies will
  not be blocked, but they will require user confirmation."

#### wang2026agentspec
Wang, Poskitt, Sun. "AgentSpec: Customizable Runtime Enforcement for Safe and Reliable LLM Agents." ICSE 2026, pp. 2938–2950.
- Verification: DOI 10.1145/3744916.3764546 (Crossref); arXiv 2503.18666 (text read).
- What it is: a lightweight DSL. Each rule has a trigger (an agent event), predicates
  and an enforcement action: `user_inspection`, `llm_self_examine`, `invoke_action` or
  `stop`. It is evaluated at runtime around agent actions in code execution, embodied
  agents and autonomous driving. Rules can also be generated by an LLM.
- D1: allow, stop, correct or escalate an agent action. D2: a DSL with a defined
  grammar; predicates are code. D3: at the agent's action boundary. D4: agent execution.
  D5: not stated. D6: not stated. D7: LangChain-style agents; domain-agnostic.
- Overlap with Dagents: declarative rules enforced outside the model, per action.
- What it does that Dagents does not: escalates to a human (`user_inspection`) and
  covers physical-world actions.
- Numbers (abstract): prevents unsafe executions "in over 90% of code agent cases",
  "eliminates all hazardous actions in embodied agent tasks", "enforces 100% compliance
  by autonomous vehicles". LLM-generated rules: precision 95.56% and recall 70.96% for
  embodied agents.

### G. Industry governance platforms and standards of practice

#### microsoft2022responsible
Microsoft. "Microsoft Responsible AI Standard, v2: General Requirements for External Release." June 2022.
- Verification: PDF URL from search; the PDF was not opened.
- What it is: Microsoft's internal standard. It sets goals with requirements under each
  principle, and companion impact-assessment materials exist. Goal A1, an impact
  assessment, applies to all AI systems (per search results). The six principles are
  accountability, transparency, fairness, reliability and safety, privacy and security,
  and inclusiveness (prior knowledge).
- D1: whether a system may proceed and what controls it needs. D2: required processes
  and human review. D3: inside the organisation. D4: design through deployment. D5:
  impact assessments and documentation. D6: n/a. D7: one vendor's internal process.
- Overlap with Dagents: requirement-level gating before release.
- What it does that Dagents does not: organisation-wide accountability, sensitive-use
  review and fitness-for-purpose. None of these runs per request.
- Note: Microsoft also sells runtime filters (e.g. Azure AI Content Safety); not
  verified here.

#### ibm2026watsonxgov / ibm2026hap
IBM watsonx.governance (with AI Factsheets) and watsonx guardrails (HAP/PII detectors).
- Verification: IBM documentation URLs from search; content seen only through search
  results.
- What it is: watsonx.governance tracks an "AI use case" through its lifecycle. The use
  case is a set of factsheets recording lineage, training data, deployments and
  evaluation results; it starts in a Draft state. Stand-alone AI Factsheets is no longer
  supported from version 5.3.0 and runs inside watsonx.governance. Separately, the
  watsonx guardrails are detectors with thresholds: hate, abuse and profanity (HAP),
  PII, jailbreak and others. Input is checked before the model and output after it, with
  results pass, flag or block. Granite Guardian models (above) provide some detectors.
- D1: registry status (governance); pass, flag or block (guardrails). D2: workflow plus
  ML detectors with thresholds. D3: a central registry; LLM input and output. D4: whole
  lifecycle (registry); inference (guardrails). D5: factsheets. D6: not stated. D7: IBM
  platform; external models can be governed.
- Overlap with Dagents: lifecycle status of a model version (Dagents's planner rules on
  every status change) and per-request blocking.
- What it does that Dagents does not: a full enterprise inventory and factsheets
  integrated with model evaluation, and PII/HAP detection in free text.
- Quote (IBM docs via search): "An AI use case in an inventory consists of a set of
  factsheets that contain lineage, history, and other relevant information about a
  model's lifecycle."

#### credoai2026platform
Credo AI Responsible AI Governance Platform.
- Verification: product page and OECD.AI catalogue URL from search; content seen only
  through search results (vendor material).
- What it is: an AI registry. Each system has an owner, purpose, risk classification,
  applicable regulations and claimed controls. "Policy packs" turn laws and standards
  (EU AI Act, NIST AI RMF, ISO/IEC 42001 and others) into assessment requirements,
  evidence checklists and report templates. Vendors submit evidence through a portal.
- D1: compliance status. D2: policy packs (configuration) plus human assessment. D3: a
  central registry. D4: governance across the lifecycle. D5: evidence and audit-ready
  reports. D6: n/a. D7: vendor platform.
- Overlap with Dagents: policy as configuration, separate from application code.
- What it does that Dagents does not: maps controls to regulations and manages
  third-party evidence.
- Enforcement: none found per request. One aggregator said CI/CD and API-gateway
  enforcement was "planned" (unverified).

#### holisticai2026enforce
Holistic AI governance platform (modules Identify, Protect, Enforce).
- Verification: vendor URLs from search; content seen only through search results
  (vendor claims).
- What it is: AI discovery and inventory, risk mapping and testing (bias, privacy,
  robustness), control mapping to the EU AI Act, NIST AI RMF and ISO 42001, and an
  "Enforce" stage. That stage places runtime controls "in the execution path" of models
  and agents, including tool-call access control and cost or rate limits.
- D1: allow or block actions; risk status. D2: preconfigured and custom controls. D3:
  inline with model and agent SDKs. D4: testing, governance and runtime. D5: audit
  trails and evidence. D6: not stated. D7: vendor platform.
- Overlap with Dagents: claims per-request enforcement in addition to governance records.
- What it does that Dagents does not: shadow-AI discovery and bias testing.
- Quote (vendor page via search): "Policies are applied as automated controls that sit
  in the execution path of your models, agents, and workflows. Every interaction is
  evaluated against your active rules in real time."

#### google2025responsibility
Google. Responsible AI Progress Report / "AI Responsibility Update", published February 2025.
- Verification: PDF URL from search; not opened.
- What it is: Google's sixth annual report. Per Google's summary, it describes methods
  "for governing, mapping, measuring, and managing AI risks aligned to the NIST
  framework". It was released with revised AI Principles and an updated Frontier Safety
  Framework (critical capability levels that trigger deployment mitigations). Google's
  engineering practices include model cards and released safeguard models (ShieldGemma).
- D1: release mitigations by capability level. D2: internal review. D3: inside the
  organisation. D4: pre-release and release. D5: reports, model cards. D6: n/a. D7: one
  vendor.
- Overlap with Dagents: capability- or metric-gated release.
- What it does that Dagents does not: frontier-capability evaluation.
- Note: news reports say the 2025 principles dropped the earlier list of applications
  Google would not pursue (secondary sources; not checked against Google's page).

#### tabassi2023nist
Tabassi (NIST). "Artificial Intelligence Risk Management Framework (AI RMF 1.0)." NIST AI 100-1, 2023.
- Verification: DOI 10.6028/NIST.AI.100-1 (Crossref; author recorded as Tabassi,
  Elham). Crossref also lists a 2026 record 10.6028/nist.ai.100-1r1 (and "ipd") with the
  same title; its status was not checked.
- What it is: a voluntary framework with four functions, Govern, Map, Measure and Manage
  (prior knowledge). Many vendor platforms above map their controls to it.
- D1: risk treatment decisions by the organisation. D2: process guidance. D3–D4:
  lifecycle-wide. D5: documentation. D6: n/a. D7: none (voluntary, sector-neutral).
- What it does that Dagents does not: covers organisational governance. Dagents's
  evidence (audit chain, release decisions) could feed its Measure and Manage functions.

---

## Unverified

- **GDPR Art. 25 wording and URL.** The ELI URL
  `https://eur-lex.europa.eu/eli/reg/2016/679/oj` and the quotes in the Art. 25 entry
  ("at the time of the determination of the means", "by default, only personal data
  which are necessary for each specific purpose") are from prior knowledge. The web
  search budget ran out before they could be checked. Check the quotes against EUR-Lex
  before using them.
- **EDPB Guidelines 4/2019 on Article 25** (version 2.0, adopted 20 October 2020 to my
  knowledge). The best regulator-level reading of Art. 25. Not searched (budget used
  up).
- **ISO/IEC 42001:2023** (AI management system standard). Mentioned by Credo AI and
  Holistic AI as a mapping target. The ISO page was not checked.
- **Cavoukian's "7 Foundational Principles"** (IPC Ontario, 2009, revised 2011). The
  usual PbD citation. Only the 2010 IDIS foreword was verified.
- **Fairlearn in JMLR.** I believe the paper appeared in *JMLR* 24 (2023). JMLR has no
  DOI and its site could not be reached.
- **What-If Tool final pagination.** Crossref holds only the early-access record. I
  believe the final version is *IEEE TVCG* 26(1):56–65, 2020.
- **Dong et al. 2024 page numbers** in PMLR 235. Not checked.
- **Raji and Buolamwini 2019 numbers.** I recall error-rate reductions between audit
  periods for the darker-skinned female subgroup, in the range of about 17.7% to 30.4%.
  Not verified, so not used.
- **Dignum et al. 2018: ethics in, by and for design**, and **Spiekermann and Cranor
  2009: privacy-by-policy vs. privacy-by-architecture.** Summarised from prior
  knowledge; the papers were not reachable.
- **Microsoft's six principles and runtime products** (Azure AI Content Safety, prompt
  shields). Prior knowledge; not searched.
- **Meta "system cards"** (2022, e.g. for Instagram feed ranking). A system-level
  documentation precedent. Not searched.
- **AI Now 2018 "Algorithmic Impact Assessments" report** (Reisman et al.) and
  **Canada's Directive on Automated Decision-Making AIA tool.** Standard AIA references;
  not searched.
- **Other agent-enforcement work** (e.g. Progent, GuardAgent, ShieldAgent). Not checked
  after the search budget ran out.
- **Works citing GRAILS.** None found (see above). A Google Scholar "cited by" check
  could not be run from this machine.
