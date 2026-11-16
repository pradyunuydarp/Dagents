# Family `policy`: access control, policy decision and enforcement, policy-as-code, information-flow control, privacy-compliance enforcement, tamper-evident audit logs

Prepared 2026-11-16. Academic metadata checked with Crossref (DOI lookups) or the arXiv API.
Products, standards and regulations cite official pages found by web search. Full text was read
for four papers (Cedar OOPSLA extended version arXiv:2403.04651, "How We Built Cedar"
arXiv:2407.01688, Blockaid arXiv:2205.06911, FACPL arXiv:1612.09339); quotes from those carry a
section or page. Other descriptions come from abstracts, official pages and search snippets, and
say so where it matters. The shared web-search budget ran out near the end of the work, so a few
details could not be checked again; they are listed under **Unverified**.

## Synthesis

**Shared assumption.** Every work here separates the policy from the code that obeys it.
Samarati and De Capitani di Vimercati call it policies versus mechanisms; IETF RFC 2753 (2000)
names the two sides Policy Decision Point (PDP) and Policy Enforcement Point (PEP); XACML, NIST ABAC,
OpenID AuthZEN (2026) and Cedar keep that split. GRAILS (Rails and Guard) and
Dagents (planner and Guard) follow the same pattern.

**What is decided.** The standard PDP answer is yes or no. XACML adds NotApplicable and
Indeterminate; AuthZEN 1.0 requires a boolean `decision`; Cedar returns Allow or Deny. "How much and in what form" is carried around that answer: XACML
obligations and advice, AuthZEN's optional `context`, Ranger's masking and row-filter policy types,
OPA's partial evaluation into SQL filters. Where transformation is the main product, the decision
and its enforcement are fused inside one query engine: Hippocratic databases rewrite queries to hide
cells, and Snowflake, Databricks, BigQuery and Lake Formation apply masks, row filters, minimum
group sizes and differential-privacy budgets at query time. Jeeves shows a viewer a policy-chosen
facet of a value instead of refusing. A per-field strategy with permit, narrow or deny, as GRAILS
and Dagents produce, is closest to these masking and aggregation policies, but expressed as a plan
returned by a separate decision process, like the mask type a Ranger plugin returns to its host
engine. Blockaid argues the other side: query modification "can alter query semantics in
unexpected ways and return misleading results" (§2), so it only blocks.

**Where enforcement happens.** Mostly at one store or chokepoint: a warehouse engine, a SQL
proxy (Blockaid, Qapla), storage (K9db), a kernel or runtime (Thoth, Riverbed, Resin, Sesame), or
IFC labels across one company's infrastructure (Meta Policy Zones). Ranger and Immuta administer
centrally and enforce in each engine. None of the works surveyed enforces at independently
governed sites under a coordinator that may not read their rows, which is Dagents's setting, and
none covers the release of a trained model.

**Analysability.** Cedar is the strongest comparison: Lean-proved properties (forbid overrides
permit, default deny), a sound and complete SMT encoding for questions such as "does this
refactoring change who is allowed", and differential testing of the Rust engine against the
model. Zelkova, Margrave and FACPL analyse AWS and XACML-style policies. Warehouse policies are SQL
functions, not analysable as a whole. Dagents's exhaustive match proves coverage of a finite input
space; it has no stated property proofs, change-impact analysis or model-versus-implementation
testing.

**When the decision point is unreachable.** Practice varies. COPS (RFC 2748) lets a local decision
point at the PEP decide in the absence of a PDP. XACML defines deny-biased, permit-biased and base
PEPs. Envoy's external authorization filter denies on error by default; OPA Gatekeeper admits on
error by default (`failurePolicy: Ignore`). Ranger plugins keep enforcing their last cached
policies. Blockaid blocks a query when every solver times out. Dagents's "deny when the planner is
unavailable" matches the deny-biased PEP. It has no fail-static cache and no dry-run stage like
Meta's logging mode, which measures what a new rule would block before it is enforced.

**Evidence.** OPA decision logs, Ranger audits and Immuta's acknowledgement records log
decisions. Tamper evidence is older work: Schneier and Kelsey's hash chain with evolving keys
(1999), Crosby and Wallach's Merkle-tree history with logarithmic proofs (2009), and Certificate
Transparency's publicly audited logs. Dagents's chain of record digests is the linking part of the
1999 design; it lacks forward-secure keys, external witnesses and logarithmic inclusion and
consistency proofs. HIPAA and the EU AI Act require activity logs but, in the text checked, do not
prescribe tamper evidence.

## Cross-cutting summary

| Work | Decides (D1) | Enforced at (D3) | Analysable (D2) | Decision point unreachable (D6) |
|---|---|---|---|---|
| RBAC, ABAC, UCON | permit/deny (UCON: also ongoing revocation) | not specified (model) | model-level only | not stated |
| PBAC, Hippocratic DBs | permit/deny by purpose; cell hiding (LeFevre) | central DBMS | no | not stated |
| RFC 2753 / COPS | admission yes/no | network device (PEP) | no | COPS: local PDP decides |
| XACML 3.0 | Permit/Deny/NotApplicable/Indeterminate + obligations, advice | PEP anywhere | yes, with external tools (Margrave, FACPL) | PEP bias: deny-, permit-biased or base |
| AuthZEN 1.0 (2026) | boolean + optional context | PEP anywhere | no | not stated in material read |
| OPA / Rego | any JSON; residual SQL filters | sidecar, library, admission webhook | partial evaluation only | Envoy default deny; Gatekeeper default admit |
| Cedar | Allow/Deny + diagnostics | application calls engine | yes: Lean proofs, SMT, DRT | not stated |
| Zanzibar | relation check (boolean) | central service | no | not stated |
| Zelkova | answers questions about policies | offline / config checks | yes (SMT) | n/a |
| Legalease/Grok, Policy Zones | allowed flows by purpose | data pipelines (central) | not checked in this session | Policy Zones: staged logging mode, then block |
| Thoth, Qapla, Blockaid, K9db, Sesame | allow/block, query rewrite (Qapla), deletion procedures (K9db) | kernel, DB proxy, storage, runtime at egress | Blockaid uses SMT per query | Blockaid blocks on solver timeout |
| Snowflake, Databricks, BigQuery, Lake Formation | masks, row filters, minimum group size, DP budget | inside the warehouse engine | no (SQL functions) | not applicable: decision and engine are one process |
| Ranger, Privacera, Immuta | allow/deny, mask type, row filter | each engine (plugin or push-down) | no | Ranger: last cached policies |
| Schneier-Kelsey, Crosby-Wallach, CT | n/a (evidence) | log | proofs of inclusion/consistency (Crosby-Wallach, CT) | n/a |

## Entries

### A. Access-control and privacy models

#### sandhu1996role, ferraiolo2001proposed — Role-based access control (RBAC) and the NIST RBAC standard
- Citation: R. S. Sandhu, E. J. Coyne, H. L. Feinstein, C. E. Youman, "Role-Based Access Control Models", *IEEE Computer* 29(2):38-47, 1996. D. F. Ferraiolo, R. Sandhu, S. Gavrila, D. R. Kuhn, R. Chandramouli, "Proposed NIST Standard for Role-Based Access Control", *ACM TISSEC* 4(3):224-274, 2001.
- Verification: DOI 10.1109/2.485845; DOI 10.1145/501978.501980 (Crossref).
- What it is: Users get permissions through roles. Sandhu et al. define a family of models (base, role hierarchies, constraints, and their combination); the NIST proposal turns this into a standard with core RBAC, hierarchical RBAC, and static and dynamic separation of duty.
- D1: permit/deny of an operation on an object, derived from role membership.
- D2: model with formal definitions; policies are configuration (role assignments). Constraints such as separation of duty are checkable.
- D3: not specified; the model is independent of where checks run.
- D4: data read and write; no computation or release stages.
- D5: not part of the model.
- D6: not stated.
- D7: domain-neutral.
- Overlap with Dagents: Dagents's "trust" input plays the part of a coarse role; agent tokens of fixed scopes (`agent_auth.py`) are role-like.
- What it does that Dagents does not: role hierarchies, separation of duty and administrative models for changing assignments.

