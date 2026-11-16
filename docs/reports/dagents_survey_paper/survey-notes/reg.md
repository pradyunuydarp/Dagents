# Family `reg`: how models are gated before release, and the rules that require it

Scope: (a) machine learning release governance in practice (MLOps papers, model
registries, deployment patterns, model risk management in banks, medical AI change
control) and (b) the regulations and standards that require minimum-necessary
access, purpose limitation, record-keeping, human oversight and controlled model
change, with exact provision numbers.

**How this was verified.** Academic metadata was checked against Crossref or arXiv
(DOIs and arXiv ids below). Regulations, guidance and product documentation were
found by web search. EUR-Lex, eCFR, fda.gov, iso.org and the Indian Gazette cannot be
reached from the sandbox, so their text was read only through search-result
excerpts. Each legal entry says which wording was confirmed in an excerpt and which
is recalled from the official text and should be checked before it is quoted in the
paper. The shared web-search budget ran out near the end of the work; the items it
left open are in **Unverified**.

---

## 1. Synthesis

**Shared assumptions.** The MLOps literature and the regulations share one
structure. A model version is an artefact that moves through states (trained,
evaluated, approved, deployed, retired), and each move needs evidence. They differ
on who decides. Production platforms (TFX, MLflow, SageMaker, Vertex AI) and the ML
Test Score automate the evidence: validation against a baseline, thresholds,
canaries and shadow traffic. The decision to promote stays in a pipeline step the
team writes, or with a person who changes a status field; registries store the
state but do not decide whether a transition is allowed. Banks (SR 11-7, PRA
SS1/23) and medical devices (GMLP, PCCP, AI Act Art. 43(4)) add an outside party:
independent validation in banks, the regulator for devices. Their idea for
controlled change is a pre-authorised envelope. The regulator approves in advance a
description of the changes, the protocol for making them and the acceptance
criteria. A change inside the envelope needs no new submission; a change outside
does. This is the closest regulatory analogue to a release gate written as a
function.

**Where decisions live.** Privacy law states principles (purpose limitation, data
minimisation, minimum necessary) as standards of reasonableness, not rules. HIPAA's
minimum necessary standard shows the split. For routine and recurring disclosures
it allows "standard protocols", a field set decided in advance that a program can
apply to each request. For every other request it requires criteria and review of
each request, which is a person. The EHDS turns this into institutions. A health
data access body issues a permit that names the purpose and the data. Processing
happens only in a secure processing environment that keeps logs of access for at
least one year. Only non-personal data, including anonymised statistical output, may
leave it. The AI Act requires logs the system records automatically (Art. 12),
testing against "prior defined metrics and probabilistic thresholds" (Art. 9(8)) and
human oversight measures (Art. 14), and leaves their content to providers and to
harmonised standards still in drafting.

**What the family does well.** It names the evidence reviewers ask for:
technical documentation, logs, risk files and change control plans. It covers stages
Dagents' description does not: monitoring after release, incident reporting,
rollback, shadow and canary deployment, quality management and impact assessment. Empirical work shows where gates fail: repeated approval of small
changes can let accuracy drift down ("bio-creep", Feng et al. 2021), and human
overseers are unreliable (Green 2022; Laux 2024; Sterz et al. 2024).

**What it leaves to others.** No regulation says how a per-request decision is
computed, how its logic is versioned, or how to show that the code that decided is
the code that was reviewed. Twelve AI Act compliance checkers tested in 2026 gave
orientation, not determinate answers (Tao et al. 2026). Formal work on HIPAA and the
GDPR (DeYoung et al. 2010; Garg et al. 2011; Basin et al. 2018) shows that part of
each law can be checked mechanically from logs and process models, while predicates
about purpose, belief or necessity stay with a person. Table 2 draws that boundary: a program can check that a request names an approved purpose, that only the fields
of the approved protocol leave, that a metric meets a threshold fixed in advance,
that a missing metric blocks release, that a named approver exists and is not the
requester, and that every decision is logged with the version of the logic. It cannot
check that the stated purpose is the real one, that a new purpose is compatible with
the old, that the thresholds are clinically right, or that the oversight is effective.

---

## 2. Which obligations a program can check

"Yes" means a program can make and log the check per request or per release without
a person. "Partly" means a program can enforce a decision a person made in advance.
"No" means a person or a document is needed. Provision numbers were confirmed as
described in each entry in section 4; check flagged wording before quoting.

| # | Obligation | Provisions | Automatic? | What a program can check (per request or release) | What needs a person or a document | Evidence that satisfies it |
|---|---|---|---|---|---|---|
| 1 | Purpose limitation | GDPR 5(1)(b), 6(4); EHDS 53, 54, 67, 68; DPDP s.5, s.6(1); HIPAA 164.502(b)(1) ("intended purpose") | Partly | The request names a purpose; the purpose is on the approved list or in the permit; the permit is in date; the data asked for is in the permit | Approving a purpose; the GDPR 6(4) compatibility test for a new purpose; whether the stated purpose is the real one | Per-request log of purpose and permit id; register of approved purposes with who approved each and when; permits |
| 2 | Data minimisation / minimum necessary | GDPR 5(1)(c), 25(2); HIPAA 164.502(b), 164.514(d)(2)-(5); EHDS 66; DPDP s.6(1); AI Act 10(5) (special categories for bias work) | Partly | Releasing only the fields of the approved protocol for this purpose and role; dropping unrequested fields; refusing a whole record (164.514(d)(5)) | Deciding what is "necessary" for a purpose; review of each non-routine request (164.514(d)(3)(ii), (d)(4)(iii)) | The protocol per purpose; per-request field-level plan and what was released |
| 3 | De-identification / anonymised output | HIPAA 164.514(b)(1) expert determination, (b)(2) Safe Harbor; 164.514(e) limited data set; GDPR 89(1); EHDS 66(2)-(3), 69, 73 (download review) | Partly | Safe Harbor removal of the 18 identifiers; small-cell suppression and minimum cohort; noise or clipping with a stated parameter | Expert determination of "very small" risk; Safe Harbor's "no actual knowledge" condition; whether an output is legally anonymous | Suppression and noise rules applied, per release; expert report; download review record |
| 4 | Automatic event logs | AI Act 12(1)-(2), 19(1), 26(6); EHDS 73; HIPAA 164.312(b), 164.528; DPDP Rules r.6; ISO/IEC 42001 A.6.2.8 | Yes | Writing a record for every decision, with time, actor, inputs, plan and outcome; tamper evidence (hash chain, signing); retention for the minimum period | Choosing which events are "relevant" (Art. 12(2)); reviewing the logs | The log itself, its integrity proof and the retention setting |
| 5 | Version of the deciding logic is traceable | No provision asks for it per record. It supports AI Act 12(2)(a) (spotting substantial modifications) and 11 with Annex IV; FDA PCCP final guidance (version control); ISO/IEC 42001 A.6.2.7 | Yes | Recording the hash or version of the code or model that decided, in each record | None, once the practice is defined | Records carrying the version; build provenance |
| 6 | Records of processing activities | GDPR 30(1) (a)-(g) | Partly | Generating the register's purposes, data categories and recipients from the system's configuration | Keeping the register complete and current | The Art. 30 register |
| 7 | Accountability ("able to demonstrate") | GDPR 5(2), 24; DPDP s.8(1) | No (aggregate) | Supplying the logs and reports that make up the demonstration | Assembling the case; policies | Logs plus policies plus DPIA plus training records |
| 8 | Impact assessment | GDPR 35(1), 35(7); DPDP s.10(2)(c), Rules r.13; AI Act 27 (deployers); ISO/IEC 42001 A.5 | No | Refusing to enable a purpose that has no DPIA reference on file | Writing and signing the assessment | DPIA or FRIA document with sign-off |
| 9 | Research safeguards and approval | GDPR 89(1), 9(2)(j); HIPAA 164.512(i); DPDP s.17(2)(b) with Rules r.16 and Second Schedule; EHDS 53(1) | Partly | An ethics or IRB approval id exists and is in date; output carries no decision about one person | Granting the waiver or approval | IRB or privacy-board record; approval id in each request log |
| 10 | Data governance for training data | AI Act 10(2)-(4); GMLP principles 3-5; ISO/IEC 42001 A.7.2-A.7.6 | Partly | Schema, completeness and range checks; train and test sets independent (GMLP 4); recording data provenance per release | Judging representativeness for the intended population; the bias examination (10(2)(f)-(g)) | Data quality and bias reports; dataset documentation |
| 11 | Risk management | AI Act 9; NIST AI RMF (GOVERN, MAP, MEASURE, MANAGE); ISO/IEC 23894; ISO/IEC 42001 cl. 6.1; SR 11-7 | No (process) | Running the tests the risk file calls for | Identifying and accepting risks | Risk register and risk management file |
| 12 | Pre-release testing against fixed thresholds | AI Act 9(8); AI Act 17(1)(d); PCCP modification protocol (performance evaluation, acceptance criteria); NIST AI RMF MANAGE 1.1; ISO/IEC 42001 A.6.2.4-A.6.2.5; SR 11-7 validation | Yes, once thresholds exist | Each metric is present and meets its threshold; a missing metric blocks; comparison with the current model | Setting the metrics and thresholds; independent validation (SR 11-7) | Evaluation report and gate decision log per candidate |
| 13 | Controlled model change | AI Act 43(4), 3(23), Annex IV 2(f), 17(1)(a); FD&C Act 515C (21 U.S.C. 360e-4); FDA PCCP final guidance; GMLP 10; ISO/IEC 42001 A.6.2.5 | Partly | A candidate stays inside the pre-authorised envelope (same intended use, same inputs, acceptance criteria met); every status change is recorded; a rollback version exists | Writing and getting the envelope authorised; deciding whether a change outside it is "substantial" | PCCP or Annex IV 2(f) description; version history; change log; rollback plan |
| 14 | Human oversight and approval | AI Act 14(1), 14(4)(a)-(e); GDPR 22(3); GMLP 7; NIST AI RMF MANAGE 2.4; SR 11-7 (effective challenge) | Partly | An approval exists before release; approver is named and is not the requester; override and stop functions exist and are logged | Competence, authority and time of the overseer; whether oversight works | Approval records with identity; override and stop logs; training records |
| 15 | Fail-safe behaviour, robustness, security | AI Act 15(1), 15(4)-(5); GDPR 32; DPDP s.8(5), Rules r.6 | Partly | Denying when a dependency fails (fail closed); robustness tests per release | Threat modelling; security programme | Test results; security assessment |
| 16 | Post-market monitoring | AI Act 72; AI Act 73 (serious incidents); GMLP 10; FDA lifecycle draft guidance; ISO/IEC 42001 A.6.2.6 | Partly | Drift and performance signals; alarms when a metric falls below its threshold | Deciding the response; reporting incidents | Monitoring plan; monitoring logs; incident reports |
| 17 | Access only by authorised persons | EHDS 68, 73; AI Act 10(5)(c); HIPAA 164.514(d)(2); DPDP Rules r.6 | Yes | Role or identity checks against the permit or role list on each access | Deciding who is on the list | Access logs; permit with named users |
| 18 | Storage limitation and erasure | GDPR 5(1)(e); DPDP s.8(7), Rules r.8; EHDS 68 (permit duration) | Partly | Retention timers and deletion jobs; refusing access after a permit ends | Legal holds and exceptions | Retention policy; deletion records |
| 19 | Quality management system | AI Act 17; ISO/IEC 42001; IMDRF GMLP | No | None beyond supplying records | The system itself | QMS documentation; certification audit |
| 20 | Transparency to users and deployers | AI Act 13; GMLP 9; PCCP labelling | No | Checking that a document is linked to the released version | Writing instructions for use | Instructions for use; model card (Mitchell et al. 2019) |

