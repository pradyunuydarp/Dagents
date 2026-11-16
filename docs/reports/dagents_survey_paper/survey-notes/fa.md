# Family `fa`: bring the code to the data (federated analytics, research networks, TREs, data spaces, clean rooms, federated query compilers)

Prepared 2026-11-16. Academic metadata checked with Crossref or the arXiv API. Full texts read from arXiv for Conclave, SMCQL, Shrinkwrap, Senate, GRAIMatter, SACRO-ML, ACRO, the federated analytics survey, Confidential Federated Computations and the two data-space papers. Product, standard and regulation claims come from official documentation pages found by web search; those pages were seen only as search excerpts, because the sandbox cannot fetch them. The shared web search budget ran out before EHDS article text, Gaia-X documentation and some product pages could be checked further; what that left open is listed under **Unverified**.

## Synthesis

**Shared assumption.** Every system in this family keeps records where they are held and controls the boundary where results leave. They differ in who writes the rule about what may leave, how the rule is written, and whether a machine or a person applies it.

**Five ways of deciding what leaves.**

| Style | Examples | Who decides | Form of the rule | Where it is enforced |
|---|---|---|---|---|
| Fixed function library plus custodian thresholds | DataSHIELD, i2b2/SHRINE, TriNetX | Library authors; the data custodian sets numbers | Server-side functions that return only aggregates; options such as `nfilter.tab`, `nfilter.subset`, `nfilter.glm` | Inside each function, at each site |
| Distributed program plus site review | FDA Sentinel (PopMedNet), PCORnet, OHDSI, Personal Health Train, vantage6, Ocean Compute-to-Data | Each site's staff approve code, and sometimes results | Human approval; allowlists of algorithms; study-package parameters such as `minCellCount` | At each site, before running and before returning |
| Central TRE with output checking | Five Safes, OpenSAFELY, ACRO/SACRO, GRAIMatter, SATRE, EHDS secure processing environments | Trained output checkers or a health data access body | Written SDC (statistical disclosure control) rules, tools that flag outputs, law | At egress from the environment |
| Platform analysis rules | AWS Clean Rooms, Snowflake Data Clean Rooms, Ads Data Hub | Data owner configures; the vendor's engine applies | Per-table rules: allowed query shapes, aggregation thresholds, DP budgets, difference checks | In the vendor's query engine, at output |
| Compiler or cryptographic enforcement | SMCQL, Shrinkwrap, Conclave, Senate, Confidential Federated Computations | A schema annotation or data access policy; the compiler derives the plan | Typed annotations; a deterministic planner run at every party; MPC or TEE | In MPC protocols or TEEs; key release |

**Where models fit.** Most of these systems were built for statistical outputs (counts, tables, coefficients). Model release arrived later and separately. GRAIMatter (2022) states that TREs "do not have mature processes and controls" for trained models and proposes model disclosure controls (attack simulation against thresholds agreed with the data controller) or model query controls (keep the model behind a limited query service). AWS Clean Rooms ML has export controls for model artifacts that are separate from its SQL analysis rules. Only two designs put training and analytics under one rule set: DataSHIELD, whose model-fitting functions pass the same filters as every other function (`nfilter.glm` caps parameters per observation), and Confidential Federated Computations, where one data access policy bound to each upload governs every server-side step, learning or analytics.

**What the family does well.** Thresholds and dominance tests come from decades of SDC practice. Human checkers handle outputs no rule anticipated. Legal and organisational controls (data permits, accredited researchers, contracts) cover what software cannot. MPC and TEE designs give guarantees that do not depend on trusting the operator, and some come with proofs (Conclave's leakage theorem, SMCQL's information-flow typing). Several networks run at national or global scale (Mini-Sentinel held records of 99,260,976 health plan members in 2011; TriNetX grew to over 220 healthcare organisations by 2022).

**What it leaves to others.** The rule about what leaves is often scattered: thresholds sit in server options, function code, checker guidance or vendor defaults, and are rarely one analysable artifact. Per-request checks can be composed: Huth et al. (2023) rebuilt protected records from means and covariances available in established frameworks, and Vinterbo et al. (2012) showed that repeated noisy count queries average out. Usage-control data spaces let data leave and can only detect, not prevent, misuse of purpose, aggregation or anonymisation obligations. Explicit fail-closed behaviour is documented only in places: Ocean's default allows no algorithm, DataSHIELD functions stop when a check fails, Conclave picks a slower plan when consent is missing, and the CFC ledger releases no key outside the policy. Audit evidence is mostly logs; only CFC offers externally verifiable evidence (attestation plus open-source binaries).

**Relation to Dagents.** Dagents combines the first and fifth styles: a deterministic planner, like Conclave's compiler that "all parties run ... deterministically" or SMCQL's typed column labels, decides the protection for each field, and the site enforces it as DataSHIELD does. Its minimum cohort plays the role of `nfilter.subset` or `minCellCount`, and its release gate resembles a GRAIMatter model disclosure control. Prior art does several things Dagents does not: human output checking, membership-inference tests before release, cross-query privacy budgets and difference checks, cryptographic or hardware isolation from the operator, remote attestation, and legal permits.

---

## Entries

### 1. ramage2020federated (with zhu2019federated) — Google federated analytics

- **Citation:** D. Ramage and S. Mazzocchi, "Federated Analytics: Collaborative Data Science without Data Collection," Google Research Blog, May 2020. Supporting method: W. Zhu et al., "Federated Heavy Hitters Discovery with Differential Privacy," arXiv:1902.08534.
- **Verification:** URL https://research.google/blog/federated-analytics-collaborative-data-science-without-data-collection/ (found by search); arXiv:1902.08534 (checked).
- **What it is:** The post that named "federated analytics": Google's federated learning infrastructure reused for statistics, without the learning step. Devices compute local statistics and only aggregated results reach the server. In the Now Playing example, Pixel phones compute recognition rates for songs in their local history and encrypt them with secure aggregation, so the server learns the total without learning which songs any one phone heard. The post points to federated heavy-hitters discovery with differential privacy as a second method.
- **D1:** Which statistic is computed and which devices join a round (the server invites eligible phones); no per-request permit/deny.
- **D2:** Code in the analysis pipeline and the protocol (secure aggregation, DP); no policy language.
- **D3:** On the device (local computation) and at aggregation.
- **D4:** Computation and result egress; first used to evaluate federated models against real data.
- **D5:** Not stated.
- **D6:** Not stated.
- **D7:** Google's federated infrastructure and Android devices.
- **Overlap with Dagents:** Computation at each source, only aggregates leave, and one infrastructure serves analytics and learning.
- **What it does that Dagents does not:** Secure aggregation, so the coordinator never sees one source's report; scale to very many small sources; DP heavy-hitter discovery.

### 2. elkordy2023federated (and wang2022federated) — surveys of federated analytics