#### hu2014guide — NIST SP 800-162, attribute-based access control (ABAC)
- Citation: V. C. Hu, D. Ferraiolo, R. Kuhn, A. Schnitzer, K. Sandlin, R. Miller, K. Scarfone, *Guide to Attribute Based Access Control (ABAC) Definition and Considerations*, NIST SP 800-162, 2014.
- Verification: DOI 10.6028/NIST.SP.800-162 (Crossref, type report).
- What it is: NIST's definition of ABAC: a decision computed from attributes of the subject, the object, the requested operation and the environment, against policy rules. It describes the functional components of an ABAC system, including the decision and enforcement points and the attribute and administration points, and discusses enterprise deployment.
- D1: permit/deny.
- D2: rules over attributes; the guide is language-neutral.
- D3: wherever a PEP sits; the guide treats PDP and PEP as separable functions.
- D4: access time only.
- D5: logging is discussed as an operational consideration, not specified.
- D6: not specified.
- D7: neutral.
- Overlap with Dagents: GRAILS's inputs (field sensitivity from metadata, user trust, requested granularity) are attributes in the ABAC sense; Dagents's classification comes from configuration, like ABAC object attributes.
- What it does that Dagents does not: a general attribute vocabulary and an attribute-provision architecture; Dagents fixes its attribute set (sensitivity, trust, granularity, purpose).

#### park2004ucon — Usage control, UCON_ABC
- Citation: J. Park and R. Sandhu, "The UCON_ABC Usage Control Model", *ACM TISSEC* 7(1):128-174, 2004.
- Verification: DOI 10.1145/984334.984339 (Crossref, with abstract).
- What it is: Generalises access control to usage control, combining Authorizations, oBligations and Conditions, with continuity (decisions during use, not only before) and mutability (attributes that change as a result of use).
- Quote (abstract, via Crossref): "The term usage control is a generalization of access control to cover authorizations, obligations, conditions, continuity (ongoing controls), and mutability."
- D1: permit/deny before and during use; revocation during use; obligations the subject must fulfil.
- D2: a model; decisions are predicates over subject and object attributes, with obligations and conditions as further decision factors.
- D3: not fixed by the model.
- D4: access and ongoing usage.
- D5: not specified.
- D6: not specified.
- D7: neutral.
- Overlap with Dagents: Dagents's model-version lifecycle and release gates are a narrow, state-changing control after use; a privacy budget that decreases with use would be UCON mutability.
- What it does that Dagents does not: ongoing decisions with revocation during use, and attribute updates as a first-class part of the decision model. Dagents decides per request and per status change.

#### byun2008purpose — Purpose-based access control (PBAC)
- Citation: J.-W. Byun and N. Li, "Purpose Based Access Control for Privacy Protection in Relational Database Systems", *The VLDB Journal* 17(4):603-619, 2008 (online 2006).
- Verification: DOI 10.1007/s00778-006-0023-0 (Crossref).
- What it is: Data elements carry intended purposes (allowed and prohibited) arranged in a purpose tree; each request carries an access purpose, which is authorised for users through roles. Access is allowed when the access purpose complies with the intended purposes. The paper extends this to relational databases with element-level labels and query modification.
- D1: permit/deny by purpose compliance, applied per data element.
- D2: purpose hierarchy and compliance rules defined formally; configuration in the database.
- D3: central DBMS.
- D4: data read.
- D5: not stated in the material checked.
- D6: not stated.
- D7: relational databases; domain-neutral.
- Overlap with Dagents: Dagents's "approved purposes" contributed by condition packs are purpose labels; a request outside the approved purposes is refused.
- What it does that Dagents does not: a purpose hierarchy with allowed and prohibited purposes per element, and a formal compliance relation between access purpose and intended purpose. Dagents's purposes are a flat allow-list per condition pack (as described in the brief).

#### agrawal2002hippocratic, lefevre2004limiting, agrawal2004auditing — Hippocratic databases
- Citation: R. Agrawal, J. Kiernan, R. Srikant, Y. Xu, "Hippocratic Databases", VLDB 2002, pp. 143-154. K. LeFevre, R. Agrawal, V. Ercegovac, R. Ramakrishnan, Y. Xu, D. DeWitt, "Limiting Disclosure in Hippocratic Databases", VLDB 2004, pp. 108-119. R. Agrawal, R. Bayardo, C. Faloutsos, J. Kiernan, R. Rantzau, R. Srikant, "Auditing Compliance with a Hippocratic Database", VLDB 2004, pp. 516-527. Related: Agrawal et al., "Implementing P3P Using Database Technology", ICDE 2003, pp. 595-606 (agrawal2003implementing).
- Verification: DOI 10.1016/B978-155860869-6/50021-4; 10.1016/B978-012088469-8.50013-9; 10.1016/B978-012088469-8.50047-4; 10.1109/ICDE.2003.1260824 (Crossref).
- What it is: A database that takes responsibility for the privacy of the data it holds, organised around principles such as purpose specification, consent, limited collection, use, disclosure and retention, and compliance. LeFevre et al. enforce disclosure limits at cell level by rewriting queries according to purpose, recipient and the data owner's choices; Agrawal et al. (2004) audit after the fact which queries disclosed specified data.
- D1: whether a cell may be disclosed for a given purpose and recipient; disallowed cells are hidden by rewriting (a transformation, not a refusal).
- D2: privacy policy and choices stored as metadata in the database.
- D3: central DBMS.
- D4: data read; retention; audit after the fact.
- D5: query logs used for compliance audits (2004 paper).
- D6: not stated.
- D7: relational DBMS; domain-neutral.
- Overlap with Dagents: purpose-bound reads; per-field protection decided from metadata; the Guard's dropping of fields the request did not ask for is a limited-disclosure rule.
- What it does that Dagents does not: retention limits, consent choices per data owner, and a retrospective audit that answers "which queries disclosed this data". Blockaid's critique (below) applies to both: cell hiding changes query results silently.

#### powers2003epal, w3c2002p3p — Privacy policy languages EPAL and P3P
- Citation: C. Powers and M. Schunter (eds.), *Enterprise Privacy Authorization Language (EPAL 1.2)*, W3C Member Submission, 10 November 2003 (submitted by IBM). W3C, *The Platform for Privacy Preferences 1.0 (P3P1.0) Specification*, W3C Recommendation, 16 April 2002.
- Verification: https://www.w3.org/Submission/2003/SUBM-EPAL-20031110/ ; https://www.w3.org/TR/2002/REC-P3P-20020416/ (W3C pages found by search). P3P was marked obsolete by W3C on 30 August 2018.
- What it is: P3P lets a web site publish its privacy practices (purposes, recipients, retention) in machine-readable form for user agents to compare with user preferences; it does not enforce anything at the server. EPAL is a language for an enterprise's internal privacy authorization rules, built from data categories, user categories, purposes, actions, conditions and obligations, for enforcement inside the enterprise or between businesses.
- D1: P3P: none (a declaration). EPAL: allow/deny plus obligations.
- D2: XML policy languages.
- D3: P3P: client side (user agent). EPAL: enterprise PEPs.
- D4: collection and use.
- D5: not specified.
- D6: not specified.
- D7: P3P bound to the web; EPAL to enterprise IT. The W3C notes that the EPAL submission implies no endorsement.
- Overlap with Dagents: EPAL's vocabulary (data categories, purposes, obligations) is close to Dagents's classifications, approved purposes and per-field strategies.
- What it does that Dagents does not: obligations (for example, notify or delete later) and a standard exchange format between organisations. Lesson for the survey: P3P had "very few implementations" (search summary of W3C history) because nothing forced servers to honour it.

#### samarati2001access — Access control: policies, models and mechanisms
- Citation: P. Samarati and S. De Capitani di Vimercati, "Access Control: Policies, Models, and Mechanisms", in *Foundations of Security Analysis and Design* (FOSAD 2000 tutorial lectures), LNCS, Springer, 2001, pp. 137-196. Often cited as 2000.
- Verification: DOI 10.1007/3-540-45608-2_3 (Crossref; issued 2001).
- What it is: A tutorial survey of discretionary, mandatory and role-based access control that separates the policy (high-level rules), the model (its formal representation) and the mechanism (the code that enforces it), and discusses open versus closed policies and conflict resolution (for example, denials taking precedence).
- D1: permit/deny.
- D2: formal models.
- D3: mechanisms in general.
- D4: access.
- D5: n/a.
- D6: the closed-policy default (deny unless authorised) is the policy-level analogue of failing closed.
- D7: neutral.
- Overlap with Dagents: the policies/mechanisms separation is the layer rule (planner decides, Guard enforces); Dagents's default is closed.
- What it does that Dagents does not: explicit treatment of conflicts between positive and negative authorisations and of administrative delegation.

### B. The PDP/PEP split and its standards

