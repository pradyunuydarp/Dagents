// The survey's content for the deck, condensed from section 4 and 5 of the paper
// (docs/reports/dagents_survey_paper/sections/). Change the paper first.

const list = [
  {
    short: "Responsible AI",
    name: "Responsible-AI practice",
    examples: "principles, Privacy by Design, model cards, audits, fairness toolkits, guardrails",
    finding: "Responsible-AI practice decides through people, at fixed points",
    points: [
      "Five principles recur across 84 guideline documents; how to apply them is not agreed (Jobin et al. 2019)",
      "Design methods, documentation and audits act before or after a system runs, not per request",
      "Fairness toolkits compute metrics in development; a data scientist decides what to apply",
      "Per-request enforcement appears only in runtime guardrails: LLM input and output, agent tool calls, and GRAILS for structured data",
      "Guardrails AI's default on a failed check is noop: the output passes and the failure is logged",
    ],
    closest: "GRAILS's strategy table. CaMeL and AgentSpec: a deterministic allow or deny, with a reason, at a boundary. FactSheets: evidence that is only added to.",
    stronger: "Eliciting values, measuring and reducing unfairness, documenting models and data, external review, and moderating free text.",
    notes: `Jobin, Ienca and Vayena 2019; Mittelstadt 2019; Morley et al. 2020 and 2023. Guardrails: NeMo Guardrails,
      Llama Guard, ShieldGemma, Granite Guardian, Guardrails AI. Agent enforcement: CaMeL and AgentSpec. We found no
      work that cites GRAILS as of 16 November 2026.`,
  },
  {
    short: "Policy engines",
    name: "Access control and policy engines",
    examples: "RBAC, ABAC, XACML, OPA, Cedar, Zanzibar, warehouse masking policies",
    finding: "Policy engines mostly answer permit or deny, at one store",
    points: [
      "The split between a decision point and an enforcement point dates from IETF RFC 2753 (2000); GRAILS and Dagents are instances of it",
      "XACML, AuthZEN and Cedar return permit or deny; how much to return travels beside the answer",
      "Warehouses decide the form of the data (masks, row filters, minimum group size) inside the engine that holds the pooled data",
      "Cedar proves properties of its language in Lean and analyses policies with SMT",
      "If the decision point is unreachable: Envoy denies by default, OPA Gatekeeper admits, Ranger uses cached policies",
    ],
    closest: "The decision and enforcement split, with an enforcement point that denies when in doubt. Purpose binding, as in Hippocratic databases and Meta's Policy Zones.",
    stronger: "Formal analysis (Cedar, Zelkova), measuring a rule before enforcing it (Meta's logging mode), and logs with proofs of inclusion (Certificate Transparency).",
    notes: `Credit the pattern to this family: Samarati and De Capitani di Vimercati; RFC 2753; XACML; NIST ABAC. Blockaid argues
      against changing results at all, because query modification can return misleading results. No work in this family
      enforces at independent sites under a coordinator that may not read their rows, or governs model release.`,
  },
  {
    short: "Privacy techniques",
    name: "Privacy-enhancing technologies",
    examples: "k-anonymity, disclosure control, differential privacy, PINQ, OpenDP, Tumult",
    finding: "Privacy techniques supply mechanisms; people set the parameters",
    points: [
      "GRAILS's strategies come from statistical disclosure control: suppression, top coding, microaggregation, generalisation",
      "Linkage, composition and reconstruction attacks defeat these syntactic protections",
      "Differential privacy holds against any outside knowledge and composes; the 2020 US Census adopted it",
      "In every DP system surveyed, a person sets ε and the unit of privacy; software derives the noise and keeps the budget",
      "No surveyed system sets the privacy parameter from the requester's trust or purpose",
    ],
    closest: "Cohere's planner never touches the data. LinkedIn's budget service refuses a query it cannot afford. Fuzz proves the bound in a type system.",
    stronger: "Provable bounds, privacy accounting across releases, and unpredictable noise. Dagents's typed table proves only that every input has a decision.",
    notes: `A rule that chooses a transformation is not a privacy bound. Generalisation, masking and exact-count thresholds give
      no bound against outside knowledge. Dagents's noise is seeded from the request id so an auditor can reproduce it, and
      for a model update the coordinator knows that id; the paper lists this as a limitation.`,
  },
  {
    short: "Federated learning",
    name: "Federated learning",
    examples: "FedAvg, secure aggregation, DP-FedAvg, Flower, NVIDIA FLARE, Substra, FLA3",
    finding: "Federated learning keeps data local and leaves release to people",
    points: [
      "Gradients can be inverted and membership inferred, so keeping raw data local is not enough",
      "Defences (secure aggregation, differential privacy, clipping, robust aggregation) are chosen in code",
      "Governance lives in provisioning, site policy files, people, ledgers, or a policy language",
      "Model release is the stage covered least; release rules are organisational",
      "Scale exists: the FeTS study used data from 71 sites on six continents",
    ],
    closest: "FLA3 (2026) checks XACML policies when a node joins and in every round, fails closed and signs its records. Gating participation outside the learning code is not new.",
    stronger: "Cryptographic secure aggregation, calibrated privacy accounting, robust aggregation, attestation, and studies across dozens of sites.",
    notes: `NVIDIA FLARE's privacy.json forces filters on every job at a site; authorization.json controls who may submit jobs.
      Substra is the one framework where release follows from coded rules: a model inherits the intersection of its inputs'
      permissions. Google: all future Gboard language model launches will require DP guarantees.`,
  },
  {
    short: "Code to data",
    name: "Code to data: analytics and research environments",
    examples: "DataSHIELD, OHDSI, OpenSAFELY, GRAIMatter, clean rooms, SMCQL, Conclave",
    finding: "Code-to-data systems control what leaves, in five different ways",
    points: [
      "A function library with thresholds the custodian sets (DataSHIELD, SHRINE)",
      "Site staff review the code and results (Sentinel, OHDSI, vantage6), or trained checkers review outputs (OpenSAFELY)",
      "Per-table rules in a vendor's engine (AWS Clean Rooms; Ads Data Hub's 50-user threshold)",
      "A compiler or cryptography (SMCQL, Conclave, Senate)",
      "Model release came later and separately (GRAIMatter); combining permitted means and covariances can rebuild records",
    ],
    closest: "DataSHIELD enforces at the site and filters model fitting too. SMCQL and Conclave derive typed, deterministic plans. GRAIMatter's controls on model release.",
    stronger: "Output checking by people, attack tests before a model leaves, budgets across queries, MPC and trusted hardware, and legal permits.",
    notes: `OpenSAFELY: two trained checkers review every output, and counts of 7 or fewer are redacted. The rule about what
      leaves is often spread across server options, function code, checker guidance and vendor defaults. Huth et al. 2023
      rebuilt protected records from statistics established frameworks permit; any per-request design is exposed to this.`,
  },
  {
    short: "Architecture",
    name: "Architectural precedents",
    examples: "mobile agents, federated databases, data mesh, query planning, Terraform, LLM agents",
    finding: "The architectural ideas are old; Dagents combines them",
    points: [
      "Moving code to data: mobile agents, MapReduce, edge computing, federated databases",
      "Data mesh's federated computational governance is the closest industry statement; a 2024 review calls automating it largely unexplored",
      "Plan before acting: System R, Terraform, Kubernetes, Nix. A query plan keeps the answer; a restriction plan changes it on purpose",
      "Functional core, imperative shell; make illegal states unrepresentable",
      "LLM-agent defences such as CaMeL also keep a trusted planner away from untrusted data",
    ],
    closest: "Wider et al. (2023) run OPA in a sidecar beside each data product, with global sensitivity levels and a deny-all default.",
    stronger: "Formal semantics (NetKAT, Cedar), reproducible builds (Nix), continuous reconciliation (Kubernetes), graded enforcement with recorded overrides (Sentinel).",
    notes: `These are combinations of known primitives, not new primitives. Dagents's agents are services, not language models.
      The compile-time guarantee holds where a non-exhaustive match stops the build: dune's development profile, which CI
      runs on every change, treats the warning as an error.`,
  },
  {
    short: "Release and regulation",
    name: "Release governance and regulation",
    examples: "TFX, model registries, SR 11-7, FDA PCCP, GDPR, HIPAA, EU AI Act, EHDS",
    finding: "Regulation asks for per-request checks and evidence",
    points: [
      "Registries store a model's state; a pipeline step or a person decides each transition",
      "Device regulators approve an envelope of allowed changes in advance (FDA PCCP; AI Act Article 43(4))",
      "A program can check: an approved purpose, protocol fields only, thresholds met, a missing metric blocks, logs with the logic's version",
      "A program cannot check that a purpose is real, that thresholds are right, or that oversight works",
      "No regulation says how a per-request decision is computed, or how to show the reviewed code decided",
    ],
    closest: "AEGIS (2026) turns change control plans into four fixed outcomes. HIPAA's standard protocols are field sets decided in advance.",
    stronger: "Monitoring after release, staged deployment, control of drift across releases, and named approvers.",
    notes: `Feng et al. 2021 show that repeated approval of small changes can let accuracy drift down. A 2026 study of twelve AI
      Act compliance checkers found they may foster a false sense of compliance. Dagents's approval route records no named
      approver; the paper lists this as a limitation.`,
  },
];

