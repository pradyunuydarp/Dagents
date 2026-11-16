# Family `arch`: the architectural ideas Dagents builds on

Scope: moving computation to data; federated and decentralised data
architectures; query planning as a model for decisions; separating pure
decisions from side effects; compiling plans from typed specifications;
plan/apply and declarative deployment; policy engines as separate processes;
and the meaning of "agent" in LLM systems, with runtime enforcement for LLM
agents. BibTeX keys refer to `arch.bib`.

Method note. Academic metadata was checked with Crossref or arXiv. Five arXiv
papers, GRAILS (from ojs.aaai.org), the data mesh review
(Goedegebuure et al.) and Wider et al. were read in full text; quotes from
them give PDF page numbers. Works behind blocked hosts (ACM, IEEE, Elsevier,
Springer, martinfowler.com) were not read in this session; their entries
describe them from metadata, abstracts and well-known content, and say so.
The shared web-search budget ran out partway through, so a few items stayed
unverified; they are listed at the end.

---

## 1. Synthesis

**Shared assumptions.** Almost every idea here separates *what should
happen* from *making it happen*, and puts the first part in a form that can
be inspected before anything runs. A query optimizer picks an access path
before execution (Selinger 1979). Terraform prints a plan before `apply`.
Kubernetes stores a desired state that controllers then reconcile. NetKAT
compiles a network-wide policy into per-switch tables. XACML and RFC 2753
split a Policy Decision Point (PDP) from a Policy Enforcement Point (PEP).
"Functional core, imperative shell" and hexagonal architecture make the same
cut inside one program, and ML-family types make the decision's input space
finite and checkable. A second assumption, from mobile agents, MapReduce and
edge computing, is that moving code to data is cheaper or safer than moving
data to code. Federated databases (Sheth and Larson 1990) add that each
source keeps its autonomy and chooses what it exports.

**Where decisions live.** In a central planner over a catalog (System R,
mediators, Presto, BigDAWG); in a declarative artefact (a Terraform plan, a
Kubernetes spec, a Git repository in GitOps, a Nix expression); or in a
separate policy engine (OPA, an XACML PDP, Cedar). Data mesh's "federated
computational governance" mixes the three: a federation of domain and
platform owners sets global rules, each domain tags its own data, and the
platform runs the rules automatically, in or beside each data product.
Wider et al. (2023) build this with OPA and a per-product "sidecar".

**Where enforcement happens.** At a gateway or at the resource: an Envoy
filter, a Kubernetes admission webhook, a mediator, a switch, a sidecar. LLM
agent defences enforce at each tool call (CaMeL, Progent, AgentSpec, Fides).

**What the family does well.** It has decades of mature machinery:
cost-based search over equivalent plans (Selinger), formal semantics and
decidable equivalence of policies (NetKAT), a policy language with a
formally verified evaluator and an analyser (Cedar), content-addressed and
reproducible builds (Nix), continuous reconciliation (Kubernetes), graded
enforcement levels with overrides (Sentinel), and a configurable failure
mode that is documented openly. Envoy's external authorizer denies by
default when the authorizer is unreachable; Gatekeeper admits by default.

**What it leaves to others.** None of the query or deployment work decides
*how much information* may leave a source, or whether a trained model may be
released. Their plans preserve meaning: an optimized query returns the same
answer as the naive one. A restriction plan changes the answer on purpose.
The data mesh literature states the governance goal, but a 2024 ACM
Computing Surveys review calls end-to-end governance automation with policy
as code "largely unexplored". LLM agent defences address control flow
hijacked by untrusted text, within one user's session; they do not address
many institutions' data across a federation, or the release of models.

**Where Dagents sits.** Its planner/Guard split is an instance of PDP/PEP
with a deny-biased PEP. Its typed planner applies "make illegal states
unrepresentable" to policy. Its separate planner process follows the
out-of-process policy engine pattern, but starts one process per call rather
than a daemon or sidecar. Its LMA is "code goes to data" without mobile
code: the code is installed at the site, not shipped there at run time. What
is less common in this prior art is the combination: one typed decision
function covering field protection, model-update protection, round
membership, quorum and release; four enforcement boundaries across the
lifecycle instead of one; audit records naming the hash of the decision
binary; and fail-closed behaviour with no switch to fail open. These are
combinations of known primitives, not new primitives.

### Key question: what is old, and what changes for governance of data and models

| Idea | Old and established (credit) | What changes when it governs distributed data and models |
|---|---|---|
| Move code to data | Mobile agents (Chess et al. 1995/1997; Lange and Oshima 1999); MapReduce locality (Dean and Ghemawat 2004/2008); edge computing (Shi et al. 2016) | The motive is legal and ethical (records may not leave), not bandwidth or latency. The worry is no longer only that the host and the agent harm each other; it is what the coordinator's code may learn. Dagents gives the site runner the guarded payload, not the raw records. |
| Source autonomy, export schemas | Federated databases (Sheth and Larson 1990); mediators (Wiederhold 1992) | Export becomes a per-request decision that depends on the requester's trust and the granularity, and it covers derived artefacts (model updates), not only rows. |
| Planner decides, executor executes | System R (Selinger et al. 1979); Presto (2019); GRAILS's own analogy | A query plan preserves the answer and minimises cost; a restriction plan reduces the answer and minimises disclosure. Dagents's planner is a table over a finite product of types, not a cost-based search. |
| Decision point separate from enforcement point | RFC 2753 (2000); XACML 3.0 (2013); OPA | The decision function is replicated at each site and identified by its binary hash in every audit record; failure to reach it always denies. |
| Typed, exhaustive decisions | ML pattern matching; Maranget (2007); Jane Street (Minsky 2008, 2011) | "Every combination is handled" becomes a governance property. It proves totality, not that each choice is right, and it holds only where non-exhaustive-match warnings stop the build. |
| Plan, review, apply; declarative desired state | Terraform; Kubernetes (Burns et al. 2016); Nix (2004/2006); GitOps | The "apply" step is a release of a model or result, which cannot be rolled back once others have copies. So aggregation yields only a candidate, and a missing metric blocks release. |
| Out-of-process policy engine with a failure mode | OPA sidecar/daemon; Envoy `ext_authz`; Kubernetes webhooks | Availability is given up: fail-open is not offered. |
| Trusted planner that never sees untrusted data | Dual LLM (Willison 2023); CaMeL (2025/2026); design patterns (2025) | Dagents's planner is not a language model and sees only metadata (classification, trust, granularity), never records. The threat is over-disclosure to a coordinator, not prompt injection, and Dagents tracks no information flow inside the computation. |

---

## 2. Entries

### A. Moving computation to data

#### `chess1997mobile` (and the 1995 IBM report)
- **Citation.** Chess, Harrison, Kershenbaum. "Mobile Agents: Are They a Good
  Idea?" In *Mobile Object Systems*, LNCS, Springer, 1997, pp. 25-45.