#### yavatkar2000framework, boyle2000cops, westerinen2001terminology — Origin of PDP and PEP (IETF)
- Citation: R. Yavatkar, D. Pendarakis, R. Guerin, "A Framework for Policy-based Admission Control", RFC 2753, January 2000. J. Boyle, R. Cohen, S. Herzog, R. Rajan, A. Sastry, "The COPS (Common Open Policy Service) Protocol", RFC 2748, January 2000. A. Westerinen et al., "Terminology for Policy-Based Management", RFC 3198, November 2001.
- Verification: DOI 10.17487/RFC2753; 10.17487/RFC2748; 10.17487/RFC3198 (Crossref).
- What it is: RFC 2753 defines the split for network admission control: "Policy Decision Point (PDP): The point where policy decisions are made. Policy Enforcement Point (PEP): The point where the policy decisions are actually enforced" (terminology section, as returned by search), with a client-server protocol between them (its Section 6). COPS is that protocol. RFC 3198 generalises the vocabulary for policy-based management.
- D1: admit or reject a resource reservation.
- D2: policy held by a policy server; protocol messages.
- D3: at network nodes; the framework allows enforcement at only some nodes, such as the boundary of an administrative domain.
- D4: n/a (network).
- D5: COPS has report messages from PEP to PDP.
- D6: COPS's introduction describes an optional Local PDP (LPDP) that "can be used by the device to make local policy decisions in the absence of a PDP" (search result of the RFC text). Early COPS drafts said the PEP should report events it admitted locally once the connection returns. This is neither fail-open nor fail-closed: it is fail-to-local-policy.
- D7: network QoS; the terms were later adopted by XACML and ABAC.
- Overlap with Dagents: the planner is a PDP called per decision, and the Guard is a PEP at each boundary; Dagents also places PEPs only at chosen boundaries.
- What it does that Dagents does not: a defined local fallback decision point and a reconciliation step after reconnection. What PDP/PEP leaves out, relevant to the survey: the decision is a yes/no on a request; nothing in the original framework covers transforming the data, ordering several PEPs along a pipeline, or proving the PDP correct.

#### oasis2013xacml, oasis2017xacml — XACML 3.0
- Citation: OASIS, *eXtensible Access Control Markup Language (XACML) Version 3.0*, OASIS Standard, January 2013; E. Rissanen (ed.), *XACML Version 3.0 Plus Errata 01*, OASIS Standard incorporating Approved Errata, 12 July 2017.
- Verification: https://docs.oasis-open.org/xacml/3.0/xacml-3.0-core-spec-os-en.html ; http://docs.oasis-open.org/xacml/3.0/errata01/os/xacml-3.0-core-spec-errata01-os-complete.html (OASIS pages found by search). Wikipedia reports a Version 4.0 Committee Specification Draft in February 2026 (not checked further).
- What it is: An XML policy language and architecture with PEP, PDP, policy information point (PIP), policy administration point (PAP) and a context handler. Policies combine rules with combining algorithms. A response is Permit, Deny, NotApplicable or Indeterminate, with obligations (which the PEP must discharge) and advice (which it may ignore).
- D1: four-valued decision plus obligations and advice. Transformations such as masking are possible only as obligations the PEP must understand.
- D2: policy language; analysable with external tools (Margrave, FACPL), not by the standard itself.
- D3: any PEP.
- D4: access.
- D5: obligations are often used for logging; no audit format is specified.
- D6: the standard defines PEP bias. In FACPL's formalisation of the XACML algorithms: "the deny-biased (resp., permit-biased) one enforces permit (resp., deny) only when all the corresponding obligations are correctly discharged, while enforces deny (resp., permit) in all other cases. Instead, the base algorithm leaves all decisions unchanged but ... enforces indet if an error occurs while discharging obligations" (Margheri et al., arXiv:1612.09339, §4.2). Secondary sources say XACML 3.0 Core §2.12 requires a PEP to deny unless it can discharge all obligations; the section number was not checked in the standard itself.
- D7: neutral; heavy XML tooling.
- Overlap with Dagents: Dagents's planner returns a plan the Guard must apply in full; an unapplied plan is a denial. This is the deny-biased PEP with obligations.
- What it does that Dagents does not: a standard policy language, combining algorithms across policy sets, and a separation between mandatory obligations and optional advice. What it leaves out: transformations are opaque obligations, so the PDP cannot reason about their effect, and nothing specifies the order of PEPs across stages.

#### margheri2019rigorous, fisler2005verification — Analysable XACML-style policies (FACPL, Margrave)
- Citation: A. Margheri, M. Masi, R. Pugliese, F. Tiezzi, "A Rigorous Framework for Specification, Analysis and Enforcement of Access Control Policies", *IEEE TSE* 45(1):2-33, 2019. K. Fisler, S. Krishnamurthi, L. A. Meyerovich, M. C. Tschantz, "Verification and Change-Impact Analysis of Access-Control Policies", ICSE 2005, pp. 196-205.
- Verification: DOI 10.1109/TSE.2017.2765640, arXiv:1612.09339 (read); DOI 10.1145/1062455.1062502 (Crossref).
- What it is: FACPL is a policy language with formal semantics for XACML-style evaluation, including missing attributes, errors and obligations, and with SMT-based analysis. Margrave verifies properties of XACML policies and computes the change impact between two versions.
- D1: XACML-style decisions; the PEP's enforcement algorithm is part of the semantics.
- D2: formal semantics; automated analysis.
- D3: PEP.
- D4: access.
- D5: n/a.
- D6: deny-biased, permit-biased and base PEP algorithms are formalised (quote above).
- D7: neutral.
- Overlap with Dagents: treating the decision as a pure function with a defined meaning for errors.
- What it does that Dagents does not: property checking and change-impact analysis between two policy versions ("which requests change outcome?"). FACPL's model also notes that "the enforced decision ... could differ from that of the PDP" (arXiv §2), which is the case when Dagents's Guard denies because it cannot apply a plan.

#### openid2026authzen — OpenID AuthZEN Authorization API 1.0 (2026)
- Citation: OpenID Foundation AuthZEN Working Group, *Authorization API 1.0*, Final Specification, January 2026 (approval announced 12 January 2026).
- Verification: https://openid.net/specs/authorization-api-1_0-final.html (found by search).
- What it is: A transport-agnostic JSON API, with an HTTPS binding, by which a PEP asks a PDP whether a subject may perform an action on a resource in a context. It also defines batched evaluations.
- D1: "a REQUIRED decision key with a boolean value, and an OPTIONAL context key"; for `false`, "The access request is denied and MUST NOT be permitted to go forward"; context may carry "obligations, reasons, or expiry information" (spec text as returned by search).
- D2: the API is neutral about the policy language.
- D3: any PEP.
- D4: access.
- D5: not part of the API.
- D6: not stated in the material read. A PEP "MAY choose to reject" a decision whose context it does not understand.
- D7: neutral; aims at interoperability between PDP vendors.
- Overlap with Dagents: Dagents's planner call is also a JSON request and response with a structured answer.
- What it does that Dagents does not: an interoperable wire format other PDPs and PEPs implement. It also shows that, as of 2026, the standard PDP answer is still a boolean; Dagents's per-field plan would travel as an opaque `context`.

### C. Policy-as-code engines

