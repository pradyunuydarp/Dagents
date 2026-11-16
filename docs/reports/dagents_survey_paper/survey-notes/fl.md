# Family `fl`: federated learning, its systems, attacks, defences and governance

Scope: federated learning (FL) foundations; attacks showing that keeping raw data at the
site is not enough; the defences; the frameworks and platforms; the large healthcare
deployments; and the governance or accountability of federated studies. 45 works.

How each work was checked: academic metadata against Crossref (DOI) or the arXiv API, and
for PMLR, NeurIPS and JMLR papers against the proceedings page found by search. Where this
file quotes a paper, the quote was read in the arXiv PDF text or in the abstract Crossref
returns; a page or section refers to the arXiv version. Product documentation could not be fetched (only
api.crossref.org and arxiv.org were reachable), so statements about product docs come from
search-result extracts of the official pages, cross-checked against the vendor's own paper
where one exists. Those statements are marked "(docs, via search)". The web-search budget
ran out before every product page could be checked; what is left is in **Unverified**.

---

## Synthesis

**Shared assumptions.** FL moves computation to the data. Clients send "focused updates
intended for immediate aggregation" (Kairouz et al. 2021) and a coordinator combines them,
either across millions of phones (cross-device) or across 2 to 100 organisations
(cross-silo). The threat model has two parts: a curious server or peer that learns from
updates, and malicious clients. Leakage work (Zhu 2019, Geiping 2020, Shokri 2017, Nasr
2019) shows that updates and models leak training data. Poisoning work (Bagdasaryan 2020,
Fang 2020) shows that one participant can implant a backdoor and that "Byzantine-robust"
aggregation can be broken.

**Where decisions live.** The defences (secure aggregation, user-level and distributed
differential privacy (DP), norm clipping, robust aggregation) are mechanisms inside the
protocol. The model engineer selects them in code: a Flower strategy wrapper, a TensorFlow
Federated aggregator, a FLARE filter. Governance decisions (who may join, what code runs,
what leaves a site, what is released) sit outside the learning code, in five places:
1. *Provisioning and role configuration*: startup kits or PKI plus role/right matrices
   (FLARE `authorization.json`, OpenFL certificates and plan, Flower node registration).
2. *Site policy files*: FLARE's `privacy.json` forces filters on every job at a site;
   Apheris Asset Policies, signed by the custodian, name allowed models, beneficiaries and
   privacy controls.
3. *People*: PySyft request approval, Fed-BioMed training-plan approval by code hash,
   Flower Hub reviewer signatures, MedPerf's "human-supervised" evaluation, Google's
   peer-reviewed-code deployment gate.
4. *Ledgers*: Substra's per-asset permission regimes, Swarm Learning's blockchain.
5. *A policy language*: FLA3 evaluates XACML policies at node activation, study join and
   every round, and fails closed.

**Where enforcement happens.** Mostly at the site boundary (FLARE filters and
authorisation, Fed-BioMed hash checks, the Apheris gateway, the Fenoglio-Treleaven
admission gateway), and at the coordinator before aggregation (FLA3) or deployment
(Bonawitz et al. 2019). **Model release is the least covered stage.** Fed-BioMed's 2023
comparison finds "None built-in" model governance in OpenFL, FLARE and Flower. Release
rules are organisational: every future Gboard neural language model launch requires DP,
and the FeTS study released its consensus model. Substra is the exception: a model inherits the intersection
of its inputs' permission whitelists.

**Evidence and failure.** FLARE writes audit files at server and clients; FLA3 writes JSON
records signed as JWS; Substra and the AF^2 FactSheets prototype use Hyperledger Fabric.
Fail-closed behaviour is rarely documented: FLA3 and Fenoglio-Treleaven state it, FLARE
applies no privacy control at a site without `privacy.json`, and Fed-BioMed's approval
check is closed but optional.

**Strengths.** The family has analysed mechanisms (secure aggregation with dropouts, DP
accounting, robust aggregators), production scale (Gboard, 71-site FeTS), identity
infrastructure (mTLS, PKI, device attestation, SGX enclaves), and review workflows that
clinical data custodians can use.

**What it leaves to others.** Decisions per data field: FLARE's `ExcludeVars` removes
model variables, not data fields. Typed or analysable policies: only FLARE ships a policy
previewer, and FLA3 lists formal verification as future work. Coded release gates. The 2024
TIST survey of trustworthy FL has no governance or accountability dimension.

**Relation to Dagents.** FLA3 is the closest prior art. It decides participation per round
before aggregation, fails closed, keeps governance separate from the aggregation algorithm,
and logs every decision. So gating participation outside the learning code is not unique to
Dagents. The rarer combination is a per-field protection plan (GRAILS-style), participation,
release gates and model-version status in one typed planner, with the planner's hash in each
audit record. The brief does not describe several family strengths for Dagents:
cryptographic secure aggregation (Dagents' `secure_aggregation` only raises the participant
floor), calibrated DP accounting, robust aggregation, expiring approvals, signed audit
records, code-hash approval, hardware attestation, and deployments of dozens of sites.

### Framework governance at a glance

| Framework | Who may join | What runs / what leaves | Release | Audit evidence | Where the decision lives |
|---|---|---|---|---|---|
| NVIDIA FLARE | Provisioned startup kits (mTLS); per-org `authorization.json` (`submit_job`, `byoc`) | Site `privacy.json` scopes force filters (ExcludeVars, percentile, SVT DP, HE) on every job | Not built in | Server and client audit files | Config files per site, set by the org admin |
| Flower | SuperNode public-key registration | DP and SecAgg+ mods chosen in app code; Hub app signatures (2026) | Not addressed | Not stated | App code, CLI registration, reviewer signatures |
| OpenFL | PKI certificates | FL plan (YAML) agreed before the run; optional SGX TEE | Not built in | Not stated | Plan file plus certificates |
| PySyft (Datasite) | Accounts on a datasite | Data owner approves or denies each code request | Owner approves output release | Request history (not detailed) | A person (the data owner) |
| Fed-BioMed | Node adds or revokes datasets | Training plan runs only if its hash was approved (optional mode); node may override parameters | Not built in | Not stated | A person at the node, through a GUI |
| Substra | Organisation whitelists per asset | Permission to process or download; purpose (Objective) whitelist | Model inherits the intersection of input whitelists | Ledger of all operations | Smart contracts on a ledger (2019 design) |
| Apheris | Beneficiaries named in Asset Policy | Allowed models and model-specific privacy controls; checked at orchestrator, gateway and data access | Not stated | Logs per data interaction | Signed policy written by the custodian |
| FLA3 (on Flower) | XACML: study membership, role, expiry | Participant set filtered before aggregation | Not addressed | JWS-signed JSON records | Policy language (XACML), fail-closed |
| TFF, FATE, FedML, IBM FL | Not addressed in the sources read | Privacy mechanisms in code | Not addressed | IBM FL: DP budget accounting; others not stated | Code |

---

## Entries

### A. Foundations

#### mcmahan2017communication
- **Citation:** McMahan, Moore, Ramage, Hampson, Agüera y Arcas. Communication-Efficient Learning of Deep Networks from Decentralized Data. AISTATS 2017, PMLR 54:1273-1282.
- **Verified:** arXiv 1602.05629 (journal ref names AISTATS 2017, PMLR 54); PMLR page v54/mcmahan17a.
- **What it is:** Introduces Federated Averaging (FedAvg): clients run several local SGD steps and the server averages their models, weighted by example count. It names the setting "federated learning" and evaluates five model architectures on four datasets.
- **D1-D7:** D1 none (a learning algorithm); D2 algorithm; D3 the server averages; D4 training; D5-D7 n/a.
- **Overlap with Dagents:** FedAvg is a manifest aggregation kind in Dagents, and the brief's round structure follows it.
- **Does what Dagents does not:** It is the optimisation method itself. Dagents leaves the combining rule to an extension's model kind.