- **Verification.** Crossref, DOI 10.1007/3-540-62852-5_4. An "update"
  chapter follows at pp. 46-47 (DOI 10.1007/3-540-62852-5_5). The 1995 IBM
  research report version (author order Harrison, Chess, Kershenbaum) is
  listed under Unverified.
- **What it is.** An IBM assessment of programs that migrate between hosts to
  run near resources. It weighs the claimed benefits (less traffic, offline
  operation, local interaction) against alternatives such as remote
  procedure calls and installed servers, and treats security as the central
  open problem. Text not re-read in this session; this is its well-known
  conclusion.
- **D1** Where code runs. **D2** n/a. **D3** At the host the agent visits.
  **D4** Computation. **D5** not stated. **D6** not stated. **D7**
  Platform-specific agent runtimes.
- **Overlap with Dagents.** The LMA is the "computation at the resource" idea.
- **What it does that Dagents does not.** It analyses whether code mobility is
  needed at all. Dagents's answer matches the paper's alternative: the
  computation is installed at the site (an LMA service), not shipped there.

#### `lange1999seven`
- **Citation.** Lange, Oshima. "Seven Good Reasons for Mobile Agents." *CACM*
  42(3):88-89, 1999.
- **Verification.** Crossref, DOI 10.1145/295685.298136.
- **What it is.** A two-page argument for mobile agents by the designers of
  IBM Aglets. Its reasons include lower network load and less sensitivity
  to latency (the full list of seven was not re-checked in this session).
- **D1-D7** Not a governance work; D3 at the host, D4 computation.
- **Overlap.** Same motive as an LMA: run where the data is.
- **What it does that Dagents does not.** Autonomous migration and
  asynchronous operation of the computation; Dagents's agents do not move.

#### `dean2008mapreduce`
- **Citation.** Dean, Ghemawat. "MapReduce: Simplified Data Processing on
  Large Clusters." *CACM* 51(1):107-113, 2008 (first at OSDI 2004).
- **Verification.** Crossref, DOI 10.1145/1327452.1327492. OSDI 2004 version
  under Unverified.
- **What it is.** A programming model where users write map and reduce
  functions and a runtime schedules map tasks, preferring machines that
  already hold a replica of the input. It is the standard reference for data
  locality as a scheduling rule.
- **D1** Task placement. **D2** Runtime heuristic. **D3** Cluster nodes.
  **D4** Computation. **D5** n/a. **D6** Re-executes failed tasks. **D7**
  One cluster, one owner.
- **Overlap.** "Map at each site, combine centrally" is the shape of a
  federated round (LMA trains, GMA combines). Beurer-Kellner et al. (2025)
  also borrow this shape for their "LLM map-reduce" pattern.
- **What it does that Dagents does not.** Automatic re-execution and
  straggler handling; data placement chosen by the runtime. In Dagents the
  data owner fixes placement and no site's work can move elsewhere.

#### `shi2016edge`
- **Citation.** Shi, Cao, Zhang, Li, Xu. "Edge Computing: Vision and
  Challenges." *IEEE Internet of Things Journal* 3(5):637-646, 2016.
- **Verification.** Crossref, DOI 10.1109/JIOT.2016.2579198.
- **What it is.** A widely cited vision paper arguing for computing near data
  producers (devices, gateways) rather than in the cloud, with privacy and
  bandwidth among the reasons. Text not read in this session.
- **D1-D7** Placement, not governance; D3 edge nodes.
- **Overlap.** Same placement idea; Dagents's sites are institutions rather
  than devices.
- **What it does that Dagents does not.** Addresses device-scale resource
  limits and partial connectivity.

### B. Federated and decentralised data architectures

#### `sheth1990federated`
- **Citation.** Sheth, Larson. "Federated Database Systems for Managing
  Distributed, Heterogeneous, and Autonomous Databases." *ACM Computing
  Surveys* 22(3):183-236, 1990.
- **Verification.** Crossref, DOI 10.1145/96602.96604.
- **What it is.** The reference survey and taxonomy of federated database
  systems. It distinguishes tightly and loosely coupled federations, defines
  kinds of component autonomy, and gives a layered schema architecture in
  which each component publishes an *export schema*: the part of its data it
  makes available to the federation.
- **D1** What each source exports. **D2** Schema definitions, set by the
  component's administrator. **D3** At each component database. **D4** Data
  read. **D5** not stated. **D6** not stated. **D7** Database-specific.
- **Overlap.** Site autonomy and "the source decides what leaves" are the
  premise of the LMA and its `before_send` boundary.
- **What it does that Dagents does not.** Schema integration and query
  translation across heterogeneous sources. Dagents decides protection per
  request, not through a static export schema.

#### `wiederhold1992mediators`
- **Citation.** Wiederhold. "Mediators in the Architecture of Future
  Information Systems." *Computer* 25(3):38-49, 1992.
- **Verification.** Crossref, DOI 10.1109/2.121508.
- **What it is.** Proposes a middle layer of *mediators*, software modules that
  hold knowledge about sources and turn their data into information for
  applications above them. It is the origin of the mediator/wrapper
  architecture for information integration.
- **D1** Integration and abstraction. **D2** Mediator code. **D3** In the
  middle layer. **D4** Data read. **D5-D6** not stated. **D7** Per domain.
- **Overlap.** The GMA plays a mediator-like role: it combines what sites
  report without holding their data.
- **What it does that Dagents does not.** Semantic integration of
  heterogeneous sources. Dagents's GMA combines model updates and counts, not
  query results.

#### `sethi2019presto`
- **Citation.** Sethi, Traverso, Sundstrom, Phillips, Xie, Sun, Yegitbasi,
  Jin, Hwang, Shingte, Berner. "Presto: SQL on Everything." ICDE 2019,
  pp. 1802-1813.
- **Verification.** Crossref, DOI 10.1109/ICDE.2019.00196 (all 11 authors
  confirmed).
- **What it is.** Facebook's distributed SQL engine, which queries many
  storage systems through connectors and pushes work to them where it can.
  It is the main industrial example of query federation. (Trino, the
  community fork, is not covered here.)
- **D1** Query plan. **D2** Optimizer over SQL. **D3** Engine workers;
  pushdown into connectors. **D4** Data read. **D5** not stated. **D6** not
  stated. **D7** SQL; connector per source.
- **Overlap.** A central planner choosing what runs at each source.
- **What it does that Dagents does not.** Cost-based optimisation and
  pushdown across arbitrary sources. Access control there is a plug-in
  concern of the engine, not a planned transformation of the result.

#### `duggan2015bigdawg`
- **Citation.** Duggan, Elmore, Stonebraker, Balazinska, Howe, Kepner,
  Madden, Maier, Mattson, Zdonik. "The BigDAWG Polystore System." *SIGMOD
  Record* 44(2):11-16, 2015.
- **Verification.** Crossref, DOI 10.1145/2814710.2814713 (all 10 authors
  confirmed).
- **What it is.** A "polystore" that federates several engines with
  different data models (relational, array, text, streams) under one query
  interface, with "islands" of shared semantics and casts between engines.
- **D1** Which engine runs which part. **D2** Planner. **D3** Each engine.
  **D4** Data read, computation. **D5-D6** not stated. **D7** Research
  prototype.