#### opa2026decisionlogs, opa2026filtering, styra2026compile, gatekeeper2026failing, envoy2026extauthz — Open Policy Agent (OPA), Rego, and their fail-open/closed settings
- Citation: Open Policy Agent documentation, "Decision Logs" and "Evaluating a Data Filter Policy"; Styra, "Data Filters Compilation API" (Enterprise OPA); OPA Gatekeeper, "Failing Closed"; Envoy, "External Authorization" filter.
- Verification: URLs in the .bib (official documentation found by search).
- What it is: A general-purpose policy engine (CNCF project) with the Datalog-inspired language Rego. It runs as a sidecar, a service or a library. Partial evaluation (the Compile API) evaluates a policy with unknown inputs and returns the residual conditions, which can be turned into SQL `WHERE` clauses so the database enforces the policy. Enterprise OPA adds column-masking rules evaluated in the engine. Decision logs record each decision with its input; a mask policy (`data.system.log.mask`) removes or replaces fields before upload.
- D1: allow/deny in most integrations, but a Rego decision can be any JSON value, so OPA can return a structure (for example a mask or a filter).
- D2: Rego policies; tests in Rego; partial evaluation; no published soundness proofs (Cedar's paper compares with Rego and reports Cedar 42.8x to 80.8x faster on its benchmarks, arXiv:2403.04651 §5.2).
- D3: where it is embedded: API gateway (Envoy), Kubernetes admission (Gatekeeper), application library, or the database through residual filters.
- D4: access, admission, configuration and deployment.
- D5: decision logs, with masking and size limits.
- D6: depends on the PEP. Envoy's `failure_mode_allow` "defaults to false": on an error or a communication failure with the authorization server "the filter will always return a Forbidden response" (API reference, as returned by search). Gatekeeper: "Currently Gatekeeper is defaulting to using failurePolicy: Ignore for admission request webhook errors" (Failing Closed page), that is, it admits on error unless the operator changes it to `Fail`.
- D7: neutral; widely used in cloud-native platforms.
- Overlap with Dagents: an external decision process called with JSON, decisions logged with their inputs, and residual decisions that a data store applies.
- What it does that Dagents does not: decision logs with configurable field masking; partial evaluation that pushes the decision into the store as a query filter; a test framework for policies; bundles distributed to many PEPs. It also shows that fail-open is a real default in widely used tools, which is a useful contrast for Dagents's fixed fail-closed rule.

#### cutler2024cedar, disselkoen2024how — Cedar and its verification
- Citation: J. W. Cutler, C. Disselkoen, A. Eline, S. He, K. Headley, M. Hicks, K. Hietala, E. Ioannidis, J. Kastner, A. Mamat, D. McAdams, M. McCutchen, N. Rungta, E. Torlak, A. M. Wells, "Cedar: A New Language for Expressive, Fast, Safe, and Analyzable Authorization", *PACMPL* 8(OOPSLA1):670-697, 2024. C. Disselkoen et al., "How We Built Cedar: A Verification-Guided Approach", FSE Companion '24, pp. 351-357.
- Verification: DOI 10.1145/3649835, arXiv:2403.04651 (read); DOI 10.1145/3663529.3663854, arXiv:2407.01688 (read).
- What it is: An authorization language used in AWS products, with permit and forbid policies over principals, actions, resources and a context, a schema-based validator, and a symbolic compiler to SMT. The language is modelled in Lean and implemented in Rust; differential random testing checks that the two agree.
- D1: Allow or Deny, plus diagnostics naming the determining policies and any policies that errored (arXiv §3.3, footnote 2).
- D2: typed, verified. Lean proofs of: "Forbid trumps permit", "Default deny: If no permit policy evaluates to true, the request is denied", "Explicit allow", "Sound slicing", "Validation soundness" (arXiv §3.5). The symbolic compiler gives "a decidable, sound, and complete encoding" to SMT (arXiv §1); analysis questions take "on average, 75.1 ms to encode and solve" (§1). "Policies have no side effects, and decisions are indifferent to the order that policies are considered" (§1); the authorizer "is guaranteed to terminate and always produce the same authorization decision" (§2.4).
- D3: the application calls the engine (library or service such as Amazon Verified Permissions).
- D4: access.
- D5: diagnostics per decision; no audit chain.
- D6: not stated for an unreachable engine. Within evaluation, default deny and forbid-overrides make errors in permit policies deny.
- D7: neutral; open source.
- Numbers: "the Cedar authorizer is 28.7x-35.2x faster than OpenFGA and 42.8x-80.8x faster than Rego" on the paper's benchmarks (§1). Verification-guided development "found and fixed 4 bugs in Cedar's policy validator, and DRT and PBT helped us find and fix 21 additional bugs" (arXiv:2407.01688, abstract).
- Overlap with Dagents: decisions as a pure, deterministic, terminating function separate from the application; a typed model; default deny. This is the closest prior art to Dagents's OCaml planner design.
- What it does that Dagents does not: machine-checked proofs of decision properties, an SMT encoding for comparing two policy sets, a schema validator, and differential testing of production code against a formal model. Cedar decides only yes or no; it has no transformation output and no notion of granularity or model release.

#### pang2019zanzibar — Google Zanzibar
- Citation: R. Pang, R. Cáceres, M. Burrows, Z. Chen, P. Dave, N. Germer, A. Golynski, K. Graney, N. Kang, L. Kissner, J. L. Korn, A. Parmar, C. D. Richards, M. Wang, "Zanzibar: Google's Consistent, Global Authorization System", USENIX ATC 2019, pp. 33-46.
- Verification: https://www.usenix.org/conference/atc19/presentation/pang (USENIX; not in Crossref). Author list: see Unverified.
- What it is: A global service that stores relation tuples (access control lists) and evaluates checks over them for Google services such as Calendar, Drive, Maps, Photos and YouTube. Its decisions respect the causal order of user actions, so a check cannot use an ACL older than the content it protects.
- Numbers (abstract, via search): "trillions of access control lists and millions of authorization requests per second"; 95th-percentile latency under 10 ms; availability above 99.999% over 3 years.
- D1: whether a user has a relation to an object (boolean).
- D2: namespace configuration language with relation rewrite rules; not formally analysed in the paper.
- D3: central service called by each application (each application is the PEP).
- D4: access.
- D5: not the focus.
- D6: not stated in the material checked.
- D7: Google-internal; open-source re-implementations exist (OpenFGA, SpiceDB).
- Overlap with Dagents: a central decision service used by many enforcement points.
- What it does that Dagents does not: consistency guarantees between policy data and protected content, and operation at very large scale. It does not decide transformations.

#### backes2018semantic — AWS Zelkova
- Citation: J. Backes, P. Bolignano, B. Cook, C. Dodge, A. Gacek, K. Luckow, N. Rungta, O. Tkachuk, C. Varming, "Semantic-based Automated Reasoning for AWS Access Policies using SMT", FMCAD 2018, pp. 1-9.
- Verification: DOI 10.23919/FMCAD.2018.8602994 (Crossref).
- What it is: Encodes AWS access-policy semantics into SMT to compare policies and check properties, such as whether a resource is publicly accessible. The abstract (via search) says it solves a PSPACE-complete problem and is "invoked many millions of times daily". Later AWS posts say it powers IAM Access Analyzer custom policy checks and AWS Config rules.
- D1: answers questions about policies (is X allowed by any request? is policy A more permissive than B?), not individual requests.
- D2: SMT; sound analysis.
- D3: offline and at configuration time.
- D4: configuration and deployment.
- D5: findings reported to users.
- D6: n/a.
- D7: AWS policy language.
- Overlap with Dagents: none in mechanism; it is the analysis layer Dagents would need to answer "does this classification change permit more?".
- What it does that Dagents does not: automated reasoning over the whole policy space at production scale.

#### oso2026filtering — Oso and Polar data filtering
- Citation: Oso, "Filter Data" (Polar data filtering guide).
- Verification: https://docs.osohq.com/guides/data_filtering.html (official docs found by search).
- What it is: Oso evaluates authorization policies in the Polar language. Data filtering turns policy constraints into queries against the data store (SQL or ORM query objects) through adapters, so that a list request returns only authorised rows. Some Polar operators are not supported in data filtering.
- D1: allow/deny per resource, and a filter for collections.
- D2: logic-programming language.
- D3: application library or Oso Cloud; the filter runs in the database.
- D4: read.
- D5: not stated.
- D6: not stated.
- D7: application frameworks.
- Overlap with Dagents: a decision compiled into a constraint that the data store enforces.
- What it does that Dagents does not: generating store-native queries from a policy.

### D. Privacy compliance at scale

#### sen2014bootstrapping — Legalease and Grok at Bing
- Citation: S. Sen, S. Guha, A. Datta, S. K. Rajamani, J. Tsai, J. M. Wing, "Bootstrapping Privacy Compliance in Big Data Systems", IEEE S&P 2014, pp. 327-342.
- Verification: DOI 10.1109/SP.2014.28 (Crossref). Note: the DOI is .28, not .36.
- What it is (from memory of the paper; the abstract could not be re-checked in this session): Legalease is a policy language for privacy and legal teams, with nested allow and deny clauses over data types, purposes, roles and retention. Grok is a data inventory that labels data and code in a MapReduce-style pipeline with those attributes, bootstrapped by heuristics and refined over time. Together they check whether data flows in Bing's analysis pipeline comply with the policy.
- D1: whether a data flow complies with policy (allow/deny), reported as violations.
- D2: a policy language meant to be written by privacy and legal staff, not programmers (the paper's formal treatment was not re-read in this session).
- D3: central analysis over the pipeline; enforcement through reports to engineers.
- D4: data use in analytics jobs; retention.
- D5: compliance reports.
- D6: n/a (offline checking).
- D7: one company's big-data pipeline; approach is general.
- Overlap with Dagents: purposes and data classifications as configuration, separated from analysis code.
- What it does that Dagents does not: an inventory that discovers which code touches which data type, and a policy language aimed at legal staff. Numbers from the abstract are listed under Unverified.

#### meta2024purpose, meta2025policyzones, meta2026privacylib, marsala2025purpose — Meta Privacy Aware Infrastructure and Policy Zones
- Citation: Meta Engineering, "How Meta enforces purpose limitation via Privacy Aware Infrastructure at scale", 27 August 2024; "Policy Zones: How Meta enforces purpose limitation at scale in batch processing systems", 23 July 2025; post on PrivacyLib, 27 October 2026; D. Marsala, "Purpose Limitation with Policy Zones: What We Tried That Didn't Work", PEPR '25.
- Verification: engineering.fb.com URLs and the USENIX PEPR page (found by search; only excerpts were read).
- What it is: Policy Zones is an information-flow control (IFC) system inside Meta's Privacy Aware Infrastructure. Data assets carry annotations that state purpose-limitation requirements; when annotated data is processed, Policy Zones checks whether the processing is allowed and whether the data may flow downstream. The 2024 post says earlier "point checking controls did not scale" once requirements "crossed dozens of our systems". A zone runs in logging mode until its violations are remediated and is then moved to enforcement, after which a new write to a sink outside the zone "will be blocked automatically" (2025 post, excerpt). The 2026 post describes PrivacyLib, a library inside the clients of Meta's data systems; before writing a record, its logging integration asks Policy Zones whether the data may flow to the destination table.
- D1: whether a flow is allowed for the purposes attached to the data (allow/block).
- D2: annotations and zone configuration; not stated whether analysable.
- D3: across the infrastructure: warehouse, batch SQL, logging, through a shared client library.
- D4: collection (logging), processing, storage; includes data used for AI per later coverage (not checked).
- D5: violations logged in logging mode.
- D6: staged rollout: logging mode first, then enforcement; behaviour when Policy Zones itself fails is not stated in the excerpts.
- D7: one company's infrastructure.
- Overlap with Dagents: purpose limitation enforced by infrastructure instead of application code; per-asset annotations as configuration.
- What it does that Dagents does not: flow tracking across many systems (labels propagate through SQL transforms), and a logging-mode stage that measures what a new rule would block before enforcing it. The PEPR talk compares service-to-service policy propagation, one policy for all purposes, and a single server-side chokepoint, which is a useful design record for Dagents's four-boundary choice.

#### muya2025approach — Google, protecting AI training data
- Citation: C. Muya, J. Novak, C. Madison, B. Kamber, N. Vempati, J. Wiesner, D. Deutscher, "Our Approach to Protecting AI Training Data", Google, 2025 (Secure AI Framework series).
- Verification: https://research.google/pubs/our-approach-to-protecting-ai-training-data/ (title, authors and year from the Google Research listing via search).
- What it is: According to the search summary only, it describes Google's internal practices for training data and argues for model and data lineage tracking and policy checks on training data before a training job runs. The full text was not read; treat this description as provisional.
- D1: whether data may be used to train (as summarised).
- D2-D7: not checked.
- Overlap with Dagents: a check before training is Dagents's `before_train` boundary.
- What it does that Dagents does not: not established; read before citing. No peer-reviewed Google paper on purpose-limitation infrastructure was found in this session.

#### ethyca2026fideslang — Fides and fideslang (privacy as code)
- Citation: Ethyca, Fideslang taxonomy and the Fides tooling.
- Verification: https://github.com/ethyca/fideslang ; https://ethyca.github.io/fideslang/ (found by search).
- What it is: An open-source taxonomy for declaring, next to code and datasets, the data categories a system handles, the data uses (purposes), the data subjects and data qualifiers (how identifiable). Declarations are YAML files in a repository, and the Fides tooling evaluates them against policies, for example in CI. Ethyca says it donated fideslang to the IAB Tech Lab in 2024.
- D1: whether declared uses of declared data categories comply with policy.
- D2: declarative YAML; hierarchical labels.
- D3: build time and data-mapping tools; runtime enforcement depends on integrations.
- D4: design and deployment; data-subject requests in the Fides product.
- D5: data maps and reports.
- D6: n/a.
- D7: neutral, aimed at GDPR, CCPA and similar regimes.
- Overlap with Dagents: per-field classifications and approved purposes declared by the application, not by framework code.
- What it does that Dagents does not: a shared, hierarchical vocabulary of data categories and uses that other tools understand, and checks in CI before deployment.

### E. Information-flow control and policy-enforcing systems

#### yip2009improving — Resin
- Citation: A. Yip, X. Wang, N. Zeldovich, M. F. Kaashoek, "Improving Application Security with Data Flow Assertions", SOSP 2009, pp. 291-304.
- Verification: DOI 10.1145/1629575.1629604 (Crossref).
- What it is: Programmers attach policy objects to data; the language runtime propagates them as data flows, and filter objects at boundaries (output, files, database) check the assertions before data crosses.
- D1: whether data may cross a boundary; the policy code can also act (for example raise an error).
- D2: code written by the application developer.
- D3: at each I/O boundary in the language runtime.
- D4: read, computation, egress.
- D5: not stated.
- D6: an assertion failure stops the flow.
- D7: PHP and Python runtimes.
- Overlap with Dagents: checks at named boundaries of a computation (Dagents has four).
- What it does that Dagents does not: tracking the policy along with the data through the computation, instead of checking only at boundaries.

#### yang2012language, yang2016precise — Jeeves and Jacqueline
- Citation: J. Yang, K. Yessenov, A. Solar-Lezama, "A Language for Automatically Enforcing Privacy Policies", POPL 2012, pp. 85-96. J. Yang, T. Hance, T. H. Austin, A. Solar-Lezama, C. Flanagan, S. Chong, "Precise, Dynamic Information Flow for Database-Backed Applications", PLDI 2016, pp. 631-647.
- Verification: DOI 10.1145/2103656.2103669; DOI 10.1145/2908080.2908098 (Crossref).
- What it is: Jeeves separates privacy policies from the program: a sensitive value has a secret view and a public view, and the runtime chooses which view each output context (viewer) sees according to the policy. Jacqueline carries this into a Python web framework with a database, using faceted execution so that query results and stored records keep both views.
- D1: which view of a value a viewer sees: a transformation (the default value replaces the secret one), not a refusal.
- D2: policies in the language; the system guarantees that outputs follow the policies.
- D3: language runtime, at output.
- D4: computation and output.
- D5: not stated.
- D6: not applicable; the policy is evaluated in-process.
- D7: research languages and frameworks.
- Overlap with Dagents: the decision is "what form may this viewer see", which is the "narrow" outcome.
- What it does that Dagents does not: a semantic guarantee that the output depends on the secret only as the policy allows, across arbitrary computation.

#### giffin2012hails — Hails
- Citation: D. B. Giffin, A. Levy, D. Stefan, D. Terei, D. Mazières, J. C. Mitchell, A. Russo, "Hails: Protecting Data Privacy in Untrusted Web Applications", OSDI 2012, pp. 47-60; journal version *J. Computer Security* 25(4-5):427-461, 2017 (giffin2017hails).
- Verification: USENIX page https://www.usenix.org/conference/osdi12/technical-sessions/presentation/giffin (found by search); DOI 10.3233/JCS-15801 (Crossref, journal version).
- What it is: A Haskell web framework that adds mandatory access control and a declarative policy language to the model-view-controller pattern. Policies live with the data model; untrusted application code runs under information-flow control. Demonstrated with GitStar, a code-hosting site.
- D1: allow or block flows of labelled data.
- D2: declarative policies with the data model; IFC labels.
- D3: framework runtime.
- D4: read, computation, output.
- D5: not stated.
- D6: label violations stop the computation.
- D7: Haskell (LIO).
- Overlap with Dagents: the code that runs on data (Dagents's federated job from the coordinator) is untrusted and sees only what policy allows; Dagents gives the local runner the guarded payload for this reason.
- What it does that Dagents does not: language-level confinement of untrusted code.

#### elnikety2016thoth — Thoth
- Citation: E. Elnikety, A. Mehta, A. Vahldiek-Oberwagner, D. Garg, P. Druschel, "Thoth: Comprehensive Policy Compliance in Data Retrieval Systems", USENIX Security 2016, pp. 637-654.
- Verification: https://www.usenix.org/conference/usenixsecurity16/technical-sessions/presentation/elnikety (USENIX; found by search).
- What it is: A kernel-level compliance layer. Declarative policies are attached to files, key-value tuples and network connections, and Thoth tracks data flow to enforce them even when application code is buggy or compromised. The prototype is integrated with Apache Lucene with small changes (the search summary reports about 50 lines in a codebase of roughly 300,000).
- D1: allow or block flows; policies can express conditions such as declassification.
- D2: declarative policy language.
- D3: operating system kernel at the server.
- D4: retrieval and output.
- D5: not stated.
- D6: enforced in the kernel; not stated.
- D7: Linux; data retrieval systems.
- Overlap with Dagents: enforcement independent of the application's correctness; policies as data.
- What it does that Dagents does not: enforcement below the application, so a compromised application cannot bypass it.

#### mehta2017qapla — Qapla
- Citation: A. Mehta, E. Elnikety, K. Harvey, D. Garg, P. Druschel, "Qapla: Policy Compliance for Database-Backed Systems", USENIX Security 2017, pp. 1463-1479.
- Verification: https://www.usenix.org/conference/usenixsecurity17/technical-sessions/presentation/mehta (USENIX; found by search).
- What it is: Middleware at the database adapter. Policies are written in an SQL-like language, stored in the database, and enforced by rewriting each application query. Evaluated on HotCRP and an academic job-application system.
- D1: which rows and columns a query may return (query rewriting).
- D2: SQL-like policies.
- D3: between application and DBMS, independent of both.
- D4: read.
- D5: not stated.
- D6: not stated.
- D7: SQL databases.
- Overlap with Dagents: transformation of what a read returns, decided outside the application.
- What it does that Dagents does not: column- and row-level policies over SQL with joins.

#### wang2019riverbed — Riverbed
- Citation: F. Wang, R. Ko, J. Mickens, "Riverbed: Enforcing User-defined Privacy Constraints in Distributed Web Services", NSDI 2019, pp. 615-630.
- Verification: https://www.usenix.org/conference/nsdi19/presentation/wang-frank (USENIX; found by search).
- What it is: Users write policies on how a remote service may process and store their data. A proxy attaches them to requests; the server must attest that it runs in a Riverbed-compatible managed runtime that enforces them with IFC. Data with compatible policies runs in the same "universe", an isolated instance of the whole service, so legacy code needs no labels.
- D1: allow or block flows (for example, to disk or to the network) per user policy.
- D2: a small policy language chosen by end users.
- D3: managed runtime on the server, verified by attestation.
- D4: processing and storage.
- D5: attestation of the runtime.
- D6: not stated.
- D7: web services.
- Numbers: worst-case slowdowns of 10% on several applications (abstract, via search).
- Overlap with Dagents: the party that owns the data sets the policy, and evidence about the enforcing code is part of the protocol (Dagents names the planner binary's SHA-256 in every audit record).
- What it does that Dagents does not: remote attestation of the enforcing runtime, and isolation by policy class.

#### zhang2022blockaid — Blockaid
- Citation: W. Zhang, E. Sheng, M. Chang, A. Panda, M. Sagiv, S. Shenker, "Blockaid: Data Access Policy Enforcement for Web Applications", OSDI 2022, pp. 701-718.
- Verification: https://www.usenix.org/conference/osdi22/presentation/zhang ; arXiv:2205.06911 (read).
- What it is: A SQL proxy between a web application and its database. Policies are SQL view definitions parameterised by the request context. For each query, Blockaid checks with SMT solvers that the result is determined by the views given the queries already answered in the same request (the trace), and blocks it otherwise. Compliance decisions are generalised into cached decision templates.
- D1: allow or block a query; never rewrites it.
- D2: views; SMT-checked compliance per query; the paper states a soundness theorem (§4).
- D3: at the database connection.
- D4: read.
- D5: not stated.
- D6: fail-closed: "If a query is not compliant, or all solvers time out after 5 s, Blockaid throws a Java SQLException" (§7). False rejections can come from approximate rewriting, strong compliance or "solver timeouts" (§9).
- D7: SQL web applications; open source.
- Quote on transformation: query modification "lacks semantic transparency as it can alter query semantics in unexpected ways and return misleading results" (§2).
- Overlap with Dagents: fail-closed decisions by an external checker; decisions cached as templates (Dagents does not cache).
- What it does that Dagents does not: verifies each query against the policy with a solver, taking earlier answers into account. Its argument against silent transformation is a direct critique of "narrow" outcomes that the survey should address.

#### albab2023k9db — K9db
- Citation: K. Dak Albab, I. Sharma, J. Adam, B. Kilimnik, A. Jeyaraj, R. Paul, A. Agvanian, L. Spiegelberg, M. Schwarzkopf, "K9db: Privacy-Compliant Storage For Web Applications By Construction", OSDI 2023, pp. 99-116.
- Verification: https://www.usenix.org/conference/osdi23/presentation/albab (USENIX; found by search). Author list: see Unverified.
- What it is: A MySQL-compatible database. Schema annotations state data ownership and sharing; K9db organises storage per user (micro-databases), generates procedures for data retrieval (access requests) and deletion, and reports compliance errors when an application risks violating the GDPR.
- D1: how data is stored and which records belong in an access or deletion request; compliance errors at schema time.
- D2: schema annotations; checks at schema time.
- D3: storage layer.
- D4: storage, access requests, deletion.
- D5: compliance error reports.
- D6: not stated.
- D7: SQL web applications; open source.
- Overlap with Dagents: per-owner partitioning of storage; Dagents keeps one store (schema and role) per hospital.
- What it does that Dagents does not: data-subject rights (access and erasure) generated from the schema.

#### ferreira2023rulekeeper — RuleKeeper
- Citation: M. Ferreira, T. Brito, J. Fragoso Santos, N. Santos, "RuleKeeper: GDPR-Aware Personal Data Compliance for Web Frameworks", IEEE S&P 2023, pp. 2817-2834.
- Verification: DOI 10.1109/SP46215.2023.10179395 (Crossref).
- What it is: Developers write a GDPR manifest; RuleKeeper generates the application's data protection policy from it and enforces it with static analysis and runtime access control, so business logic cannot use personal data for purposes users did not consent to. Prototype for the MERN stack, evaluated on four applications.
- D1: whether a use of personal data is allowed for a purpose with consent.
- D2: manifest; static analysis plus runtime checks.
- D3: web framework runtime.
- D4: processing.
- D5: not checked.
- D6: not checked.
- D7: JavaScript web stack.
- Overlap with Dagents: purpose-bound use declared by the application and enforced by the framework.
- What it does that Dagents does not: per-user consent as an input, and static analysis of application code.

#### albab2024sesame — Sesame
- Citation: K. Dak Albab, A. Agvanian, A. Aby, C. Tiffany, A. Portland, S. Ridley, M. Schwarzkopf, "Sesame: Practical End-to-End Privacy Compliance with Policy Containers and Privacy Regions", SOSP 2024, pp. 709-725.
- Verification: DOI 10.1145/3694715.3695984 (Crossref).
- What it is: A Rust web framework. Data is wrapped in policy containers that carry its policy; code that operates on the data runs in privacy regions, protected by sandboxing and a static analysis; the policy is checked before data leaves the system. Custom I/O through unsupported libraries uses critical regions, which a code reviewer must review and sign. The authors report that automated guarantees cover 95% of application code in their case studies (search summary of the abstract).
- D1: whether data may leave through a given sink.
- D2: policies as code attached to data; static analysis; human sign-off for critical regions.
- D3: at egress, inside the application.
- D4: computation and egress.
- D5: signatures on reviewed regions.
- D6: not checked.
- D7: Rust web applications.
- Overlap with Dagents: the check before data leaves (Dagents's `before_send`) and evidence about the code allowed to touch data.
- What it does that Dagents does not: binding policy to data through the whole computation, and a signed human review as part of the enforcement chain.

#### kalinowski2025making — Comparative evaluation of GDPR-enforcement frameworks
- Citation: F. Kalinowski, D. Klein, M. Johns, V. Moonsamy, "Making Web Applications GDPR Compliant: A Comparative Evaluation of GDPR-Enforcement Frameworks", *PoPETs* 2025(4):777-794.
- Verification: DOI 10.56553/popets-2025-0157 (Crossref).
- What it is: Derives GDPR requirements that have technical implications for software, builds benchmarks, and evaluates three frameworks that separate application code from GDPR rules and decide whether each data-processing operation is permitted. Finding (search summary): each framework, if configured correctly, enforces a different subset of the requirements. Which three frameworks were evaluated was not confirmed.
- D1-D7: evaluation paper; not applicable as a system.
- Overlap with Dagents: a benchmark method the survey could apply to Dagents's own claims.
- What it does that Dagents does not: an external, requirement-by-requirement evaluation.

### F. Industry data-access governance

#### snowflake2026masking, snowflake2026rowaccess, snowflake2026aggregation, snowflake2024projection, snowflake2026dp — Snowflake
- Citation: Snowflake documentation: CREATE MASKING POLICY; Understanding row access policies; Aggregation policies (and entity-level privacy); Aggregation and projection policies release notes (15 February 2024); Differential privacy and privacy budgets.
- Verification: URLs in the .bib (official docs found by search).
- What it is: Schema-level policy objects evaluated by the query engine. Masking policies mask column values at query time; row access policies decide which rows are visible; a column cannot be in both kinds of policy, and the row access policy is evaluated first. Aggregation policies set a minimum group size: groups below it are merged into a remainder group; with an entity key, the minimum counts unique entities, not rows. Projection policies control whether a column may be projected. Differential-privacy policies attach a privacy budget; "When a query would cause the cumulative privacy loss to exceed the privacy budget limit, the query fails until the privacy budget refreshes". Policies can be attached through tags (tag-based policies, described by Snowflake as ABAC).
- D1: transformation (mask), row selection, minimum group size, noise and budget, and refusal when the budget is spent. This is the "how much and in what form" decision.
- D2: SQL expressions (often `CASE` on role or context); not analysable as a set.
- D3: inside the warehouse engine.
- D4: read and query results; clean rooms for sharing.
- D5: account usage views of policy references (secondary sources); query history.
- D6: decision and enforcement are one process, so there is no separate decision point to lose; an exhausted budget fails the query.
- D7: one vendor.
- Overlap with Dagents: minimum group size (Dagents's minimum cohort), per-column masking chosen by attributes, and a privacy budget that limits release. These are the closest industrial counterparts to GRAILS-style strategies.
- What it does that Dagents does not: entity-level minimum group sizes, cumulative privacy-loss accounting across queries, projection control, and tag-based attachment across many tables.

#### databricks2026filters, databricks2026abac — Databricks Unity Catalog row filters and column masks
- Citation: Databricks documentation, "Row filters and column masks" and "Create and manage row filter and column mask policies" (ABAC).
- Verification: URLs in the .bib (official docs found by search).
- What it is: Row filters and column masks are SQL user-defined functions attached to tables and applied at query time. ABAC policies, driven by governed tags, can be attached at catalog, schema or table level and are evaluated dynamically; Databricks recommends them for consistent filtering across many tables. Limitations include no row-level security or masks on views, and older dedicated compute cannot read tables that have them.
- D1: row selection and column transformation.
- D2: SQL functions; tag-driven policies.
- D3: inside the engine (Unity Catalog-enabled compute).
- D4: read.
- D5: Unity Catalog audit logs (not checked in detail).
- D6: compute that cannot enforce the policies cannot read the table (as described in the limitation), which is fail-closed by construction.
- D7: one vendor.
- Overlap with Dagents: classification by tags and strategies chosen per column.
- What it does that Dagents does not: inheritance of policies down a catalog hierarchy.

#### google2026bqcls, google2026bqmasking — BigQuery column-level access control and dynamic data masking
- Citation: Google Cloud documentation, "Introduction to column-level access control" and "Introduction to data masking".
- Verification: URLs in the .bib (official docs found by search).
- What it is: A taxonomy of policy tags is attached to columns; access to tagged columns needs the Fine-Grained Reader role. Data masking, layered on the same tags or attached to a column as a data policy, shows null, default or hashed values to users with the Masked Reader role.
- D1: allow, deny, or a masked form per column.
- D2: taxonomy plus IAM roles; masking rules chosen from a fixed set.
- D3: inside BigQuery.
- D4: read.
- D5: Cloud audit logs (not checked).
- D6: not stated. A search summary of a third-party post says columns can be tagged without enforcement to see which queries would fail; see Unverified.
- D7: one vendor.
- Overlap with Dagents: a fixed menu of masking strategies chosen by classification, like GRAILS strategies.
- What it does that Dagents does not: a hierarchical tag taxonomy shared across datasets.

#### aws2026lakeformation — AWS Lake Formation data filters
- Citation: AWS documentation, "Data filtering and cell-level security in Lake Formation".
- Verification: https://docs.aws.amazon.com/lake-formation/latest/dg/data-filtering.html (official docs found by search).
- What it is: Data filters attached to Data Catalog tables provide column-level, row-level and cell-level security; a grant of SELECT can carry one or more filters. Filters apply to reads only. Cell-level security is not supported on nested columns, views or resource links; engine support varies (Athena engine version 3, Redshift Spectrum).
- D1: row and column selection.
- D2: filter definitions (row expressions, column lists).
- D3: central catalog decides; integrated engines enforce.
- D4: read.
- D5: AWS CloudTrail (not checked).
- D6: not stated.
- D7: one vendor.
- Overlap with Dagents: one catalog decision applied by several engines.
- What it does that Dagents does not: cell-level filters combining row and column conditions.

#### ranger2026integrating, cloudera2026rangerha, privacera2026ranger — Apache Ranger and Privacera
- Citation: Apache Ranger, "Integrating Applications with Apache Ranger"; Cloudera, CDP reference architecture checklist; Privacera, "Access Policy Definition in Apache Ranger".
- Verification: URLs in the .bib (official pages found by search).
- What it is: Ranger has a central policy administration service and plugins embedded in each data engine (Hive, HDFS, Kafka and others). A service definition declares resources, access types, and data-masking and row-filtering capabilities; the plugin evaluates authorization requests locally and the host application enforces the masking and filtering it returns; the plugin also generates audit events. Privacera is a commercial distribution that extends the Ranger model to cloud services, converts policies to native mechanisms where possible, and denies by default unless a policy allows.
- D1: allow/deny, mask type per column, row filter expression.
- D2: policies in the admin service; no analysis.
- D3: in each engine (embedded PDP and PEP), administered centrally.
- D4: read and write.
- D5: audit events to Solr and other destinations.
- D6: fail-static. "Ranger plugins keep a local cache of downloaded policy so the authorization decisions continue" when Admin instances go offline (Cloudera page, as returned by search). Plugins poll for policy changes (a 30-second default is reported by a community answer, not checked). Revocation therefore waits until the Admin returns.
- D7: Hadoop ecosystem; Privacera is one vendor.
- Overlap with Dagents: the plan-shaped answer (mask type, row filter) returned by a policy component and applied by the host is the closest architectural match to the planner and Guard. Ranger also decides centrally and enforces at each engine.
- What it does that Dagents does not: local policy caches so enforcement continues during an admin outage, and one administration point for many engines. The trade-off is that stale policies stay in force, which Dagents avoids by denying.

#### immuta2026snowflake, immuta2026purposes — Immuta
- Citation: Immuta documentation, "Snowflake Integration" reference guide; "Purposes explained".
- Verification: URLs in the .bib (official docs found by search).
- What it is: A policy platform that pushes its policies into each data platform's native features: in Snowflake, "Immuta policies are pushed down into Snowflake as Snowflake governance features (row access policies and masking policies)"; users keep querying Snowflake directly. Purpose-based controls let users join a project for a stated purpose after accepting an acknowledgement statement, and Immuta logs each response with purpose, time and statement text. One SaaS page says projects and purposes are deprecated in favour of "intents" (not checked further).
- D1: row selection, masking, purpose-based access.
- D2: Immuta's policy builder; compiled into native policies.
- D3: inside each platform (push-down).
- D4: read.
- D5: acknowledgement records; audit.
- D6: once pushed down, enforcement does not depend on Immuta being reachable; changes wait until Immuta is back (inference from the push-down design; not stated in the material read).
- D7: works across vendors, but is one vendor itself.
- Overlap with Dagents: one policy compiled into enforcement at several stores; purposes recorded with the access.
- What it does that Dagents does not: a recorded acknowledgement of purpose by the human requester.

### G. Tamper-evident logging and audit requirements

#### schneier1999secure, ma2009new — Secure audit logs (hash chains with evolving keys) and forward-secure aggregate logs
- Citation: B. Schneier and J. Kelsey, "Secure Audit Logs to Support Computer Forensics", *ACM TISSEC* 2(2):159-176, 1999. D. Ma and G. Tsudik, "A New Approach to Secure Logging", *ACM Transactions on Storage* 5(1):1-21, 2009.
- Verification: DOI 10.1145/317087.317089; DOI 10.1145/1502777.1502779 (Crossref, with abstracts).
- What it is: Schneier and Kelsey keep logs on an untrusted machine: each entry is encrypted and linked into a hash chain, with keys evolved after each entry so that an attacker who takes the machine cannot read or undetectably change earlier entries. Abstract: "We describe a computationally cheap method for making all log entries generated prior to the logging machine's compromise impossible for the attacker to read, and also impossible to modify or destroy undetectably." Ma and Tsudik replace per-entry MACs with forward-secure sequential aggregate (FssAgg) signatures, with private and public verification and "without any reliance on online trusted third parties or secure hardware" (abstract).
- D1: n/a.
- D2: cryptographic construction.
- D3: on the logging machine; verification by a trusted party (Schneier-Kelsey) or anyone with the public key (Ma-Tsudik public scheme).
- D4: all stages that log.
- D5: tamper-evident logs.
- D6: entries before compromise stay protected; entries after compromise are not.
- D7: neutral.
- Overlap with Dagents: Dagents's audit record includes the digest of the previous record, which is the hash-chain part.
- What it does that Dagents does not: forward security (key evolution, so a later compromise cannot rewrite history), confidentiality of entries, and verification by a third party.

#### crosby2009efficient — Tamper-evident logging with Merkle-tree histories
- Citation: S. A. Crosby and D. S. Wallach, "Efficient Data Structures for Tamper-Evident Logging", 18th USENIX Security Symposium, 2009.
- Verification: https://www.usenix.org/conference/usenixsecurity09/technical-sessions/presentation/efficient-data-structures-tamper-evident (USENIX; found by search).
- What it is: A tree-based history (a Merkle tree over entries) that gives logarithmic-size proofs that an entry is in the log and that a later version of the log extends an earlier one, where hash chains need linear work to audit. It assumes an untrusted logger and auditors who keep commitments.
- D1-D4: n/a; logging.
- D5: tamper-evident logs with membership and consistency proofs.
- D6: n/a.
- D7: neutral.
- Overlap with Dagents: same goal as Dagents's chain.
- What it does that Dagents does not: efficient proofs for one entry and for append-only growth, so an auditor need not replay the whole chain. Dagents's GMA sees "each audit chain's state and length, never its records"; a Merkle commitment would let the coordinator check that a site's chain only grew, without reading records.

#### laurie2013certificate, laurie2021certificate — Certificate Transparency (RFC 6962, RFC 9162)
- Citation: B. Laurie, A. Langley, E. Kasper, "Certificate Transparency", RFC 6962, June 2013. B. Laurie, E. Messeri, R. Stradling, "Certificate Transparency Version 2.0", RFC 9162, December 2021.
- Verification: DOI 10.17487/RFC6962; DOI 10.17487/RFC9162 (Crossref).
- What it is: Public, append-only Merkle-tree logs of TLS certificates. Logs sign tree heads; anyone can request inclusion proofs and consistency proofs; monitors and auditors watch logs, so a misbehaving log can be detected. RFC 9162 revises the protocol (version 2.0).
- D1: n/a.
- D2: protocol.
- D3: independent log operators.
- D4: certificate issuance.
- D5: signed tree heads, inclusion and consistency proofs.
- D6: the logs are not on the issuance path for a decision; clients check that certificates carry proofs of logging.
- D7: Web PKI.
- Overlap with Dagents: append-only evidence.
- What it does that Dagents does not: independent witnesses. Dagents's chains are kept by the party they describe (each site's chain on its own store), so a site could rewrite its whole chain; publishing chain heads to a log that others watch would detect that.

#### eu2024aiact, hhs2026hipaa — Audit requirements in practice: EU AI Act Article 12 and HIPAA 164.312(b)
- Citation: Regulation (EU) 2024/1689 (Artificial Intelligence Act), Article 12 Record-keeping, OJ L 12 July 2024. 45 CFR 164.312(b), HIPAA Security Rule, audit controls.
- Verification: https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-12 (European Commission AI Act Service Desk, found by search); eCFR URL in the .bib (standard form, not opened).
- What it is: Article 12 requires that high-risk AI systems technically allow the automatic recording of events (logs) over the lifetime of the system, to support risk identification, post-market monitoring and monitoring by deployers; Article 26(6) requires deployers to keep logs under their control for at least six months (search summary). Some sources report that the Digital Omnibus on AI moves application dates (see Unverified). HIPAA 164.312(b) requires mechanisms that record and examine activity in systems containing electronic protected health information; review is a separate requirement (164.308(a)(1)(ii)(D), per secondary sources); 164.312(b) sets no retention period.
- D5: logging obligations. In the text checked, neither names a cryptographic integrity mechanism.
- D7: EU high-risk AI systems; US health data.
- Overlap with Dagents: Dagents's audit chain and job logs are the kind of record these rules ask for; the healthcare demo is the HIPAA-shaped case.
- What it does that Dagents does not: legal retention periods and review duties, which are organisational, not code.

## Unverified

- **Search budget.** The shared web-search budget ran out before these could be checked: the exact abstract of Sen et al. 2014, the P3P and EPAL author lists, the Jeeves POPL implementation details, and the full title of Meta's October 2026 PrivacyLib post.
- **Sen et al. 2014 numbers.** I recall the abstract stating that the deployed system checks compliance daily over millions of lines of ad hoc data-analysis code written by several thousand developers. Not re-checked; do not quote without reading the paper.
- **P3P authors.** I recall Massimo Marchiori as editor, with L. Cranor, M. Langheinrich, M. Marchiori, M. Presler-Marshall and J. Reagle as authors. The .bib uses W3C as author instead.
- **EPAL authors.** The W3C submission names C. Powers and M. Schunter as editors (confirmed by search). I recall P. Ashley, S. Hada, G. Karjoth, C. Powers and M. Schunter as authors; not confirmed.
- **RFC 2748 editor.** Crossref lists Boyle, Cohen, Herzog, Rajan and Sastry. I recall the RFC header naming D. Durham as editor; not checked.
- **RFC 2753 and LPDP.** I recall RFC 2753 also describing a Local PDP at the PEP; search confirmed the LPDP only in RFC 2748.
- **XACML section numbers.** The claims that XACML 3.0 Core §2.12 requires a PEP to deny unless it can discharge all obligations, and that PEP bias is defined in §7, come from secondary sources; check against the standard. The XACML 4.0 draft (February 2026) was seen only on Wikipedia.
- **Zanzibar author list.** USENIX and search results confirm Pang plus 13 co-authors, including Kissner, Parmar, Germer, Graney and Korn. The full list in the .bib (with Cáceres, Burrows, Chen, Dave, Golynski, Kang, Richards, Wang) is from memory and consistent with the dblp key PangCBCDGGGKKKP19.
- **K9db author list.** From memory, consistent with the dblp key AlbabSAKJPASS23; not checked against the paper.
- **Crosby and Wallach pages.** A USPTO citation gives pp. 317-334; not confirmed from USENIX, so omitted from the .bib.
- **Samarati and De Capitani di Vimercati LNCS volume.** Commonly given as LNCS 2171; Crossref did not return the volume, so it is omitted.
- **OPA decisions as arbitrary JSON.** The OPA documentation's statement that decisions are not limited to allow/deny is recalled, not re-fetched; the masking and filter features are confirmed.
- **Meta scale numbers.** A search summary attributed to Meta "millions of daily data flows" and "trillions of consent checks per hour" and PrivacyLib "billions of checks every second". These are the company's claims, seen only in summaries; not quoted above.
- **BigQuery monitor-only mode.** Tagging columns without enforcing, to see which queries would fail, came from a third-party post (Pythian) in a search summary; not confirmed in Google's docs.
- **Ranger polling interval and historical fail-open.** The 30-second default poll comes from a community answer. Apache JIRA RANGER-466 describes an older design in which a plugin with no local cache skipped enforcement and logged a warning; treat as history, not current behaviour.
- **Immuta deprecation.** A SaaS page says projects and purposes are deprecated in favour of "intents"; version-specific, not checked.
- **EU AI Act dates.** Sources disagree on when Article 12 applies after the Digital Omnibus on AI (2 August 2026, 2 December 2027 or 2 August 2028 by category). Check the Official Journal.
- **Kalinowski et al. 2025.** Which three frameworks were evaluated was not confirmed.
- **Google training-data paper.** Content known only from a search summary.
- **GDPR Article 30 (records of processing).** Not searched; not included.