// Rows: family, read, train or compute, send a result, release a model, evidence.
const matrix = [
  ["F1 Responsible AI", "LLM text only", "design time, by hand", "—", "by hand", "documents"],
  ["F2 Policy engines", "decides, central", "IFC runtimes", "IFC egress", "—", "decision logs"],
  ["F3 Privacy techniques", "mechanism, central", "DP training", "local DP", "budget", "budget ledger"],
  ["F4 Federated learning", "site filters", "in the protocol", "site filters", "by hand", "audit files"],
  ["F5 Code to data", "function library", "site review", "thresholds, by hand", "separate, later", "logs"],
  ["F6 Architecture", "mediators", "—", "—", "deploys, not models", "plans"],
  ["F7 Release and regulation", "principles", "—", "—", "gates, by hand", "logs required"],
  ["GRAILS", "decides, one vault", "—", "—", "—", "plan, score"],
  ["Dagents", "decides, each site", "before_train", "before_send", "gates, Guard, person", "chain, planner hash"],
];

const gaps = [
  ["G1", "The form of the data, decided outside the store", "Where a system decides how much to return and in what form, it usually does so inside the engine that already holds the pooled data."],
  ["G2", "Enforcement at independent sources", "Few systems apply one decision model at each source under a coordinator that may not read the source's rows."],
  ["G3", "One rule set across the lifecycle", "Reading, training, sending and releasing use different mechanisms and people; release is covered least."],
  ["G4", "Decisions that can be read, tested and replayed", "The rule about what leaves is often spread across options, code, guidance and defaults."],
  ["G5", "Failure behaviour and evidence", "Defaults when the decision point fails differ, and logs rarely name the logic that decided."],
];