#### kairouz2021advances
- **Citation:** Kairouz, McMahan, et al. Advances and Open Problems in Federated Learning. Foundations and Trends in Machine Learning 14(1-2):1-210, 2021.
- **Verified:** DOI 10.1561/2200000083 (Crossref); arXiv 1912.04977.
- **What it is:** The reference survey. It defines FL, contrasts cross-device and cross-silo FL (Table 1), and surveys efficiency, privacy, robustness, fairness and systems open problems.
- **D1-D7:** D1 a survey, not a system; D5 discusses auditing and verifiability as open problems; D6 n/a.
- **Overlap with Dagents:** Its "data minimization" framing is the premise of the LMA/GMA split.
- **Does what Dagents does not:** Covers threat models and formal privacy far beyond the brief.
- **Quotes (arXiv v3):** Definition (Sec. 1): "Each client's raw data is stored locally and not exchanged or transferred; instead, focused updates intended for immediate aggregation are used to achieve the learning objective." Sec. 4: "there is still no formal guarantee of privacy in this baseline federated learning model." Table 1 gives cross-silo as "Typically 2 - 100 clients" and cross-device as "up to 10^10 clients". Sec. 4 also asks: "Might federated learning give users a false sense of privacy?"

#### bonawitz2019towards
- **Citation:** Bonawitz, Eichner, Grieskamp, et al. Towards Federated Learning at Scale: System Design. Proc. MLSys (SysML) 1, 2019.
- **Verified:** arXiv 1902.01046; MLSys 2019 proceedings page (page numbers not confirmed).
- **What it is:** Google's production cross-device FL system. Devices check in, are selected, run an FL plan and report. It also describes Secure Aggregation deployment, device attestation, and the model-engineer workflow.
- **D1:** Which devices take part (eligibility, selection, attestation); whether an FL task may be deployed.
- **D2:** Server logic plus an organisational gate. Sec. 7.3: a plan "is not accepted by the server for deployment unless certain conditions are met. First, it must have been built from auditable, peer reviewed code. Second, it must have bundled test predicates for each FL task that pass in simulation. Third, the resources consumed during testing must be within a safe range ... And finally, the FL task tests must pass on every version of the TensorFlow runtime that the FL task claims to support."
- **D3:** Server side (deployment), and on device (attestation).
- **D4:** Deployment of training tasks, then training.
- **D5:** Versioned plans and tests; no audit log described.
- **D6:** A plan that fails the conditions is not accepted (closed).
- **D7:** Google and Android (Android remote attestation).
- **Overlap with Dagents:** A coordinator-side gate with explicit conditions before work runs, comparable to release gates and planner checks.
- **Does what Dagents does not:** Device attestation, scale to millions of devices, simulation tests as a deploy gate.

#### li2020federated
- **Citation:** Li, Sahu, Talwalkar, Smith. Federated Learning: Challenges, Methods, and Future Directions. IEEE Signal Processing Magazine 37(3):50-60, 2020.
- **Verified:** DOI 10.1109/MSP.2020.2975749 (Crossref).
- **What it is:** A short survey of four core FL challenges: expensive communication, systems heterogeneity, statistical heterogeneity and privacy.
- **D1-D7:** Survey; n/a.
- **Overlap with Dagents:** Background only.
- **Does what Dagents does not:** Covers heterogeneity-aware optimisation (such as FedProx), which Dagents leaves to model kinds.

#### huang2022crosssilo
- **Citation:** Huang, Huang, Liu. Cross-Silo Federated Learning: Challenges and Opportunities. arXiv 2206.12949, 2022.
- **Verified:** arXiv 2206.12949.
- **What it is:** An overview of cross-silo FL ("clients are organizations or companies and the client number is usually small (e.g., within a hundred)"): applications, challenges, and how methods differ from cross-device FL.
- **D1-D7:** Survey; n/a.
- **Overlap with Dagents:** Dagents is a cross-silo design: one LMA per data source.
- **Does what Dagents does not:** Discusses incentives and competition between silos, which the brief does not cover.

### B. Leakage and attacks

#### zhu2019deep
- **Citation:** Zhu, Liu, Han. Deep Leakage from Gradients. NeurIPS 32, 2019.
- **Verified:** arXiv 1906.08935; NeurIPS 2019 proceedings PDF.
- **What it is:** Reconstructs training inputs by optimising dummy data until its gradients match the shared gradients.
- **D1-D7:** Attack; n/a. It implies that whatever leaves a site as a gradient or update must itself be governed.
- **Quote (abstract):** "the recovery is pixel-wise accurate for images and token-wise matching for texts"; "The most effective defense method is gradient pruning."
- **Overlap with Dagents:** Motivates treating "model update" as a governed granularity with clipping and noise (`before_send`).
- **Does what Dagents does not:** n/a (an attack).

#### geiping2020inverting
- **Citation:** Geiping, Bauermeister, Dröge, Moeller. Inverting Gradients - How easy is it to break privacy in federated learning? NeurIPS 33, 2020.
- **Verified:** arXiv 2003.14053; NeurIPS 2020 proceedings listing (page numbers not confirmed).
- **What it is:** High-resolution image reconstruction from parameter gradients, including for trained networks. It proves that the input to a fully connected layer can be reconstructed analytically.
- **D1-D7:** Attack; n/a.
- **Quote (abstract):** "even averaging gradients over several iterations or several images does not protect the user's privacy in federated learning applications in computer vision."
- **Overlap with Dagents:** As above; shows that aggregation alone is not protection.

#### shokri2017membership
- **Citation:** Shokri, Stronati, Song, Shmatikov. Membership Inference Attacks Against Machine Learning Models. IEEE S&P 2017, pp. 3-18.
- **Verified:** DOI 10.1109/SP.2017.41 (Crossref).
- **What it is:** Shadow-model attack that decides whether a record was in a model's training set from black-box outputs.
- **D1-D7:** Attack; n/a. It implies that a released model is itself an egress channel, so release needs governing.
- **Overlap with Dagents:** Supports a `before_release` boundary at the coordinator.

#### nasr2019comprehensive
- **Citation:** Nasr, Shokri, Houmansadr. Comprehensive Privacy Analysis of Deep Learning: Passive and Active White-box Inference Attacks against Centralized and Federated Learning. IEEE S&P 2019, pp. 739-753.
- **Verified:** DOI 10.1109/SP.2019.00065 (Crossref).
- **What it is:** White-box membership inference using gradients and activations, by passive and active adversaries, including a participant or server in FL.
- **D1-D7:** Attack; n/a.
- **Overlap with Dagents:** Supports guarding updates as well as models.

#### bagdasaryan2020backdoor
- **Citation:** Bagdasaryan, Veit, Hua, Estrin, Shmatikov. How To Backdoor Federated Learning. AISTATS 2020, PMLR 108:2938-2948.
- **Verified:** arXiv 1807.00459; PMLR page v108/bagdasaryan20a.
- **What it is:** Model-replacement attack: a malicious participant scales its update so that it replaces the joint model with a backdoored one.
- **D1-D7:** Attack; n/a. Notes a tension: secure aggregation hides updates, so the server cannot inspect them for anomalies.
- **Quote (abstract):** "Even a single-shot attack, where a single attacker is selected in a single round of training, causes the joint model to achieve 100% accuracy on the backdoor task." Also: "Federated learning employs secure aggregation to protect confidentiality of participants' local models and thus cannot prevent our attack by detecting anomalies in participants' contributions."
- **Overlap with Dagents:** Motivates clipping at `before_send` and a release gate.