- **Overlap.** One planner, many heterogeneous back ends.
- **What it does that Dagents does not.** Moves data between engines when
  that is faster; Dagents never moves records between sites.

#### `dehghani2019monolithic`, `dehghani2020principles`, `dehghani2022datamesh` (data mesh)
- **Citation.** Dehghani. "How to Move Beyond a Monolithic Data Lake to a
  Distributed Data Mesh," martinfowler.com, 20 May 2019; "Data Mesh
  Principles and Logical Architecture," martinfowler.com, December 2020;
  *Data Mesh: Delivering Data-Driven Value at Scale*, O'Reilly, 2022
  (ISBN 978-1-4920-9239-1).
- **Verification.** URLs and ISBN confirmed by search; the 2020 URL is also
  given in Goedegebuure et al. (ref. [41]). The texts themselves could not be
  fetched (host blocked).
- **What it is.** The industry proposal to decentralise analytical data by
  business domain. Four principles: domain ownership, data as a product, a
  self-serve data platform, and **federated computational governance**.
- **Federated computational governance, stated precisely.** Three parts,
  as described in the primary sources' secondary coverage and in the two
  academic papers below:
  1. *Federated:* a governance group of domain data product owners, platform
     owners and specialists (legal, security) sets a small set of **global
     rules** that apply to every data product and its interfaces
     (interoperability, identity, schema conventions, privacy
     classification). Everything else stays a domain-local decision.
  2. *Computational:* those rules are written as code (standards as code,
     policies as code, automated tests and monitoring) and **executed
     automatically by the platform**, not by people reviewing after the fact.
  3. *Embedded:* policy execution is **embedded in each data product** (the
     book's table of contents has a section titled "Embed Policy Execution
     in Each Data Product"; seen via search only), so enforcement is
     distributed while the rule set is shared.

  The widely quoted definition ("a decision making model led by the
  federation of domain data product owners and data platform product owners,
  with autonomy and domain-local decision making power, while creating and
  adhering to a set of global rules") was confirmed only in secondary
  sources; see Unverified.
- **D1** Global rules (interoperability, classification, access) plus local
  decisions. **D2** Policy as code, chosen by a federated group. **D3** In
  each data product (sidecar) or in the platform. **D4** Data publication and
  consumption. **D5** Monitoring; lineage. **D6** not stated. **D7**
  Organisation-level; vendor-neutral.
- **Overlap.** The closest industry statement of Dagents's idea: shared
  rules, decided once, executed automatically at each source, with domain
  knowledge (classifications) supplied by the domain. In Dagents the global
  rules are the OCaml planner, the domain knowledge comes from extensions,
  and execution is the Guard at each LMA.
- **What it does that Dagents does not.** A governance *process* (who sets
  rules, how they change), data products with owners and service levels, and
  interoperability standards. Dagents has no organisational model; who may
  change a classification is outside it.

#### `machado2022datamesh`
- **Citation.** Machado, Costa, Santos. "Data Mesh: Concepts and Principles of
  a Paradigm Shift in Data Architectures." *Procedia Computer Science*
  196:263-271, 2022.
- **Verification.** Crossref, DOI 10.1016/j.procs.2021.12.013. Not read
  (host blocked).
- **What it is.** One of the first academic accounts of data mesh. According
  to Goedegebuure et al. (2024, arXiv PDF p. 27, §7.1), this paper examines existing,
  mostly commercial, tools for building a data mesh and validates the
  authors' architectural model with a proof-of-concept implementation.
- **D1-D7** As for data mesh; D7 commercial tool stacks.
- **Overlap.** Same as data mesh.
- **What it does that Dagents does not.** Maps the principles onto existing
  commercial products.

#### `goedegebuure2024datamesh`
- **Citation.** Goedegebuure, Kumara, Driessen, Van Den Heuvel, Monsieur,
  Tamburri, Di Nucci. "Data Mesh: A Systematic Gray Literature Review." *ACM
  Computing Surveys* 57(1):1-36, 2024.
- **Verification.** Crossref, DOI 10.1145/3687301; arXiv 2304.01062 (read).
- **What it is.** A systematic review of practitioner literature on data
  mesh. Section 3.3 describes federated computational governance: a federated
  team defines and enforces organisation-wide standards, and automation
  covers classification (sensitivity labels), anonymisation and encryption,
  quality checks, retention and deletion, and monitoring.
- **D1** Standards, classification, access. **D2** Policy as code. **D3**
  Platform and data products. **D4** Publication, use. **D5** Monitoring.
  **D6** not stated. **D7** Vendor-neutral.
- **Overlap.** Names sensitivity labelling and anonymisation as the
  governance actions to automate, which is what the Dagents planner and
  Guard do.
- **What it does that Dagents does not.** Organisational change and roles.
- **Quote.** "the end-to-end data governance automation using the
  policy-as-code approach (i.e., computational governance) is largely
  unexplored" (arXiv PDF p. 26, §6, "Research Challenges").
  "Automation plays a significant role in the federated computational
  governance model to ensure it can effectively scale" (p. 12, §3.3).

#### `wider2023decentralized`
- **Citation.** Wider, Verma, Akhtar. "Decentralized Data Governance as Part
  of a Data Mesh Platform: Concepts and Approaches." IEEE ICWS 2023,
  pp. 746-754.
- **Verification.** Crossref, DOI 10.1109/ICWS60048.2023.00101; arXiv
  2307.02357 (read).
- **What it is.** An experience report from building a data mesh platform on
  AWS. Data protection experts define global sensitivity levels and the
  handling each level needs; domain teams tag their own data with those
  levels; the platform automates or checks the policy for each level. OPA is
  the policy engine. It compares a central gateway, token-based access and
  encryption at rest, and describes a per-product "sidecar".
- **D1** Access grant or deny per request. **D2** OPA (Rego), "policies as
  code". **D3** Gateway or sidecar per data product. **D4** Data access.
  **D5** not stated. **D6** A global default rule denies all access unless a
  more specific rule allows it. **D7** AWS and OPA in the reference build.
- **Overlap.** Very close in structure: global sensitivity levels, local
  tagging, automated handling per level, a default deny. Dagents's extensions
  play the role of domain tagging, and its planner the role of the global
  per-level policy.
- **What it does that Dagents does not.** Lets a domain-specific rule
  override a global rule (their example: a domain ACL grants access the global
  default denies). Dagents does not let a runtime registration replace an
  extension's classification.
- **Quotes (arXiv PDF).** "A sidecar is a library or process which is part
  of each data product but is maintained by the platform" (p. 4, §III).
  "A global default rule of the platform is to deny all access requests"
  (p. 6, §IV-B). "no single tool covers all aspects required for
  implementing federated computational governance" (p. 8, §IV-B).

### C. Query planning as the model for decisions

#### `selinger1979access` (and `kulkarni2025grails`)
- **Citation.** Selinger, Astrahan, Chamberlin, Lorie, Price. "Access Path
  Selection in a Relational Database Management System." SIGMOD 1979,
  pp. 23-34.
- **Verification.** Crossref, DOI 10.1145/582095.582099 (start page only;
  end page under Unverified).
- **What it is.** The System R optimizer: it turns a declarative SQL query
  into a plan by estimating costs from catalog statistics and searching join
  orders by dynamic programming. It established the split between a planner
  that chooses and an executor that runs.
- **D1** Which plan among equivalent ones. **D2** Cost model plus search.
  **D3** n/a (plan executed by the engine). **D4** Query execution. **D5**
  Plan can be shown (later EXPLAIN tools). **D6** n/a. **D7** One engine.
- **GRAILS's analogy.** GRAILS (`kulkarni2025grails`, p. 1517): "Much like the
  database query processor that produces a query plan for a given input
  query, the Restriction Planner is responsible for generating a Restriction
  Plan based on the KYU Profile and Data Protection Profile." It then calls
  the planner "a mapping system" from the two profiles to a strategy, i.e. a
  lookup, not a search.
- **Overlap.** Declarative request in, plan out, separate executor.
- **What it does that Dagents does not.** Search over alternatives with a
  cost model. Note the limit of the analogy: all of Selinger's candidate
  plans return the same answer, while restriction strategies (redact,
  generalise, aggregate, add noise) return different answers. A restriction
  planner chooses among non-equivalent outputs, so a "cost" would be a
  disclosure or utility measure, which neither GRAILS nor Dagents defines
  as an optimisation objective. Dagents's strategy table is a fixed mapping,
  as in GRAILS.

### D. Separating decisions from effects

#### `bernhardt2012boundaries`
- **Citation.** Bernhardt. "Boundaries." Conference talk, 2012
  (destroyallsoftware.com).
- **Verification.** URL confirmed by search; venue given as SCNA 2012 by the
  publisher and as RubyConf 2012 elsewhere.
- **What it is.** The talk that popularised "functional core, imperative
  shell": keep decisions in pure functions over simple values, and put I/O
  and mutation in a thin outer shell. Values, not objects, form the boundary
  between components.
- **D1-D7** A design pattern; D2 pure functions; D7 none.
- **Overlap.** Dagents applies it across a process and language boundary: the
  OCaml planner is the functional core; the Python LMA/GMA and the Guard are
  the shell. JSON values form the boundary.
- **What it does that Dagents does not.** Nothing beyond the pattern; the
  talk keeps the core and shell in one process and one language, which
  avoids serialisation and process start costs.

#### `cockburn2005hexagonal`
- **Citation.** Cockburn. "Hexagonal Architecture" (Ports and Adapters),
  alistair.cockburn.us, 2005.
- **Verification.** URL and year from secondary citations (exact 2005 date
  differs between sources).
- **What it is.** The application core talks to the outside world only
  through ports (interfaces); adapters connect each port to a technology.
  The core can then be tested without databases or user interfaces.
- **D1-D7** A design pattern; D7 none.
- **Overlap.** Dagents's agent layout (`domain/`, `application/`,
  `adapters/`, `infrastructure/`) follows this, with stores, update channels
  and the federation engine behind interfaces.
- **What it does that Dagents does not.** n/a (a pattern).

#### `minsky2011ocaml`, `minsky2008caml`, `minsky_effective_ml_revisited`
- **Citation.** Minsky. "OCaml for the Masses." *CACM* 54(11):53-58, 2011
  (also *ACM Queue* 9(9), 2011). Minsky, Weeks. "Caml Trading: Experiences
  with Functional Programming on Wall Street." *JFP* 18(4):553-564, 2008.
  Minsky. "Effective ML Revisited," Jane Street blog.
- **Verification.** Crossref, DOIs 10.1145/2018396.2018413,
  10.1145/2030256.2038036, 10.1017/S095679680800676X; blog URL by search.
- **What it is.** Industrial reports from Jane Street on using OCaml for
  trading systems, arguing that algebraic data types, pattern matching and
  exhaustiveness checks catch errors at compile time and make code easier to
  review. The maxim "make illegal states unrepresentable" comes from
  Minsky's "Effective ML" talk (2010) and the "Revisited" blog post; whether
  the CACM article uses the exact phrase was not checked.
- **D2** Typed functions. **D6** n/a. **D7** OCaml.
- **Overlap.** Dagents models sensitivity, trust, granularity and strategy as
  variants and matches every combination (`select_strategy` in
  `bindings/ocaml/lib/governance_compiler`).
- **What it does that Dagents does not.** Long production evidence at scale.

#### `maranget2007warnings`
- **Citation.** Maranget. "Warnings for Pattern Matching." *JFP*
  17(3):387-421, 2007.
- **Verification.** Crossref, DOI 10.1017/S0956796807006223.
- **What it is.** The algorithms OCaml uses to warn about non-exhaustive
  matches and unused match cases, with their formal basis.
- **D2** A static check of a typed decision function; analysable.
- **Overlap.** Dagents's claim that "adding a level fails to compile until
  every combination is handled" relies on this check.
- **What it does that Dagents does not.** n/a. An observation for the paper:
  in OCaml a non-exhaustive match is a *warning*; it stops a build only when
  warnings are treated as errors. The repository's dune files set no warning
  flags of their own, so the guarantee depends on the build profile used.
  This was not tested here (no OCaml toolchain in this environment).
  Exhaustiveness also proves only that every case has *some* answer, not
  that the answer is right.

### E. Compiling plans from typed specifications

#### `foster2011frenetic`, `anderson2014netkat`
- **Citation.** Foster et al. "Frenetic: A Network Programming Language."
  ICFP 2011, pp. 279-291. Anderson et al. "NetKAT: Semantic Foundations for
  Networks." POPL 2014, pp. 113-126.
- **Verification.** Crossref, DOIs 10.1145/2034773.2034812,
  10.1145/2535838.2535862.
- **What it is.** High-level languages for software-defined networks: a
  controller compiles one declarative, network-wide policy into rules for
  each switch. NetKAT gives the policy language a sound and complete
  equational theory, so two policies can be checked for equivalence.
- **D1** Forward, drop, modify packets. **D2** A typed declarative language
  with formal semantics; decidable equivalence. **D3** At every switch.
  **D4** Run time traffic. **D5** n/a. **D6** n/a. **D7** OpenFlow-style
  switches.
- **Overlap.** The closest precedent for "compile a decision from a typed
  specification, enforce it at many distributed points".
- **What it does that Dagents does not.** Formal semantics and automated
  equivalence and verification of policies. Dagents's planner is typed and
  tested, not formally verified. NetKAT compiles centrally and pushes
  rules; Dagents ships the planner binary to each site and decides there.

### F. Plan/apply and declarative deployment

#### `burns2016borg`, `kubernetes_controllers`
- **Citation.** Burns, Grant, Oppenheimer, Brewer, Wilkes. "Borg, Omega, and
  Kubernetes." *CACM* 59(5):50-57, 2016 (also *ACM Queue* 14(1)). Kubernetes
  documentation, "Controllers".
- **Verification.** Crossref, DOIs 10.1145/2890784, 10.1145/2898442.2898444;
  docs URL by search.
- **What it is.** Google's account of three cluster managers and the lessons
  carried into Kubernetes, including a declarative API of desired state and
  independent controllers that reconcile actual state towards it. The docs
  describe a controller as a control loop that moves current state towards
  the desired state in an object's spec.
- **D1** What should run. **D2** Declarative objects (YAML/JSON). **D3** In
  the cluster, by controllers. **D4** Deployment, operation. **D5** Object
  status, events. **D6** Admission webhooks have a configurable failure
  policy (see section G). **D7** Kubernetes.
- **Overlap.** Dagents's manifest compiler renders Kubernetes YAML and
  Compose files from a typed workload spec. GMA recovery after a restart
  ("keep or close" each unfinished round, decided by the planner) resembles
  level-triggered reconciliation.
- **What it does that Dagents does not.** Continuous reconciliation. Dagents
  renders manifests but has no controller, and per `CLAUDE.md` its manifests
  have not been validated on a live cluster.

#### `hashicorp_terraform_plan`, `hashicorp_sentinel_levels`
- **Citation.** HashiCorp. "terraform plan command"; "Sentinel: Enforcement
  Levels".
- **Verification.** Official docs found by search.
- **What it is.** Terraform computes an execution plan, which can be saved and
  then applied exactly as shown. Policy-as-code checks (Sentinel or OPA in
  HCP Terraform) run between plan and apply. Sentinel has three enforcement
  levels: *advisory* (may fail, with a warning), *soft mandatory* (must pass
  unless someone with override permission overrides it), *hard mandatory*
  (must pass; removing the policy is the only override).
- **D1** Whether a plan may be applied. **D2** Policy language over the plan.
  **D3** Between plan and apply. **D4** Deployment. **D5** Saved plan; run
  records. **D6** Set per policy by its enforcement level. **D7** HashiCorp.
- **Overlap.** Dagents's release flow has the same shape: aggregation yields
  a candidate (the plan), a release gate checks it, and approval releases it.
- **What it does that Dagents does not.** Graded enforcement levels with an
  audited override. Dagents's outcomes are permit, narrow and deny, and
  `CLAUDE.md` describes no override path for a failed release gate.

#### `opengitops_principles`
- **Citation.** OpenGitOps (CNCF GitOps Working Group). "GitOps Principles
  v1.0.0."
- **Verification.** opengitops.dev found by search.
- **What it is.** Four principles: desired state is declarative, versioned
  and immutable, pulled automatically by software agents, and continuously
  reconciled.
- **D1** Deployed state. **D2** Declarative files in version control. **D3**
  Agents in the target environment. **D4** Deployment. **D5** Version
  history. **D6** not stated. **D7** Vendor-neutral.
- **Overlap.** Dagents's policy (classifications in extensions, planner
  rules in OCaml) is versioned with the code, and the planner hash can be
  pinned (`DAGENTSC_SHA256`).
