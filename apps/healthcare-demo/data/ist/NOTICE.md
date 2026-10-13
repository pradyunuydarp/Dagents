# International Stroke Trial extract: notice

`ist_three_hospitals.csv` holds 1,014 patient records from three hospitals in the
International Stroke Trial (IST-1). The demo uses them as each hospital's own data.
They are real, de-identified trial records from 1991-1996.

> Contains information from the International Stroke Trial database (version 2)
> (https://doi.org/10.7488/ds/104), made available under the ODC Attribution License
> (https://opendatacommons.org/licenses/by/1-0/).

## Licence

The database and this extract are available under the Open Data Commons Attribution
License (ODC-By) 1.0. The full text is in [`LICENSE`](LICENSE), copied unchanged from
the licence file distributed with the data. This extract is a derivative database, and
it is offered under the same licence with no further restrictions.

ODC-By section 4.3 asks for the notice above on any public work made from the data.
Every page and chart the demo builds from these records carries it.

## Credit

- **Collected by:** the International Stroke Trial Collaborative Group. The trial
  randomised 19,435 patients. The data hold 466 hospital numbers in 36 countries, and
  dates of randomisation from 1991 to 1996.
- **Funding:** the database paper states that the trial "was principally funded by the
  UK Medical Research Council, the UK Stroke Association, and the European Union
  BIOMED-1 program". The paper lists further national funders.
- **Acknowledgement:** the database paper's authors ask that work using the data
  acknowledge the source of the dataset, its funding and the collaborative group that
  collected it. This section is that acknowledgement.

## Citation

- Sandercock PAG, Niewada M, Członkowska A; International Stroke Trial Collaborative
  Group. The International Stroke Trial database. *Trials* 2011;12:101.
  https://doi.org/10.1186/1745-6215-12-101
- Erratum: *Trials* 2012;13:24. https://doi.org/10.1186/1745-6215-13-24
- Dataset: Sandercock P, Niewada M, Członkowska A (2011). International Stroke Trial
  database (version 2) [dataset]. University of Edinburgh, Department of Clinical
  Neurosciences. https://doi.org/10.7488/ds/104
- Trial report: International Stroke Trial Collaborative Group. The International
  Stroke Trial (IST): a randomised trial of aspirin, subcutaneous heparin, both, or
  neither among 19435 patients with acute ischaemic stroke. *Lancet*
  1997;349:1569-1581. https://doi.org/10.1016/S0140-6736(97)04011-7

## Source

| | |
|---|---|
| Canonical record | Edinburgh DataShare, https://datashare.ed.ac.uk/handle/10283/124 |
| File | `IST_corrected.csv` (version 2, with the 2012 corrections) |
| Pinned copy used | https://raw.githubusercontent.com/ebergel/ist-1-database/e9ebea9d6202bed786abc89785740e13ac087fed/data/IST_corrected.csv |
| Second identical copy | https://raw.githubusercontent.com/AtlantaL/Survival-Analysis-International-Strokes-Trial/ffff8832ea4d01c436e44209422a0dca1f30db84/Dataset/IST_corrected.csv |
| Source SHA-256 | `a350aed2329bbcde23c05caaa96b9275b44b017adecc2fd02231d590454695d5` |
| Source size and shape | 4,798,288 bytes; 19,435 rows x 112 columns; Windows-1250; CRLF line endings |
| Extract SHA-256 | `817c85be1499d5a6436319a2b04a7ecb06bdfd40611fa860a5be09852d8710da` |
| Extract format | UTF-8, LF line endings, 1,014 rows plus a header |

The two pinned copies are byte-identical. The DataShare host could not be reached from
the environment that made this extract, so the copies were checked against the
DataShare listing instead: the same size, the same row and column counts, and the same
Windows-1250 month codes in the date column.

## Which hospitals

Rule: take the three countries with the most patients in the trial. In each, take the
hospital with the most patients; a tie goes to the lowest hospital number.

| Demo site | IST `HOSPNUM` | Country | Rows |
|---|---|---|---|
| `uk-134` | 134 | United Kingdom | 524 |
| `it-002` | 2 | Italy | 292 |
| `ch-245` | 245 | Switzerland | 198 |

## How the columns were mapped

| Extract column | IST column | Rule |
|---|---|---|
| `encounter_id` | | `{site_id}-{source_row}`, the row number padded to 5 digits |
| `site_id` | `HOSPNUM` | 134 to `uk-134`, 2 to `it-002`, 245 to `ch-245` |
| `source_row` | | 1-based position among the data rows of `IST_corrected.csv`; the header is not counted |
| `age` | `AGE` | years |
| `age_band` | `AGE` | `<50`, `50-59`, `60-69`, `70-79`, `80-89`, `90+` |
| `sex` | `SEX` | M to `male`, F to `female` |
| `conscious_level` | `RCONSC` | F to `alert`, D to `drowsy`, U to `unconscious` |
| `face_weakness` | `RDEF1` | Y to true, N to false, C (cannot assess) to empty |
| `arm_weakness` | `RDEF2` | as above |
| `leg_weakness` | `RDEF3` | as above |
| `speech_difficulty` | `RDEF4` | dysphasia; as above |
| `hours_since_onset` | `RDELAY` | hours from stroke to randomisation |
| `systolic_bp` | `RSBP` | mmHg at randomisation |
| `visual_field_loss` | `RDEF5` | hemianopia; Y, N, C as above |
| `atrial_fibrillation` | `RATRIAL` | Y to true, N to false, blank to empty (not coded in the trial's pilot phase) |
| `onset_on_waking` | `RSLEEP` | Y to true, N to false |
| `stroke_subtype` | `STYPE` | `TACS`, `PACS`, `LACS`, `POCS`; OTH to `other` |
| `poor_outcome_6m` | `OCCODE` | 1 (dead) or 2 (dependent) to true; 3 (not recovered) or 4 (recovered) to false; 0, 8, 9 or blank to empty |

An empty cell is a missing value. The extraction stops on any code not listed here.

## Columns left out, and why

The extract keeps only the columns above. Of the 112 source columns, these were left
out on purpose:

| IST columns | Reason |
|---|---|
| `EXPDD`, `EXPD6`, `EXPD14` | The trial's own predicted probabilities of the outcomes. A model given them would be reading the answer. |
| `DMAJNCHX`, `DSIDEX`, `DNOSTRKX`, `DDEADX`, `FDEADX` | Free-text comments, which can identify a patient. |
| `RDATE`, `RTIME`, `HOURLOCAL`, `MINLOCAL`, `DAYLOCAL` | Date and time of randomisation, which are quasi-identifiers. |
| `DMAJNCHD`, `DSIDED`, `DRSISCD`, `DRSHD`, `DRSUNKD`, `DPED`, `DALIVED`, `DDEADD`, `FLASTD`, `FDEADD`, `FU1_RECD`, `FU2_DONE`, `FU1_COMP` | Follow-up dates, which are quasi-identifiers. |
| `RXASP`, `RXHEP` | The trial's treatment allocation, which is not a patient characteristic. |

The remaining source columns (treatments in hospital, the 14-day events, causes of
death, CT findings and others) are not part of the demo's feature contract, so the
extract does not carry them.

## How to regenerate

From the repository root:

```bash
.venv/bin/python apps/healthcare-demo/scripts/extract_ist.py
```

The script downloads the pinned copy, checks the SHA-256 above, decodes Windows-1250,
checks that the selection rule still picks the three hospitals, maps the columns, checks
the row count of each hospital and writes `ist_three_hospitals.csv`. It stops with an
error if any check fails. To use a local copy of `IST_corrected.csv` instead of
downloading it:

```bash
.venv/bin/python apps/healthcare-demo/scripts/extract_ist.py --source path/to/IST_corrected.csv
```

The result is byte-identical to the committed file. If the extract SHA-256 above
changes, update it here; `tests/test_dataset.py` checks it.