---

## 3. Application dates

| Instrument | Dates that matter |
|---|---|
| GDPR (EU) 2016/679 | Applies since 25 May 2018. |
| HIPAA Privacy Rule | Compliance required since 14 April 2003 for most covered entities. |
| EU AI Act (EU) 2024/1689 (Art. 113) | In force 1 August 2024. Chapters I-II (incl. prohibitions) from 2 February 2025. Governance, general-purpose AI and penalties chapters from 2 August 2025. General date 2 August 2026. Art. 6(1) (Annex I products) originally 2 August 2027. |
| Digital Omnibus on AI, (EU) 2026/1744 | Published in the OJ on 24 July 2026, in force 27 July 2026 (law-firm summaries). Stand-alone Annex III high-risk obligations move to 2 December 2027; high-risk AI in Annex I products (including medical devices) to 2 August 2028. Art. 50 transparency still applies from 2 August 2026. A new Art. 5 prohibition applies from 2 December 2026. |
| EHDS (EU) 2025/327 | In force 26 March 2025. General application 26 March 2027. Chapter IV (secondary use) from 26 March 2029; some data categories (e.g. human genetic data, clinical trial data) from 26 March 2031 (secondary sources). |
| India DPDP Act 2023 and DPDP Rules 2025 | Rules notified 13 November 2025 (G.S.R. 846(E)). Rules 1, 2, 17-21 at once; Rule 4 (consent managers) after one year (13 November 2026); Rules 3, 5-16, 22-23 after 18 months (13 May 2027). A January 2026 proposal to shorten the 18 months was found; no notified amendment was found. |
| FDA | FD&C Act s.515C since 29 December 2022. GMLP principles October 2021. PCCP principles October 2023. PCCP final guidance December 2024. AI-enabled device software functions draft guidance 7 January 2025, still a draft in October 2026 (RAPS report on the CDRH guidance agenda). |
| NIST | AI RMF 1.0: 26 January 2023. AI 600-1: 26 July 2024. Privacy Framework 1.0: 16 January 2020; 1.1 initial public draft 14 April 2025, no final found. |
| ISO/IEC | 23894: February 2023. 42001: December 2023. 27701: second edition October 2025 (replaces 2019). |

---

## 4. Entries

### 4a. ML release governance and MLOps

#### sculley2015hidden
- **Citation:** Sculley et al., "Hidden Technical Debt in Machine Learning Systems", NIPS 28, 2015, pp. 2503-2511.
- **Verification:** No DOI. NeurIPS proceedings PDF and Google Research listing found by search (pages 2503-2511, ten authors). Not in Crossref.
- **What it is:** A position paper from Google that names the maintenance costs of production ML: entanglement ("changing anything changes everything"), hidden feedback loops, undeclared consumers, unstable data dependencies, glue code, pipeline jungles and configuration debt. Its main figure shows the ML code as a small box among data collection, verification, serving and monitoring.
- **D1:** n/a (diagnosis, not a decision mechanism). **D2:** n/a. **D3:** n/a. **D4:** whole lifecycle. **D5:** none prescribed. **D6:** not discussed. **D7:** none.
- **Overlap with Dagents:** The argument for keeping decision rules out of application glue code and in one versioned place.
- **What it does that Dagents does not:** Treats configuration itself as debt; a planner configured by extensions inherits this risk (classifications, thresholds and purposes are configuration).

#### breck2017ml
- **Citation:** Breck, Cai, Nielsen, Salib, Sculley, "The ML Test Score: A Rubric for ML Production Readiness and Technical Debt Reduction", IEEE Big Data 2017, pp. 1123-1132.
- **Verification:** DOI 10.1109/BigData.2017.8258038 (Crossref).
- **What it is:** 28 tests in four groups (features and data, model development, infrastructure, monitoring), scored half a point when done by hand and a full point when automated; the final score is the minimum over the four groups. Recalled from the paper (check before quoting): the infrastructure tests include testing a model through a canary before serving and being able to roll back quickly to the previous serving version; the data tests include privacy controls in the data pipeline.
- **D1:** whether a system is ready for production (a score, not a gate). **D2:** checklist; automation rewarded. **D3:** training pipeline and serving. **D4:** training, deployment, monitoring. **D5:** the score. **D6:** n/a. **D7:** Google-derived but generic.
- **Overlap:** A missing check scores zero, which is the spirit of "a missing metric blocks release".
- **What it does that Dagents does not:** Canary deployment, rollback tests, monitoring tests, and a scoring method for readiness.

#### baylor2017tfx
- **Citation:** Baylor et al., "TFX: A TensorFlow-Based Production-Scale Machine Learning Platform", KDD 2017, pp. 1387-1395.
- **Verification:** DOI 10.1145/3097983.3098021 (Crossref; 22 authors).
- **What it is:** Google's ML platform with components for data analysis, transformation, data validation, training, model evaluation and validation, and serving. A model is pushed to serving only after validation against quality and "safe to serve" checks; data validation compares new data with a schema.
- **D1:** whether a trained model may be pushed to serving. **D2:** pipeline components configured by the team. **D3:** central pipeline. **D4:** data read, training, release. **D5:** validation outputs and metadata. **D6:** a model that fails validation is not pushed (recalled; check). **D7:** TensorFlow and Google infrastructure.
- **Overlap:** Validation before release as a pipeline step; data validation before training resembles a `before_train` check.
- **What it does that Dagents does not:** Schema-based data validation, comparison with the previous model, integrated serving. Dagents' gate is a typed function with recorded refusals; TFX's is pipeline code.