- **What it does that Dagents does not.** Pull-based, continuous
  reconciliation of the policy itself across sites.

#### `dolstra2004nix`, `dolstra2006purely`, `dolstra2008nixos`
- **Citation.** Dolstra, de Jonge, Visser. "Nix: A Safe and Policy-Free
  System for Software Deployment." LISA 2004, pp. 79-92. Dolstra. *The Purely
  Functional Software Deployment Model.* PhD thesis, Utrecht University,
  2006. Dolstra, Löh. "NixOS: A Purely Functional Linux Distribution." ICFP
  2008, pp. 367-378.
- **Verification.** LISA paper and thesis confirmed by search (USENIX and
  Utrecht pages; no DOI); NixOS by Crossref, DOI 10.1145/1411204.1411255.
- **What it is.** Deployment as a pure function: each component is stored
  under a path containing a cryptographic hash of all its build inputs, is
  never changed after it is built, and upgrades and rollbacks are atomic.
- **D1** What is installed. **D2** A lazy functional language. **D3** Each
  machine's store. **D4** Build, deployment. **D5** Input hashes identify
  every component. **D6** Atomic switch; old versions kept. **D7** Nix.
- **Overlap.** Dagents names the planner binary's SHA-256 in each audit
  record and can refuse any other binary. That identifies *which* decision
  function ran. The governance module's header also states that it has
  "no I/O, no clock, and no randomness, so a restriction plan is
  reproducible from its request alone and can be replayed during an audit"
  (`bindings/ocaml/lib/governance_compiler/dagents_governance_compiler.ml`),
  which is the same purity argument Nix makes for builds.
