# References

Every source used in the learning guide, grouped by topic.

## Videos and courses

| Title | Who | Length or format | Topic |
|---|---|---|---|
| [Federated Learning: Machine Learning on Decentralized Data](https://www.youtube.com/watch?v=89BGjQYA0uE) | Google I/O 2019 | Talk | Introduction to federated learning |
| [On Heterogeneity in Federated Settings](https://www.youtube.com/watch?v=laCyJICLyWg) | Virginia Smith, Stanford MLSys Seminar | Talk | Training when each site's data is different |
| [Open-Source Systems for Federated Learning](https://www.youtube.com/watch?v=TcbOMbg4F9g) | Mosharaf Chowdhury, Stanford MLSys Seminar | Talk | How federated systems are built |
| [Introduction to Federated Learning and Privacy-preserving ML with Flower](https://www.youtube.com/watch?v=rwi3SamXpPY) | Flower | Session 1 of 2 | Federated learning and privacy, with code |
| [Federated AI Simulations with Flower (2025)](https://www.youtube.com/playlist?list=PLNG4feLHqCWkdlSrEL2xbCtGa6QBxlUZb) | Flower | Playlist | Strategies and simulation |
| [Intro to Federated Learning](https://www.deeplearning.ai/short-courses/intro-to-federated-learning/) | DeepLearning.AI with Flower Labs | Short course, about 1 hour | Building a federated system |
| [Protecting Privacy with MATH](https://www.youtube.com/watch?v=pT19VwBAqKA) | minutephysics with the [US Census Bureau](https://census.gov/library/video/2019/protecting-privacy.html) | 12 minutes | Differential privacy |

## Blogs and introductions

- [Federated Learning: Collaborative Machine Learning without Centralized Training Data](https://research.google/blog/federated-learning-collaborative-machine-learning-without-centralized-training-data/) — McMahan and Ramage, Google Research, 2017.
- [Federated Analytics: Collaborative Data Science without Data Collection](https://research.google/blog/federated-analytics-collaborative-data-science-without-data-collection/) — Ramage and Mazzocchi, Google Research, 2020.
- [Federated Learning comic](https://federated.withgoogle.com/) — Google.

## Papers

**Federated learning**

- McMahan, Moore, Ramage, Hampson, Agüera y Arcas. [Communication-Efficient Learning of Deep Networks from Decentralized Data](https://arxiv.org/abs/1602.05629). AISTATS 2017. Introduces federated averaging.
- Kairouz, McMahan, et al. [Advances and Open Problems in Federated Learning](https://arxiv.org/abs/1912.04977). 2021. A broad survey.
- Bonawitz et al. [Practical Secure Aggregation for Privacy-Preserving Machine Learning](https://eprint.iacr.org/2017/281). ACM CCS 2017.

**Federated learning in healthcare**

- Rieke et al. [The future of digital health with federated learning](https://doi.org/10.1038/s41746-020-00323-1). npj Digital Medicine, 2020.
- Sheller et al. [Federated learning in medicine: facilitating multi-institutional collaborations without sharing patient data](https://doi.org/10.1038/s41598-020-69250-1). Scientific Reports, 2020.

**Privacy and governance**

- Kulkarni and Ramanathan. [GRAILS: A Framework for Embedding Ethical Safeguards in Software Applications for Responsible AI](https://ojs.aaai.org/index.php/AIES/article/view/36650). AIES 2025. The model behind Dagents' governance planner.
- Sweeney. [k-anonymity: a model for protecting privacy](https://dataprivacylab.org/projects/kanonymity/). International Journal on Uncertainty, Fuzziness and Knowledge-based Systems, 2002.
- Dwork and Roth. [The Algorithmic Foundations of Differential Privacy](https://www.cis.upenn.edu/~aaroth/Papers/privacybook.pdf). Foundations and Trends in Theoretical Computer Science, 2014.

## Standards and guidance

- [Minimum Necessary Requirement](https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/minimum-necessary-requirement) — US Department of Health and Human Services (HIPAA).
- [HL7 FHIR](https://hl7.org/fhir/) — the healthcare data exchange standard the stroke demo maps from.
- [NIH Stroke Scale](https://www.ninds.nih.gov/node/9970) — National Institute of Neurological Disorders and Stroke. The `nihss_total` field in the demo. Its LOINC code is [70182-1](https://loinc.org/70182-1).

## Frameworks and tools

- [Flower: get started with PyTorch](https://flower.ai/docs/framework/tutorial-get-started-with-flower-pytorch.html)
- [TensorFlow Federated tutorials](https://www.tensorflow.org/federated/tutorials/tutorials_overview)
- [NVIDIA FLARE documentation](https://nvflare.readthedocs.io/)
- [A Tour of OCaml](https://ocaml.org/docs/tour-of-ocaml) and [Real World OCaml](https://dev.realworldocaml.org/)
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [Kubernetes basics](https://kubernetes.io/docs/tutorials/kubernetes-basics/)
- [Mermaid flowchart syntax](https://mermaid.js.org/syntax/flowchart.html) and [GitHub: creating diagrams](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams) — how the diagrams in this guide are written.
