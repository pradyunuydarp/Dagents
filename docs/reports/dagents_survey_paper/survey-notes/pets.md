# Family `pets`: privacy-enhancing technologies for releasing data and statistics

Scope: syntactic anonymisation and statistical disclosure control (SDC), the attacks
that mark their limits, differential privacy (DP) foundations, DP query and analytics
systems, privacy-budget and release management, and recent practice and guidance.
37 works (some entries cover two closely linked papers). BibTeX: `pets.bib`.

Method note: metadata was checked with Crossref and arXiv (abstracts, and full text for
five arXiv PDFs), PyPI package metadata and wheels (OpenDP, SmartNoise SQL, PipelineDP,
Tumult Analytics), and official pages found by web search. The shared web-search budget
ran out part-way through, so a few items could not be checked further; they are listed
under **Unverified** or carry a note in their entry.

---

## Synthesis

Two traditions ask what can be released about a dataset without exposing its people.

**Statistical disclosure control and syntactic models** (k-anonymity, l-diversity,
t-closeness, microaggregation) treat privacy as a property of the released table. A
methodologist, or a search over transformations (ARX), applies recoding, top and bottom
coding, suppression, microaggregation, noise addition, or a frequency threshold for
tables, until the output meets a target property and a utility measure is acceptable.
GRAILS's strategies come from this tradition, and so do Dagents's `Generalize`,
`Redact`, `AggregateOnly` (a minimum-frequency rule) and `Refuse`. Agencies have used
it for decades, and its tools (sdcMicro, ARX) report risk and utility for each
transformation.