- **What it does that Dagents does not.** Input-addressed, reproducible
  builds. Dagents's hash is of the built binary, which `CLAUDE.md` says
  differs between builds; the same source cannot be shown to give the same
  hash.

#### `cue_lang`, `jsonnet_lang` (typed configuration languages)
- **Citation.** CUE (cuelang.org); Jsonnet (jsonnet.org). Dhall is under
  Unverified.
- **Verification.** Homepages confirmed through the Go module proxy (CUE,
  module `cuelang.org/go`, v0.17.1) and PyPI (Jsonnet, v0.22.0).
- **What it is.** Languages for generating configuration. CUE unifies types,
  constraints and values in one lattice, so a schema and data can be checked
  against each other. Jsonnet is a templating language that emits JSON.
- **D2** Configuration languages; CUE constraints are checkable.
- **Overlap.** Same target as Dagents's manifest compiler (validated
  deployment files from a specification), by a different route.
- **What it does that Dagents does not.** Lets users write and check
  constraints themselves. Dagents's rules are compiled OCaml, so changing a
  rule means changing code, not configuration.

#### `rahman2019systematic`, `johann2017morris` (infrastructure as code)
- **Citation.** Rahman, Mahdavi-Hezaveh, Williams. "A Systematic Mapping
  Study of Infrastructure as Code Research." *IST* 108:65-77, 2019. Johann.
  "Kief Morris on Infrastructure as Code." *IEEE Software* 34(1):117-120,
  2017.
- **Verification.** Crossref, DOIs 10.1016/j.infsof.2018.12.004,
  10.1109/MS.2017.13; arXiv 1807.04872 (abstract read).
- **What it is.** Rahman et al. screened 33,887 publications and kept 31 on
  IaC; 52% proposed a framework or tool, and the authors call for research on
  defects and security flaws in IaC scripts. Johann interviews the author of
  the main practitioner book on IaC (book under Unverified).
- **D1-D7** Deployment practice; D7 tool-specific.
- **Overlap.** Dagents's manifest compiler is a form of IaC generation.
- **What it does that Dagents does not.** Empirical evidence about defects
  in deployment code, which Dagents's Python and OCaml renderers would also
  face; `CLAUDE.md` records that the two renderers once drifted apart.

### G. Policy decision points, process isolation, and failure modes

#### `yavatkar2000framework`, `oasis2013xacml` (PDP/PEP)
- **Citation.** Yavatkar, Pendarakis, Guerin. RFC 2753, "A Framework for
  Policy-based Admission Control," 2000. OASIS. XACML Version 3.0, OASIS
  Standard, 22 January 2013.