const compare = [
  ["F1 Responsible AI", "GRAILS's table; allow or deny with a reason", "enforces per request on typed data at each source", "fairness, documentation, external review"],
  ["F2 Policy engines", "decision and enforcement split; deny when in doubt", "decides a per-field form outside the store", "formal analysis (Cedar); tamper-proof logs"],
  ["F3 Privacy techniques", "generalise, suppress, minimum counts, clip, noise", "chooses from trust, purpose and granularity", "provable bounds; accounting; unpredictable noise"],
  ["F4 Federated learning", "rounds, quorum, clipping; delegates the protocol", "one planner for fields, participation, release", "secure aggregation; attestation; scale"],
  ["F5 Code to data", "site enforcement; typed plans; minimum counts", "one rule set for reads, updates and release", "output checkers; attack tests; MPC; permits"],
  ["F6 Architecture", "code to data; plan first; functional core", "applies them to disclosure across organisations", "formal semantics; reproducible builds"],
  ["F7 Release and regulation", "gates on fixed thresholds; missing metric blocks", "planner decides every status change", "monitoring; staged roll-out; named approvers"],
];

module.exports = {
  total: 293,
  list,
  matrix,
  matrixNotes: `Each cell names the usual practice in the family, not every system in it. GRAILS covers one stage, at one
    store. Dagents covers all four stages with one decision model, at each site.`,
  gaps,
  gapNotes: `None of these gaps means that no system does the thing; the survey names systems that do part of each. The gaps
    are in the combination. GRAILS addresses G1 for one application with one store.`,
  compare,
  compareNotes: `Where a team already has mature parts (Cedar or OPA, FLARE's site filters, a DP library, a model registry), it
    could assemble most of this. What it would still write is the glue the gaps describe. Dagents is an alpha with one
    worked study, so assembling mature parts may be the better choice for some teams.`,
};