#### fang2020local
- **Citation:** Fang, Cao, Jia, Gong. Local Model Poisoning Attacks to Byzantine-Robust Federated Learning. USENIX Security 2020.
- **Verified:** arXiv 1911.11815 (comment: "Appeared in Usenix Security Symposium 2020"); page numbers not confirmed.
- **What it is:** Optimisation-based attacks on local models that raise the test error of four aggregation rules claimed to be Byzantine-robust (including Krum, trimmed mean and median).
- **D1-D7:** Attack; n/a.
- **Overlap with Dagents:** Shows that robust aggregation is not a complete defence; a release gate on held-out metrics is a complementary check.

### C. Defences

#### bonawitz2017practical
- **Citation:** Bonawitz, Ivanov, Kreuter, Marcedone, McMahan, Patel, Ramage, Segal, Seth. Practical Secure Aggregation for Privacy-Preserving Machine Learning. ACM CCS 2017, pp. 1175-1191.
- **Verified:** DOI 10.1145/3133956.3133982 (Crossref).
- **What it is:** A secure multi-party protocol, using pairwise masks and secret sharing, that lets a server learn only the sum of client vectors, with tolerance for clients dropping out.
- **D1:** How updates are combined (only the sum is revealed).
- **D2:** A cryptographic protocol with security proofs.
- **D3:** Clients and server jointly.
- **D4:** Result egress from the site to the coordinator.
- **D5:** None.
- **D6:** The protocol aborts below a threshold of surviving clients.
- **D7:** Generic.
- **Overlap with Dagents:** Dagents' manifest can name `secure_aggregation` with a threshold. In the code (`federation_compiler`, `dagents/common/domain/federation.py`) this only raises the participant floor and requires a clip bound.
- **Does what Dagents does not:** Hides individual updates from the coordinator cryptographically. In Dagents the GMA's update store holds each site's clipped update.

#### bell2020secure
- **Citation:** Bell, Bonawitz, Gascón, Lepoint, Raykova. Secure Single-Server Aggregation with (Poly)Logarithmic Overhead. ACM CCS 2020, pp. 1253-1269.
- **Verified:** DOI 10.1145/3372297.3417885 (Crossref).
- **What it is:** Replaces the complete pairwise-mask graph with a sparse random graph, so each client's cost grows polylogarithmically in the number of clients. It is the basis of SecAgg+ (which Flower implements).
- **D1-D7:** As for bonawitz2017practical.
- **Does what Dagents does not:** Scalable cryptographic aggregation.

