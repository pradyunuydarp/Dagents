# Where governance decisions live: the survey paper

This folder holds the LaTeX source of the paper *Where Governance Decisions Live:
A Survey of Governed Analytics and Learning across Data Sources, and the Design
of Dagents*. The built paper is [`../dagents_survey_paper.pdf`](../dagents_survey_paper.pdf),
and the slide deck that presents it is
[`docs/presentation/survey/dagents-survey-and-design.pptx`](../../presentation/survey/dagents-survey-and-design.pptx).

The paper starts from GRAILS (Kulkarni and Ramanathan, AIES 2025). It surveys 293
research works, products and regulations in seven families, asks of each where
the governance decision is made, what it decides, where it is enforced and what
evidence it leaves, and then states the Dagents design as ten principles tied to
code. It ends with a case study on the stroke demo and the limits of the work.

## Files

| File | Contents |
|---|---|
| `dagents_survey_paper.tex` | The main file: preamble, title and the order of the sections |
| `sections/` | One file per section; the survey has one file per family (`survey_f1_rai.tex` to `survey_f7_reg.tex`) |
| `dagents_survey_paper.bib` | 379 references; the paper cites 297 of them |
| `core.bib` | The references only the case study and the design section need |
| `merge_bib.py` | Merges bibliographies, keeping one entry per key and per DOI |
| `survey-notes/` | The research notes behind each family: a synthesis, one entry per work with the seven dimensions, quotes with page or section numbers, and an "Unverified" list. `BRIEF.md` is the brief every family followed |

## Build

TeX Live with `latexmk` is enough:

```bash
cd docs/reports/dagents_survey_paper
latexmk -pdf -outdir=build dagents_survey_paper.tex
cp build/dagents_survey_paper.pdf ../dagents_survey_paper.pdf
```

`build/` is ignored by git. The PDF in `docs/reports/` is listed on the site's
Resources page, so after copying it run
`.venv/bin/python scripts/export_presentations.py --write` and commit
`docs/presentation/resources.json` with it.

## How the references were checked

The survey was written in November 2026. Every academic reference was checked
against Crossref or the arXiv API: authors, title, venue, year and DOI or arXiv
id. Products and standards are cited by their official pages, with the date they
were read. Where a page could only be read through search results, the notes in
`survey-notes/` say so, and claims that could not be checked are listed in each
note's "Unverified" section and kept out of the paper.

## Numbers from the repository

The case study's numbers come from the healthcare demo, run with the planner
built from `bindings/ocaml`:

- `apps/healthcare-demo/scripts/run_pilot.sh` prints the release gates and the
  decision.
- The cost of the Guard compares a pilot run with the committed classification
  with one where `hours_since_onset` and `systolic_bp` are lowered to medium
  sensitivity: 0.7864 against 0.7859 size-weighted AUC.
- The per-hospital scores of the same candidate on exact and guarded records
  come from the records the LMA gave its Guard in the validation round, as in
  `apps/healthcare-demo/tests/test_federated_pilot.py`.

## Status

This is a draft. The author list, the venue and its template are still to be
decided; the paper uses a plain `article` class until then.