**Differential privacy** treats privacy as a property of the algorithm. Cohere puts it
as "DP is a characteristic of the algorithm, not the data". A DP guarantee needs four
things: a unit of privacy (whose presence is hidden), a bound on each unit's
contribution, noise calibrated to that bound and drawn unpredictably, and accounting
across every release on the same units. Systems put these in composable operators
(PINQ, Ektelo, OpenDP, Tumult Core), a type system (Fuzz), query rewriting (FLEX,
Chorus, Google's DP SQL), a session or odometer (Tumult, SmartNoise), or a separate
budget service or scheduler (LinkedIn, Sage, PrivateKube, DPack, Cohere).

**Attacks separate the two traditions.** Linkage (Narayanan and Shmatikov), composition
of independent releases (Ganta et al.), predicate singling out (Cohen and Nissim),
downcoding of generalisation hierarchies (Cohen 2022) and reconstruction from many
accurate statistics (Dinur and Nissim; the Census Bureau's 2010 experiment) all break
syntactic protections. A property of one output table says nothing about the adversary's
other knowledge or about other releases. DP's guarantee holds against any auxiliary
information and composes, which is why the 2020 US Census adopted it.

**Who decides.** In every DP system surveyed, a person chooses the privacy-loss
parameter (ε or ρ) and the unit of privacy: a policy committee (the Census Bureau), the
data owner (Airavat, PINQ), the analyst (Tumult), the pipeline author (PipelineDP) or
the operator (Sage, LinkedIn). Software then derives the noise scale from that target
and the contribution bound (OpenDP's search over a chain, Google's accuracy-to-parameter
tool). Most systems also check each query against the remaining budget; SmartNoise only
records spend. Cohere's data-independent resource planner comes closest to a planner
that decides: it allocates budget among requests by optimisation. No surveyed system sets ε from the requester's trust or purpose, as
GRAILS sets a strategy.

**Where enforcement happens.** Mostly at output, in a central engine that holds the raw
data. Local DP (RAPPOR, Apple, Microsoft) enforces on the device. "Release management"
here means budget management, not approval of a model.

**The line between "a rule chose a transformation" and "a privacy bound was proved".**
A rule table (GRAILS, Dagents, SDC threshold rules) can be total and analysable. Its
output carries a bound only when the chosen transformation is a DP mechanism with a
fixed unit, a contribution bound, calibrated noise and an accountant. Generalisation,
binning, masking, suppression and exact-count thresholds give no bound against
auxiliary information. At most they give a syntactic property (k-anonymity) when they
are chosen to reach a given k, and fixed-level rounding does not do that. Dagents's
typed planner proves that every combination of inputs gets a decision. That is a
property of the policy, not of privacy loss, and Dagents's own Guard says its noise "is
not a calibrated differential-privacy mechanism".

**What the family leaves to others:** who is asking and why (trust and purpose), access
control beyond the budget (Airavat and Cohere are exceptions), audit evidence beyond a
budget ledger, and model release decisions other than DP accounting.

---

## Table 1. GRAILS strategies mapped to the SDC and DP literature, and to Dagents

| GRAILS strategy | Name in SDC / DP and source | Dagents counterpart (planner / Guard) | Provable guarantee? |
|---|---|---|---|
| Suppression | Local or record suppression (`hundepool2012statistical`; sdcMicro; ARX) | `Redact` (field withheld), `Refuse` | None on its own. It is one of the two means of reaching k in k-anonymity |
| Top/bottom coding | Global recoding of the tails (`hundepool2012statistical`; sdcMicro) | No separate strategy. Closest: a scalar clamped to ±bound by `ClipContribution` | None |
| Microaggregation | Perturbative grouping into clusters of at least k, published as centroids (`domingoferrer2002practical`) | Not implemented. `AggregateOnly` returns per-field summaries instead | Syntactic only (groups of at least k), no bound against auxiliary information |
| Generalisation | Global recoding with hierarchies (`sweeney2002kanonymity`; ARX lattice search) | `Generalize level`: numbers rounded to 10^level, strings cut to a prefix plus `*` | A syntactic property only when chosen to reach k. Downcoding attacks (`cohen2022attacks`) |
| Binning | Interval recoding of numeric attributes, a form of generalisation | `Generalize` on numbers | As for generalisation |
| Masking | Partial suppression of characters | `Generalize` on strings | None |
| Noise injection | SDC noise addition. DP when calibrated to sensitivity (`dwork2006calibrating`) | `AddNoise scale`: Gaussian noise with a fixed scale, seeded per request | DP only with a unit of privacy, a contribution bound, calibrated noise and an accountant |
| (Tabular frequency rule) | Minimum-frequency (threshold) rule for tables (`hundepool2012statistical`) | `AggregateOnly minimum_cohort` and the request-level cohort gate | None against differencing. A DP variant thresholds on a noisy count (`wilson2020differentially` §3.5) |
| (Contribution bounding) | Clamping and per-user bounds (`wilson2020differentially`); clipping in DP-SGD (`ponomareva2023dpfy`) | `ClipContribution bound` (L2 clip of a model update) | Necessary for DP, not sufficient |

Dagents sources: `bindings/ocaml/lib/governance_compiler/dagents_governance_compiler.ml`
(`select_strategy`; the constants `generalize_level = 1`, `noise_scale = 0.5`,
`clip_bound = 1.0`; `request_violations`; `plan_obligations`) and
`dagents/common/application/ethical_guard.py` (`_transform`, `_noise_vector`).

## Table 2. Who chooses the mechanism and its parameters

| System | Mechanism chosen by | ε, bounds and unit chosen by | What is proved | When the budget runs out |
|---|---|---|---|---|
| sdcMicro / SDC practice | Methodologist | Methodologist, guided by risk and utility measures | Nothing formal. Risk is estimated | n/a |
| ARX | User picks a privacy model. ARX searches the generalisation lattice for a transformation | User (k, l, t, hierarchies) | The output meets the syntactic model | n/a |
| PINQ | Analyst composes operators | Analyst per query. The data owner's agent holds the total | ε-DP by stability and composition | Agent refuses |
| Airavat | Platform's trusted reducers, analyst's untrusted mapper | Data provider (ε, budget). Computation provider declares output ranges | DP of the reducer outputs. MAC (mandatory access control) blocks other channels | Refuses further computation |
| Fuzz | Programmer | Programmer | The type checker proves sensitivity, so the program is DP | n/a |
| FLEX / Chorus | Engine (elastic sensitivity) / framework rewrites mechanisms into SQL | Operator or analyst | DP for supported SQL with equijoins | Not stated |
| Google DP SQL | Engine rewrites `SELECT WITH ANONYMIZATION` into a bounded-contribution operator | Query author (ε, δ, bounds, contribution bound). A tool derives parameters from accuracy needs | (ε, δ)-DP. A stochastic tester checks the implementation | Not stated |
| OpenDP library | User builds a chain | User sets the target. `binary_search_chain` finds the scale | Each component has a privacy or stability map with a proof document | n/a (library) |
| Tumult Analytics | Engine compiles each query to a Core measurement | Analyst at Session creation (budget, privacy definition, unit) | End-to-end proof by inductive composition | Fixed budget per Session |
| PipelineDP | Engine | Pipeline author (`total_epsilon`, `total_delta`); the accountant splits it | DP per aggregation | Not stated |
| SmartNoise SQL | Engine | User (`Privacy(epsilon, delta)`), `max_contrib` default 1 | DP per query | The odometer "accrues indefinitely"; no block |
| 2020 Census TopDown | Bureau scientists | Data Stewardship Executive Policy committee (ε = 17.14 persons, 2.47 housing units). Invariants set by policy | zCDP for statistics that are not invariants | n/a (one release) |
| Sage | Platform (privacy-adaptive training) | Operator sets a global (ε, δ) per data block. Developer sets a quality target | Global DP over the stream (block composition) | Not stated here |
| PrivateKube / DPack | Scheduler allocates budget | Operator's global budget per block. Tasks state their demand | Global DP over blocks | Tasks wait or are not granted |
| Cohere | Resource planner (optimisation, data-independent) | Data stewards (privacy spaces, budget). Requests state their costs | RDP filter per block | Requests not granted |
| LinkedIn audience API | DP library picks the algorithm by query type | Per-analyst budget, equal for all, refreshed | User-level DP per analyst, assuming analysts do not collude | Refuses, also when the remainder is below the expected cost |
| RAPPOR / Apple / Microsoft | Vendor, at design time | Vendor | Local DP per report | n/a |
| GRAILS / Dagents | Rule table over sensitivity × trust × granularity | Constants in the planner. Classification from an extension | That every combination has a decision (exhaustive match). Not privacy loss | Planner unavailable: the Guard denies (fails closed). No budget |

## Notes for the comparison with Dagents (from reading the code, 2026-11-16)

1. **Parameters are constants, not derived.** `generalize_level = 1`, `noise_scale = 0.5`
   and `clip_bound = 1.0` are fixed in the planner. In every DP system above, the noise
   scale follows from a privacy target and a sensitivity, and a person sets the target.
2. **Noise is reproducible by design.** `_noise_vector` seeds Python's `random.Random`
   with `"{request_id}:{field}:{index}"`. This makes an audit replayable. Some request ids
   are fixed strings (for example `summary:{site_id}:label_rate` in
   `dagents/lma/application/site_service.py`). Anyone who knows the id can then recompute
   the noise and subtract it. Repeating an identical request gives identical noise, which
   blocks averaging of that one request but not differencing across requests. The DP
   implementation literature treats the noise source as security-critical
   (`mironov2012significance`; `wilson2020differentially` §5.2).
3. **The minimum cohort is an exact-count threshold.** This is the SDC frequency rule.
   Google's DP SQL thresholds on an ε-DP count of distinct users instead (§3.5), so the
   threshold decision is itself private.
4. **Clipping has no accountant behind it.** `ClipContribution` (L2 clip) is the
   contribution-bounding step that DP needs. The plan emits the obligation "record
   privacy accounting for the bounded contribution before it leaves the boundary", and
   the Guard records norms before and after clipping (`_Accounting`). But nothing
   converts these into ε or tracks a budget across rounds. The Guard's docstring says so:
   "that needs a privacy accountant and a tracked budget, which are separate controls."
5. **No unit of privacy is declared.** NIST SP 800-226, Tumult and Cohere all require one
   to be stated (for example a person, or a person-month).
6. **Failure behaviour has parallels.** LinkedIn's budget service refuses a query when
   the remaining budget is below its expected cost. Tumult's Session holds a fixed budget.
   SmartNoise's odometer only records spend. Dagents's fail-closed planner covers a
   different failure: the decision function is unavailable, not the budget spent.

---

## Entries

### A. Syntactic models and statistical disclosure control

#### `sweeney2002kanonymity`: k-anonymity
- **Citation:** L. Sweeney. k-Anonymity: A Model for Protecting Privacy. *IJUFKS* 10(5):557–570, 2002.
- **Verification:** DOI 10.1142/S0218488502001648 (Crossref).
- **What it is:** Defines k-anonymity. A release is k-anonymous when every record is indistinguishable from at least k−1 others on its quasi-identifiers (attributes that can be linked to outside data). It is reached by generalising and suppressing values. This is the base model behind GRAILS's generalisation and suppression strategies.
- **D1:** a transformation (how far to generalise or suppress). **D2:** a property checked on the output. The transformation is chosen by a person or an algorithm. **D3:** at the publisher, before release. **D4:** result egress (data publishing). **D5:** none specified. **D6:** n/a. **D7:** domain-neutral. Needs a choice of quasi-identifiers.
- **Overlap with Dagents:** `Generalize` and `Redact` are k-anonymity's two operations.
- **What it does that Dagents does not:** it states a target property (k) that the transformation must reach. Dagents applies a fixed rounding level whatever the resulting group sizes.

#### `machanavajjhala2007ldiversity`: l-diversity
- **Citation:** A. Machanavajjhala, D. Kifer, J. Gehrke, M. Venkitasubramaniam. ℓ-Diversity: Privacy Beyond k-Anonymity. *ACM TKDD* 1(1), article 3, 2007.
- **Verification:** DOI 10.1145/1217299.1217302 (Crossref).
- **What it is:** Shows that k-anonymous tables leak through the homogeneity attack (every record in a group has the same sensitive value) and through background knowledge. It requires each group to contain at least ℓ "well-represented" sensitive values.
- **D1:** a transformation. **D2:** an output property. **D3:** at the publisher. **D4:** egress. **D5–D6:** n/a. **D7:** needs a choice of sensitive attribute.
- **Overlap:** like Dagents's per-field sensitivity, it separates sensitive attributes from quasi-identifiers.
- **What it does that Dagents does not:** it reasons about the distribution of sensitive values inside a released group.

#### `li2007tcloseness`: t-closeness
- **Citation:** N. Li, T. Li, S. Venkatasubramanian. t-Closeness: Privacy Beyond k-Anonymity and l-Diversity. *ICDE 2007*, pp. 106–115.
- **Verification:** DOI 10.1109/ICDE.2007.367856 (Crossref).
- **What it is:** Requires the distribution of a sensitive attribute in each group to be within distance t of its distribution in the whole table (Earth Mover's Distance). This answers the skewness and similarity attacks on ℓ-diversity.
- **D1–D7:** as for ℓ-diversity.
- **Overlap:** none beyond the syntactic family.
- **What it does that Dagents does not:** bounds what a released group reveals about the sensitive attribute, relative to the whole population.

#### `domingoferrer2002practical`: microaggregation
- **Citation:** J. Domingo-Ferrer, J. M. Mateo-Sanz. Practical Data-Oriented Microaggregation for Statistical Disclosure Control. *IEEE TKDE* 14(1):189–201, 2002.
- **Verification:** DOI 10.1109/69.979982 (Crossref).
- **What it is:** Microaggregation partitions records into groups of at least k similar records and publishes each group's centroid in place of the original values. The paper gives practical heuristics for multivariate data, with a minimum group size and an information-loss measure.
- **D1:** a transformation (group size k, which attributes). **D2:** an algorithm with parameters set by a person. **D3:** at the publisher. **D4:** egress. **D5–D6:** n/a. **D7:** numeric microdata.
- **Overlap:** GRAILS lists microaggregation as a strategy. Dagents has no microaggregation; `AggregateOnly` summarises whole columns instead.
- **What it does that Dagents does not:** keeps record-level microdata usable while hiding each record in a group, and measures the information lost.

#### `hundepool2012statistical`: the SDC handbook
- **Citation:** A. Hundepool, J. Domingo-Ferrer, L. Franconi, S. Giessing, E. Schulte Nordholt, K. Spicer, P.-P. de Wolf. *Statistical Disclosure Control*. Wiley, 2012.
- **Verification:** DOI 10.1002/9781118348239 (Crossref).
- **What it is:** The reference text of official-statistics practice. For microdata it covers non-perturbative methods (global recoding, top and bottom coding, local suppression) and perturbative ones (noise addition, microaggregation, swapping, PRAM). For tables it covers sensitivity rules (minimum-frequency, dominance and p% rules), cell suppression, controlled rounding, and disclosure by differencing. It also covers risk and information-loss measures.
- **D1:** a transformation, and which cells to suppress. **D2:** rule tables (sensitivity rules) and methodologist judgement, supported by tools (sdcMicro, τ-ARGUS). **D3:** at the agency, before publication. **D4:** egress. **D5:** documentation of methods. **D6:** n/a. **D7:** official statistics, domain-neutral.
- **Overlap:** a rule table that maps data properties to a protection, like GRAILS. Dagents's minimum cohort is the minimum-frequency rule.
- **What it does that Dagents does not:** measures disclosure risk and information loss for each choice. Treats differencing between released tables as a risk.

#### `prasser2020flexible`: ARX
- **Citation:** F. Prasser, J. Eicher, H. Spengler, R. Bild, K. A. Kuhn. Flexible Data Anonymization Using ARX—Current Status and Challenges Ahead. *Software: Practice and Experience* 50(7):1277–1304, 2020.
- **Verification:** DOI 10.1002/spe.2812 (Crossref). Earlier description: Prasser and Kohlmayer, in *Medical Data Privacy Handbook*, Springer 2015, pp. 111–148, DOI 10.1007/978-3-319-23633-9_6 (Crossref; not in the bib).
- **What it is:** An open-source anonymisation tool (graphical interface and Java API), widely used for biomedical data. The user chooses privacy models (k-anonymity, ℓ-diversity, t-closeness and others, including a differential-privacy mode), generalisation hierarchies and a utility measure. ARX then searches the lattice of generalisation levels for the transformation that meets the models with the best utility, and reports re-identification risk.
- **D1:** a transformation (which generalisation level per attribute, which records to suppress). **D2:** the user configures the models. A search algorithm chooses the transformation, so the choice is optimised, not hand-picked. **D3:** at the data holder, before release. **D4:** egress. **D5:** risk and utility reports. **D6:** n/a. **D7:** tabular microdata. Domain-neutral; built with biomedical use in mind.
- **Overlap:** like GRAILS's planner, it separates the protection policy (models and hierarchies) from the data.
- **What it does that Dagents does not:** searches for the least damaging transformation that meets a stated target, and reports risk and utility. Dagents picks one fixed strategy per cell of its table.

#### `templ2015statistical`: sdcMicro
- **Citation:** M. Templ, A. Kowarik, B. Meindl. Statistical Disclosure Control for Micro-Data Using the R Package sdcMicro. *Journal of Statistical Software* 67(4), 2015.
- **Verification:** DOI 10.18637/jss.v067.i04 (Crossref).
- **What it is:** An R package used by statistical agencies. It estimates disclosure risk (frequency counts, individual risk, SUDA) and applies the SDC methods: global recoding, top and bottom coding, local suppression, PRAM, microaggregation, noise addition and swapping or shuffling. It reports information loss after each step.
- **D1:** a transformation. **D2:** methodologist-driven and iterative. **D3:** at the agency. **D4:** egress. **D5:** risk and utility reports, and a record of the steps applied. **D6:** n/a. **D7:** R and microdata. Domain-neutral.
- **Overlap:** every GRAILS strategy has an sdcMicro function.
- **What it does that Dagents does not:** measures risk before and after each method, so the person sees what each transformation achieved.

### B. Limits of syntactic approaches, and reconstruction

#### `narayanan2008robust`: de-anonymising sparse data (Netflix Prize)
- **Citation:** A. Narayanan, V. Shmatikov. Robust De-anonymization of Large Sparse Datasets. *IEEE S&P 2008*, pp. 111–125.
- **Verification:** DOI 10.1109/SP.2008.33 (Crossref). Earlier version arXiv:cs/0610105.
- **What it is:** Shows that sparse, high-dimensional records (movie ratings) are re-identifiable from a little background knowledge, and that the method tolerates perturbed data. The authors matched Netflix Prize records to public IMDb ratings.
- **D1–D7:** an attack paper; dimensions n/a.
- **Relevance to Dagents:** removing identifiers, or treating only some attributes as quasi-identifiers, does not protect sparse data. A field-by-field sensitivity table assumes that some fields are safe to release in full.
- **Quote (arXiv abstract):** the dataset "contains anonymous movie ratings of 500,000 subscribers of Netflix"; "an adversary who knows only a little bit about an individual subscriber can easily identify this subscriber's record in the dataset."

#### `ganta2008composition`: composition attacks
- **Citation:** S. R. Ganta, S. P. Kasiviswanathan, A. Smith. Composition Attacks and Auxiliary Information in Data Privacy. *KDD 2008*, pp. 265–273.
- **Verification:** DOI 10.1145/1401890.1401926 (Crossref). arXiv:0803.0032.
- **What it is:** Shows that independent k-anonymous (and similar) releases about overlapping populations can be combined to breach privacy, and proves that DP and some relaxations resist such composition.
- **Relevance:** a per-request rule that sees one request at a time cannot defend against composition across requests or across sites. This applies directly to several LMAs releasing about overlapping cohorts.
- **Quote (abstract):** composition attacks work against "a large class of currently proposed techniques. The class includes k-anonymity and several recent variants"; randomisation-based notions "such as differential privacy" "provably resist composition attacks".

#### `cohen2020towards`: singling out under GDPR
- **Citation:** A. Cohen, K. Nissim. Towards Formalizing the GDPR's Notion of Singling Out. *PNAS* 117(15):8344–8352, 2020.
- **Verification:** DOI 10.1073/pnas.1914598117 (Crossref). arXiv:1904.06009.
- **What it is:** Defines predicate singling out (PSO): finding a predicate that matches exactly one row with probability well above a baseline. It argues that a mechanism that "renders anonymous" under GDPR must be PSO secure. It proves that DP implies PSO security, that k-anonymity does not, and that PSO security does not self-compose.
- **Relevance:** gives a legal-technical test that Dagents's generalisation would not pass by construction. Its composition result also applies to releasing many exact counts.
- **Quote (abstract):** "differential privacy implies PSO security. However, k-anonymity does not"; "a combination of ω(log n) exact counts, each individually PSO secure, enables an attacker to predicate single out."

#### `cohen2022attacks`: downcoding attacks
- **Citation:** A. Cohen. Attacks on Deidentification's Defenses. *USENIX Security 2022*.
- **Verification:** arXiv:2202.13470 (venue from its comment "To appear in USENIX Security 2022"; the USENIX page was not checked).
- **What it is:** Introduces downcoding attacks and proves that every quasi-identifier de-identification scheme that is minimal and hierarchical is vulnerable to them. Converts them into PSO attacks, and re-identifies students in a k-anonymised EdX release using LinkedIn.
- **Relevance:** the strongest result against "generalisation gives a guarantee". Generalisation through a hierarchy can itself leak, even when every attribute is treated as a quasi-identifier.
- **Quote (abstract):** "QI-deidentification may offer no protection even if every attribute is treated as a quasi-identifier"; "we use LinkedIn.com to reidentify 3 students in a k-anonymized dataset published by EdX".

#### `dinur2003revealing`: the reconstruction result
- **Citation:** I. Dinur, K. Nissim. Revealing Information While Preserving Privacy. *PODS 2003*, pp. 202–210.
- **Verification:** DOI 10.1145/773153.773173 (Crossref).
- **What it is:** Shows that answering many subset-sum queries with too little noise lets an adversary reconstruct almost the whole database. With perturbation of order o(√n) per answer, a polynomial-time adversary succeeds. This is the origin of the "fundamental law of information recovery" that motivates privacy budgets.
- **Relevance:** every accurate statistic spends privacy, whether or not a budget is tracked. A system without an accountant is still spending, only without a record of it.

#### `garfinkel2019understanding` and `abowd2025simulated`: reconstruction of the 2010 US Census
- **Citations:** S. Garfinkel, J. M. Abowd, C. Martindale. Understanding Database Reconstruction Attacks on Public Data. *CACM* 62(3):46–53, 2019. J. M. Abowd et al. (15 authors). A Simulated Reconstruction and Reidentification Attack on the 2010 U.S. Census. *Harvard Data Science Review* 7(3), 2025.
- **Verification:** DOIs 10.1145/3287287 and 10.1162/99608f92.4a1ebf70 (Crossref). arXiv:2312.11283.
- **What they are:** The CACM article explains how a solver rebuilds individual records from published tables. The 2025 paper reports the Bureau's own attack on the 2010 tabulations, which were protected by traditional SDC (swapping), and compares it with the 2020 DP system.
- **Relevance:** shows at national scale that table-level SDC choices do not bound reconstruction.
- **Numbers (2025 abstract):** "Ninety-seven million person records (every resident in 70% of all census blocks) are exactly reconstructed with provable certainty using only public information"; re-identification "with 95% accuracy" of population-unique individuals outside the modal race and ethnicity category of their block "(3.4 million persons)".

### C. Differential privacy foundations and implementation

#### `dwork2006calibrating`: calibrating noise to sensitivity
- **Citation:** C. Dwork, F. McSherry, K. Nissim, A. Smith. Calibrating Noise to Sensitivity in Private Data Analysis. *TCC 2006*, LNCS 3876, pp. 265–284.
- **Verification:** DOI 10.1007/11681878_14 (Crossref; volume number from the TCC 2006 proceedings).
- **What it is:** Defines ε-differential privacy and the Laplace mechanism. Noise with scale equal to the function's global sensitivity divided by ε gives ε-DP.
- **Relevance:** the dividing line in this survey. Noise is a privacy mechanism only when its scale follows from a sensitivity bound and a target ε. Dagents's `AddNoise 0.5` follows from neither.

#### `dwork2014algorithmic`: the DP textbook
- **Citation:** C. Dwork, A. Roth. The Algorithmic Foundations of Differential Privacy. *Foundations and Trends in Theoretical Computer Science* 9(3–4):211–407, 2014.
- **Verification:** DOI 10.1561/0400000042 (Crossref, which lists pages 211–487; the usual citation is 211–407).
- **What it is:** The standard reference. It covers basic and advanced composition, the Laplace, Gaussian and exponential mechanisms, the sparse vector technique, reconstruction lower bounds, and (ε, δ)-DP.
- **Relevance:** the composition theorems are what an accountant implements. The Gaussian mechanism is the calibrated form of what `AddNoise` does.

#### `mironov2012significance`: floating-point attacks on DP noise
- **Citation:** I. Mironov. On Significance of the Least Significant Bits for Differential Privacy. *CCS 2012*, pp. 650–661.
- **Verification:** DOI 10.1145/2382196.2382264 (Crossref).
- **What it is:** Shows that textbook floating-point implementations of the Laplace mechanism leak the exact value through the low-order bits of the noisy output, and gives a fix (snapping).
- **Relevance:** the way noise is generated matters for the guarantee. The same concern applies to seeded or predictable noise (see note 2 above).

### D. DP query and analytics systems that plan or check queries

#### `mcsherry2009privacy`: PINQ
- **Citation:** F. McSherry. Privacy Integrated Queries: An Extensible Platform for Privacy-Preserving Data Analysis. *SIGMOD 2009*, pp. 19–30.
- **Verification:** DOI 10.1145/1559845.1559850 (Crossref).
- **What it is:** A LINQ-based query layer. Transformations (Where, Select, GroupBy, Join, Partition) carry a stability factor. Aggregations (noisy count, sum, average, median) add calibrated noise. An agent attached to the protected data deducts each query's ε from a total and refuses queries beyond it. Partition gives parallel composition.
- **D1:** permit or deny each query against a budget, and the noise added. **D2:** code (typed operators). The guarantee follows from operator stability, by construction. **D3:** at the data, through the only interface to it. **D4:** computation and egress. **D5:** budget ledger. **D6:** the agent refuses once the budget is spent. **D7:** .NET/LINQ.
- **Overlap:** a single mediating interface in front of the data, which refuses requests. This is close to Dagents's Guard at `before_read`.
- **What it does that Dagents does not:** proves a privacy bound for any composition of its operators, and tracks cumulative loss.

#### `roy2010airavat`: Airavat
- **Citation:** I. Roy, S. Setty, A. Kilzer, V. Shmatikov, E. Witchel. Airavat: Security and Privacy for MapReduce. *NSDI 2010*.
- **Verification:** official PDF https://www.usenix.org/event/nsdi10/tech/full_papers/roy.pdf (found by web search). No DOI.
- **What it is:** Combines mandatory access control (SELinux-based, extended to the distributed file system) with DP for MapReduce jobs written by untrusted parties. Data providers set the policy, including the privacy budget and ε. The computation provider declares the range of its mapper outputs, and Airavat enforces that range. Trusted reducers add the noise.
- **D1:** whether a computation may run, and how much noise its output gets. **D2:** a data-owner policy plus platform-enforced mechanisms. **D3:** at the data store and at output. The untrusted code runs near the data, but cannot leak through storage or the network. **D4:** computation and egress. **D5:** not stated. **D6:** the budget is enforced, so computations beyond it are refused. **D7:** Hadoop/MapReduce, Java.
- **Overlap:** the closest prior art to Dagents's site guards. Untrusted computation from elsewhere runs at the data and sees only what policy allows, and its output passes an egress control. The healthcare demo's rule that "the code that runs a round comes from the coordinator, so it sees only what the coordinator may see" matches Airavat's threat model.
- **What it does that Dagents does not:** a DP bound on outputs, and access control at the OS level against channels other than the API.
- **Number (from the search summary of the abstract; PDF not read):** run times on Amazon's cloud were "within 32% of a MapReduce system with no security".

#### `reed2010distance`: Fuzz, a type system for DP
- **Citation:** J. Reed, B. C. Pierce. Distance Makes the Types Grow Stronger: A Calculus for Differential Privacy. *ICFP 2010*, pp. 157–168.
- **Verification:** DOI 10.1145/1863543.1863568 (Crossref).
- **What it is:** A functional language with a linear type system in which a function's type states its sensitivity. A well-typed program built on a primitive noise mechanism is differentially private, and type checking is the proof.
- **D1:** n/a (a language). **D2:** a typed, statically checked program. Privacy is verified by the type system. **D3–D7:** n/a.
- **Overlap:** like Dagents, it uses a typed functional language so that the compiler checks a property.
- **What it does that Dagents does not:** the property the compiler checks is a privacy bound. Dagents's compiler checks that the policy is exhaustive. This is the clearest example of the line between "a rule chose a transformation" and "a privacy bound was proved".

#### `zhang2018ektelo`: Ektelo
- **Citation:** D. Zhang, R. McKenna, I. Kotsogiannis, M. Hay, A. Machanavajjhala, G. Miklau. EKTELO: A Framework for Defining Differentially-Private Computations. *SIGMOD 2018*, pp. 115–130.
- **Verification:** DOI 10.1145/3183713.3196921 (Crossref). arXiv:1808.03555.
- **What it is:** A framework in which DP algorithms for linear counting queries are written as plans: compositions of operators from a few operator classes. The framework ensures that a plan is private, and the operators make it accurate and efficient.
- **D2:** typed operator plans. Privacy is guaranteed by the framework.
- **Overlap:** a "plan" built from typed operators, similar in spirit to a restriction plan.
- **What it does that Dagents does not:** the plan's operators are privacy mechanisms with a proved cost, and the framework optimises the plan for accuracy.

#### `johnson2018towards` and `johnson2020chorus`: FLEX and Chorus (Uber)
- **Citations:** N. Johnson, J. P. Near, D. Song. Towards Practical Differential Privacy for SQL Queries. *PVLDB* 11(5):526–539, 2018. N. Johnson, J. P. Near, J. M. Hellerstein, D. Song. Chorus: A Programming Framework for Building Scalable Differential Privacy Mechanisms. *IEEE EuroS&P 2020*, pp. 535–551.
- **Verification:** DOIs 10.1145/3187009.3177733 (Crossref; arXiv:1706.09479 lists a different DOI that Crossref does not resolve) and 10.1109/EuroSP48549.2020.00041 (Crossref). arXiv:1809.07750.
- **What they are:** FLEX analyses an SQL query and computes its elastic sensitivity, an upper bound on local sensitivity for queries with equijoins, then adds noise accordingly. It works in front of any database. Chorus rewrites DP mechanisms (Weighted PINQ, MWEM, the matrix mechanism) into SQL that runs inside a production DBMS, and was deployed at Uber.
- **D1:** how much noise a query's result gets, and whether the query is supported. **D2:** static query analysis (a planner-like step over SQL). **D3:** between the analyst and the database, at output. **D4:** egress. **D5:** not stated. **D6:** unsupported queries are rejected. **D7:** SQL, database-agnostic.
- **Overlap:** a pure analysis of the request, separate from the data store, decides the protection. This is like a query planner, the analogy GRAILS uses.
- **What it does that Dagents does not:** derives the protection from the structure of the query (joins, aggregates), not from a sensitivity label alone.
- **Numbers (FLEX abstract):** an empirical study of "8.1 million real-world queries"; "negligible (0.03%) performance overhead".

#### `wilson2020differentially`: Google's DP SQL with bounded user contribution
- **Citation:** R. J. Wilson, C. Y. Zhang, W. Lam, D. Desfontaines, D. Simmons-Marengo, B. Gipson. Differentially Private SQL with Bounded User Contribution. *PoPETs* 2020(2):230–250.
- **Verification:** DOI 10.2478/popets-2020-0025 (Crossref). arXiv:1909.01917 (full text read).
- **What it is:** A DP aggregation operator in relational algebra, implemented in an SQL engine (`SELECT WITH ANONYMIZATION`, `ANON_SUM(col, L, U)` and similar). It bounds each user's contribution even when a user has many rows, thresholds group keys on a noisy count of users, and comes with a stochastic tester for the implementation. Components are open source.
- **D1:** noise and contribution bounds per query, and which groups are released. **D2:** query rewriting in the engine. Parameters are set by the query author or by a tool that derives them from accuracy targets. **D3:** inside the query engine, at output. **D4:** computation and egress. **D5:** a test framework. **D6:** not stated. **D7:** SQL and Google's engine, with open-source parts.
- **Overlap:** contribution bounding is what `ClipContribution` does for a model update. The minimum-user threshold is the DP version of Dagents's minimum cohort.
- **What it does that Dagents does not:** user-level bounding across many rows, a private threshold decision, and testing that the implementation keeps its guarantee.
- **Quotes:** §3.5: "for each grouping or aggregation result row we first calculate an ε-DP count of unique contributing users." §5.2, on floating-point side channels: "ANON_SUM(IF uid=4217 THEN 0/0 ELSE 0). The NaN value will survive naive contribution bounding". §6: the system "provides a mechanism to deduce privacy parameters from accuracy requirements".

#### `gaboardi2020programming`, `opendp2026library` and `smartnoise2026sql`: OpenDP and SmartNoise
- **Citations:** M. Gaboardi, M. Hay, S. Vadhan. A Programming Framework for OpenDP. Working paper, 2020. OpenDP Library (Python package `opendp` 0.16.0). SmartNoise SQL (`smartnoise-sql` 1.0.10).
- **Verification:** working paper at https://salil.seas.harvard.edu/resource/tpdp-2020pdf (found by web search, not read directly). Docs at https://docs.opendp.org/. PyPI metadata and the opendp 0.16.0 wheel were inspected. SmartNoise at https://pypi.org/project/smartnoise-sql/ and https://github.com/opendp/smartnoise-sdk.
- **What they are:** OpenDP builds DP computations from transformations, which carry a stability map, and measurements, which carry a privacy map, chained by function composition. Each component is linked to a proof document: the Python package has a `proven` decorator, "Decorator for functions that have an associated proof document". Components that have not finished the project's vetting are gated behind the `contrib` feature flag, and idealised arithmetic behind `idealized-numerics`. `binary_search_chain` finds "the highest-utility (`d_in`, `d_out`)-close Transformation or Measurement": the user states a privacy target, and the library finds the noise scale. SmartNoise SQL runs DP SQL on top of OpenDP.
- **D1:** the noise scale for a target. Whether an unvetted component may be used. **D2:** a typed library with per-component proofs. Feature flags gate unvetted parts. **D3:** wherever the library runs. **D4:** computation and egress. **D5:** proof documents. SmartNoise has an odometer. **D6:** SmartNoise does not block: "Privacy cost accrues indefinitely as new queries are executed" (PyPI page). Using a gated feature without enabling it raises an error. **D7:** Rust core with Python bindings. Open source.
- **Overlap:** a pure library that is called from a runtime, with explicit opt-in for what is not proven. This is similar to Dagents's split between planner and Guard.
- **What it does that Dagents does not:** a proof per component, and solving for parameters from a privacy target. SmartNoise defaults `max_contrib` to 1 and warns against changing it, so contribution bounding is on by default.

#### `berghel2022tumult`: Tumult Analytics
- **Citation:** S. Berghel et al. (13 authors). Tumult Analytics: A Robust, Easy-to-Use, Scalable, and Expressive Framework for Differential Privacy. arXiv:2212.04133, 2022.
- **Verification:** arXiv:2212.04133 (full text read). Software: PyPI `tmlt.analytics` 0.21.0, repository https://github.com/opendp/tumult-analytics (the project now sits under OpenDP).
- **What it is:** A Python and Spark DP framework. Its abstract says it is "used at institutions such as the U.S. Census Bureau, the Wikimedia Foundation, or the Internal Revenue Service". Tumult Core composes transformations (with stability) and measurements (with privacy) and infers the guarantee of the whole program. Analysts work through a Session that fixes the budget, the privacy definition and the unit of privacy.
- **D1:** the mechanism for each query, and the budget it spends. **D2:** the analyst states queries and budget, and the engine compiles them into proved measurements. **D3:** in the Session, at the data. **D4:** computation and egress. **D5:** remaining budget can be queried. **D6:** a fixed budget per Session. **D7:** Python and Spark.
- **Overlap:** the Session as the only path to the data matches Dagents's rule that every read goes through a site's store and Guard.
- **What it does that Dagents does not:** an end-to-end proof for any program, private joins, and a unit of privacy the user must declare.
- **Quotes (§2–3):** "This allows Tumult Core to build an end-to-end proof of the privacy guarantee of an entire program, no matter how complex." "An analyst defines the privacy budget when they create a Session".

#### `pipelinedp2026`: PipelineDP
- **Citation:** PipelineDP (OpenMined and Google). Python package `pipeline-dp` 0.3.1.
- **Verification:** https://pipelinedp.io/overview/ (found by web search). PyPI metadata and wheel inspected.
- **What it is:** DP aggregations for batch pipelines. A framework-neutral `DPEngine` holds the DP logic, and backends exist for Apache Beam, Apache Spark and local runs. A budget accountant (`NaiveBudgetAccountant` or `PLDBudgetAccountant`) takes `total_epsilon` and `total_delta` for "the entire pipeline" and splits them across aggregations by weight. The overview page calls the project experimental.
- **D1:** the noise per aggregation, and the split of the budget. **D2:** code. The pipeline author sets the total. **D3:** in the pipeline. **D4:** computation and egress. **D5:** not stated. **D6:** the constructor raises `ValueError` on out-of-range arguments. **D7:** Beam and Spark.
- **Overlap:** a pure DP engine with pluggable execution backends, much as Dagents separates planner and runtime.
- **What it does that Dagents does not:** budget accounting across all aggregations of one job, including PLD (privacy loss distribution) accounting.

#### `abowd2022topdown` and `census2021parameters`: the 2020 US Census
- **Citations:** J. M. Abowd et al. (14 authors). The 2020 Census Disclosure Avoidance System TopDown Algorithm. *Harvard Data Science Review*, Special Issue 2, 2022. U.S. Census Bureau. Census Bureau Sets Key Parameters to Protect Privacy in 2020 Census Results. Press release, 9 June 2021.
- **Verification:** DOI 10.1162/99608f92.529e3cb9 (Crossref). arXiv:2204.08986. Press release URL https://www.census.gov/newsroom/press-releases/2021/2020-census-key-parameters.html (found by web search).
- **What it is:** The largest DP release. TopDown takes noisy measurements under zero-concentrated DP, post-processes them hierarchically from the nation down to blocks, and keeps policy-chosen invariants exact. The privacy-loss budget was set by the Bureau's Data Stewardship Executive Policy (DSEP) committee.
- **D1:** which statistics get noise and how much. Which statistics are exact. **D2:** a policy committee sets ε and the invariants. The algorithm derives the rest. **D3:** central, at the agency. **D4:** egress (publication). **D5:** extensive public documentation and demonstration data. **D6:** n/a. **D7:** Census-specific.
- **Overlap:** a policy body, not code, chooses the protection level. This matches Dagents's choice to put policy in extensions.
- **What it does that Dagents does not:** a published, quantified privacy-loss budget, and explicit accounting of what is exempt.
- **Quote (abstract):** invariants are "statistics that the Census Bureau has determined, as matter of policy, to exclude from the privacy-loss accounting." **Numbers (press release, via the search summary):** ε = 17.14 for the persons file and ε = 2.47 for housing units, 19.61 in total, for the redistricting data.

### E. Privacy budget and release management

#### `lecuyer2019privacy`: Sage
- **Citation:** M. Lécuyer, R. Spahn, K. Vodrahalli, R. Geambasu, D. Hsu. Privacy Accounting and Quality Control in the Sage Differentially Private ML Platform. *SOSP 2019*, pp. 181–195.
- **Verification:** DOI 10.1145/3341301.3359639 (Crossref). arXiv:1909.01502.
- **What it is:** A platform that bounds the cumulative leakage of a data stream through every model trained on it. Block composition splits the stream into time blocks, each with a global (ε, δ) budget, so training can continue as new data arrives. Privacy-adaptive training raises the amount of data or the privacy parameters until a model meets the developer's quality target.
- **D1:** whether a model may be trained and released within the budget, and with what parameters. **D2:** platform accounting plus a developer-set quality target. **D3:** at the training platform, before models leave. **D4:** training and model release. **D5:** a budget ledger per block. **D6:** not stated here. **D7:** ML pipelines (TFX-based prototype).
- **Overlap:** links model release to a check on quality and privacy. This is close to Dagents's release gates and its rule that a missing metric blocks release.
- **What it does that Dagents does not:** cumulative DP accounting across every model trained from the same data.
- **Quote (abstract):** trains "until, with high probability, the model meets developer-configured quality criteria."

#### `luo2021privacy` and `tholoniat2025dpack`: PrivateKube and DPack
- **Citations:** T. Luo, M. Pan, P. Tholoniat, A. Cidon, R. Geambasu, M. Lécuyer. Privacy Budget Scheduling. *OSDI 2021*. P. Tholoniat et al. DPack: Efficiency-Oriented Privacy Budget Scheduling. *EuroSys 2025*, pp. 1194–1209.
- **Verification:** arXiv:2106.15335 (venue from its comment). DOI 10.1145/3689031.3696096 (Crossref).
- **What they are:** PrivateKube extends Kubernetes with privacy budget as a resource alongside CPU and memory. Its scheduler, DPF (Dominant Private Block Fairness), allocates a non-replenishable budget among training tasks. DPack schedules for efficiency, to fit more tasks into the same budget.
- **D1:** which tasks get budget. **D2:** a scheduler algorithm with stated fairness properties. **D3:** at the orchestrator. **D4:** training, and deployment as scheduling. **D5:** allocation state. **D6:** tasks wait or are not granted. **D7:** Kubernetes.
- **Overlap:** Dagents renders Kubernetes manifests and coordinates federated rounds. PrivateKube shows how privacy could be a first-class resource in that layer.
- **What it does that Dagents does not:** global privacy budgets per data block, shared fairly across workloads.
- **Quote (abstract):** "traditional compute resources are replenishable while privacy is not".

#### `kuchler2024cohere`: Cohere
- **Citation:** N. Küchler, E. Opel, H. Lycklama, A. Viand, A. Hithnawi. Cohere: Managing Differential Privacy in Large Scale Systems. *IEEE S&P 2024*, pp. 991–1008.
- **Verification:** DOI 10.1109/SP54263.2024.00122 (Crossref). arXiv:2301.08517 (full text read). Code at https://github.com/pps-lab/cohere (as stated in the paper).
- **What it is:** A privacy management layer for an organisation. Applications request data (a subpopulation chosen by partitioning attributes, plus a sampling rate) and budget (in ε-DP, ρ-zCDP or RDP). A budget control system, comparable to a data catalogue's policy interface, lets data stewards define privacy spaces. A resource planner allocates budget among requests by optimisation and emits ABAC (attribute-based access control) policies for the data layer. User rotation and budget unlocking keep the system running as budget is used up.
- **D1:** which requests get data and budget. **D2:** an optimisation, data-independent, over public inputs. Policy is expressed as ABAC. **D3:** the data layer enforces access. Each authorised DP system enforces its own budget use. **D4:** data read and egress. **D5:** allocation history. **D6:** requests that cannot be met are not granted. **D7:** platform-neutral; the prototype has its own stack.
- **Overlap:** the closest system to GRAILS's Restriction Planner. A planner that never touches data decides what each request may have, and a separate layer enforces it. It also issues access-control policies, as Dagents issues restriction plans.
- **What it does that Dagents does not:** budget allocation across competing requests, and tighter accounting through partitioning and subsampling.
- **Quotes:** §4.3: "Cohere's resource planner is data-independent, meaning it never touches sensitive data." §3: "DP is a characteristic of the algorithm, not the data." §3: enforcement "requires correct DP system implementation, which can be ensured through auditing or privacy budget enforcement mechanisms based on trusted hardware". **Number (abstract):** "a 6.4--28x improvement in utility compared to the state-of-the-art".

#### `rogers2021linkedin`: LinkedIn's Audience Engagements API
- **Citation:** R. Rogers, S. Subramaniam, S. Peng, D. Durfee, S. Lee, S. K. Kancha, S. Sahay, P. Ahammad. LinkedIn's Audience Engagements API: A Privacy Preserving Data Analytics System at Scale. *Journal of Privacy and Confidentiality* 11(3), 2021.
- **Verification:** DOI 10.29012/jpc.782 (Crossref). arXiv:2002.05839 (full text read).
- **What it is:** A production DP analytics system for marketers, built on Apache Pinot. It has user-level guarantees, and a separate budget management service that tracks each analyst's spend and refreshes it on a schedule.
- **D1:** permit or deny a query against the analyst's budget, and the DP algorithm for the query type. **D2:** a service with fixed rules. A per-analyst budget assumes that analysts do not collude. **D3:** at the analytics API, at output. **D4:** egress. **D5:** a budget ledger per analyst. **D6:** refuses when the budget is exhausted, and also when the remainder is below the query's expected cost. **D7:** LinkedIn, Pinot and Espresso.
- **Overlap:** a budget service kept separate from the algorithms. This matches Dagents's split between deciding and enforcing. The service refuses rather than degrading.
- **What it does that Dagents does not:** per-requester cumulative accounting.
- **Quotes (§4):** "If the budget is exhausted for an analyst then the budget management service does not allow the query to be executed"; "If the budget is not depleted, yet what remains is less than the expected cost of the query, then we still do not evaluate the query." "We assume that each analyst will have its own budget and each analyst starts with the same budget."

#### `erlingsson2014rappor`: RAPPOR (Google Chrome)
- **Citation:** Ú. Erlingsson, V. Pihur, A. Korolova. RAPPOR: Randomized Aggregatable Privacy-Preserving Ordinal Response. *CCS 2014*, pp. 1054–1067.
- **Verification:** DOI 10.1145/2660267.2660348 (Crossref).
- **What it is:** Local DP for client telemetry. Each client applies randomised response to the bits of a Bloom filter. A permanent randomised response is memoised for each value, to limit longitudinal leakage, and a fresh instantaneous one is added on each report. It was deployed in Chrome.
- **D1:** how much randomisation each report gets. **D2:** fixed by the designers in client code. **D3:** at the source (the device), before collection. **D4:** data collection. **D5:** n/a. **D6:** n/a. **D7:** client telemetry.
- **Overlap:** protection applied at the source before anything leaves it, the same placement as Dagents's `before_send`.
- **What it does that Dagents does not:** protects even against the collector, with a quantified local guarantee.

#### `apple2017learning`: Apple's local DP deployment
- **Citation:** Differential Privacy Team, Apple. Learning with Privacy at Scale. *Apple Machine Learning Journal*, December 2017.
- **Verification:** https://machinelearning.apple.com/research/learning-with-privacy-at-scale (found by web search; content from the search summary).
- **What it is:** Describes on-device local DP (the Count Mean Sketch and related algorithms) for opt-in telemetry such as popular emojis and Safari settings. It adds a per-use-case limit on how many privatised records a device sends each day, and removes identifiers and IP addresses at the server.
- **D1–D3:** the mechanism and its parameters are fixed by the vendor and applied on the device. **D4:** collection. **D5:** a public description. **D6:** n/a. **D7:** Apple's platforms.
- **Overlap:** layered controls (on-device noise plus server-side separation), not one mechanism.
- **What it does that Dagents does not:** a rate limit per use case as part of the privacy design.

#### `ding2017collecting`: Microsoft telemetry with repeated collection
- **Citation:** B. Ding, J. Kulkarni, S. Yekhanin. Collecting Telemetry Data Privately. *NIPS 2017*.
- **Verification:** arXiv:1712.01524. Proceedings PDF URL (found by web search). No DOI.
- **What it is:** Local DP mechanisms for counters collected again and again (for example daily app usage), with guarantees that do not degrade over time. Deployed by Microsoft.
- **D1–D7:** as for RAPPOR. Fixed by the vendor, enforced on the device.
- **Relevance:** repeated collection from the same people is the federated-round setting. Dagents's per-round `AddNoise` has no answer for repeated rounds over the same patients.
- **Quote (abstract):** "Our mechanisms have been deployed by Microsoft to collect telemetry across millions of devices."

### F. Practice, reporting and guidance (recent work included)

#### `dwork2019differential`: "Expose your epsilons!"
- **Citation:** C. Dwork, N. Kohli, D. Mulligan. Differential Privacy in Practice: Expose Your Epsilons! *Journal of Privacy and Confidentiality* 9(2), 2019.
- **Verification:** DOI 10.29012/jpc.689 (Crossref).
- **What it is:** Interviews practitioners about how they choose ε and other parameters. It finds no shared method, and proposes a public Epsilon Registry for deployments (summary from memory; the full text was not read in this session).
- **Relevance:** a published registry of parameters is the DP analogue of Dagents's audit record naming the planner hash. Both make the choice inspectable.

#### `munillagarrido2023lessons`: DP in industry, interview study
- **Citation:** G. Munilla Garrido, X. Liu, F. Matthes, D. Song. Lessons Learned: Surveying the Practicality of Differential Privacy in the Industry. *PoPETs* 2023(2):151–170.
- **Verification:** DOI 10.56553/popets-2023-0045 (Crossref). arXiv:2211.03898.
- **What it is:** Interviews with 24 privacy practitioners at 9 companies about why DP is rarely adopted, and what tools lack.
- **Relevance:** the gap it reports (slow access approval, then weak enforcement) is the gap that governance layers like GRAILS and Dagents address with policy, and that DP addresses with bounds.
- **Quote (abstract):** "analysts suffer from lengthy bureaucratic processes for requesting access to sensitive data, yet once granted, only scarcely-enforced privacy policies stand between rogue practitioners and misuse of private information."

#### `ponomareva2023dpfy`: practical guide to DP machine learning
- **Citation:** N. Ponomareva et al. (9 authors). How to DP-fy ML: A Practical Guide to Machine Learning with Differential Privacy. *JAIR* 77:1113–1201, 2023.
- **Verification:** DOI 10.1613/jair.1.14649 (Crossref). arXiv:2303.00654.
- **What it is:** A guide to DP training (DP-SGD: per-example clipping plus Gaussian noise, with an accountant). It covers choosing the privacy unit and definition, accounting and its assumptions, and how to report guarantees.
- **Relevance:** the reference for turning Dagents's `ClipContribution` and `AddNoise` on model updates into a reportable guarantee (clip norm, noise multiplier, sampling, accountant).
- **Quote (abstract):** "consistently and fully reporting privacy guarantees is critical, and so we propose a set of specific best practices for stating guarantees."

#### `near2025guidelines`: NIST SP 800-226
- **Citation:** J. P. Near, D. Darais, N. Lefkovitz, G. S. Howarth. Guidelines for Evaluating Differential Privacy Guarantees. NIST SP 800-226, March 2025.
- **Verification:** DOI 10.6028/NIST.SP.800-226 (Crossref, dated 2025-03-06). The NIST announcement was found by web search. The PDF was not read in this session.
- **What it is:** US government guidance for judging DP claims, for readers from policymakers to data scientists. It introduces a "differential privacy pyramid" of considerations, catalogues privacy hazards (for example cumulative loss across releases), and comes with Jupyter notebooks (from the NIST announcement and summaries).
- **Relevance:** gives the checklist against which a "noise was added" claim should be read. A deployment that cannot state its unit of privacy and its ε does not make a DP claim.

---

## Unverified

These are items I know of but could not check in this session (the shared web-search
budget was used up, and other hosts are blocked from the shell). They are not in
`pets.bib`.

- **ISO/IEC 20889:2018**, *Privacy enhancing data de-identification terminology and
  classification of techniques*. As I recall, it classifies statistical tools, suppression
  (masking, local and record suppression), generalisation (rounding, top and bottom
  coding), randomisation (noise addition, permutation, microaggregation), synthetic data,
  and formal models (k-anonymity, DP). That would make it a standards-based mapping for
  GRAILS's strategies. The search to confirm it was refused because the budget was used up.
- **Article 29 Working Party, Opinion 05/2014 on Anonymisation Techniques (WP216).** It
  assesses randomisation and generalisation techniques against singling out, linkability
  and inference. It is relevant to the GDPR side of `cohen2020towards`. Not checked.
- **Domingo-Ferrer and Torra (2005)** on achieving k-anonymity through microaggregation.
  Cited from memory for the link between microaggregation and k-anonymity. Not checked, so
  Table 1 does not cite it.
- **Airavat page numbers** (297–312 in one index found by search) and **Ding et al.
  NeurIPS page numbers** (3571–3580 in a search summary). Not checked against the
  proceedings, so left out of the bib.
- **The NIST SP 800-226 pyramid layers.** I believe the top layer covers the privacy
  parameters and the unit of privacy, but the PDF was not read.
- **Apple's published ε values per use case.** Not checked, so not stated.
- **The full text of `dwork2019differential`** (the Epsilon Registry proposal). Summarised
  from memory; metadata checked.
- **Google's open-source differential-privacy library** (the components released with
  `wilson2020differentially`). The repository URL was not checked.
- **That Tumult Analytics raises an error when a query would exceed the Session's
  remaining budget.** Inferred from the paper's description of a Session with a fixed
  guarantee. A secondary source in a search summary said OpenDP and Tumult "block new
  queries if the budget is spent". Not checked against the code.
- **The usability study comparing DP tools** (arXiv:2309.13506, seen in a search result
  list). Not checked, so not cited.