#### mcmahan2018learning
- **Citation:** McMahan, Ramage, Talwar, Zhang. Learning Differentially Private Recurrent Language Models. ICLR 2018.
- **Verified:** arXiv 1710.06963 (comment: "Camera-ready ICLR 2018 version").
- **What it is:** DP-FedAvg. It gives user-level DP (protecting a user's whole history) by sampling users, clipping each user's update to a bounded L2 norm, and adding Gaussian noise to the aggregate, with a moments accountant.
- **D1:** How much noise and clipping; the privacy budget.
- **D2:** Algorithm with formal accounting.
- **D3:** Clipping per user; noise at the server.
- **D4:** Training and result egress.
- **D5:** The (epsilon, delta) guarantee.
- **D6-D7:** n/a.
- **Quote:** "We enforce clipping of per-user updates so the total update has bounded L2 norm" (Sec. 2, arXiv v3).
- **Overlap with Dagents:** Dagents clips each site's update at `before_send` and adds noise.
- **Does what Dagents does not:** A calibrated guarantee with an accountant. Dagents' own `ethical_guard.py` docstring says its noise "is not a calibrated differential-privacy mechanism: that needs a privacy accountant and a tracked budget, which are separate controls."

#### kairouz2021distributed
- **Citation:** Kairouz, Liu, Steinke. The Distributed Discrete Gaussian Mechanism for Federated Learning with Secure Aggregation. ICML 2021, PMLR 139:5201-5212.
- **Verified:** arXiv 2102.06387; PMLR page v139/kairouz21a.
- **What it is:** Distributed DP. Each client adds discrete Gaussian noise before secure aggregation, so the server sees only a noisy sum, with an analysis of quantisation and modular arithmetic.
- **D1:** Noise level; discretisation.
- **D2:** Algorithm with proofs.
- **D3:** At each client, before egress.
- **D4:** Result egress.
- **D5:** A DP guarantee.
- **Quote (abstract):** "essentially able to match the accuracy to central differential privacy with less than 16 bits of precision per value."
- **Overlap with Dagents:** Noise applied at the site before sending, like `before_send`.
- **Does what Dagents does not:** Calibrated DP that does not trust the server; integration with secure aggregation.

#### sun2019really
- **Citation:** Sun, Kairouz, Suresh, McMahan. Can You Really Backdoor Federated Learning? arXiv 1911.07963, 2019 (2nd FL workshop at NeurIPS 2019).
- **Verified:** arXiv 1911.07963.
- **What it is:** An EMNIST study of backdoor attacks. It finds that norm clipping and "weak" DP (clipping plus small Gaussian noise) limit known attacks with little loss on the main task. Implemented in TensorFlow Federated.
- **D1:** The clip norm and noise level.
- **D2:** Server-side rule.
- **D3:** At aggregation.
- **D4:** Training.
- **Numbers (Sec. 5, arXiv v2):** Norm bounds of 3, 5 and 10 were tested; "the 90 percentile of benign users' updates are below 2 for most of the rounds"; "selecting 3 as the norm bound will successfully mitigate the attack with almost no effect on the performance of the main task."
- **Overlap with Dagents:** Gives empirical support for clipping updates, which Dagents does at the site.
- **Does what Dagents does not:** Measures clipping against attacks; Dagents does not report such a measurement.

#### blanchard2017machine
- **Citation:** Blanchard, El Mhamdi, Guerraoui, Stainer. Machine Learning with Adversaries: Byzantine Tolerant Gradient Descent. NeurIPS 30, 2017, pp. 119-129.
- **Verified:** NeurIPS 2017 proceedings page (hash f4b9ec30...); Crossref has only a PODC 2017 brief announcement, DOI 10.1145/3087801.3087861.
- **What it is:** Shows that linear (averaging) aggregation fails with even one Byzantine worker, and proposes Krum, which picks the update closest to its neighbours.
- **D1:** Which updates to trust in aggregation.
- **D2:** Algorithm with a resilience proof.
- **D3:** Coordinator.
- **D4:** Training.
- **Does what Dagents does not:** Robust aggregation. Dagents leaves combining to the model kind.

#### yin2018byzantine
- **Citation:** Yin, Chen, Ramchandran, Bartlett. Byzantine-Robust Distributed Learning: Towards Optimal Statistical Rates. ICML 2018, PMLR 80:5650-5659.
- **Verified:** arXiv 1803.01498; PMLR page v80/yin18a.
- **What it is:** Coordinate-wise median and trimmed-mean gradient descent, with order-optimal error rates under a fraction of Byzantine machines.
- **D1-D7:** As for blanchard2017machine.
- **Does what Dagents does not:** Robust aggregation with statistical guarantees.

#### xu2023federated
- **Citation:** Xu, Zhang, Andrew, Choquette-Choo, Kairouz, McMahan, Rosenstock, Zhang. Federated Learning of Gboard Language Models with Differential Privacy. ACL 2023 (Industry Track), pp. 629-639.
- **Verified:** DOI 10.18653/v1/2023.acl-industry.60 (Crossref); arXiv 2305.18465.
- **What it is:** A production report: more than twenty Gboard language models trained with DP-FTRL, adaptive clip estimation, a client participation criterion, and two models also trained with secure aggregation.
- **D1:** Who participates (participation criterion); release (DP required to launch).
- **D2:** Organisational policy plus algorithm.
- **D3:** Server and devices.
- **D4:** Training and release.
- **D5:** rho-zCDP guarantees per model.
- **D7:** Google.
- **Quote (abstract):** "all the next word prediction neural network LMs in Gboard now have DP guarantees, and all future launches of Gboard neural network LMs will require DP guarantees."
- **Overlap with Dagents:** A release rule ("launch requires a guarantee") that resembles a release gate.
- **Does what Dagents does not:** Formal DP at production scale. The rule is an organisational policy, not code.

### D. Frameworks and platforms

#### beutel2020flower
- **Citation:** Beutel, Topal, Mathur, Qiu, Fernandez-Marques, Gao, Sani, Li, et al. Flower: A Friendly Federated Learning Research Framework. arXiv 2007.14390, 2020.
- **Verified:** arXiv 2007.14390 (v5). Docs: flower2026supernodeauth, flower2026dp, flower2026secagg, flower2026appverification (docs, via search).
- **What it is:** A framework-agnostic FL framework. A strategy runs on the server (ServerApp on a SuperLink) and clients run ClientApps on SuperNodes. It is built for research and for moving to real devices.
- **D1:** Which nodes may connect (SuperNode public-key allowlist; since about version 1.23, registration through `flwr supernode register` with `--enable-supernode-auth`). Which privacy mechanism runs: DP strategy wrappers such as `DifferentialPrivacyClientSideFixedClipping` with `fixedclipping_mod`, `LocalDpMod`, and SecAgg/SecAgg+ workflows with matching client mods. Since May 2026, which apps may run: SuperNode operators can define a trusted-entities list so that only apps signed by trusted Flower Hub reviewers run (docs, via search).
- **D2:** Application code for privacy; CLI registration for identity; reviewer signatures for apps.
- **D3:** SuperLink (identity); client mods at the site (clipping, local DP).
- **D4:** Training and egress of updates.
- **D5:** Not stated in the sources read.
- **D6:** Unregistered nodes cannot connect; unsigned apps are blocked when a trusted-entities list is set (docs, via search).
- **D7:** Generic; Flower Labs.
- **Overlap with Dagents:** Site-side clipping before sending; an allowlist of participating nodes.
- **Does what Dagents does not:** SecAgg+ and calibrated DP with an accountant option; large-scale simulation; app signing by reviewers.
- **Independent assessment:** Fed-BioMed's 2023 comparison says Flower "does not come pre-equipped with any data governance tools". FLA3 (built on Flower) says Flower "does not enforce study-scoped authorisation or temporal validity constraints at the orchestration layer".

#### roth2023nvidia
- **Citation:** Roth, Cheng, Wen, Yang, Xu, Hsieh, Kersten, Harouni, et al. NVIDIA FLARE: Federated Learning from Simulation to Real-World. IEEE Data Engineering Bulletin 46(1), 2023 (arXiv 2022; FL workshop at NeurIPS 2022).
- **Verified:** arXiv 2210.13291 (journal ref IEEE Data Eng. Bull. 46(1), 2023). Docs: nvidia2026flaresitepolicy, nvidia2023flareauthz, nvidia2026flareaudit, nvidia2026flareprivacy (docs, via search).
- **What it is:** An industrial cross-silo SDK. A controller sends tasks to executors, and filters can process every message in both directions. Provisioning produces startup kits for mTLS. Each site can set policies for authorisation, privacy and resources.
- **D1:**
  - *Who may do what* (federated authorisation): since 2.2.1, "each organization defines and enforces its own authorization policy". Rights such as `submit_job` (checked by the server at submission and again by each site at scheduling) and `byoc` (custom code) are granted to roles: Project Admin, Org Admin, Lead Researcher, Member Researcher (docs, via search).
  - *What leaves a site* (filters): ExcludeVars, PercentilePrivacy, SVTPrivacy (DP by the sparse vector technique) and homomorphic-encryption filters (paper, Sec. 3).
  - *Site privacy policy*: `privacy.json`, set by the org admin, "applies to every job"; scopes carry properties and filters; a job picks a scope with the `scope` meta key or gets the default; site scope filters run before job filters (docs, via search).
- **D2:** JSON configuration files per site, written by people. `nvflare authz_preview` previews an authorisation policy file (docs, via search). Not formally verified.
- **D3:** At each site (client) for authorisation and filters; at the server for submission.
- **D4:** Job submission and deployment, training, result egress. Model release: not built in (Fed-BioMed 2023 table: "None built-in").
- **D5:** Paper, Sec. 3.2: "NVFlare automatically records all user commands and job events in system audit files on both the server and client sides. In addition, the audit API can be used by application developers to record additional events in the audit files." In 2.6 there are server and client parent logs plus per-job logs (docs, via search).
- **D6:** Mixed. In 2.2, a job naming a scope the site does not define is rejected at deploy time (closed). But "If a site has no `privacy.json`, no privacy control is applied" (open by default) (docs, via search).
- **D7:** NVIDIA; generic domain.
- **Quotes (paper, Sec. 3.2):** "Each FL client can define its authorization policy that specifies what a role can or cannot do to the client. For example, one client could allow a role to run jobs from any researchers. In contrast, another client may only allow jobs submitted by its researchers." "The client can change its policies at any time after the system is up and running without having to be re-provisioned. For example, the client could require all jobs running on it to be subject to a set of filters."
- **Overlap with Dagents:** The strongest framework overlap. A site-owned policy forces outbound filtering on every job, like `before_send` and the Guard; site-side authorisation of who may run what; audit files at both ends.
- **Does what Dagents does not:** PKI provisioning; homomorphic encryption; resource policies; a policy previewer; production use across many institutions.
- **Differs:** Filters work on model variables (Shareable/DXO), not on data fields with a sensitivity class. There is no typed planner. Missing policy means no control rather than denial. Release is not a framework decision.
- **Conflicting assessment:** FLA3 says FLARE's "authorisation is configured at provisioning time and does not support runtime enforcement of study-specific participation constraints, automatic authorisation expiry, or temporal validity predicates." The FLARE paper says clients can change policies at runtime. Both can hold: policies are editable, but they have no study or time predicates.

#### google2026tff
- **Citation:** Google. TensorFlow Federated documentation. https://www.tensorflow.org/federated (accessed 2026-11-16).
- **Verified:** Official docs URL (docs, via search).
- **What it is:** Two layers: a Federated Learning API (`tff.learning`) for training and evaluating TensorFlow models, and the Federated Core, a language for distributed computations with operators such as `federated_sum` and `federated_broadcast`. It is a research and simulation framework; Sun et al. 2019 implement their defences in it.
- **D1:** Aggregation and privacy mechanisms, in code.
- **D2:** Typed federated computations (the FC has placement types). It is analysable in that sense, but nothing about governance is analysed.
- **D3:** Simulation runtime.
- **D4:** Training.
- **D5-D7:** Not stated; Google and TensorFlow.
- **Overlap with Dagents:** A typed description of federated computations. FC types say where a value lives (server or clients); Dagents types say what protection applies.
- **Does what Dagents does not:** Composable federated algorithms in a typed language; a large research use base.

#### liu2021fate
- **Citation:** Liu, Fan, Chen, Xu, Yang. FATE: An Industrial Grade Platform for Collaborative Learning With Data Protection. JMLR 22(226):1-6, 2021.
- **Verified:** JMLR page jmlr.org/papers/v22/20-815.html (via search).
- **What it is:** WeBank's cross-silo platform. It supports horizontal and vertical FL with several secure computation protocols (homomorphic encryption, secret sharing).
- **D1:** The cryptographic protocol for each algorithm.
- **D2:** Code and pipeline configuration.
- **D3:** Parties and arbiter.
- **D4:** Training and inference.
- **D5-D6:** Not stated in the sources read.
- **D7:** WeBank; finance origins.
- **Overlap with Dagents:** Cross-silo; services for orchestration.
- **Does what Dagents does not:** Vertical (feature-partitioned) FL and cryptographic protocols.

#### ziller2021pysyft
- **Citation:** Ziller, Trask, Lopardo, Szymkow, Wagner, Bluemke, Nounahon, Passerat-Palmbach, et al. PySyft: A Library for Easy Federated Learning. In Federated Learning Systems, Studies in Computational Intelligence, Springer, 2021, pp. 111-139.
- **Verified:** DOI 10.1007/978-3-030-70604-3_5 (Crossref). Current docs: openmined2026review (docs, via search).
- **What it is:** OpenMined's library for remote data science. The 2021 chapter covers FL with secure multi-party computation (SMPC) and DP. Current PySyft (0.9.x, "Datasites") has a data scientist submit code as a request. The data owner inspects it and calls `request.approve()` or `request.deny(reason=...)`; only an admin or data owner may approve. Approved code then runs on the real data, and the scientist retrieves the result (docs, via search).
- **D1:** Whether a specific computation may run on private data, and whether its result may be released.
- **D2:** A human reviewer.
- **D3:** At the data owner's server (the datasite).
- **D4:** Computation and result egress.
- **D5:** A request record (details not verified).
- **D6:** Nothing runs on private data without approval (closed by design).
- **D7:** OpenMined.
- **Overlap with Dagents:** Egress requires an explicit decision at the data owner's side.
- **Does what Dagents does not:** Per-request human review of arbitrary code; mock data for development.
- **Differs:** The decision is a person, not a typed plan; there is no automatic per-field rule.

#### foley2022openfl
- **Citation:** Foley, Sheller, Edwards, Pati, Riviera, Sharma, Narayana Moorthy, Wang, Martin, Mirhaji, Shah, Bakas. OpenFL: the open federated learning library. Physics in Medicine & Biology 67(21):214001, 2022.
- **Verified:** DOI 10.1088/1361-6560/ac97d9 (Crossref). The quotes below are from the arXiv preprint 2105.06413 (Reina et al. 2021), whose arXiv record names this DOI; the published wording may differ.
- **What it is:** Intel's FL library, started for healthcare (the FeTS initiative). It uses an aggregator and collaborators, mTLS with PKI certificates, an FL plan in YAML, and optional Intel SGX TEEs via Graphene.
- **D1:** Which tasks run, the parameters and the participants: all fixed in the FL plan.
- **D2:** A configuration file agreed by all parties. Preprint Sec. 2.1: "the OpenFL plan is agreed upon by all parties before the workload begins"; Sec. 2.2: "The FL plan and model code are manually shared with each participant prior to the start of the federation."
- **D3:** Collaborators (sites) and aggregator; TEEs give "remote attestation of the execution".
- **D4:** Training; the plan fixes the workflow.
- **D5:** PKI certificates; TEE attestation; no audit log described.
- **D6:** Peers without a valid certificate cannot connect.
- **D7:** Intel (SGX for the TEE features).
- **Overlap with Dagents:** A plan agreed before execution that participants validate.
- **Does what Dagents does not:** Hardware attestation of the code that runs; PKI.
- **Independent assessment (Fed-BioMed 2023):** "Governance is implemented mainly through API operations or code snippets written by data owners"; "Model governance: None built-in".

#### he2020fedml
- **Citation:** He, Li, So, Zeng, Zhang, Wang, Wang, Vepakomma, et al. FedML: A Research Library and Benchmark for Federated Machine Learning. arXiv 2007.13518, 2020.
- **Verified:** arXiv 2007.13518 (v4, "white paper V3").
- **What it is:** A research library and benchmark with three computing paradigms (on-device training, distributed computing, single-machine simulation) and reference baselines.
- **D1-D7:** Algorithms in code; governance not addressed in the paper.
- **Overlap with Dagents:** Little; benchmarking.
- **Does what Dagents does not:** Algorithm breadth; edge and IoT support.

#### ludwig2020ibm
- **Citation:** Ludwig, Baracaldo, Thomas, Zhou, Anwar, Rajamoni, Ong, Radhakrishnan, et al. IBM Federated Learning: an Enterprise Framework White Paper V0.1. arXiv 2007.10987, 2020.
- **Verified:** arXiv 2007.10987.
- **What it is:** An enterprise FL framework with pluggable fusion algorithms, secure multi-party computation (including partial homomorphic encryption) and DP building blocks. Sec. 4.3: "For other models, an accounting module is added at the aggregator side to keep tract of the differential privacy budget."
- **D1:** Privacy mechanism; DP budget.
- **D2:** Code and configuration.
- **D3:** Parties and aggregator.
- **D4:** Training.
- **D5:** DP budget accounting at the aggregator. Accountability was added later by the AF^2 work (baracaldo2022towards).
- **D7:** IBM.
- **Overlap with Dagents:** A coordinator-side accounting of privacy spend, though Dagents does not have a budget.
- **Does what Dagents does not:** DP budget accounting; SMPC.

#### galtier2019substra
- **Citation:** Galtier, Marini. Substra: a framework for privacy-preserving, traceable and collaborative Machine Learning. arXiv 1910.11567, 2019.
- **Verified:** arXiv 1910.11567. Current hosting: owkin2022substra.
- **What it is:** Owkin's FL framework, designed around a distributed ledger (Hyperledger Fabric in the 2019 design). Every asset (dataset, algorithm, model) has a permission regime, and smart contracts admit computations only if they respect those regimes.
- **D1:** Which organisations may *process* or *download* each asset; for which purposes (an Objective whitelist); and what permissions a trained model gets.
- **D2:** Smart contracts on a ledger ("trustless smart-contracts which filter the addition of traintuples in the ledger", Sec. 3.3).
- **D3:** At the ledger, before any computation is scheduled ("enforced a priori").
- **D4:** Computation and model creation.
- **D5:** The ledger "records the history of all past" operations (Sec. 5).
- **D6:** A traintuple that violates permissions is not added (closed).
- **D7:** Owkin; Hyperledger Fabric in 2019. The current implementation may differ (not verified).
- **Quotes (Sec. 3.3):** "an Asset can also be made available for processing only for specific purposes. To do so the Asset owner must provide a whitelist of Objectives for which the Asset can be used." "Models that are created by Substra inherit their permission regime from the Assets that were used during its creation. By default, the process and download whitelists of the new model are the intersection of the whitelists of the Dataset, Algorithm and initial Models used for training."
- **Overlap with Dagents:** Purpose whitelists resemble Dagents' approved purposes; checks before computation; a tamper-evident record.
- **Does what Dagents does not:** Derives the downstream rights of a model automatically from the rights of its inputs. This is a release rule by construction, with a replicated ledger instead of a single hash chain.

#### cremonesi2025fedbiomed
- **Citation:** Cremonesi, Vesin, Cansiz, Bouillard, Balelli, Innocenti, Taiello, Silva, et al. Fed-BioMed: Open, Transparent and Trusted Federated Learning for Real-world Healthcare Applications. In Federated Learning Systems (2nd ed.), Studies in Computational Intelligence, Springer, 2025, pp. 19-41.
- **Verified:** DOI 10.1007/978-3-031-78841-3_2 (Crossref); arXiv 2304.12012.
- **What it is:** Inria's healthcare FL framework. It is node-centric: data providers add or revoke datasets, approve training plans through a GUI, and may override training parameters. DP is available through Opacus; secure aggregation was in development (2023 text).
- **D1:** Which datasets are available; which researcher code may run; some training parameters.
- **D2:** A human reviewer at the node, backed by a code-hash allowlist.
- **D3:** At each node.
- **D4:** Data read and training.
- **D5:** Approved-plan hashes (no audit chain described).
- **D6:** In approval mode, a plan runs "only in the case of a match"; but the mode is optional ("When enabled").
- **D7:** Healthcare focus; MQTT and Django.
- **Quotes (arXiv v1, Sec. 4):** "When enabled, this feature prevents the execution of code contained in TrainingPlans that has not yet been inspected and approved by the node. A hash of the code is computed and checked at every training execution to prevent substitution attacks". "nodes are granted the right to override certain training parameters, regardless of the researcher's original request". The rights it requires (Sec. 2.1) include "the ability to review, add or revoke at any time the availability of any given dataset" and "the ability to approve, audit and monitor the execution of specific FL workflows".
- **Overlap with Dagents:** Site-side control and enforcement at the data source; hashes used for integrity (Dagents hashes its planner binary and its audit chain).
- **Does what Dagents does not:** Human approval of researcher code by hash; a GUI for clinical data managers; site override of parameters.
- **Also useful:** Its Table 2 compares SubstraFL, OpenFL, FLARE and Flower on data governance, model governance and attack defences.

#### ogierduterrail2022flamby
- **Citation:** Ogier du Terrail, Ayed, Cyffers, Grimberg, He, Loeb, Mangold, Marchand, et al. FLamby: Datasets and Benchmarks for Cross-Silo Federated Learning in Realistic Healthcare Settings. NeurIPS 35 (Datasets and Benchmarks Track), 2022.
- **Verified:** arXiv 2210.04620 (comment: "Accepted to NeurIPS, Datasets and Benchmarks Track").
- **What it is:** 7 healthcare datasets with natural splits, for cross-silo FL "of few (2--50) reliable clients", with baseline code.
- **D1-D7:** A benchmark; n/a.
- **Overlap with Dagents:** The healthcare demo also uses natural per-hospital splits (International Stroke Trial records).
- **Does what Dagents does not:** Standard benchmarks across seven tasks.

#### apheris2026assetpolicies
- **Citation:** Apheris. Asset Policies overview; Privacy controls; Architecture. Apheris Compute Gateway documentation (accessed 2026-11-16).
- **Verified:** Official docs URLs (docs, via search; pages not opened).
- **What it is:** A commercial federated platform. A data custodian's Compute Gateway runs computations next to the data. An Asset Policy, written by a user with the "Owner" role, lists the datasets, beneficiaries and permissions (the allowed models from a Model Registry, with model-specific privacy controls such as noise, bounding or rounding for statistics), and has an optional signature.
- **D1:** Who may compute (beneficiaries); which models; which privacy controls; whether a job may run.
- **D2:** A declarative policy written by the custodian. A signed policy is verified at the gateway against a stored public key. For custom models, Apheris recommends human code review.
- **D3:** Three points: the orchestrator when a Compute Spec is created, the Compute Gateway agent before launch, and the data access layer when data is read.
- **D4:** Computation, data read, result egress.
- **D5:** "detailed logs for each data interaction".
- **D6:** Only computations that meet the policy run (closed). The docs disagree on whether a custodian also approves each spec by hand.
- **D7:** Apheris; its docs point to NVIDIA FLARE filters for custom privacy controls.
- **Overlap with Dagents:** Close in spirit. A policy decides permit or deny and privacy controls per model; it is enforced at the source, at several boundaries, including data access.
- **Does what Dagents does not:** Signed policies; a model registry that limits which code may touch data; commercial support.
- **Differs:** Controls are per model, not per field and sensitivity class; the policy is not a typed planner.

#### owkin2022substra
- **Citation:** Owkin. Press release: Owkin open-sources its AI software Substra (now hosted by the Linux Foundation for AI and Data) and releases two open-source innovations at NeurIPS, 2022 (accessed 2026-11-16).
- **Verified:** Official Owkin URL (via search).
- **What it is:** Substra moved to LF AI & Data in November 2022. Owkin remains the main contributor. Substra powered MELLODDY (ten pharmaceutical companies named in the release) and the HealthChain hospital consortium.
- **D1-D7:** See galtier2019substra.
- **Overlap with Dagents:** As for Substra.

### E. Healthcare deployments

#### sheller2020federated
- **Citation:** Sheller, Edwards, Reina, Martin, Pati, Kotrotsou, Milchenko, Xu, Marcus, Colen, Bakas. Federated learning in medicine: facilitating multi-institutional collaborations without sharing patient data. Scientific Reports 10:12598, 2020.
- **Verified:** DOI 10.1038/s41598-020-69250-1 (Crossref, with abstract).
- **What it is:** A multi-institution study of brain-tumour segmentation comparing FL with centralised data and other collaborative methods.
- **D1-D7:** A study, not a governance system; n/a.
- **Numbers (abstract):** "federated learning among 10 institutions results in models reaching 99% of the model quality achieved with centralized data".
- **Overlap with Dagents:** Healthcare cross-silo motivation.

#### rieke2020future
- **Citation:** Rieke, Hancox, Li, Milletarì, Roth, Albarqouni, Bakas, Galtier, et al. The future of digital health with federated learning. npj Digital Medicine 3:119, 2020.
- **Verified:** DOI 10.1038/s41746-020-00323-1 (Crossref).
- **What it is:** A perspective on FL in medicine: benefits for stakeholders, open technical challenges, and the point that privacy needs more than FL alone.
- **D1-D7:** Perspective; n/a.
- **Overlap with Dagents:** Motivation.

#### dayan2021federated
- **Citation:** Dayan, Roth, Zhong, Harouni, Gentili, et al. Federated learning for predicting clinical outcomes in patients with COVID-19. Nature Medicine 27(10):1735-1743, 2021.
- **Verified:** DOI 10.1038/s41591-021-01506-3 (Crossref; abstract not available there).
- **What it is:** The EXAM study: a global FL collaboration that trained a model predicting the oxygen needs of COVID-19 patients from chest X-rays and EHR data, using NVIDIA tooling (counts and gains are in **Unverified**).
- **D1-D7:** A study; governance by consortium agreements (not verified in detail).
- **Overlap with Dagents:** A multi-hospital clinical prediction model, like the stroke-triage demo.
- **Does what Dagents does not:** A real multi-continent deployment.

#### pati2022federated
- **Citation:** Pati, Baid, Edwards, Sheller, et al. Federated learning enables big data for rare cancer boundary detection. Nature Communications 13:7346, 2022.
- **Verified:** DOI 10.1038/s41467-022-33407-5 (Crossref, with abstract).
- **What it is:** The FeTS study: the largest FL study at the time, for glioblastoma boundary detection. Fed-BioMed's review states that OpenFL was used for it.
- **D1:** Release: "releasing our consensus model".
- **D2:** Not stated; a decision of the study team.
- **D4:** Training and model release.
- **Numbers (abstract):** "data from 71 sites across 6 continents"; "n = 6,314"; "a 33% delineation improvement for the surgically targetable tumor, and 23% for the complete tumor extent, over a publicly trained model."
- **Overlap with Dagents:** A release decision after federated training, made here by people.
- **Does what Dagents does not:** Scale (71 sites) and a real release.

#### heyndrickx2024melloddy
- **Citation:** Heyndrickx, Mervin, Morawietz, Sturm, Friedrich, Zalewski, Pentina, Humbeck, et al. MELLODDY: Cross-pharma Federated Learning at Unprecedented Scale Unlocks Benefits in QSAR without Compromising Proprietary Information. Journal of Chemical Information and Modeling 64(7):2331-2344, 2024 (online 2023).
- **Verified:** DOI 10.1021/acs.jcim.3c00799 (Crossref: online 2023-08-29, issue 2024-04-08).
- **What it is:** Cross-pharma FL for quantitative structure-activity relationship (QSAR) models, run on Substra (owkin2022substra). Partners were competitors protecting proprietary chemistry.
- **D1-D7:** Governance by consortium and platform permissions (details not verified).
- **Overlap with Dagents:** Participants who do not trust each other or the coordinator.
- **Does what Dagents does not:** Industrial scale among competitors.

#### warnatherresthal2021swarm
- **Citation:** Warnat-Herresthal, Schultze, Shastry, et al. Swarm Learning for decentralized and confidential clinical machine learning. Nature 594(7862):265-270, 2021.
- **Verified:** DOI 10.1038/s41586-021-03583-3 (Crossref, with abstract).
- **What it is:** Swarm Learning (HPE). Peer-to-peer learning with "blockchain-based peer-to-peer networking and coordination ... without the need for a central coordinator", shown on COVID-19, tuberculosis, leukaemia and lung pathology classifiers.
- **D1:** Who joins and who merges (by blockchain coordination; details not verified).
- **D2:** Smart contracts (not verified in detail).
- **D3:** Peers.
- **D5:** A blockchain record.
- **D7:** HPE.
- **Numbers (abstract):** "more than 16,400 blood transcriptomes derived from 127 clinical studies" and "more than 95,000 chest X-ray images".
- **Quote:** "Swarm Learning completely fulfils local confidentiality regulations by design." This is a strong claim; the attack literature above argues that keeping data local is not sufficient.
- **Overlap with Dagents:** Tamper-evident coordination records.
- **Does what Dagents does not:** No central coordinator.

#### karargyris2023federated
- **Citation:** Karargyris, Umeton, Sheller, et al. Federated benchmarking of medical artificial intelligence with MedPerf. Nature Machine Intelligence 5(7):799-810, 2023.
- **Verified:** DOI 10.1038/s42256-023-00652-2 (Crossref, with abstract).
- **What it is:** MLCommons' open platform for *federated evaluation*. It sends models to facilities so that each facility can "assess and verify the performance of AI models in an efficient and human-supervised process".
- **D1:** Whether a model performs acceptably on each site's data (an input to release).
- **D2:** A benchmark workflow with human supervision.
- **D3:** At each facility.
- **D4:** Validation before release or deployment.
- **D5:** Benchmark results.
- **Overlap with Dagents:** Release gates on metrics. Dagents blocks release when a gate's metric is missing; MedPerf collects such metrics across sites.
- **Does what Dagents does not:** Benchmarking of third-party models across institutions as a service.

### F. Governance and accountability of federated studies

#### baracaldo2022towards
- **Citation:** Baracaldo, Anwar, Purcell, Rawat, Sinn, Altakrouri, Balta, Sellami, Kuhn, Schopp, Buchinger. Towards an Accountable and Reproducible Federated Learning: A FactSheets Approach. arXiv 2202.12443, 2022.
- **Verified:** arXiv 2202.12443.
- **What it is:** AF^2. FL parties and the aggregator record claims about each process step (project setup, preprocessing, training, fusion) in an Accountability Service. A Claim Verifier checks those claims against the project owner's predicates, and an FL FactSheet Generator produces a view for an auditor. The prototype extends IBM FL and uses Evidentia nodes (Datalog on Hyperledger Fabric).
- **D1:** No permit or deny at run time. It decides afterwards whether the process complied ("an auditor can validate, reproduce and certify a FL process").
- **D2:** Predicates over logged claims, in Datalog.
- **D3:** Records at every party and the aggregator; verification by the auditor.
- **D4:** All stages, including the final model.
- **D5:** Signed claims in an immutable ledger; a FactSheet.
- **D6:** Detects faults after the fact; does not block.
- **D7:** IBM FL and Hyperledger Fabric.
- **Quote (Sec. 3):** claims must be "Verifiable", "Auditable" and "Tamper-evident"; "every interaction between actors must be recorded based on a predefined specification".
- **Overlap with Dagents:** Tamper-evident evidence of each step. Dagents' audit records name the planner hash, so an auditor can tell which rules made a decision.
- **Does what Dagents does not:** Claims from every party, verified independently; reproducible arguments; a third-party auditor role.
- **Differs:** Dagents decides before acting; AF^2 verifies after.

#### zhang2026building
- **Citation:** Zhang, Kreuter, Fernandez-Marques, BloodCounts! Consortium, Verghese, Butler, Lane, Sivapalaratnam, et al. Building Privacy-and-Security-Focused Federated Learning Infrastructure for Global Multi-Centre Healthcare Research. arXiv 2603.10063, 2026. The PDF header names IEEE Journal of Biomedical and Health Informatics; no DOI found.
- **Verified:** arXiv 2603.10063 (cs.CR, 2026-03-09). Code: github.com/bloodcounts/FLAAA (stated in the paper).
- **What it is:** FLA3 (FL with authentication, authorisation and accounting), built on Flower. A Policy Decision Point using XACML attribute-based access control checks study approval, membership validity, role and time-bounded validity. It runs at three lifecycle points: node activation, study join, and "every communication round before model aggregation". Deployed across five BloodCounts! institutions in four countries; clinical utility tested on a simulated federation of 25 INTERVAL centres.
- **D1:** Who may take part in a study and in each round; whether an action is allowed (Permit or Deny). It does not decide what leaves a site per field, or release.
- **D2:** A policy language (XACML). The validation suite has 47 cases (28 baseline, plus adversarial ones). Formal verification is future work: "XACML policies are manually authored from regulatory analysis, introducing potential for specification errors; formal verification of policy correctness and tools for policy conflict detection represent areas for future investigation."
- **D3:** At the coordinator (SuperLink) and the Flower server components, as policy enforcement points.
- **D4:** Node registration, study join, each training round.
- **D5:** "For each authorisation decision, the PDP emits a structured audit record encoded in JSON and written to append-only log files", optionally signed as a JSON Web Signature (ES256).
- **D6:** Fail-closed: "if any predicate evaluates to false or cannot be evaluated (e.g., missing attributes, policy evaluation failure), the request is denied".
- **D7:** Flower; healthcare (GDPR, HIPAA, DPDPA, ECOWAS analysis); domain-neutral policies.
- **Quotes:** "Governance determines who may participate; the aggregation algorithm determines how to combine compliant participants' contributions." "AAA controls which entity participates but does not validate the integrity of model updates during training." DP and secure aggregation integration "has not been empirically validated in the current deployment."
- **Overlap with Dagents:** The closest prior art in this family. Participation is decided outside the learning code, per round, before aggregation; denial when evaluation fails; governance separate from the aggregation rule; an audit record per decision.
- **Does what Dagents does not:** Time-bounded approvals that expire automatically; signed audit records; a standard policy language with existing tooling; a real deployment in four countries.
- **What Dagents does that it does not:** Per-field protection plans (GRAILS-style); release gates; model-version status changes; typed exhaustive decision functions.

#### fenoglio2026auditable
- **Citation:** Fenoglio, Treleaven. Auditable Session Admission for Cross-Silo Federated Learning. arXiv 2607.16559, 2026.
- **Verified:** arXiv 2607.16559 (cs.CR, 2026-07-18).
- **What it is:** A session admission gateway. Organisations approve roles and constraints at setup and mint signed session capability tokens listing permitted operations for a `session_id`. Each request carries a token and a proof-of-possession, and the gateway returns ALLOW or DENY with a decision record. The proof of concept is built on Flower with MNIST.
- **D1:** Whether a session operation is admitted.
- **D2:** Capability tokens plus "stateless per-request cryptographic verification and capability matching".
- **D3:** At the organisational boundary (the gateway).
- **D4:** Session operations (start, train, evaluate).
- **D5:** Each decision "emits a structured record (operation, session id, reason code, timestamp, and hashes of the token and request) that is appended to an evidence log"; JSON is canonicalised with JCS (RFC 8785) before hashing.
- **D6:** DENY blocks the request (closed).
- **D7:** Generic.
- **Quote (abstract):** admission "is implemented via centralized policy services, platform configuration, or ad hoc checks, which drift over time and are hard to audit from boundary-visible evidence."
- **Overlap with Dagents:** Decisions made outside the orchestration code, with hashed records that can be checked later.
- **Does what Dagents does not:** Proof-of-possession tokens that make replay detectable. Dagents uses bearer tokens of fixed scopes.

#### eden2025scoping
- **Citation:** Eden, Chukwudi, Bain, Barbieri, Callaway, de Jersey, George, Gorse, et al. A scoping review of the governance of federated learning in healthcare. npj Digital Medicine 8:427, 2025.
- **Verified:** DOI 10.1038/s41746-025-01836-3 (Crossref; abstract not available, full text not read).
- **What it is:** A scoping review of how healthcare FL is governed.
- **Number (second-hand, as summarised by FLA3, Sec. 1):** "only 7 of 39 reviewed papers examining FL governance mechanisms and none providing operational implementation guidance". Check against the review before citing.
- **D1-D7:** Review; n/a.
- **Overlap with Dagents:** Evidence that governance is under-implemented.

#### zhang2024survey
- **Citation:** Zhang, Zeng, Luo, Fu, Chen, Xu, King. A Survey of Trustworthy Federated Learning: Issues, Solutions, and Challenges. ACM Transactions on Intelligent Systems and Technology 15(6):1-47, 2024.
- **Verified:** DOI 10.1145/3678181 (Crossref, with abstract). An earlier version is arXiv 2302.10637.
- **What it is:** A survey that organises trustworthy FL into four dimensions: privacy and security, robustness, fairness, and explainability.
- **D1-D7:** Survey; n/a.
- **Observation:** Accountability, participation control and release are not among its four dimensions. This supports the view that governance sits outside the trustworthy-FL mainstream.

#### lo2022architectural
- **Citation:** Lo, Lu, Zhu, Paik, Xu, Wang. Architectural patterns for the design of federated learning systems. Journal of Systems and Software 191:111357, 2022.
- **Verified:** DOI 10.1016/j.jss.2022.111357 (Crossref); arXiv 2101.02373.
- **What it is:** 14 patterns from a systematic review, grouped as client management (client registry, client selector, client cluster), model management (message compressor, model co-versioning registry, model replacement trigger, deployment selector), model training, and model aggregation (including secure aggregator).
- **D1:** Names where participation (client registry, client selector) and release or deployment (model co-versioning registry, deployment selector, model replacement trigger) belong architecturally.
- **D2:** Design patterns, not decision logic.
- **D3-D7:** n/a.
- **Overlap with Dagents:** Dagents' GMA registry, eligibility planner and model-version lifecycle fill the client-registry, client-selector and co-versioning-registry roles.
- **Does what Dagents does not:** Gives a vocabulary that could position Dagents' components.

---

## Unverified

- **Rhino Health (Rhino Federated Computing Platform).** An AWS Industries blog
  (aws.amazon.com/blogs/industries/collaborative-ai-model-training-with-rhino-federated-computing-on-aws)
  and reseller pages describe a client behind the custodian's firewall plus cloud
  orchestration, and "role-based permissions that govern data access, data subject privacy,
  and code execution rights". A 2021 news item says Rhino uses NVIDIA technology. No
  official Rhino documentation was found, so no claim about Rhino goes in the BibTeX file.
- **EXAM numbers (dayan2021federated).** From memory: 20 institutes, AUC above 0.92, a 16%
  average AUC improvement and a 38% gain in generalisability. Crossref has no abstract for
  this paper, and search ran out before the paper could be checked. Do not cite these numbers
  without checking the paper.
- **MELLODDY scale (heyndrickx2024melloddy).** From memory: ten companies and billions of
  activity data points. Only "ten pharmaceutical companies" is supported, by Owkin's press
  release as seen through search.
- **Vantage6 / Personal Health Train** (Moncada-Torres et al., AMIA Annual Symposium
  Proceedings, about 2020/2021). A healthcare federated infrastructure with node-side
  algorithm allowlists. Its metadata could not be checked (not in Crossref under the queried
  title; web search budget exhausted).
- **Current Substra permissions API.** Whether the current Substra (LF AI & Data) still uses
  Hyperledger Fabric, and how its permission fields are named today, was not verified; the
  entry relies on the 2019 whitepaper.
- **Fed-BioMed and FLARE release features.** Fed-BioMed's 2023 table says FLARE, OpenFL and
  Flower have no built-in model governance. Later FLARE versions may have added model
  selection or approval components (not checked).
- **Exact `privacy.json` and `authorization.json` schemas (NVIDIA FLARE).** The descriptions
  come from search extracts of the 2.2-2.7 docs, which differ between versions; the exact
  field names were not read.
- **Flower Hub app verification (May 2026).** From the Flower blog post as summarised by
  search; the exact SuperNode configuration key was not confirmed.
- **Swarm Learning onboarding by smart contract.** That new nodes are admitted through
  blockchain smart contracts is from memory; the abstract confirms only "blockchain-based
  peer-to-peer networking and coordination".
- **Page numbers** for bonawitz2019towards (MLSys 2019), geiping2020inverting (NeurIPS 2020),
  zhu2019deep (NeurIPS 2019; MLAnthology lists 14774-14784) and fang2020local (USENIX
  Security 2020) were not confirmed from a primary source and are left out of the BibTeX.
- **Other candidates found but not read:** Lo et al., IEEE IoT Journal 10(4):3276-3284, 2023
  (blockchain-based accountability for FL, DOI 10.1109/JIOT.2022.3144450; metadata
  verified, content not read); Zhang et al., Patterns 5(6):101006, 2024 (a review of 89
  healthcare FL methods, DOI 10.1016/j.patter.2024.101006; FLA3 reports that 87 of 89
  lacked node authentication); Bonawitz et al., "Federated Learning and Privacy", ACM Queue
  19(5):87-114, 2021 (DOI 10.1145/3494834.3500240); Gorkhali and Shrestha, arXiv
  2607.10467, 2026 (MLOps and governance for healthcare FL); Zhang et al., arXiv 2605.21563,
  2026 (an FLA3 deployment at AUMC and NHSBT).