- **Citation:** A. R. Elkordy et al., "Federated Analytics: A Survey," APSIPA Trans. Signal and Information Processing 12(1):1–33, 2023. D. Wang, S. Shi, Y. Zhu, Z. Han, "Federated Analytics: Opportunities and Challenges," IEEE Network 36(1):151–158, 2022.
- **Verification:** DOI 10.1561/116.00000063 (arXiv:2302.01326, full text read); DOI 10.1109/MNET.101.2100328 (metadata only; full text not read).
- **What it is:** Elkordy et al. classify federated analytics queries (heavy hitters, set intersection, statistics, model evaluation) and the privacy tools used for each (local and central DP, secure aggregation, masking). They model a *querier* and *data owners* who assist "without violating its data privacy" (Fig. 2).
- **D1–D7:** A survey; it maps mechanisms, not governance. It does not treat who authorises a query or what may leave beyond the mechanism.
- **Overlap with Dagents:** Treats model evaluation as one federated analytics query, as Dagents runs evaluation through the same agents.
- **What it does that Dagents does not:** Catalogues protocol-level mechanisms (PSI, secure aggregation variants) that Dagents does not implement.
- **Quote:** "there is, unfortunately, no clear definition for what constitutes federated analytics" (Introduction, arXiv p. 2).

### 3. eichner2024confidential — Confidential Federated Computations (Google)