#### amershi2019software
- **Citation:** Amershi et al., "Software Engineering for Machine Learning: A Case Study", ICSE-SEIP 2019, pp. 291-300.
- **Verification:** DOI 10.1109/ICSE-SEIP.2019.00042 (Crossref).
- **What it is:** A Microsoft study (interviews and a survey of engineers) that sets out a nine-stage ML workflow, from model requirements and data collection through training and evaluation to deployment and monitoring, with feedback loops between stages.
- **D1:** n/a. **D2:** n/a. **D3:** n/a. **D4:** whole lifecycle. **D5:** n/a. **D6:** n/a. **D7:** one company.
- **Overlap:** Places evaluation and deployment as distinct stages, which is where a release gate sits.
- **What it does that Dagents does not:** Covers requirements and data labelling stages, and the organisational side.

#### zaharia2018accelerating (with chen2020developments and mlflow2026registry)
- **Citation:** Zaharia et al., "Accelerating the Machine Learning Lifecycle with MLflow", IEEE Data Eng. Bull. 41(4):39-45, 2018. Chen et al., "Developments in MLflow", DEEM 2020, pp. 1-4. MLflow Model Registry documentation.
- **Verification:** Zaharia: dblp and the Bulletin PDF found by search (no DOI). Chen: DOI 10.1145/3399579.3399867 (Crossref). Docs: https://mlflow.org/docs/latest/ml/model-registry.
- **What it is:** MLflow tracks runs, packages models and, since the registry was added, keeps registered models with versions. The registry had stages (Staging, Production, Archived); from MLflow 2.9 stages are deprecated in favour of aliases (for example a `champion` alias that serving reads) and tags. More than one alias may point to a version.
- **D1:** which version a name or alias points to. **D2:** a person or a CI job sets the alias or stage; no built-in rule decides. **D3:** central registry. **D4:** release, deployment. **D5:** version history, tags. **D6:** n/a. **D7:** open source; Databricks offers a managed form (Unity Catalog uses aliases and tags).
- **Overlap:** Versions with a status and a pointer to the one in use, like Dagents' candidate, released, rejected and superseded statuses.
- **What it does that Dagents does not:** Several aliases per version (champion and challenger at once), integration with serving, a UI. It does not refuse a transition; Dagents' planner can.

#### aws2026sagemakerapproval
- **Citation:** AWS, "Update the Approval Status of a Model", SageMaker AI Model Registry documentation.
- **Verification:** https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry-approve.html (search excerpt).
- **What it is:** A model package version has `ModelApprovalStatus`: `PendingManualApproval`, `Approved` or `Rejected`, changed with `update_model_package` or in Studio. Setting `Approved` can start a CI/CD deployment, for example through an EventBridge rule; moving a version from Approved to Rejected starts a deployment of the latest approved version.
- **D1:** whether a version may be deployed. **D2:** a person or a pipeline sets a status field. **D3:** central registry. **D4:** release, deployment. **D5:** status and its history in AWS logs. **D6:** not stated. **D7:** AWS.
- **Overlap:** Close to Dagents' approval step; both separate "candidate" from "approved".
- **What it does that Dagents does not:** Event-driven deployment on approval and automatic redeployment of the last approved version on rejection.

#### aws2026sagemakershadow
- **Citation:** AWS, "Shadow tests", SageMaker AI documentation.
- **Verification:** https://docs.aws.amazon.com/sagemaker/latest/dg/shadow-tests.html (search excerpt).
- **What it is:** A shadow variant receives a copy of a share of production requests. "Only the responses of the production variant are returned to the calling application." After review, the operator promotes the shadow variant or keeps the production one. One shadow and one production variant per endpoint. Deployment guardrails add linear or canary traffic shifting with automatic rollback.
- **D1:** whether a challenger replaces the champion. **D2:** a person decides from a dashboard. **D3:** serving endpoint. **D4:** deployment. **D5:** metrics in CloudWatch. **D6:** auto rollback with guardrails. **D7:** AWS.
- **Overlap:** None in Dagents' described design.
- **What it does that Dagents does not:** Champion and challenger compared on live traffic before release; canary shifting and rollback.

#### google2026vertexalias
- **Citation:** Google Cloud, "How to use model version aliases", Vertex AI Model Registry.
- **Verification:** https://docs.cloud.google.com/vertex-ai/docs/model-registry/model-alias (search excerpt).
- **What it is:** Each model has exactly one version holding the `default` alias, used when a call names no version; the first version gets it. An alias belongs to one version at a time; versions are addressed as `models/{id}@{alias}`.
- **D1:** which version is used by default. **D2:** a person or pipeline moves the alias. **D3:** central registry. **D4:** deployment. **D5:** version metadata. **D6:** if the default version is deleted the alias moves to the next most recent version. **D7:** Google Cloud.
- **Overlap:** An invariant on the registry ("exactly one default"), like Dagents' rule that a release supersedes the previous one.
- **What it does that Dagents does not:** Named aliases for staged use. Its deletion behaviour moves the default automatically; Dagents' lifecycle makes every status change an explicit planner answer.

#### paleyes2022challenges
- **Citation:** Paleyes, Urma, Lawrence, "Challenges in Deploying Machine Learning: A Survey of Case Studies", ACM Computing Surveys 55(6):1-29, 2022.
- **Verification:** DOI 10.1145/3533378 (Crossref).
- **What it is:** Maps published deployment reports onto workflow stages (data management, model learning, verification, deployment) and finds problems at every stage; ethics, law, user trust and security are treated as cross-cutting concerns.
- **D1-D7:** survey; n/a.
- **Overlap:** Supports the claim that legal requirements are cross-cutting rather than one stage.
- **What it does that Dagents does not:** Evidence base across industries.

#### kreuzberger2023machine
- **Citation:** Kreuzberger, Kühl, Hirschl, "Machine Learning Operations (MLOps): Overview, Definition, and Architecture", IEEE Access 11:31866-31879, 2023.
- **Verification:** DOI 10.1109/ACCESS.2023.3262138 (Crossref); arXiv:2205.02302.
- **What it is:** A literature review, tool review and expert interviews that define MLOps through principles (including versioning, metadata tracking, continuous training and evaluation, monitoring), components (including a model registry) and roles.
- **D1-D7:** reference architecture; n/a.
- **Overlap:** Model registry and metadata store as named components.
- **What it does that Dagents does not:** Defines roles (who owns which step), which the regulations' oversight duties assume.

#### shankar2024we
- **Citation:** Shankar, Garcia, Hellerstein, Parameswaran, "'We Have No Idea How Models will Behave in Production until Production': How Engineers Operationalize Machine Learning", PACM HCI 8(CSCW1):1-34, 2024.
- **Verification:** DOI 10.1145/3653697 (Crossref); preprint arXiv:2209.09125.
- **What it is:** Interviews with 18 ML engineers. They describe evaluation "throughout a multi-staged deployment" and continual monitoring, coordinated by people over chat and ticketing tools. The authors name three virtues: velocity, visibility and versioning.
- **D1:** promotion between stages. **D2:** mostly people and team conventions. **D3:** staged environments. **D4:** evaluation, deployment, monitoring. **D5:** tickets and dashboards. **D6:** n/a. **D7:** n/a.
- **Overlap:** Confirms that release decisions in practice involve a person.
- **What it does that Dagents does not:** Staged rollout; evidence that offline metrics do not predict production behaviour, which is a limit of any offline release gate.

#### lavin2022technology
- **Citation:** Lavin et al., "Technology Readiness Levels for Machine Learning Systems", Nature Communications 13:6039, 2022.
- **Verification:** DOI 10.1038/s41467-022-33128-9 (Crossref).
- **What it is:** Adapts spacecraft technology readiness levels to ML (MLTRL), with reviews between levels before a system advances, illustrated with cases including medical diagnostics.
- **D1:** whether work moves to the next level. **D2:** review by people against level criteria. **D3:** organisation-wide. **D4:** research to deployment. **D5:** review records. **D6:** n/a. **D7:** none.
- **Overlap:** Gated progression.
- **What it does that Dagents does not:** Gates earlier stages (research, prototyping), not only release.