- **Verification.** RFC 2753 by Crossref, DOI 10.17487/RFC2753. XACML spec
  URL and date by search; normative text not read.
- **What it is.** RFC 2753 introduced, for network admission control, the
  split between a Policy Enforcement Point (PEP) and a Policy Decision Point
  (PDP). XACML generalised it for authorisation, adding a Policy
  Administration Point (PAP), a Policy Information Point (PIP), and
  *obligations* that the PDP returns and the PEP must carry out. XACML also
  defines a *deny-biased* PEP, which permits only on an explicit Permit it can
  fully carry out, and denies on every other result, including errors.
- **D1** Permit/deny plus obligations. **D2** XML policy language. **D3** At
  the PEP. **D4** Access. **D5** not stated in what was read. **D6**
  Deny-biased or permit-biased PEP, chosen by the implementer. **D7**
  Vendor-neutral standards.
- **Overlap.** The established vocabulary for Dagents's design: planner =
  PDP, Guard = PEP, extension registry = PAP, classifications and trust
  scores = PIP, restriction strategies (redact, generalise, aggregate, clip,
  add noise) = obligations, fail closed = deny-biased PEP. The paper should
  use and credit this vocabulary.
- **What it does that Dagents does not.** A standard request/response format
  and policy language that independent products implement.

#### `opa_deploy`, `cncf_opa_selfassessment`, `gatekeeper_failing_closed`
- **Citation.** Open Policy Agent, "How to Deploy OPA"; CNCF TAG Security,
  "OPA Security Self-Assessment"; OPA Gatekeeper, "Failing Closed".
- **Verification.** Official pages found by search; content as reported by
  search, not read in full.
- **What it is.** OPA is a general policy engine (policies in Rego) that
  runs as a host-level daemon, a sidecar, or an embedded Go library. The CNCF
  self-assessment argues for running it next to the service so it "shares
  fate" with it, for availability and speed. Gatekeeper runs OPA as a
  Kubernetes admission webhook; its main validating webhook defaults to
  `failurePolicy: Ignore` (fail open on webhook errors), and its docs explain
  how to switch to `Fail`. A second Gatekeeper webhook, which protects a
  namespace exemption label, defaults to `Fail`.
- **D1** Permit/deny, or a structured decision. **D2** Rego. **D3** Sidecar,
  daemon or library at each service; admission control in Kubernetes. **D4**
  Access; deployment admission. **D5** not checked here (see Unverified).
  **D6** Configurable; Gatekeeper defaults to fail open. **D7** Vendor-neutral.
- **Overlap.** Dagents also keeps the decision engine out of the application
  process and close to the data (the planner binary is on each LMA).
- **What it does that Dagents does not.** A long-running engine with policy
  bundles and per-decision logs, so no process start per call; one engine
  for many domains. The trade-off the Gatekeeper docs state openly
  (availability against enforcement) is one Dagents settles one way: it
  always denies.

#### `envoy_extauthz`
- **Citation.** Envoy, "External Authorization" (architecture overview,
  v1.7.1).
- **Verification.** Official docs found by search.
- **What it is.** Envoy's `ext_authz` filter asks an external authorisation
  service (often OPA) about each request. If the service is unavailable, the
  `failure_mode_allow` setting decides; the default is `false`, so the
  request is denied.
- **D1** Permit/deny. **D2** Delegated to the external service. **D3** At the
  proxy. **D4** Request handling. **D5** Statistics counters. **D6**
  Fail-closed by default, fail-open by setting. **D7** Envoy.
- **Overlap.** Same fail-closed default as Dagents.
- **What it does that Dagents does not.** Offers fail-open as an option.
  Search results also point to a 2023 Envoy advisory in which
  `failure_mode_allow: true` allowed requests through after a malformed
  message; it is an example of why the setting is risky (advisory not read).

#### `reis2009isolating`
- **Citation.** Reis, Gribble. "Isolating Web Programs in Modern Browser
  Architectures." EuroSys 2009, pp. 219-232.
- **Verification.** Crossref, DOI 10.1145/1519065.1519090.
- **What it is.** A browser architecture that runs web programs in separate
  operating-system processes so that a crash or compromise in one does not
  affect the others; a key reference for multi-process isolation.
- **D3** Process boundary. **D6** A crash stays in its process.
- **Overlap.** Dagents's reason for running `dagentsc` as a subprocess
  ("isolates failures, lets each side be upgraded on its own") is this
  argument.
- **What it does that Dagents does not.** OS-level sandboxing of the child.
  Dagents limits the planner's time and input and output size and kills its
  process group, but `CLAUDE.md` does not describe a sandbox.

#### `cutler2024cedar`
- **Citation.** Cutler et al. (15 authors). "Cedar: A New Language for
  Expressive, Fast, Safe, and Analyzable Authorization." *PACMPL* 8(OOPSLA1):
  670-697, 2024.
- **Verification.** Crossref, DOI 10.1145/3649835.
- **What it is.** Amazon's authorisation language, designed so that policies
  can be analysed automatically (by translation to SMT) and with its
  evaluator and validator formally modelled and proved in a proof assistant,
  then checked against the production implementation by differential
  testing.
- **D1** Permit/deny. **D2** A policy language with formal semantics and an
  analyser. **D3** In the application, as a library, or as a managed service.
  **D4** Access. **D5** not stated. **D6** Default deny. **D7** Language is
  open source.
- **Overlap.** Same aim as Dagents's typed planner: decisions that can be
  reasoned about, not just executed.
- **What it does that Dagents does not.** Formal proofs and automated policy
  analysis (for example, whether one policy set allows more than another).
  Dagents relies on types, exhaustiveness and tests.

### H. What "agent" means, and runtime enforcement for LLM agents

#### `wooldridge1995intelligent`
- **Citation.** Wooldridge, Jennings. "Intelligent Agents: Theory and
  Practice." *Knowledge Engineering Review* 10(2):115-152, 1995.
- **Verification.** Crossref, DOI 10.1017/S0269888900008122.
- **What it is.** The classic definition of a software agent in the
  multi-agent systems tradition: a "weak notion" of agency with autonomy,
  social ability, reactivity and pro-activeness.
- **Overlap.** Dagents's LMA and GMA are agents in this older,
  non-LLM sense: autonomous services that act for a site or a coordinator
  and talk to each other. They contain no language model.
- **For the paper.** Readers will read "agent" as "LLM agent". The naming
  clash is real in Dagents's own history: the package was called `agents`
  before 0.1.0a1, and `openai-agents` installs a top-level module of the same
  name (`CLAUDE.md`, "The old name").

#### `wu2023autogen`
- **Citation.** Wu et al. "AutoGen: Enabling Next-Gen LLM Applications via
  Multi-Agent Conversation." arXiv 2308.08155, 2023.
- **Verification.** arXiv (abstract read).
- **What it is.** A framework where "conversable" agents, each backed by an
  LLM, a human or tools, solve tasks by exchanging messages. Interaction
  patterns are programmed in natural language or code.