- **Citation:** H. Eichner et al., "Confidential Federated Computations," arXiv:2404.10764, 2024 (v2 2025).
- **Verification:** arXiv:2404.10764 (checked; full text read).
- **What it is:** An architecture for federated learning and analytics in which devices upload data encrypted to a *ledger* running in a TEE (trusted execution environment). Each upload is bound to a *data access policy*: a graph of the TEE-hosted binaries (with their privacy configuration, such as DP parameters) that may process it, and how many times. The device checks the policy and the ledger's attestation before uploading; later workloads ask the ledger for decryption keys, and the ledger grants or refuses them under the policy and records the use.
- **D1:** Which processing steps may access the data, with which parameters, how often; release of outputs only after the permitted steps.
- **D2:** A declarative policy bound to the data cryptographically (as associated data in HPKE key wrapping), plus open-source binaries; analysable by reading the policy and code.
- **D3:** On the device (policy check before upload) and at the ledger (key release).
- **D4:** Computation, result egress, retention (keys expire, which crypto-erases data); covers both learning and analytics pipelines.
- **D5:** Remote attestation reports, ledger access records, published source code.
- **D6:** Effectively fail-closed: data is unreadable without a key the ledger releases only under the policy. Restarting the ledger to erase the access history also erases the private key.
- **D7:** Google's platform and TEE hardware.
- **Overlap with Dagents:** A policy decided before data use, enforced at one choke point; stateful limits on use; evidence naming the code that decided (Dagents records the planner binary's SHA-256 in each audit record; CFC attests the binaries).
- **What it does that Dagents does not:** Hardware attestation that a remote party can check; enforcement by key release, so even the operator cannot read data outside the policy; the data contributor checks the policy before contributing; access counts that stop averaging attacks on DP outputs.
- **Quotes:** "A data access policy specifies a specific sequence of TEE-hosted transformations that can access uploaded data." (Sec. 3.5.1, p. 11). Fig. 2's example allows a SELECT stage and then a DP sum with ε ≤ 1 and δ ≤ 10⁻¹⁵, each once. "attempting to wipe out access history by restarting the ledger would also wipe out the private key, rendering the associated client data unreadable" (Sec. 3.5.1).

### 4. gaye2014datashield, marcon2021orchestrating, datashield2026disclosure — DataSHIELD

- **Citation:** A. Gaye et al., "DataSHIELD: taking the analysis to the data, not the data to the analysis," Int. J. Epidemiology 43(6):1929–1944, 2014. Y. Marcon et al., "Orchestrating privacy-protected big data analyses of data from different resources with R and DataSHIELD," PLOS Comput. Biol. 17(3):e1008880, 2021. DataSHIELD developer wiki, "Disclosure control."
- **Verification:** DOI 10.1093/ije/dyu188; DOI 10.1371/journal.pcbi.1008880; wiki URL found by search.
- **What it is:** R packages in a client-server design. Each study's data stays on an Opal server at the institution; the analyst sends commands, and server-side functions return only non-disclosive summaries. Marcon et al. add "resources", so large data in its original format and location can be analysed the same way. The wiki lists the disclosure filters each server applies.
- **D1:** Whether each function call's result may return; numeric thresholds; which methods a server offers.
- **D2:** A fixed function library written by the DataSHIELD developers, with thresholds as server options set by the custodian. Examples from the wiki: `nfilter.tab` (minimum non-zero cell count in a table; 5 is common), `nfilter.subset` (minimum subset size, typically 3), `nfilter.glm` (maximum model parameters as a share of the sample, default 0.33, so 330 parameters for 1,000 individuals). `datashield.privacyControlLevel` (from version 6.2, default "permissive") lets an administrator offer only a predefined subset of methods.
- **D3:** At each data source, inside each server-side function.
- **D4:** Data read (subsetting), computation including model fitting (GLMs), result egress. No separate model-release step: fitted models return coefficients through the same filters.
- **D5:** Not documented in the sources read.
- **D6:** Server functions stop with an error when a check fails (for example the subset function when a subset is below the minimum, per the wiki and source code seen in search).
- **D7:** R and Opal; domain-agnostic but built for epidemiology consortia.
- **Overlap with Dagents:** Very close in structure. Enforcement at the site, thresholds from the custodian's configuration, denial when a check fails, and model fitting under the same rules as analytics. `nfilter.subset` plays the role of Dagents' minimum cohort.
- **What it does that Dagents does not:** A large tested library of non-disclosive statistical functions; per-server custodian settings; server-side objects that never leave ("assign" functions next to "aggregate" functions); a decade of consortium use.
- **Quotes (Marcon 2021, abstract):** "Each institution has control over who can access their data." "The platform allows an analyst to pass commands to each server and the analyst receives results that do not disclose the individual-level data of any study participants."

### 5. huth2023accessibility — reconstruction attack on federated statistics functions

- **Citation:** M. Huth et al., "Accessibility of covariance information creates vulnerability in Federated Learning frameworks," Bioinformatics 39(9):btad531, 2023.
- **Verification:** DOI 10.1093/bioinformatics/btad531 (abstract read).
- **What it is:** An attack that reconstructs protected records from sample means, sample covariances and known linearly independent vectors built on the data owner's side, functions available in several established frameworks. The authors used DataSHIELD's CNSIM1 data set. The attack survives defences that add random noise; the authors discuss differential privacy as a defence.
- **D1–D7:** An evaluation, not a system.
- **Overlap with Dagents:** The lesson applies directly. A decision taken per request (permit or narrow this field for this request) cannot see that several permitted requests together disclose records.
- **What it does that Dagents does not:** Shows a concrete composition attack that any per-request governance layer should test against.
- **Quote:** "these basic functionalities, which are available in several established FL frameworks, are sufficient to reconstruct privacy-protected data" (abstract).

### 6. weber2009shared, murphy2010serving, vinterbo2012protecting — i2b2 and SHRINE

- **Citation:** G. M. Weber et al., "The Shared Health Research Information Network (SHRINE): A Prototype Federated Query Tool for Clinical Data Repositories," JAMIA 16(5):624–630, 2009. S. N. Murphy et al., "Serving the enterprise and beyond with informatics for integrating biology and the bedside (i2b2)," JAMIA 17(2):124–130, 2010. S. A. Vinterbo, A. D. Sarwate, A. A. Boxwala, "Protecting count queries in study design," JAMIA 19(5):750–757, 2012.
- **Verification:** DOI 10.1197/jamia.M3191; DOI 10.1136/jamia.2009.000893; DOI 10.1136/amiajnl-2011-000459 (metadata; content from search excerpts of the PMC copies).
- **What it is:** i2b2 is a clinical data warehouse platform for cohort discovery. SHRINE federated i2b2 repositories at Harvard-affiliated hospitals: a query goes to each hospital, which returns a count of matching patients; each institution keeps control of its database. Vinterbo et al. analyse how i2b2 perturbs counts (Gaussian noise, then rounding), show that fixed-size perturbation distorts small counts most and that repeated queries can average the noise out, and propose a differentially private alternative.
- **D1:** A transformation: only counts, and those perturbed.
- **D2:** Platform code and site configuration.
- **D3:** At each site's i2b2 instance before the count leaves.
- **D4:** Result egress for cohort counts only; no models.
- **D5:** Not stated in sources read.
- **D6:** Not stated.
- **D7:** i2b2 platform, clinical research.
- **Overlap with Dagents:** Site-side narrowing of a result (noise on counts) as the only form allowed out.
- **What it does that Dagents does not:** Multi-institution IRB agreements set up before any query; long production use for cohort discovery.
- **Unverified:** the SHRINE noise size (±3) and the small-count display rule (below 10); see the Unverified section.

### 7. hripcsak2015observational, ohdsi2026network — OHDSI / OMOP network studies

- **Citation:** G. Hripcsak et al., "Observational Health Data Sciences and Informatics (OHDSI): Opportunities for Observational Researchers," Stud. Health Technol. Inform. (MEDINFO 2015), IOS Press. The Book of OHDSI, Ch. 20 "OHDSI Network Research."
- **Verification:** DOI 10.3233/978-1-61499-564-7-574; book URL found by search.
- **What it is:** OHDSI is an international collaboration around the OMOP common data model. In a network study, a study package written in R or SQL fully specifies the analysis; each site runs it behind its firewall against its own OMOP database, and only aggregate results leave. Investigators never see other sites' data.
- **D1:** Which summary statistics leave; a small-cell threshold (`minCellCount`, often 5 in study packages; secondary source).
- **D2:** Code in the study package plus parameters; a local analyst runs it; site governance approves the protocol.
- **D3:** At each site.
- **D4:** Computation and result egress.
- **D5:** Aggregate results are published (the book says on the OHDSI GitHub).
- **D6:** Not stated.
- **D7:** OMOP CDM and OHDSI tools; health.
- **Overlap with Dagents:** The same code at every site against a common data model, and aggregate-only egress with a minimum count.
- **What it does that Dagents does not:** Standard vocabularies and a common data model, so one study package runs unchanged at many sites; large-scale published network studies.

### 8. curtis2012design, platt2018fda, fleurence2014launching — FDA Sentinel and PCORnet

- **Citation:** L. H. Curtis et al., "Design considerations, architecture, and use of the Mini-Sentinel distributed data system," Pharmacoepidemiol. Drug Saf. 21(S1):23–31, 2012. R. Platt et al., "The FDA Sentinel Initiative — An Evolving National Resource," NEJM 379(22):2091–2093, 2018. R. L. Fleurence et al., "Launching PCORnet, a national patient-centered clinical research network," JAMIA 21(4):578–582, 2014.
- **Verification:** DOI 10.1002/pds.2336 (abstract read); DOI 10.1056/NEJMp1809643 and DOI 10.1136/amiajnl-2014-002747 (metadata only).
- **What it is:** Mini-Sentinel built a distributed database: each data partner converts its records to a common data model and keeps them; programs are written centrally and executed locally; a secure portal distributes queries and returns results. On the PopMedNet software used by Sentinel and PCORnet, partners review a query before running it and review results before returning them (secondary sources).
- **D1:** Whether to run a query, and whether to return its result: a human decision at each partner.
- **D2:** Human review and agreements; centrally written programs.
- **D3:** At each data partner.
- **D4:** Execution and result egress.
- **D5:** Not stated in sources read.
- **D6:** Not stated.
- **D7:** US health plans and FDA; PopMedNet.
- **Overlap with Dagents:** Code goes to the data, partners keep possession, and the coordinator combines what partners return.
- **What it does that Dagents does not:** Human approval by each partner, twice; national regulatory use.
- **Numbers (Curtis 2012, abstract):** "As of July 2011, information on 99 260 976 health plan members was included"; 316 009 067 person-years; "All data partners have successfully executed distributed code and returned findings to the Mini-Sentinel Operations Center."

### 9. palchuk2023global — TriNetX

- **Citation:** M. B. Palchuk et al., "A global federated real-world data and analytics platform for research," JAMIA Open 6(2):ooad035, 2023.
- **Verification:** DOI 10.1093/jamiaopen/ooad035 (abstract read).
- **What it is:** A commercial federated network of electronic health record data. Data sits on hardware in each healthcare organisation's (HCO's) data centre, with a cloud-hosted option; HCOs supply de-identified, pseudonymised or limited data sets depending on local rules. Industry sponsors pay for access to network analytics; HCOs get analytics and trial opportunities. Several authors are TriNetX employees.
- **D1:** What aggregate a query may show; count obfuscation (institutional pages say counts are rounded up to the nearest 10; unverified).
- **D2:** Platform-wide rules set by the vendor.
- **D3:** In the platform.
- **D4:** Cohort queries and analytics.
- **D5–D6:** Not stated in the abstract.
- **D7:** Single vendor.
- **Overlap with Dagents:** One rule set applied uniformly across all sites.
- **What it does that Dagents does not:** Scale and a sustained business model: "expanding from 55 HCOs and 7 countries in 2017 to over 220 HCOs and 30 countries in 2022"; "Over 19 000 sponsored clinical trial opportunities"; "over 350 peer-reviewed scientific publications" (abstract).

### 10. beyan2020distributed — the Personal Health Train

- **Citation:** O. Beyan et al., "Distributed Analytics on Sensitive Medical Data: The Personal Health Train," Data Intelligence 2(1–2):96–107, 2020.
- **Verification:** DOI 10.1162/dint_a_00032 (abstract read).
- **What it is:** An architecture in which analytical tasks ("trains") travel to data sources ("stations") and execute there, so data owners stay in control; it is aligned with the FAIR principles.
- **D1:** Whether a task may run at a station (station owner).
- **D2:** Governance at the station; the abstract does not give a policy language.
- **D3:** At each station.
- **D4:** Computation; output control is not described in the abstract.
- **D5–D7:** Not stated in the abstract; health domain.
- **Overlap with Dagents:** The same compute-at-the-source principle; site autonomy.
- **What it does that Dagents does not:** FAIR metadata as a first-class design goal.
- **Quote:** "The main principle of the PHT is that data remain in their original location, and analytical tasks visit data sources and execute the tasks." (abstract).