#### haakman2021ai
- **Citation:** Haakman, Cruz, Huijgens, van Deursen, "AI lifecycle models need to be revised: An exploratory study in Fintech", Empirical Software Engineering 26(5):95, 2021.
- **Verification:** DOI 10.1007/s10664-021-09993-1 (Crossref, with abstract).
- **What it is:** A case study at the bank ING with 17 interviews. Stages missing from published lifecycle models: data collection, feasibility study, documentation, model monitoring and model risk assessment.
- **D1:** model risk assessment before use. **D2:** people (model risk function). **D3:** organisation. **D4:** before deployment, monitoring. **D5:** documentation. **D6:** n/a. **D7:** finance.
- **Overlap:** A required risk step before release.
- **What it does that Dagents does not:** Shows a regulated industry's independent model risk review, which Dagents' approval step does not model.

#### raji2020closing
- **Citation:** Raji et al., "Closing the AI Accountability Gap: Defining an End-to-End Framework for Internal Algorithmic Auditing", FAT* 2020, pp. 33-44.
- **Verification:** DOI 10.1145/3351095.3372873 (Crossref); arXiv:2001.00973.
- **What it is:** An internal audit framework (SMACTR: scoping, mapping, artefact collection, testing, reflection) applied during development, where each stage produces documents that form an audit report.
- **D1:** whether a system should proceed. **D2:** auditors. **D3:** organisation. **D4:** design to pre-deployment. **D5:** audit documents. **D6:** n/a. **D7:** none.
- **Overlap:** Evidence produced at each stage.
- **What it does that Dagents does not:** Ethical review before any code is written.

#### mitchell2019model
- **Citation:** Mitchell et al., "Model Cards for Model Reporting", FAT* 2019, pp. 220-229.
- **Verification:** DOI 10.1145/3287560.3287596 (Crossref); arXiv:1810.03993.
- **What it is:** Short documents released with a model, reporting intended use and evaluation results broken down by group.
- **D1:** n/a. **D2:** document. **D3:** at release. **D4:** release. **D5:** documentation. **D6:** n/a. **D7:** none.
- **Overlap:** Release-time evidence.
- **What it does that Dagents does not:** Disaggregated evaluation and intended-use statement attached to a version (AI Act Art. 13, GMLP 9).

### 4b. Model risk management in banks

#### fed2011sr117
- **Citation:** Federal Reserve and OCC, SR 11-7 "Supervisory Guidance on Model Risk Management", 4 April 2011 (OCC Bulletin 2011-12).
- **Verification:** https://www.federalreserve.gov/supervisionreg/srletters/sr1107.htm (search excerpt).
- **What it is:** US banking guidance in three parts: model development, implementation and use; model validation; governance, policies and controls. It asks for "effective challenge" by competent parties independent of development and use, validation of material changes, ongoing monitoring and a model inventory (paraphrase; check wording).
- **D1:** whether a model may be used. **D2:** people (independent validators), policy. **D3:** organisation. **D4:** development, before use, ongoing. **D5:** validation reports, inventory. **D6:** not stated. **D7:** banking.
- **Overlap:** The decision to use a model is separated from the team that built it.
- **What it does that Dagents does not:** Requires independence of the approver from the developer, and an inventory of all models.

#### pra2023ss123
- **Citation:** PRA, SS1/23 "Model risk management principles for banks", 17 May 2023, effective 17 May 2024.
- **Verification:** https://www.bankofengland.co.uk/prudential-regulation/publication/2023/may/model-risk-management-principles-for-banks-ss (search excerpt).
- **What it is:** Five principles for UK banks with internal models, covering model identification and tiering, governance, development, independent validation and risk mitigation, including post-model adjustments with documented rationale and independent review.
- **D1-D7:** as SR 11-7, UK banking.
- **Overlap and difference:** As SR 11-7. It also treats manual adjustments to model output as governed objects.

### 4c. Medical AI change control

#### fda2021gmlp (with imdrf2025gmlp)
- **Citation:** FDA, Health Canada, MHRA, "Good Machine Learning Practice for Medical Device Development: Guiding Principles", 27 October 2021. IMDRF/AIML WG/N88 FINAL:2025 (PDF dated 4 February 2025).
- **Verification:** FDA and Health Canada pages and the IMDRF PDF found by search.
- **What it is:** Ten principles, not regulations. From memory, with principles 1, 2, 4, 7 and 10 confirmed in excerpts: (1) multidisciplinary expertise across the product life cycle; (2) good software engineering and security practices; (3) representative clinical study participants and data; (4) training data independent of test data; (5) reference data from best available methods; (6) model design tailored to the data and intended use; (7) focus on the performance of the human-AI team; (8) testing under clinically relevant conditions; (9) users given clear, essential information; (10) deployed models monitored and re-training risks managed. IMDRF N88 adopts the same ten with small wording changes.
- **Quote (excerpts, principle 10):** "Deployed models have the capability to be monitored in 'real world' use with a focus on maintained or improved safety and performance"; when retrained after deployment there are "appropriate controls in place to manage risks of overfitting, unintended bias, or degradation of the model (for example, dataset drift)".
- **D1:** n/a (principles). **D2:** guidance. **D3:** manufacturer. **D4:** whole lifecycle. **D5:** not specified. **D6:** n/a. **D7:** medical devices.
- **Overlap:** Principle 4 (train and test independence) and principle 10 (controlled retraining) are checks a release gate can make.
- **What it does that Dagents does not:** Human-AI team performance (7) and monitoring after deployment (10).

#### fda2023pccpprinciples
- **Citation:** FDA, Health Canada, MHRA, "Predetermined Change Control Plans for Machine Learning-Enabled Medical Devices: Guiding Principles", October 2023.
- **Verification:** FDA page found by search.
- **What it is:** Five principles for a PCCP: focused and bounded (changes within intended use), risk-based, evidence-based, transparent, and with a total product lifecycle perspective.
- **Overlap:** "Focused and bounded" is the envelope a typed release rule can check.

#### usc2022pccp
- **Citation:** 21 U.S.C. 360e-4, FD&C Act s.515C, added by FDORA (Pub. L. 117-328, div. FF, title III, s.3308(a), 29 December 2022, 136 Stat. 5835).
- **Verification:** govinfo link found by search; summary in excerpt.
- **What it is:** The statute that lets FDA approve (515C(a), PMA) or clear (515C(b), 510(k)) a plan for future changes, so that changes made under the plan need no new approval or notification. It applies to all devices, not only AI. Subsection (c) limits use of a changed device as a predicate.
- **Overlap:** The legal basis for a pre-authorised change envelope.

#### fda2024pccp
- **Citation:** FDA, "Marketing Submission Recommendations for a Predetermined Change Control Plan for Artificial Intelligence-Enabled Device Software Functions", final guidance, December 2024.
- **Verification:** Law-firm summaries found by search (they give 3 or 4 December 2024). The FDA page was not retrieved.
- **What it is:** A PCCP has three parts: a description of modifications, a modification protocol and an impact assessment. The modification protocol covers data management practices, re-training practices, performance evaluation protocols and update procedures (the fourth recalled; check). The final version covers all AI-enabled device software functions, not only ML; it adds version control and maintenance of PCCPs and asks for criteria or a plan to roll back an update. A change outside the plan, or one that cannot meet its acceptance criteria, needs a new submission. Labelling states that the device has an authorised PCCP.
- **D1:** whether a modification may be deployed without a new submission. **D2:** a plan the FDA authorises; acceptance criteria fixed in advance. **D3:** manufacturer, checked by FDA. **D4:** retraining, validation, release, deployment. **D5:** PCCP, test reports, labelling. **D6:** a failed criterion means no deployment under the plan. **D7:** US medical devices.
- **Overlap:** Strong. Acceptance criteria fixed in advance and a blocked release when they are not met is what Dagents' release gate and "missing metric blocks" rule implement for one study.
- **What it does that Dagents does not:** Rollback criteria, update procedures (communication to users), real-world monitoring, and a regulator who reviews the envelope.