- **D1** What to do next (chosen by the model). **D2** LLM plus code. **D3**
  In the agent loop. **D6** not stated.
- **Overlap.** Multi-agent coordination only in name.
- **What it does that Dagents does not.** Open-ended task solving.

#### `langgraph_docs`, `anthropic2024building`
- **Citation.** LangChain, "LangGraph Overview" (docs). Anthropic, "Building
  Effective Agents," 19 December 2024.
- **Verification.** Docs URL via PyPI metadata and search; Anthropic post by
  search.
- **What it is.** LangGraph is a low-level orchestration runtime: a graph of
  nodes and edges over shared state, mixing deterministic and LLM-driven
  steps, with durable execution and human-in-the-loop interrupts.
  Anthropic's post distinguishes *workflows* ("systems where LLMs and tools
  are orchestrated through predefined code paths") from *agents* ("systems
  where LLMs dynamically direct their own processes and tool usage").
- **Overlap.** By Anthropic's definition, Dagents is neither: it has no LLM.
  Its control flow is fixed code, closest to a "workflow" without a model.
- **What it does that Dagents does not.** Durable, resumable graph execution
  as a general runtime (Dagents has round recovery for one protocol only).

#### `mcp2025specification`, `a2a_protocol`
- **Citation.** Model Context Protocol Specification, version 2025-11-25.
  Agent2Agent (A2A) Protocol, a2a-protocol.org.
- **Verification.** MCP spec page by search, homepage via PyPI `mcp`
  metadata; A2A homepage via PyPI `a2a-sdk` metadata; A2A's donation to the
  Linux Foundation (June 2025) via InfoQ coverage found by search.
- **What it is.** MCP connects LLM applications (hosts, with clients inside
  them) to servers that expose *tools*, *resources* and *prompts*, using
  JSON-RPC 2.0 with capability negotiation. A2A lets independent, opaque
  agents discover each other through "Agent Cards" and exchange tasks and
  artifacts over JSON-RPC on HTTP.
- **D1-D3** Protocols; they define roles and messages, not a policy decision
  procedure. **D7** Vendor-neutral.
- **Overlap.** A2A's opacity (agents do not expose internal state or tools)
  resembles the rule that the GMA never reads site rows. Dagents's agents
  also talk over HTTP with bearer tokens of fixed scopes.
- **What it does that Dagents does not.** Standard discovery and
  interoperability between vendors. Dagents's LMA/GMA API is its own.

#### `willison2023dual`
- **Citation.** Willison. "The Dual LLM Pattern for Building AI Assistants
  That Can Resist Prompt Injection." Blog, 25 April 2023.
- **Verification.** URL by search.
- **What it is.** A *privileged* LLM plans and calls tools but never sees
  untrusted content; a *quarantined* LLM reads untrusted content but has no
  tools. Its outputs reach the privileged side only as opaque references.
- **Overlap.** The same split as GRAILS and Dagents: the planner sees the
  request and metadata, never the records; the component that touches the
  records (the Guard) cannot change the plan.
- **What it does that Dagents does not.** n/a; a pattern. CaMeL shows the
  pattern alone still lets injected data steer tool *arguments*.

#### `debenedetti2026camel`
- **Citation.** Debenedetti, Shumailov, Fan, Hayes, Carlini, Fabian, Kern,
  Shi, Terzis, Tramèr. "Defeating Prompt Injections by Design." IEEE SaTML
  2026, pp. 587-618.
- **Verification.** Crossref, DOI 10.1109/SaTML68715.2026.00040; arXiv
  2503.18813 (read).
- **What it is.** CaMeL turns the user's query into code with a privileged
  LLM, runs it in a custom Python interpreter that tracks data flow and
  attaches *capabilities* (provenance and allowed readers) to every value,
  and checks security policies before each tool call. A quarantined LLM
  parses untrusted data into typed values.
- **D1** Whether a tool call may run, given where its arguments came from.
  **D2** Policies written as Python functions returning Allowed or Denied
  with a reason. **D3** At each tool call, in the interpreter. **D4** Agent
  execution. **D5** Data-flow graph per value. **D6** Execution halts on a
  failed check; in deployment, a violation would ask the user instead.
  **D7** One agent and its tools.
- **Overlap.** Planner/executor separation; deterministic policy checks
  outside the model; a security argument that does not depend on the model
  behaving.
- **What it does that Dagents does not.** Information-flow tracking inside
  the computation. Dagents checks data at four boundaries but does not
  track what the site runner computes from the guarded payload.
- **Differences.** CaMeL's planner is an LLM and its plan is a program;
  Dagents's planner is a deterministic function and its plan is a list of
  per-field protections. CaMeL chose Python over a DSL for policies;
  Dagents chose a typed compiled language.
- **Quotes and numbers (arXiv PDF).** "solving 77% of tasks with provable
  security (compared to 84% with an undefended system) in AgentDojo"
  (abstract, p. 1). "We opted to use Python code over a custom DSL to allow
  for arbitrary logic to be implemented within the policy" (p. 9, §5.2).
  "In a real-world application, executions that violate security policies
  will not be blocked, but they will require user confirmation" (p. 11,
  §5.4). Named failure mode: "The P-LLM cannot write a plan based on data it
  can't read" (p. 11, §6), which also applies to any metadata-only planner.

#### `beurerkellner2025design`
- **Citation.** Beurer-Kellner et al. (14 authors). "Design Patterns for
  Securing LLM Agents against Prompt Injections." arXiv 2506.08837, 2025.
- **Verification.** arXiv (read).
- **What it is.** Six patterns that isolate untrusted data from an agent's
  control flow: action-selector, plan-then-execute, LLM map-reduce, dual
  LLM, code-then-execute and context-minimisation, with case studies.
- **D1** Which actions untrusted input can trigger. **D2** Architecture,
  not a policy language. **D3** At pattern boundaries. **D6** not stated.
- **Overlap.** "Plan-then-execute" and "LLM map-reduce" have the shape of
  Dagents's round: a fixed plan, isolated work per data source, a reduce
  step that sees only results.
- **What it does that Dagents does not.** Compares utility and security of
  each pattern on case studies.
- **Quotes (arXiv PDF).** "once an LLM agent has ingested untrusted input, it
  must be constrained so that it is impossible for that input to trigger any
  consequential actions" (p. 4, §3). "This design pattern mirrors the
  map-reduce framework for distributed computations (Dean & Ghemawat, 2008)"
  (p. 6).

#### `shi2025progent`
- **Citation.** Shi, He, Wang, Li, Wu, Guo, Song. "Progent: Securing AI Agents
  with Privilege Control." arXiv 2504.11703, 2025.
- **Verification.** arXiv (read).
- **What it is.** Privilege policies as symbolic rules over tool names and
  arguments, checked deterministically on every tool call. An LLM proposes
  the initial policy and updates; an SMT solver classifies each update as a
  narrowing (applied automatically) or an expansion (needs approval), so
  privileges can only shrink without approval.