### 11. moncadatorres2021vantage6, smits2022improved — vantage6

- **Citation:** A. Moncada-Torres et al., "VANTAGE6: an open source priVAcy preserviNg federaTed leArninG infrastructurE for Secure Insight eXchange," AMIA Annu. Symp. Proc. 2020:870–877 (published 2021). D. Smits et al., "An Improved Infrastructure for Privacy-Preserving Analysis of Patient Data," Stud. Health Technol. Inform., 2022.
- **Verification:** PMID 33936462, PMCID PMC8075508 (no Crossref DOI; metadata from search); DOI 10.3233/SHTI220682 (checked).
- **What it is:** Open-source infrastructure, by authors mostly at the Netherlands Comprehensive Cancer Organisation (IKNL), that runs federated algorithms at nodes held by each party, for horizontally and vertically partitioned data. Its design goals are autonomy, heterogeneity and flexibility; each party keeps control of its data. Version 3 adds secure node-to-node communication.
- **D1:** Which algorithms run on a node (party's choice); output control not described in sources read.
- **D2–D6:** Not stated in the sources read.
- **D7:** Open source; cancer epidemiology use.
- **Overlap with Dagents:** A node per data source plus a central server; open source; health use.
- **What it does that Dagents does not:** Vertically partitioned data; node-to-node channels.

### 12. desai2016five, green2023present — the Five Safes

- **Citation:** T. Desai, F. Ritchie, R. Welpton, "Five Safes: designing data access for research," UWE Economics Working Paper 1601, 2016. E. Green and F. Ritchie, "The present and future of the Five Safes framework," J. Privacy and Confidentiality 13(2), 2023.
- **Verification:** URL https://uwe-repository.worktribe.com/output/914745 (found by search); DOI 10.29012/jpc.831 (abstract read).
- **What it is:** A framework for designing and judging data access along five dimensions: safe projects, safe people, safe settings, safe data and safe outputs. It dates from 2003 (the UK Virtual Microdata Laboratory). Green and Ritchie review its use, limits and fit with principles-based regulation.
- **D1:** Whether a project, person, setting, data set and output are acceptable together.
- **D2:** A framework applied by people; not machine-checkable.
- **D3:** Mostly the research environment; outputs at egress.
- **D4:** All stages, from project approval to output release.
- **D5–D6:** Not applicable.
- **D7:** Domain-agnostic.
- **Overlap with Dagents:** GRAILS's trust score corresponds to safe people, its sensitivity to safe data, its restriction plan to safe outputs, and Dagents' approved purposes to safe projects.
- **What it does that Dagents does not:** Treats researcher accreditation and project approval as controls in their own right; relies on judgement for outputs no rule anticipated.
- **Quote:** "The Five Safes has become the default framework for confidential data governance across multiple sectors and countries." (Green and Ritchie 2023, abstract).

### 13. williamson2020factors, opensafely2026release, opensafely2023disclosure — OpenSAFELY

- **Citation:** E. J. Williamson et al., "Factors associated with COVID-19-related death using OpenSAFELY," Nature 584(7821):430–436, 2020. OpenSAFELY documentation "Requesting release of research outputs" and the permitted outputs policy; "Updated disclosure control guidance," 2023-09-18.
- **Verification:** DOI 10.1038/s41586-020-2521-4 (metadata); documentation URLs found by search, read as excerpts.
- **What it is:** A platform in which researchers' code runs against NHS primary care records where they are held (see Unverified); the 2020 Nature paper was its first major study. Outputs leave only after review: every output requested for release is reviewed independently by two trained output checkers, and only trained OpenSAFELY team members can release files. Since September 2023, outputs based on counts of 7 or fewer must be redacted (raised from 5), and counts should be rounded after suppression. A file for release may have at most 5,000 rows. Requests go through a tool called Airlock.
- **D1:** Whether each file may leave; redaction and rounding.
- **D2:** Written policy, applied by researchers first and then by two human checkers.
- **D3:** At egress from the secure server.
- **D4:** Code execution and result egress; model release is not addressed in the sources read.
- **D5:** Release requests describe the controls applied to each output; further audit not examined.
- **D6:** Nothing leaves without approval.
- **D7:** NHS England records; domain-specific.
- **Overlap with Dagents:** Code goes to the data and only checked aggregates leave.
- **What it does that Dagents does not:** Two-person human review of every output; numeric SDC rules revised in light of experience (the 2023 threshold change).

### 14. jefferson2022graimatter — GRAIMatter: model release from TREs

- **Citation:** E. Jefferson et al., "GRAIMATTER Green Paper: Recommendations for disclosure control of trained Machine Learning (ML) models from Trusted Research Environments (TREs)," Zenodo / arXiv:2211.01656, 2022.
- **Verification:** arXiv:2211.01656, DOI 10.5281/zenodo.7089491 (arXiv record checked; full text read).
- **What it is:** Recommendations from a DARE UK sprint (September 2022) on whether trained models may leave a TRE. Thirteen high-level recommendations (R 1–R 13) and detailed technical (TECH) and legal (ELA) ones. Two groups of controls: *model query controls* (MQCs), which keep the model behind a service that limits who queries it and how often, and *model disclosure controls* (MDCs), which test the model and release it only if it passes. MDCs include a risk checklist, running the model on data the TRE set aside, checking model size and file type, reading the training code, and membership and attribute inference attacks.
- **D1:** Whether a trained model may leave, or must stay behind a query service.
- **D2:** Human decision by TRE staff, informed by attack metrics and thresholds agreed with the data controller during governance approval; legal clauses (end-user licence) after release.
- **D3:** At training (the "training zone": DP or safe wrappers for high-capacity models), at export (the "export zone"), and after release.
- **D4:** Training, model release, post-release use.
- **D5:** A data use register entry for each exported model (R 11); a stored snapshot of the software that trained an approved model (TECH 3, ELA 2.3).
- **D6:** Release is refused when the model is too vulnerable (quote below).
- **D7:** UK TREs; domain-agnostic.
- **Overlap with Dagents:** A release decision separate from training, with thresholds owned by the governance side (Dagents: application extensions); DP during training for risky model types, similar to Dagents' clipping and noise on model updates; explicit treatment of federated learning.
- **What it does that Dagents does not:** Empirical privacy attacks as release evidence; checks of model size, file type and code; model query controls as an alternative to release; contract terms for the released model.
- **Quotes:** "TREs do not have mature processes and controls against these risks." (abstract). "The model should not be allowed to be exported if it is considered to be too vulnerable (based upon the thresholds agreed with the Data Controller during the data governance approval process -ELA 8)." (p. 27). "Trained models should not be allowed to be released from TREs if they were trained with unsafe hyperparameters, with unsafe variants ... or have been manually altered after the training algorithm had been run." (TECH 9, p. 46). "When using Federated Learning the final models should be tested for vulnerability (TECH 14)." (R 10, p. 33).

### 15. smith2022safe — SACRO-ML

- **Citation:** J. Smith et al., "Safe machine learning model release from Trusted Research Environments: The SACRO-ML package," arXiv:2212.01233, 2022 (v4 2025).
- **Verification:** arXiv:2212.01233 (checked; abstract read). Behaviour of the safe wrappers taken from the GRAIMatter paper's appendix (p. 80).
- **What it is:** Python tools for disclosure control of models. *SafeModel* wraps common ML models for checks before training ("ante-hoc"); *Attacks* runs simulated attacks after training ("post-hoc"). In the GRAIMatter description, a safe model's constructor compares the researcher's hyperparameters with the TRE's `rules.json`, changes them where it can and warns the researcher; `request_release()` starts the TRE's output checking.
- **D1:** Whether training parameters are acceptable; risk evidence for release.
- **D2:** A rules file set by the TRE plus library code; a human decides release.
- **D3:** In the TRE at training time and at release.
- **D4:** Training and model release.
- **D5:** Reports for the output checker.
- **D6:** Adjusts unsafe parameters and warns, rather than refusing (per GRAIMatter appendix).
- **D7:** Python ML libraries.
- **Overlap with Dagents:** Close to Dagents' `before_train` boundary: a configured check of training parameters before training, then a separate release step.
- **What it does that Dagents does not:** Measures disclosure risk empirically by attacking the model.

### 16. preen2025multi — ACRO / SACRO semi-automated output checking

- **Citation:** R. J. Preen, M. Albashir, S. Davy, J. Smith, "A Multi-Language Toolkit for the Semi-Automated Checking of Research Outputs," IEEE Trans. Privacy 2:55–66, 2025.
- **Verification:** DOI 10.1109/TP.2025.3566052 (checked); arXiv:2212.02935 (full text read).
- **What it is:** A Python package (with R and Stata front ends) that wraps common analysis calls. It flags potentially disclosive tables, plots and regressions, optionally suppresses or rounds them, records why, and writes summaries for TRE staff, who decide in a GUI. The data custodian sets the test parameters in a YAML file loaded when ACRO starts.
- **D1:** Flag, suppress or round each output; final release by a human.
- **D2:** Configuration plus principles-based tests. Defaults (Table II): minimum cell frequency 10; minimum residual degrees of freedom 10; NK dominance test N = 2, K = 0.9; p% ratio 0.1; zeros treated as disclosive.
- **D3:** In the TRE, as each output is produced, and at egress review.
- **D4:** Result egress for statistics; models through SACRO-ML.
- **D5:** "auditable records" and summary documents (abstract).
- **D6:** Assistive; the human decides.
- **D7:** TREs; several analysis languages.
- **Overlap with Dagents:** Thresholds owned by the custodian in configuration; checks applied when the result is produced, before it leaves.
- **What it does that Dagents does not:** Dominance and degrees-of-freedom tests; a review interface for a human checker. The paper says parameters per data set or attribute are planned; Dagents already decides per field.
- **Quote:** "SACRO is designed to assist human checkers rather than seeking to replace them as with current automated rules-based approaches." (abstract).

### 17. mansouribenssassi2021machine — the model-disclosure problem stated

- **Citation:** E. Mansouri-Benssassi et al., "Machine Learning Models Disclosure from Trusted Research Environments (TRE), Challenges and Opportunities," arXiv:2111.05628, 2021.
- **Verification:** arXiv:2111.05628 (checked; abstract read).
- **What it is:** The problem statement that preceded GRAIMatter: TREs have output policies for statistics but none for trained models; the paper lists model vulnerabilities and the factors that affect them.
- **D1–D7:** A position paper.
- **Overlap with Dagents:** Motivates a separate release boundary for models.
- **Quote:** "Although TREs have clear policies for the disclosure of statistical outputs, the extent to which trained models can leak personal training data once released is not well understood and guidelines do not exist within TREs for the safe disclosure of these models." (abstract).

### 18. satre2023standard — SATRE

- **Citation:** SATRE project, "Standard Architecture for Trusted Research Environments," v1.0.0.
- **Verification:** URL https://satre-specification.readthedocs.io/en/v1.0.0/ (found by search; read only as excerpts).
- **What it is:** An open, community-led UK specification of TRE capabilities. Its data management pillar includes an output management capability.
- **D1–D7:** A capability specification; it states what a TRE must be able to do, not the rules for any one output. Details beyond this were not checked.
- **Overlap with Dagents:** Names output management as a required capability of the environment.
- **What it does that Dagents does not:** Covers organisational and infrastructure capabilities that a framework does not.

### 19. eu2025ehds, ec2026ehds — European Health Data Space

- **Citation:** Regulation (EU) 2025/327 of 11 February 2025 on the European Health Data Space, OJ L 2025/327, 5.3.2025. European Commission EHDS page.
- **Verification:** EUR-Lex URL https://eur-lex.europa.eu/eli/reg/2025/327/oj/eng (citation details confirmed by search); article text seen only through a secondary site's excerpt of Article 73.
- **What it is:** EU law for primary and secondary use of electronic health data. For secondary use, health data access bodies (HDABs) issue data permits, and personal electronic health data is processed only in secure processing environments (SPEs). Per the excerpt of Article 73: access to the SPE is limited to authorised persons named in the data permit; access and activity logs are kept for at least one year; and HDABs review data in a download request so that users can download only non-personal data, including data in an anonymised statistical format. The Commission's page says no personal data can be downloaded from these environments.
- **D1:** Who may access (permit holders), what may be downloaded (non-personal only).
- **D2:** Law; a human review by the HDAB of each download request.
- **D3:** At SPE egress.
- **D4:** Access, processing, download.
- **D5:** Logs of access and activity, kept at least one year.
- **D6:** Downloads need review; default is no personal data out.
- **D7:** EU law; health.
- **Overlap with Dagents:** Permit purposes resemble Dagents' approved purposes; egress limited to non-personal results; mandatory logs resemble the audit chain.
- **What it does that Dagents does not:** A legal mandate, permits and a public body that reviews each download.
- **Open question:** Whether a trained model counts as "non-personal" data that may be downloaded was not established here (see Unverified).

### 20. ids2026ram, dam2023policy, dam2023survey, zrenner2019usage, edc2026policy — data spaces and usage control

- **Citation:** IDSA, IDS Reference Architecture Model 4, sections 3.4.6 (policy enforcement) and 4.1.6 (usage control). T. Dam, A. Krimbacher, S. Neumaier, "Policy Patterns for Usage Control in Data Spaces," arXiv:2309.11289, 2023. T. Dam et al., "A Survey of Dataspace Connector Implementations," ITADATA 2023, CEUR-WS 3606. J. Zrenner et al., "Usage control architecture options for data sovereignty in business ecosystems," J. Enterprise Inf. Manag. 32(3):477–495, 2019. Eclipse Dataspace Components (EDC), "Policy Engine" documentation.
- **Verification:** IDS and EDC URLs found by search (excerpts); arXiv:2309.11289 and arXiv:2309.11282 (checked; 2309.11289 full text read); DOI 10.1108/JEIM-03-2018-0058 (abstract read).
- **What it is:** Here data does move. A provider attaches usage policies to data, written in the IDS Usage Control Language based on ODRL, and connectors enforce them: a policy enforcement point intercepts the data flow and asks a policy decision point. MY DATA (Fraunhofer IESE) and LUCON (Fraunhofer AISEC) implement this. In EDC, the control plane's policy engine evaluates policies at fixed scopes (contract negotiation, transfer process, and a policy monitor that ends a running transfer when its policy stops holding); the checks are policy functions contributed as extensions. Dam et al. classify which obligations a connector can prevent and which it can only detect.
- **D1:** Whether to share, under which contract; obligations on later use.
- **D2:** A policy language (ODRL profile); policy functions in code (EDC).
- **D3:** At provider and consumer connectors; after transfer, in the consumer's systems.
- **D4:** Contract, transfer and use after transfer.
- **D5:** Activity logging as an obligation; "detective" evidence.
- **D6:** Not stated.
- **D7:** Domain-agnostic standards; EDC is open source under Eclipse.
- **Overlap with Dagents:** A policy engine whose checks come from extensions, evaluated at fixed points in a process (EDC scopes, Dagents' four boundaries).
- **What it does that Dagents does not:** A standard policy language; contract negotiation between organisations; identity and catalogue services.
- **Key finding:** In Dam et al.'s Table 1, time limits, access counts and rate limits are *preventive*, while purpose, deletion, aggregation and anonymisation obligations are only *detective* once data has reached the consumer. This is the main argument for compute-to-data designs. "Currently, there is no agreed-upon set of policy patterns implemented by the EDC that supports enforceability by the connector" (Sec. 1, p. 2; 2023).

### 21. aws2026analysisrules, aws2026dp, aws2026minagg, aws2026mlexport — AWS Clean Rooms

- **Citation:** AWS Clean Rooms User Guide pages: analysis rules; differential privacy policy; minimum aggregation thresholds for the custom rule; exporting model artifacts from AWS Clean Rooms ML.
- **Verification:** URLs found by search; read as excerpts.
- **What it is:** A managed service in which collaborators query each other's tables without copying them. Each member who contributes a table configures an analysis rule of one of three types: *aggregation* (only aggregation queries on listed columns, with output constraints that remove rows built from fewer than a minimum number of distinct values of a column), *list*, or *custom* (custom queries or approved analysis templates). Differential privacy is available on the custom rule (AWS Clean Rooms SQL engine, data in S3); a privacy budget limits how many aggregations the querying member can run and sets the noise. The custom rule can also set a minimum identity count, between 2 and 100,000, below which result rows are suppressed; the documentation we saw says these thresholds cannot be combined with DP. Clean Rooms ML sends model artifacts only to members allowed to receive them, exports only the files selected when the algorithm was associated, up to a set maximum size, and writes metrics and logs under privacy controls.
- **D1:** Permit or refuse a query shape; suppress rows; add DP noise within a budget; who receives results; whether model artifacts leave and how large.
- **D2:** Configuration per table and per collaboration, through the API or console.
- **D3:** In AWS's query engine, at output; model export at the ML service.
- **D4:** Query, result egress, ML training and inference, model export.
- **D5:** Logs and metrics with access controls (ML); query logging not examined.
- **D6:** Not stated.
- **D7:** AWS only.
- **Overlap with Dagents:** The data owner configures rules per table and column; minimum thresholds; a release step for model artifacts that is distinct from query rules.
- **What it does that Dagents does not:** Restrictions on query shape (which columns may be joined or aggregated); a cumulative DP budget across queries; a separate result-receiver role.

### 22. snowflake2026policies — Snowflake Data Clean Rooms

- **Citation:** Snowflake documentation, "Understanding Snowflake Data Clean Room policies."
- **Verification:** URL found by search; read as excerpts.
- **What it is:** Analyses run through templates. The clean room has no aggregation policy engine of its own: aggregation constraints come from Snowflake aggregation policies on the source tables or are written into a template. Projection policies stop a column from being selected while still allowing filters and joins on it; column policies say which columns a given template may project.
- **D1:** Which columns may appear, be joined or filtered; minimum group sizes.
- **D2:** Database policies plus templates.
- **D3:** In Snowflake's engine.
- **D4:** Query and result egress.
- **D5–D6:** Not examined.
- **D7:** Snowflake only.
- **Overlap with Dagents:** Per-column rules that say whether a field may be output, used only for joining, or must be aggregated.
- **What it does that Dagents does not:** Separates "may be projected" from "may be joined or filtered on."

### 23. google2026adh — Google Ads Data Hub privacy checks

- **Citation:** Google, "Privacy checks in Ads Data Hub."
- **Verification:** URL found by search; read as excerpts.
- **What it is:** A clean room for advertising data with fixed vendor rules. Rows of results must aggregate at least 50 users (10 for queries that access only clicks and conversions). Difference checks compare a job's results with earlier results and filter them when they could reveal individual users; noise injection is offered as an alternative. Filtered rows can be pooled into a summary row, which is itself filtered if it fails the threshold.
- **D1:** Remove rows or whole result sets.
- **D2:** Fixed vendor rules.
- **D3:** At output, in Google's engine.
- **D4:** Result egress.
- **D5–D6:** Not examined.
- **D7:** Google advertising only.
- **Overlap with Dagents:** A minimum aggregation size enforced before results leave.
- **What it does that Dagents does not:** Difference checks across successive jobs, which address the composition problem that per-request decisions miss.

### 24. bater2017smcql — SMCQL

- **Citation:** J. Bater et al., "SMCQL: Secure Querying for Federated Databases," PVLDB 10(6):673–684, 2017.
- **Verification:** DOI 10.14778/3055330.3055334; arXiv:1606.06808 (full text read).
- **What it is:** A "private data network": mutually distrustful data providers, a shared schema and an honest broker that plans queries. A DBA, working with stakeholders, annotates each column as *public* (readable by all), *protected* (visible at its site; released only under k-anonymity) or *private* (never disclosed outside its provider; may not appear in results). A security type system labels each operator in the query plan low or high; high operators run as secure computation (garbled circuits, ORAM), the rest run in plaintext at each site, and the plan is sliced into smaller secure computations.
- **D1:** Which operators run under secure computation; which attributes may appear in results.
- **D2:** An annotated schema and typing rules over relational algebra (formal inference rules).
- **D3:** At each provider (plaintext steps) and in the secure computation; the honest broker assembles outputs.
- **D4:** Query evaluation and result egress. A query admission policy (for example "at least k data providers must contribute tuples") is discussed but not built; the paper points to existing systems for it.
- **D5–D6:** Not stated.
- **D7:** SQL; demonstrated on clinical data (HealthLNK).
- **Overlap with Dagents:** The closest design analogue in this family. A per-column sensitivity label, set by policy rather than by the querier, determines through typed rules how each operation is protected.
- **What it does that Dagents does not:** Cryptographic secure computation; information-flow typing across the whole query plan.
- **Quotes:** "We decouple the security policy from the querier by providing the honest broker with a schema-based security policy at startup." (Sec. 1). "Private attributes may not appear in any results returned to the user." (Sec. 5.1, p. 5). A fully secure evaluation "is on the order of 4–5 orders of magnitude slower than its plaintext execution" (Sec. 4.1).

### 25. bater2018shrinkwrap — Shrinkwrap

- **Citation:** J. Bater et al., "Shrinkwrap: Efficient SQL Query Processing in Differentially Private Data Federations," PVLDB 12(3):307–320, 2018.
- **Verification:** DOI 10.14778/3291264.3291274; arXiv:1810.01816 (full text read in part).
- **What it is:** Secure query processing pads every intermediate result to its worst-case size. Shrinkwrap instead gives data owners a differentially private view of intermediate result sizes, padding each to a noisy size, and allocates a privacy budget across the operators of the plan.
- **D1:** How much padding each operator gets; the privacy budget per operator.
- **D2:** An allocation algorithm in the query coordinator; computational DP guarantees.
- **D3:** In the secure computation.
- **D4:** Query evaluation; optionally DP outputs.
- **D5–D7:** Not stated; SQL.
- **Overlap with Dagents:** Treats a privacy budget as a planned quantity, decided before execution.
- **What it does that Dagents does not:** Allocates one budget across all steps of a computation.
- **Number:** "up to 35X performance improvement over oblivious query processing" (abstract).

### 26. volgushev2019conclave — Conclave (the local-versus-MPC planner)

- **Citation:** N. Volgushev et al., "Conclave: secure multi-party computation on big data," EuroSys 2019, pp. 1–18.
- **Verification:** DOI 10.1145/3302424.3303982; extended report arXiv:1902.06288 (full text read).
- **What it is:** A query compiler that turns a relational query into a mix of local cleartext jobs (sequential Python or Spark) and MPC jobs (Sharemind or Obliv-C). Each party runs a Conclave agent. Analysts annotate each input relation with its owner (`at=`), each output with its recipients (`to=`), and optionally a column with a *trust set*: the parties allowed to see that column in the clear. One party may be a selectively-trusted party (STP). The planner, as described in Sec. 5:
  1. Start with the whole query DAG in one MPC. Propagate input owners down and output recipients up. A unary operator inherits its input's owner; a multi-input operator whose inputs have different owners has no owner and must run under MPC.
  2. Push the MPC frontier down: pull operators on single-owner relations out of MPC; push a `concat` of parties' inputs below operators that distribute over it; split an aggregation into per-party pre-aggregations plus a secondary aggregation under MPC. Push the frontier up: run reversible operators at the output (such as multiplying by a constant) in the clear at the recipient; rewrite a final `count` as an MPC projection plus a cleartext count.
  3. Propagate trust sets: a result column's trust set is the intersection of the trust sets of every operand column it depends on, including group-by and join keys.
  4. Where trust sets allow, replace operators with hybrid operators (a hybrid join when both key columns share an STP, a public join, a hybrid aggregation when the group-by column's trust set includes an STP), which splits the large MPC into smaller ones.
  5. Remove or localise expensive oblivious sorts.
  6. Cut the DAG at each local/MPC transition and generate code for each backend.
  Push-ups do not change security. Push-downs change the sizes of MPC inputs, which can leak counts (for example the number of distinct group-by values per party), so they need consent; the appendix proves that this leaked length is the only security effect (Theorem A.2). The model is semi-honest.
- **D1:** Where each operator runs (local, MPC or hybrid) and which columns an STP may see.
- **D2:** A compiler over annotations, with a proof of what its rewrites leak.
- **D3:** At each party's agent and in the MPC.
- **D4:** Query execution. What the result is, and who gets it, is fixed by the analyst's annotations and agreed outside Conclave.
- **D5:** Not stated.
- **D6:** A safe default: without authorisation it chooses the slower plan.
- **D7:** Relational queries (SQL or LINQ front end); domain-agnostic.
- **Overlap with Dagents:** A pure, deterministic planner that every party can run and get the same answer; the plan decides where each step may run from declared ownership and trust; a safe default when permission is missing (compare Dagents failing closed).
- **What it does that Dagents does not:** Cryptographic MPC; a formal leakage bound; trust sets propagated through a dataflow graph (Dagents decides per field of a request, not along a computation DAG).
- **Quotes:** "parties agree via out-of-band mechanisms on the query to run" (Sec. 4.1). "For data-dependent sizes, Conclave only proceeds with a rewrite if all affected parties have authorized it, choosing a slower query plan otherwise." (Sec. 3.2, p. 4). "Conclave compiles a query in six stages ...; all parties run these stages deterministically." (Sec. 5, p. 5). "Conclave only reveals a column to a party if the column can be derived from input columns that the party is authorized to learn." (Sec. 5.1, p. 7).

### 27. poddar2021senate — Senate

- **Citation:** R. Poddar et al., "Senate: A Maliciously-Secure MPC Platform for Collaborative Analytics," USENIX Security 2021.
- **Verification:** arXiv:2010.13752 (comment "USENIX Security 2021"; full text read). No DOI.
- **What it is:** SQL analytics across parties under malicious security. A new decomposition protocol splits the MPC into circuits that subsets of parties evaluate, in parallel; a query planner decides the decomposition with a cost model. Filters and partial aggregates run locally on plaintext where that is safe.
- **D1:** How the computation is split across parties; local versus joint steps.
- **D2:** Compiler and planner, run at each party, producing the same plan.
- **D3:** At each party and in the MPC.
- **D4:** Query execution. Which results may be released is decided by people before Senate runs.
- **D5–D7:** Not stated; SQL; domain-agnostic.
- **Overlap with Dagents:** A deterministic plan computed independently by each party; local work first.
- **What it does that Dagents does not:** Security against parties who deviate from the protocol.
- **Quotes:** "even if m − 1 out of m parties fully misbehave and collude, an honest party is guaranteed that nothing leaks about their data other than the result of the agreed upon query." (Sec. 1.1). Agreement stage: "This happens before invoking Senate and may involve humans." (Sec. 1.1). "the query result might leak information about the underlying datasets, and the parties should choose carefully what query results they are willing to share with each other." (p. 4). Up to 145× faster than prior work (abstract).

### 28. ocean2026c2d — Ocean Protocol Compute-to-Data

- **Citation:** Ocean Protocol developer documentation, "Compute-to-Data: compute options."
- **Verification:** URL found by search; read as excerpts.
- **What it is:** A data marketplace feature in which algorithms run where the data is. By default a compute data set allows no algorithm at all. The publisher lists trusted algorithms (`publisherTrustedAlgorithms`) by identifier or by trusted publisher, or allows all with a wildcard; the documentation warns that allowing raw algorithms is the option most prone to data escape.
- **D1:** Which algorithm may run on a data set.
- **D2:** Configuration in the asset's metadata.
- **D3:** At the provider's compute environment.
- **D4:** Computation; checks on the returned output are not described in the pages seen.
- **D5:** Not examined.
- **D6:** Default deny.
- **D7:** Ocean Protocol (web3 marketplace).
- **Overlap with Dagents:** Default deny, and an allowlist of computations the data owner approved (compare Dagents' model kinds and approved purposes from extensions).
- **What it does that Dagents does not:** Marketplace and payment; per-algorithm approval by the data publisher.

---

## Cross-cutting answers to the key question

| System | Who decides what leaves | In what form | Enforced at | Same rules for training and model release? |
|---|---|---|---|---|
| Google FA | System designer | Fixed aggregate, secure aggregation, DP | Device and aggregator | Same infrastructure as FL |
| CFC | Data access policy author; device verifies | Graph of attested binaries with DP settings, use counts | Ledger key release (TEE) | Yes, one policy for FL and FA |
| DataSHIELD | Library authors; custodian thresholds | Non-disclosive functions plus `nfilter.*` | Each function at each site | Yes for fitting (`nfilter.glm`); no separate release |
| i2b2/SHRINE | Platform and site configuration | Perturbed counts | Each site | No models |
| OHDSI | Study package author; site analyst | Aggregates with `minCellCount` | Each site | Not examined |
| Sentinel/PCORnet | Each data partner's staff | Reviewed query and reviewed result | Each partner | Not examined |
| TriNetX | Vendor | Platform obfuscation | Platform | Not examined |
| PHT, vantage6, Ocean | Station or node owner | Approval or allowlist of algorithms | Each site | Same approval covers training code; outputs not described |
| OpenSAFELY | Two trained output checkers | Redaction ≤7, rounding, ≤5,000 rows | Egress | Not addressed in sources read |
| ACRO/SACRO | TRE checker, assisted by tool | Configured SDC tests | Analysis time and egress | Separate tool (SACRO-ML) for models |
| GRAIMatter | TRE staff with data controller thresholds | Attack metrics, checks, or query limits | Training zone, export, post-release | Defines the model rules |
| EHDS | Health data access body | Only non-personal data downloaded | SPE egress | Not established |
| IDS/EDC | Provider's policy | ODRL obligations | Connectors; then trust | n/a (data moves) |
| AWS Clean Rooms | Each data contributor | Analysis rules, thresholds, DP budget | Vendor engine | Separate ML export controls |
| Snowflake DCR | Data owner | Templates, aggregation and projection policies | Vendor engine | Not examined |
| Ads Data Hub | Vendor | 50-user threshold, difference checks | Vendor engine | n/a |
| SMCQL | DBA's annotated schema | Typed plan: plaintext vs secure; k-anonymity | Providers and MPC | No models |
| Shrinkwrap | Coordinator's budget allocation | DP-padded intermediates | MPC | No models |
| Conclave | Annotations; compiler derives plan | Local/MPC/hybrid plan; consent for leaks | Parties and MPC | No models |
| Senate | Humans agree the query | Decomposed malicious MPC | Parties and MPC | No models |

---

## Unverified

- **SHRINE obfuscation values.** A secondary guide (CASRAI) says i2b2/SHRINE adds bounded noise of about ±3 patients and masks counts below a configurable threshold; a common description is that counts under 10 show as "<10". Not confirmed in Weber et al. 2009 or official i2b2/SHRINE documentation (the full text was not reachable). Only the general mechanism (Gaussian noise then rounding) is supported, by Vinterbo et al. 2012 as seen in search excerpts.
- **TriNetX count rounding.** University pages (Rochester, Cincinnati) say counts are rounded up to the nearest 10 or shown as "<10"; they disagree on the display, and neither is TriNetX documentation or the Palchuk paper.
- **OHDSI `minCellCount` default of 5 and a local check of outputs.** Seen in study-package source code and in a fork of the Book of OHDSI, not in the official chapter text.
- **PopMedNet review steps.** That data partners review queries before running them and results before returning them comes from a PMC figure caption and a Sentinel slide deck seen in search, not from PopMedNet's specification. PCORnet details beyond the 2014 paper's metadata were not checked.
- **vantage6 algorithm allowlists.** We recall that vantage6 node configuration can restrict which algorithm images run (an "allowed algorithms" setting); not verified in this session.
- **EHDS article wording.** Article 73 content came from a secondary site's excerpt (streamlex) and the Commission's summary page, not from the Official Journal text. Other provisions we expect but did not check: data provided in anonymised form by default and pseudonymised only when the purpose needs it; a list of prohibited purposes; and how a trained model is treated on download. An EUI presentation seen in search notes that "download" is not the only way to copy data out and that "non-personal" is hard to delimit for aggregated data. The search budget ran out before these could be checked.
- **Gaia-X.** No official Gaia-X documentation was examined. Braud et al. 2021 (IEEE Network 35(2):4–5, DOI 10.1109/MNET.2021.9387709) was verified for metadata only; it is a two-page column, and its content was not read.
- **AWS Clean Rooms after August 2026.** A third-party blog (ecorpit.com) says AWS added minimum aggregation thresholds that work with the custom rule on 13 August 2026 and that combine with differential privacy differently from older pages. Not confirmed in AWS release notes. The AWS pages seen say thresholds cannot be used with differential privacy.
- **AWS custom rule's allowed analyses.** A documentation page titled "custom-allowed-analyses" exists; we did not see its text, so the exact mechanism (lists of allowed templates or allowed query providers) is not described above.
- **OpenSAFELY numbers.** The ≤7 threshold, rounding advice, 5,000-row limit, two-checker rule and Airlock come from OpenSAFELY pages, but seen only as search excerpts.
- **DataSHIELD defaults.** Defaults differ by version and source (an older test script sets `nfilter.tab` to 3; the wiki suggests 5).
- **Handbook on Statistical Disclosure Control for Outputs** (Safe Data Access Professionals working group, v1.0, July 2019; authors include E. Griffiths, C. Greci and Y. Kotrotsios). No official URL or full author list was confirmed, so it is not in the .bib.
- **HDR UK Trusted Research Environments Green Paper** (2020). Not checked; not in the .bib.
- **Wang et al. 2022 (IEEE Network).** Metadata verified; content not read, so no claims are made about it.
- **OpenSAFELY architecture.** That researchers' code runs inside the record holders' data centres is from our knowledge of the 2020 Nature paper; its abstract was not available through Crossref and the paper was not read in this session.
- **DataSHIELD "assign" functions.** The split between aggregate functions (results return to the analyst) and assign functions (objects stay on the server) is from our knowledge of DataSHIELD; in this session it is supported only by the `.ag` suffix of a dsBase function name seen in search.
- **Senate page numbers.** The USENIX proceedings pages were not checked; the .bib entry gives the venue and arXiv id only.
- **Not covered for lack of search budget:** Microsoft Azure confidential clean rooms, LiveRamp and other clean-room vendors, Opaque and other confidential-computing analytics systems, and the Gaia-X Trust Framework.