#### fda2025aidsf
- **Citation:** FDA, "Artificial Intelligence-Enabled Device Software Functions: Lifecycle Management and Marketing Submission Recommendations", draft guidance, January 2025 (Federal Register 7 January 2025, docket FDA-2024-D-4488).
- **Verification:** FDA guidance page and Federal Register notice found by search. A RAPS report of 2 October 2026 lists its finalisation as a priority for fiscal year 2027, so it is still a draft.
- **What it is:** Recommendations for the content of marketing submissions for AI-enabled devices across the total product life cycle, including data management, validation, performance monitoring after marketing (it notes that data drift may not be visible to users) and transparency.
- **Overlap and difference:** Like GMLP, it adds monitoring after release, which Dagents' description does not cover.

#### feng2021approval
- **Citation:** Feng, Emerson, Simon, "Approval policies for modifications to machine learning-based software as a medical device: A study of bio-creep", Biometrics 77(1):31-44, 2021.
- **Verification:** DOI 10.1111/biom.13379 (Crossref, with abstract). Discussion by FDA statisticians (Pennello, Sahiner, Gossmann, Petrick), DOI 10.1111/biom.13381.
- **What it is:** Frames an automatic algorithmic change protocol (aACP) as online hypothesis testing. Repeated non-inferiority approvals can let accuracy fall step by step ("bio-creep"). Defines two error rates, bad approval count (BAC) and bad approval and benchmark ratios (BABR), and controls them with alpha-investing, group-sequential and gate-keeping methods.
- **Quote (abstract):** "In simulation studies, bio-creep regularly occurred when using policies with no error-rate guarantees."
- **D1:** whether a modification is approved. **D2:** a statistical policy (analysable). **D3:** at release. **D4:** release. **D5:** test results. **D6:** n/a. **D7:** medical devices, but general.
- **Overlap:** An automated release decision.
- **What it does that Dagents does not:** Error-rate control across a sequence of releases. A gate that compares each candidate with the previous release only can approve a chain of small losses.

#### gilbert2021algorithm
- **Citation:** Gilbert et al., "Algorithm Change Protocols in the Regulation of Adaptive Machine Learning-Based Medical Devices", J Med Internet Res 23(10):e30545, 2021.
- **Verification:** DOI 10.2196/30545 (Crossref, with abstract).
- **What it is:** A viewpoint that the device framework built for slowly updated devices fits retrained ML models badly, and that the EU had not said how change protocols would work. Argues for oversight of developers' quality management systems plus defined change protocols.
- **Overlap and difference:** Names the gap that AI Act Art. 43(4) later partly fills.

#### feng2022clinical
- **Citation:** Feng et al., "Clinical artificial intelligence quality improvement: towards continual monitoring and updating of AI algorithms in healthcare", npj Digital Medicine 5:66, 2022.
- **Verification:** DOI 10.1038/s41746-022-00611-y (Crossref, with abstract).
- **What it is:** Proposes hospital "AI-QI" units that monitor deployed models with quality-assurance tools and decide on updates.
- **Overlap and difference:** Puts monitoring and update decisions at the deploying site, which in a federated setting is each LMA; Dagents' description has no monitoring after release.

#### gonzalez2024regulating
- **Citation:** González et al., "Regulating radiology AI medical devices that evolve in their lifecycle", arXiv:2412.20498, 2024.
- **Verification:** arXiv:2412.20498 (arXiv API).
- **What it is:** Reviews the AI Act and the FDA's PCCP final guidance and names the building blocks for updating a model without new approval: clear descriptions of data collection and retraining, and real-world quality monitoring.
- **Overlap and difference:** As above; preprint only.

#### afdideh2026aegis
- **Citation:** Afdideh, Astaraki, Seoane, Abtahi, "AEGIS: an operational infrastructure for post-market governance of adaptive medical AI under US and EU regulatory frameworks", npj Digital Medicine 9:789, 2026.
- **Verification:** DOI 10.1038/s41746-026-03290-1 (Crossref); arXiv:2603.22322.
- **What it is:** Three modules (data assimilation and retraining, model monitoring, conditional decision) that "operationalize FDA PCCP and EU AI Act Article 43(4) provisions". Each iteration ends in one of four decisions, APPROVE, CONDITIONAL APPROVAL, CLINICAL REVIEW or REJECT, plus an independent post-market surveillance ALARM.
- **Numbers (abstract):** over 11 simulated sepsis iterations, 8 APPROVE, 1 CONDITIONAL APPROVAL, 1 CLINICAL REVIEW, 1 REJECT; ALARM at iterations 8 and 10, including a state with no deployable model while the released one fails.
- **D1:** whether a retrained model may replace the released one. **D2:** configuration of thresholds; the same architecture for two clinical tasks. **D3:** at release, at one site. **D4:** retraining, release, monitoring. **D5:** decision records (not detailed in abstract). **D6:** the ALARM state flags "no deployable model"; fail behaviour otherwise not stated. **D7:** healthcare; configurable.
- **Overlap:** The closest prior art to Dagents' release gate found in this family: regulatory change control turned into executable decisions with a fixed set of outcomes.
- **What it does that Dagents does not:** A "clinical review" outcome that routes to a person, conditional approval, drift detection and a post-market alarm. Not federated; decisions are not described as typed or verified.

### 4d. Regulation

#### eu2016gdpr
- **Citation:** Regulation (EU) 2016/679 (GDPR), OJ L 119, 4.5.2016, pp. 1-88.
- **Verification:** ELI https://eur-lex.europa.eu/eli/reg/2016/679/oj. The quotations below are from the OJ text as recalled; EUR-Lex was not reachable, so check wording before quoting.
- **Provisions:**
  - **Art. 5(1)(b) purpose limitation:** "collected for specified, explicit and legitimate purposes and not further processed in a manner that is incompatible with those purposes"; further processing for research or statistics is not incompatible "in accordance with Article 89(1)".
  - **Art. 5(1)(c) data minimisation:** "adequate, relevant and limited to what is necessary in relation to the purposes for which they are processed" (this wording is also quoted in Biega et al. 2020, verified on arXiv).
  - **Art. 5(1)(e)** storage limitation.
  - **Art. 5(2) accountability:** the controller "shall be responsible for, and be able to demonstrate compliance with, paragraph 1".
  - **Art. 6(4):** the compatibility test for a new purpose (link, context, nature of data, consequences, safeguards).
  - **Art. 9(2)(j):** special categories processed for research with Art. 89(1) safeguards.
  - **Art. 22(3):** right to obtain human intervention in automated decisions.
  - **Art. 25(1)** data protection by design: measures "such as pseudonymisation" that implement principles "such as data minimisation"; **Art. 25(2)** by default "only personal data which are necessary for each specific purpose" are processed, covering amount, extent of processing, storage period and accessibility.
  - **Art. 30(1):** records of processing activities, points (a)-(g): controller, purposes, categories of data subjects and data, recipients, transfers, erasure time limits, security measures.
  - **Art. 32:** security of processing (pseudonymisation and encryption in 32(1)(a); regular testing in 32(1)(d)).
  - **Art. 35:** DPIA where processing is "likely to result in a high risk"; 35(3)(b) large-scale special categories; 35(7) contents: description and purposes, necessity and proportionality, risks, measures.
  - **Art. 89(1):** research safeguards must ensure technical and organisational measures "in particular in order to ensure respect for the principle of data minimisation", which "may include pseudonymisation"; where research can be done with data that do not identify people, it shall be done that way.
- **D1:** lawfulness, purpose, amount of data. **D2:** principles; controller decides; DPA enforces. **D3:** controller. **D4:** collection to deletion. **D5:** Art. 30 records, DPIA, demonstrable compliance. **D6:** n/a. **D7:** EU, all sectors.
- **Overlap:** Dagents' per-request plan that drops unrequested fields implements 25(2) and 5(1)(c) for structured requests; approved purposes from extensions are a form of 5(1)(b).
- **What it does that Dagents does not:** Art. 6(4) compatibility, DPIA, storage limitation, data subject rights; these are outside a planner.