- **D1** Allow/block a tool call. **D2** Symbolic rules; SMT-checked updates.
  **D3** Tool-call boundary. **D4** Agent execution. **D5** not stated.
  **D6** Calls that match no rule are blocked by default, and the agent gets
  an error explaining the denial. **D7** Shown with LangChain and the OpenAI
  Agents SDK.
- **Overlap.** Default deny; deterministic enforcement outside the model;
  refusal to widen silently, which resembles Dagents's rule that a runtime
  registration cannot replace an extension's classification.
- **What it does that Dagents does not.** Policies that change during a task,
  with a machine-checked distinction between narrowing and widening.
- **Quote (arXiv PDF p. 3).** "calls that match no rule are blocked by
  default".

#### `wang2026agentspec`
- **Citation.** Wang, Poskitt, Sun. "AgentSpec: Customizable Runtime
  Enforcement for Safe and Reliable LLM Agents." ICSE 2026, pp. 2938-2950.
- **Verification.** Crossref, DOI 10.1145/3744916.3764546; arXiv 2503.18666
  (read).
- **What it is.** A small DSL of rules with a *trigger* (an agent event), a
  *predicate* and an *enforcement*: `user_inspection`, `llm_self_examine`,
  `invoke_action(...)` or `stop`. Built on LangChain and evaluated on code
  agents, embodied agents and autonomous driving.
- **D1** Stop, ask a user, self-check, or run a corrective action. **D2** DSL;
  rules can be generated by an LLM. **D3** At agent events. **D4** Agent
  execution. **D6** not stated.
- **Overlap.** Enforcement at named lifecycle events, like Dagents's four
  boundaries.
- **What it does that Dagents does not.** User-defined triggers and
  corrective actions. One of its enforcements (`llm_self_examine`) is not
  deterministic, which Dagents would not allow in a decision.
- **Numbers (abstract).** Prevents unsafe executions in over 90% of code agent
  cases; LLM-generated rules (OpenAI o1) reach 95.56% precision and 70.96%
  recall for embodied agents.

#### `costa2025fides`
- **Citation.** Costa et al. "Securing AI Agents with Information-Flow
  Control." arXiv 2505.23643, 2025.
- **Verification.** arXiv (abstract read).
- **What it is.** A formal model of agent planners, a characterisation of
  what dynamic taint tracking can enforce, and Fides, a planner that tracks
  confidentiality and integrity labels and enforces policies
  deterministically, with primitives for hiding information from the model.
- **D2** Labels and deterministic policies; formal model. **D3** In the
  planner. **D5** Labels on data.
- **Overlap.** Confidentiality labels play the role of Dagents's field
  sensitivity.
- **What it does that Dagents does not.** Label propagation through the
  computation and a formal account of what can be enforced.

#### `wu2025isolategpt`
- **Citation.** Wu, Roesner, Kohno, Zhang, Iqbal. "IsolateGPT: An Execution
  Isolation Architecture for LLM-Based Agentic Systems." NDSS 2025.
- **Verification.** Crossref, DOI 10.14722/ndss.2025.241131; arXiv 2403.04960
  (abstract read).
- **What it is.** Execution isolation for third-party LLM apps, by analogy
  with isolation in earlier computing platforms; it reports overhead under
  30% for three-quarters of tested queries.
- **Overlap.** Isolation as the security mechanism, as in Dagents's per-site
  stores and separate planner process.
- **What it does that Dagents does not.** Isolates mutually untrusting
  components that share one user's session.

**LLM agents and Dagents: summary of similarities and differences.**
Similar: deterministic checks outside the component that handles untrusted
material; a planner that does not see the data; default deny; enforcement at
fixed events. Different: (1) the LLM defences protect one user's
data and tools inside one session from injected instructions; Dagents
protects many institutions' records from over-disclosure to a coordinator
and to each other; (2) their planners are LLMs or interpreters of
LLM-written code; Dagents's planner is a fixed typed function; (3) they gate
actions (tool calls); Dagents gates data and models (reads, training input,
outgoing updates, releases); (4) several of them track information flow,
which Dagents does not; (5) none of them decides about federated rounds,
quorum or model release.

---

## 3. Unverified

Items below are known or likely but were not confirmed in this session. The
shared web-search budget ran out before the last searches; a follow-up
session with more searches could confirm them.

1. **Harrison, Chess, Kershenbaum (1995)**, "Mobile Agents: Are They a Good
   Idea?", IBM Research Report RC 19887. The report number and date are from
   memory; only the 1997 LNCS chapter was verified. The LNCS volume number
   (believed to be 1222) was not returned by Crossref.
2. **Dean, Ghemawat (2004)**, OSDI 2004 version of MapReduce (pages believed
   137-150). Not verified; cite the 2008 CACM version.
3. **Selinger et al. (1979)** end page 34. Crossref gives only the start page.
4. **Dehghani's definition of federated computational governance** (the
   quoted wording in section B). Found only in secondary sources (Atlan,
   Commons, Level Up). Check against the 2020 article or the book before
   quoting. The book heading "Embed Policy Execution in Each Data Product"
   was seen in search summaries of the table of contents, not in the book.
5. **Minsky**: whether "make illegal states unrepresentable" appears in
   "OCaml for the Masses"; the date of the "Effective ML Revisited" post; the
   2010 date of the "Effective ML" talk.
6. **Lange and Oshima (1999)**: the full list of seven reasons.
7. **Shi et al. (2016)**: the paper's own definition of edge computing.
8. **Kief Morris**, *Infrastructure as Code*, O'Reilly (first edition 2016;
   later editions). Edition details not verified; Crossref has no record of
   the book.
9. **Dhall** (believed homepage dhall-lang.org): not confirmed.
10. **Kubernetes admission webhooks**: the API default `failurePolicy: Fail`
    for `admissionregistration.k8s.io/v1` was seen in API type comments and
    vendor docs, not on kubernetes.io.
11. **XACML 3.0** section numbers for the deny-biased and permit-biased PEP,
    and RFC 2753's "local PDP": seen in mailing-list quotes of drafts, not in
    the normative texts.
12. **OPA decision logs and signed bundles**: believed to exist; not checked.
13. **Envoy CVE-2023-27488** (the `failure_mode_allow: true` advisory): seen
    in a search summary only.
14. **Venues**: AutoGen (believed COLM 2024), Progent, Fides and the design
    patterns paper; none found on Crossref, so they are cited as arXiv.
15. **Bernhardt** talk venue (SCNA 2012 or RubyConf 2012) and **Cockburn**
    article date (sources give 2005-04-01 and 2005-09-05).
16. **Trino** as the renamed community fork of Presto: not checked.
17. **Burns et al. (2016)**, **Sheth and Larson (1990)**, **Wiederhold (1992)**,
    **Presto**, **BigDAWG**, **NetKAT**, **Cedar**: described from metadata
    and well-known content; full texts were not read in this session (hosts
    blocked). Claims are kept general for that reason.
18. **dune build profiles**: whether the repository's default and `static`
    profiles turn the non-exhaustive-match warning into an error. Not tested
    (no OCaml toolchain in this environment).