#### ecfr2026part164 (with hhs2026minimum and hhs2012deidentification)
- **Citation:** 45 CFR Part 164 (HIPAA Privacy Rule, Subpart E), eCFR; HHS OCR guidance on minimum necessary; HHS OCR de-identification guidance, 26 November 2012.
- **Verification:** eCFR and HHS pages found by search; structure confirmed in excerpts; sentence quoted below recalled from the CFR text.
- **Provisions:**
  - **164.502(b)(1) minimum necessary standard:** a covered entity "must make reasonable efforts to limit protected health information to the minimum necessary to accomplish the intended purpose of the use, disclosure, or request". **164.502(b)(2):** six exceptions, including disclosures to a provider for treatment, to the individual, under an authorisation, to HHS, and as required by law.
  - **164.514(d)(2):** workforce access by role (who needs which categories).
  - **164.514(d)(3):** routine and recurring disclosures under policies that "may be standard protocols" limited to the minimum necessary for that type of disclosure ((d)(3)(i)); other disclosures under criteria with review of each request ((d)(3)(ii)); reasonable reliance on certain requesters ((d)(3)(iii)).
  - **164.514(d)(4):** the same split for requests. **164.514(d)(5):** no use of the entire medical record unless justified.
  - **164.514(b)(1) expert determination:** an expert finds the risk "very small" and documents the methods and results. **164.514(b)(2) Safe Harbor:** removal of 18 identifier types and no actual knowledge that the rest could identify a person. HHS: a determination does not expire but may need review as data and technology change.
  - **164.514(e):** limited data set with a data use agreement.
  - **164.512(i):** research uses with an IRB or privacy board waiver of authorisation.
  - **164.312(b) (Security Rule) audit controls:** mechanisms "that record and examine activity in information systems that contain or use electronic protected health information".
  - **164.528:** accounting of disclosures.
- **D1:** how much PHI may be used or disclosed. **D2:** reasonableness standard; protocols for routine cases, people for others. **D3:** covered entity. **D4:** use, disclosure, request. **D5:** policies, audit controls, accounting. **D6:** n/a. **D7:** US health care.
- **Overlap:** The "standard protocols" route is exactly what a per-request planner does; Dagents' field-level strategies are protocols decided in advance.
- **What it does that Dagents does not:** The individual-review route for non-routine requests; expert determination.

#### eu2024aiact (with eu2026omnibus)
- **Citation:** Regulation (EU) 2024/1689 (AI Act), OJ L, 2024/1689, 12.7.2024; amended by Regulation (EU) 2026/1744 (Digital Omnibus on AI).
- **Verification:** ELI https://eur-lex.europa.eu/eli/reg/2024/1689/oj; article texts read through the AI Act Service Desk and mirrors in search excerpts. Art. 12(1) and 14(4)(d) wording confirmed in excerpts; Art. 43(4) partly confirmed; other wording recalled. The Omnibus dates come from law-firm summaries; its OJ text was not read.
- **Provisions (high-risk systems):**
  - **Art. 3(23)** substantial modification: a change not foreseen or planned in the initial conformity assessment that affects compliance or the intended purpose.
  - **Art. 9** risk management system across the lifecycle; **9(8)** (recalled) testing "against prior defined metrics and probabilistic thresholds that are appropriate to the intended purpose", before placing on the market.
  - **Art. 10** data and data governance: 10(2) practices covering design choices, collection and origin of data and, for personal data, the original purpose of collection, preparation, assumptions, bias examination ((f)) and mitigation ((g)), and data gaps; 10(3) relevant, sufficiently representative, as free of errors and complete as possible; 10(5) special categories for bias detection only under conditions (a)-(f), including strict access controls and documentation of access ((c)), no transfer to other parties ((d)), deletion once the bias is corrected ((e)), and records of why it was strictly necessary ((f)).
  - **Art. 11 and Annex IV** technical documentation; Annex IV point 2(f) describes pre-determined changes (recalled).
  - **Art. 12(1):** high-risk systems "shall technically allow for the automatic recording of events (logs) over the lifetime of the system". **12(2):** logs must allow identifying situations that may present a risk or lead to a substantial modification, facilitate post-market monitoring (Art. 72) and monitoring of operation (Art. 26(5)). **12(3):** minimum fields for remote biometric identification.
  - **Art. 13** transparency and instructions for use.
  - **Art. 14** human oversight: 14(1) effective oversight by natural persons during use; 14(4)(a)-(c) understand capacities and limits, stay aware of automation bias, interpret output; **14(4)(d)** "decide, in any particular situation, not to use the high-risk AI system or to otherwise disregard, override or reverse the output"; **14(4)(e)** intervene or interrupt through a "stop" button or similar procedure; 14(5) two-person verification for remote biometric identification.
  - **Art. 15** accuracy, robustness and cybersecurity; 15(4) technical redundancy "which may include backup or fail-safe plans" and feedback loops in systems that keep learning; 15(5) data and model poisoning, adversarial examples, confidentiality attacks.
  - **Art. 17** quality management system: 17(1)(a) a compliance strategy including "procedures for the management of modifications"; (d) examination, test and validation before, during and after development; (f) data management; (h) post-market monitoring; (k) record-keeping.
  - **Art. 18** documentation kept 10 years; **Art. 19(1)** logs kept at least six months; **Art. 26(6)** the same for deployers.
  - **Art. 43(4):** a substantial modification requires a new conformity assessment; for systems that continue to learn, changes "pre-determined by the provider at the moment of the initial conformity assessment" and described in the technical documentation are not a substantial modification.
  - **Art. 72** post-market monitoring system and plan; **Art. 73** serious incident reporting.
- **D1:** whether a system may be placed on the market and how it must behave. **D2:** legal requirements; harmonised standards and conformity assessment. **D3:** provider and deployer. **D4:** design to post-market. **D5:** technical documentation, logs, QMS records, PMM plan. **D6:** 15(4) fail-safe plans; no explicit fail-closed rule. **D7:** EU, risk-based; medical AI is Annex I (MDR/IVDR).
- **Overlap:** Art. 12 logging (Dagents' hash-chained audit records naming the planner); Art. 9(8) thresholds and Art. 43(4) pre-determined change (Dagents' release gate and lifecycle rule); Art. 15(4) fail-safe (fail closed).
- **What it does that Dagents does not:** QMS, PMM, incident reporting, instructions for use, bias examination of training data, and oversight measures at the point of use (Art. 14 concerns deployers' use, not only release approval).
- **Dates:** see section 3. For high-risk AI in medical devices, obligations now apply from 2 August 2028.

#### eu2025ehds
- **Citation:** Regulation (EU) 2025/327 on the European Health Data Space, OJ L, 2025/327, 5.3.2025.
- **Verification:** ELI https://eur-lex.europa.eu/eli/reg/2025/327/oj/eng; article contents read through search excerpts (streamlex, law firms, Commission page). Paragraph numbers flagged where not confirmed.
- **Provisions (Chapter IV, secondary use):**
  - **Art. 51** minimum categories of data for secondary use.
  - **Art. 53** purposes for which data may be processed (exhaustive per one guideline summary); recalled: it includes scientific research related to health, including training, testing and evaluating algorithms in medical devices and AI systems (check point numbers).
  - **Art. 54** prohibited secondary uses.
  - **Art. 55** health data access bodies.
  - **Art. 61** duties of health data users: access only under a data permit (Art. 68), an approved health data request (Art. 69) or another listed basis; a secondary source places a ban on re-identification in 61(3) (unconfirmed).
  - **Art. 66** data minimisation and purpose limitation: anonymised data by default; pseudonymised data only where the user shows the purpose "cannot be achieved with anonymised data" (66(3), excerpt), linked to Art. 68(1)(c).
  - **Art. 67** data access applications; **Art. 68** data permits (a permit may run up to ten years, prolongable, per a secondary source citing 68(12)).
  - **Art. 69** health data request: "with the aim of obtaining a response only in an anonymised statistical format" (excerpt); the user gets no access to the underlying data.
  - **Art. 73** secure processing environment: access only through it, for authorised persons named in the permit; identifiable logs of access and activities, "logs of access shall be kept for at least one year" (excerpt); users may download only non-personal data, "including electronic health data in an anonymised statistical format" (excerpt), after review; 73(5) implementing acts on technical requirements.
- **D1:** who may access which data for which purpose, and in what form results leave. **D2:** permits issued by a public body; implementing acts. **D3:** in the secure processing environment and at download (egress). **D4:** data read, computation, egress. **D5:** permits, logs of access (at least one year). **D6:** not stated. **D7:** EU health data.
- **Overlap:** Strong with Dagents' `before_send` (only permitted aggregates leave a site) and with minimum cohort sizes for statistical output. The permit naming purpose and users parallels approved purposes and the trust input.
- **What it does that Dagents does not:** An external permit authority, named users, permit expiry, and review of each download by the access body. The EHDS assumes a central or federated secure environment run by a public body; Dagents runs the check at each site.
- **Dates:** in force 26 March 2025; secondary use from 26 March 2029 (some categories 2031).

#### india2023dpdp (with india2025dpdprules)
- **Citation:** Digital Personal Data Protection Act, 2023 (No. 22 of 2023); Digital Personal Data Protection Rules, 2025, G.S.R. 846(E), 13 November 2025.
- **Verification:** Act PDF on indiacode.nic.in found by search; Rules described in law-firm and news summaries (Gazette text not read). Section and rule numbers confirmed in summaries; wording paraphrased.
- **Provisions:**
  - **s.5** notice; **s.6(1)** consent for a specified purpose, limited to the personal data necessary for that purpose (the Act's minimisation rule; wording recalled, check); **s.7** legitimate uses without consent.
  - **s.8(4)** appropriate technical and organisational measures; **s.8(5)** reasonable security safeguards to prevent breaches; **s.8(7)** erase data when consent is withdrawn or the purpose is no longer served.
  - **s.10(2)** significant data fiduciaries: data protection officer, independent data auditor, periodic DPIA and audit.
  - **s.17(2)(b)** the Act does not apply to processing necessary for research, archiving or statistical purposes if the data are not used for a decision about a specific person and the processing follows prescribed standards.
  - **Rules r.6** security safeguards, including access control, logs and continuity measures, with logs and relevant data kept for one year (summaries); **r.8** erasure; **r.13** significant data fiduciaries: DPIA and audit every twelve months, and due diligence that technical measures "including algorithmic software" do not pose a risk to the rights of data principals; **r.16 with the Second Schedule** standards for the research exemption.
- **D1:** lawful processing and what data may be used. **D2:** statute and rules; Data Protection Board. **D3:** data fiduciary. **D4:** collection to erasure. **D5:** logs (one year), DPIA and audit for SDFs. **D6:** n/a. **D7:** India, all sectors.
- **Overlap:** r.13's algorithmic due diligence is one of few provisions that names algorithms directly; per-purpose minimisation (s.6(1)); one-year logs (r.6).
- **What it does that Dagents does not:** Consent and consent managers; erasure; SDF audit regime.
- **Dates:** see section 3.

### 4e. Standards and frameworks

#### tabassi2023nist (with nist2024genai)
- **Citation:** NIST AI 100-1, AI RMF 1.0, 26 January 2023; NIST AI 600-1, Generative AI Profile, 26 July 2024.
- **Verification:** DOIs 10.6028/NIST.AI.100-1 and 10.6028/NIST.AI.600-1 (Crossref).
- **What it is:** Voluntary framework with four functions, GOVERN, MAP, MEASURE and MANAGE, each split into categories and subcategories. Subcategories relevant here, wording recalled (check against the PDF): GOVERN 1.6 (inventory of AI systems); MEASURE 2.10 (privacy risk examined and documented); MANAGE 1.1 (a determination whether the system meets its purpose and "whether its development or deployment should proceed"); MANAGE 2.4 (mechanisms to supersede, disengage or deactivate systems whose outcomes are inconsistent with intended use). AI 600-1 lists 12 risks that generative AI creates or makes worse and maps actions to the RMF.
- **D1:** go or no-go, risk treatment. **D2:** organisational process. **D3:** organisation. **D4:** whole lifecycle. **D5:** documentation. **D6:** n/a. **D7:** voluntary, US, sector-neutral.
- **Overlap:** MANAGE 1.1 is a release decision; MANAGE 2.4 is the ability to withdraw a version.
- **What it does that Dagents does not:** Organisational governance, roles and inventory.

#### nist2020privacy (with nist2025privacy11)
- **Citation:** NIST Privacy Framework 1.0, 16 January 2020; 1.1 initial public draft (CSWP 40 ipd), 14 April 2025.
- **Verification:** DOI 10.6028/NIST.CSWP.01162020 (Crossref); 1.1 draft page on csrc.nist.gov found by search. No final 1.1 found as of November 2026.
- **What it is:** Five functions (Identify-P, Govern-P, Control-P, Communicate-P, Protect-P). Control-P covers data processing management and disassociated processing such as de-identification (recalled; check subcategory ids).
- **Overlap and difference:** A catalogue of outcomes; Dagents implements some Control-P outcomes in code.

#### iso2023iec42001
- **Citation:** ISO/IEC 42001:2023, AI management system, December 2023.
- **Verification:** https://www.iso.org/standard/42001 (search). Annex A titles from secondary sources, consistent with each other except where noted.
- **What it is:** A certifiable management system standard. Annex A controls relevant here: A.5 assessing impacts of AI systems; A.6.2.4 verification and validation; A.6.2.5 deployment (one source calls it change management); A.6.2.6 operation and monitoring; A.6.2.7 technical documentation; A.6.2.8 recording of event logs; A.7.2-A.7.6 data for development, acquisition, quality, provenance and preparation.
- **D1-D7:** organisational controls; certification by audit; sector-neutral.
- **Overlap:** A.6.2.8 logging and A.6.2.5 deployment approval.
- **What it does that Dagents does not:** The management system around the code (policy, roles, impact assessment, improvement cycle). Certification does not by itself mean AI Act conformity.

#### iso2023iec23894
- **Citation:** ISO/IEC 23894:2023, Guidance on AI risk management, February 2023.
- **Verification:** https://www.iso.org/standard/77304.html (search).
- **What it is:** Guidance, not certifiable, that applies ISO 31000 risk management to AI across the lifecycle.
- **Overlap and difference:** Process guidance; nothing a program checks.

#### iso2025iec27701
- **Citation:** ISO/IEC 27701:2025, Privacy information management systems, Edition 2, October 2025.
- **Verification:** https://www.iso.org/standard/27701 (search). Published 14 October 2025, replaces the 2019 edition, now standalone (no longer an extension of ISO/IEC 27001).
- **What it is:** Requirements and guidance for PII controllers and processors.
- **Overlap and difference:** As ISO/IEC 42001, for privacy.

### 4f. Human oversight research

#### green2022flaws
- **Citation:** Green, "The flaws of policies requiring human oversight of government algorithms", Computer Law & Security Review 45:105681, 2022.
- **Verification:** DOI 10.1016/j.clsr.2022.105681 (Crossref).
- **What it is:** Reviews policies that require human oversight of government algorithms and the evidence that people cannot perform the oversight these policies expect. Argues that such policies can lend legitimacy to flawed systems, and proposes institutional oversight in which agencies must justify algorithm use before adopting it (description recalled; abstract not in Crossref).
- **Overlap:** Relevant to Dagents' single approval step: an approval record shows that a person acted, not that the review was effective.
- **What it does that Dagents does not:** Moves the check earlier, to whether the system should be used at all.

#### sterz2024quest
- **Citation:** Sterz et al., "On the Quest for Effectiveness in Human Oversight: Interdisciplinary Perspectives", FAccT 2024, pp. 2495-2507.
- **Verification:** DOI 10.1145/3630106.3659051 (Crossref); arXiv:2404.04059.
- **What it is:** Defines effective oversight: the overseer needs causal power over the system, epistemic access, self-control and fitting intentions. Applies this to AI Act Art. 14.
- **Overlap:** Gives criteria a release approval step can be designed against (what the approver sees, and whether the approver can refuse).

#### laux2024institutionalised
- **Citation:** Laux, "Institutionalised distrust and human oversight of artificial intelligence", AI & Society 39(6):2853-2866, 2024 (online 2023).
- **Verification:** DOI 10.1007/s00146-023-01777-z (Crossref, with abstract).
- **What it is:** Finds that the AI Act leaves developers' oversight duties underdefined, gives a taxonomy of oversight roles (constitutive or corrective) and six principles that assume overseers are fallible.
- **Overlap:** Supports separation of duties and logging of each approval.

### 4g. Turning obligations into checks

#### garg2011policy (with deyoung2010experiences)
- **Citation:** Garg, Jia, Datta, "Policy auditing over incomplete logs: theory, implementation and applications", CCS 2011, pp. 151-162. DeYoung et al., "Experiences in the logical specification of the HIPAA and GLBA privacy laws", WPES 2010, pp. 73-82.
- **Verification:** DOIs 10.1145/2046707.2046726 and 10.1145/1866919.1866930 (Crossref). Description recalled from the papers; abstracts not retrieved.
- **What it is:** DeYoung et al. write the disclosure clauses of HIPAA and GLBA in a first-order temporal logic. Garg et al. give an algorithm (`reduce`) that checks an audit log against such a policy, checks what the log allows and returns a residual policy for what it cannot decide, such as predicates about purpose or a person's belief, which need human input.
- **D1:** whether past disclosures complied. **D2:** formal logic (analysable). **D3:** after the fact, over logs. **D4:** disclosure. **D5:** audit verdicts and residual obligations. **D6:** undecidable parts are left open, not passed. **D7:** HIPAA and GLBA, method general.
- **Overlap:** The same split as Table 2: mechanical parts versus parts needing a person.
- **What it does that Dagents does not:** Audits after the fact against the legal text itself; Dagents decides before release against a classification.

#### basin2018purpose
- **Citation:** Basin, Debois, Hildebrandt, "On Purpose and by Necessity: Compliance Under the GDPR", Financial Cryptography and Data Security 2018, LNCS, pp. 20-37.
- **Verification:** DOI 10.1007/978-3-662-58387-6_2 (Crossref). Description recalled; abstract not retrieved.
- **What it is:** Ties purpose limitation and data minimisation to business process models: a purpose is a process, data are necessary if the process uses them, and the model can be checked for collected data that no purpose needs.
- **Overlap:** Gives a computable meaning to "necessary for the purpose", which is what per-purpose field sets in Dagents assume.
- **What it does that Dagents does not:** Derives necessity from a process model instead of taking it from a classification someone wrote.

#### biega2020operationalizing
- **Citation:** Biega et al., "Operationalizing the Legal Principle of Data Minimization for Personalization", SIGIR 2020, pp. 399-408.
- **Verification:** DOI 10.1145/3397271.3401034 (Crossref); arXiv:2005.13718.
- **What it is:** Finds no single interpretation of data minimisation, tests two performance-based definitions on recommender systems and finds that minimisation may cost little accuracy overall but "might disparately impact different users".
- **Overlap and difference:** Shows that a minimisation rule has a fairness side effect, which a field-level plan does not measure.

#### goldsteen2022data
- **Citation:** Goldsteen et al., "Data minimization for GDPR compliance in machine learning models", AI and Ethics 2(3):477-491, 2022.
- **Verification:** DOI 10.1007/s43681-021-00095-8 (Crossref); arXiv:2008.04113.
- **What it is:** Removes or generalises input features of a trained model with little effect on accuracy, as a way to show minimisation of the inputs needed at prediction time.
- **Overlap and difference:** Decides the field set from the model; Dagents takes it from the request and classification.

#### marino2024compliance
- **Citation:** Marino et al., "Compliance Cards: Automated EU AI Act Compliance Analyses amidst a Complex AI Supply Chain", arXiv:2406.14758, 2024.
- **Verification:** arXiv:2406.14758.
- **What it is:** Machine-readable cards that carry AI Act metadata for a system and its datasets and pre-trained models, and an algorithm that predicts whether the whole complies.
- **Overlap and difference:** Machine-checkable compliance at provider level, from documentation, not from enforcement at run time.

#### tao2026empirical
- **Citation:** Tao et al., "An Empirical Study and Assessment of EU AI Act Compliance Checkers", arXiv:2609.36228, 2026.
- **Verification:** arXiv:2609.36228.
- **What it is:** Characterises 12 AI Act compliance checkers and finds that they simplify or omit key obligations, fail to give determinate guidance, and "may foster a false sense of compliance".
- **Overlap:** A warning for any claim that a framework satisfies the AI Act: running checks cover only the rows of Table 2 marked "Yes" or "Partly".

---

## 5. Dagents against this family (for the comparison section)

| Dagents element (from the brief and a quick read of the code) | Provisions it supports | Gaps this family points to |
|---|---|---|
| `before_read` per-request plan dropping unrequested fields | GDPR 5(1)(c), 25(2); HIPAA 164.502(b), 164.514(d)(3)(i) standard protocols; EHDS 66 | No route for non-routine requests needing individual review (164.514(d)(3)(ii)) |
| `before_send` at the site; minimum cohort | EHDS 69 and 73 (only anonymised statistical output leaves); HIPAA 164.514(b) in part | No external download review; legal anonymity is not decided by a threshold |
| `before_train` | AI Act 10(5)(c)-(d) for special categories; GMLP 4 in part | No bias examination or representativeness check (AI Act 10(2)(f)-(g), 10(3)) |
| `before_release`; aggregation yields a candidate; missing metric blocks; every status change from the planner | AI Act 9(8), 17(1)(a), 43(4); PCCP acceptance criteria; NIST MANAGE 1.1; GMLP 10 | No error-rate control across a sequence of releases (Feng et al. 2021); no shadow or canary; no monitoring after release (AI Act 72) |
| Model statuses candidate, released, rejected, superseded; a rollback version kept when a candidate is not released (`dagents/common/domain/federation.py`) | PCCP rollback criteria; SageMaker-like approval states | Rollback after a released model degrades is not described |
| Approval route `POST /model-versions/{version}:approve`, admin-scoped token (`dagents/gma/routes.py`) | AI Act 14 in part; SR 11-7 | The route takes only the version; a named approver and a check that approver is not the requester were not seen |
| Hash-chained audit with planner SHA-256 | AI Act 12, 19; EHDS 73; HIPAA 164.312(b); DPDP r.6; ISO/IEC 42001 A.6.2.8 | The in-memory log's docstring says real deployments need append-only storage, retention rules and signing; minimum retention (six months, one year) is a deployment duty |
| Fail closed when the planner is unavailable | AI Act 15(4) fail-safe | None found; most sources do not state failure behaviour |
| Classifications, approved purposes, minimum cohort from extensions | GDPR 5(1)(b); EHDS 53 | Who approves a purpose, and with what DPIA (GDPR 35), is outside the framework |

---

## 6. Unverified

- **Exact wording of most legal quotations.** EUR-Lex, eCFR, fda.gov, iso.org and the Indian Gazette were not reachable. Confirmed in search excerpts: AI Act Art. 12(1), 14(4)(d); EHDS Art. 66(3), 69, 73 (one-year logs, download limit); GMLP principle 10; SageMaker shadow-test sentence. Recalled, to check before quoting: GDPR Arts. 5, 25, 89; HIPAA 164.502(b)(1), 164.312(b); AI Act 9(8), 17(1), 43(4) second sentence, Annex IV 2(f); DPDP s.6(1).
- **AI Act Digital Omnibus details.** Dates (OJ 24 July 2026; in force 27 July 2026; Annex III 2 December 2027; Annex I 2 August 2028) come from law-firm summaries and trackers, not the OJ. Whether it moved Art. 10(5) bias processing into a new article, or changed Art. 4 (AI literacy) or the Art. 72 template, was not checked.
- **EHDS paragraph numbers.** Art. 53 point listing algorithm training; Art. 61(3) re-identification ban; Art. 68(12) permit duration; Art. 105 dates for 2029 and 2031 (secondary sources only).
- **FDA PCCP final guidance.** Exact issue date (3 or 4 December 2024), docket number, and the fourth modification-protocol component ("update procedures") are from law-firm summaries and memory. A June 2026 CRS report reportedly mentions an August 2025 final PCCP guidance; no such reissue was found, so it is likely a reporting error.
- **FDA lifecycle draft.** Whether it contains an example model card in an appendix (recalled, not checked).
- **NIST subcategory wording** (GOVERN 1.6, MEASURE 2.10, MANAGE 1.1, MANAGE 2.4) recalled from AI 100-1.
- **ISO/IEC 42001 Annex A titles.** Secondary sources disagree on A.6.2.5 (deployment or change management).
- **DPDP Rules.** Rule numbers and the one-year log retention come from summaries; the Second Schedule research standards were not read. A proposal to shorten the 18-month window (January 2026) was found; no notified amendment was found.
- **ML Test Score individual test wording** (canary, rollback, privacy controls), recalled.
- **Garg et al. 2011 and Basin et al. 2018 descriptions** recalled; abstracts not retrieved.
- **Not found or not checked because the search budget ran out:** Sato, Wider and Windheuser, "Continuous Delivery for Machine Learning" (martinfowler.com, 2019); TFX Evaluator "blessing" documentation; Rastegarpanah et al. on auditing data minimisation (NeurIPS 2021, title not found on arXiv); CEN-CENELEC JTC 21 harmonised standards for AI Act Art. 12 logging (prEN ISO/IEC 24970) and quality management; ISO/IEC 42006 and ISO/IEC 27001 Annex A logging controls; the HIPAA Security Rule NPRM of January 2025 and its status; Health Canada and MHRA PCCP guidance of their own.
