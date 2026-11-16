# Common brief for every survey agent

## The paper
A survey paper (literature + industry) that positions an open-source framework, Dagents,
against existing ideas. It uses GRAILS (Kulkarni and Ramanathan, AIES 2025,
DOI 10.1609/aies.v8i2.36650) as its starting point. GRAILS separates ethical constraints
from application logic: "Ethical-Restriction Rails" (a Know-Your-User trust score, a data
protection profile from metadata and regulations, a Restriction Planner likened to a database
query planner) produce a restriction plan; an "Ethical Guard" enforces it on structured data.
Its strategy is chosen from sensitivity (low/moderate/high) x user trust (low/moderate/high)
x granularity (cell/row/column/table). Java proof of concept on Open Government Data.

## What Dagents is (neutral description, for comparison only)
Dagents runs analytics and machine learning across many data sources without moving the
data. A local agent (LMA) runs at each data source; a global agent (GMA) coordinates.
Decisions are made by pure OCaml functions over algebraic data types with exhaustive pattern
matching, called as a separate process with JSON: what protection each field of a request
needs (GRAILS's sensitivity x trust x granularity, plus "model update" as a fifth granularity
with clipping and noise), which sites may join a federated round, quorum, whether a candidate
model may be released, and every change of a model version's status. A Python runtime
enforces each plan at four boundaries (before_train, before_read and before_send at the site;
before_release at the coordinator), denies when the planner is unavailable (fails closed), and
writes a hash-chained audit record naming the planner binary's SHA-256. Aggregation yields a
candidate, never a release; a release gate whose metric is missing blocks release. Domain
knowledge (field classifications, approved purposes, minimum cohort, model kinds) comes from
application extensions, not framework code.

Do NOT try to prove Dagents is novel. We need an even-handed map. Actively look for prior
art that already does some of what Dagents does, and for strengths of each surveyed approach
that Dagents lacks. Both are as valuable to us as differences.

## Comparison dimensions (fill for each work where it applies; "n/a" or "not stated" is fine)
- D1 What is decided: permit/deny; a transformation (how much, in what form); which privacy
  mechanism; who may participate; whether a model/result may be released.
- D2 How the decision is expressed: code in the app, configuration, a policy language, a human
  reviewer, a learned model, a typed/verified function. Is it analysable or formally verified?
- D3 Where enforcement happens: a central store, at each data source, at output/egress, at model release.
- D4 Lifecycle stages covered: data read, computation/training, result egress, model release, deployment.
- D5 Evidence produced: audit logs, attestations, documentation, provenance.
- D6 Failure behaviour (fail-open or fail-closed), if documented.
- D7 Coupling: to a domain, to application code, to one vendor or platform.

## Tools and network
- WebSearch works (server-side). Use mode "standard"; use "extended" only for hard-to-find items.
- From Bash, ONLY these hosts are reachable: api.crossref.org, export.arxiv.org, arxiv.org,
  ojs.aaai.org (plus package registries). Every other host is blocked by policy: do not try to
  reach other hosts with curl or WebFetch, and do not try to work around the block.
- Verify every academic reference's metadata (authors, title, venue, year, volume/pages, DOI)
  with Crossref, e.g.
  curl -sS "https://api.crossref.org/works?query.bibliographic=<url-encoded title>&rows=3"
  or with arXiv, e.g. curl -sS "https://export.arxiv.org/api/query?search_query=ti:%22<title>%22"
  or curl -sS "https://export.arxiv.org/api/query?id_list=<id>".
- For products and standards with no paper, cite the official documentation URL found by
  search, as a BibTeX @misc with howpublished={\url{...}} and note={Accessed 2026-11-16}.
- NEVER invent a reference or a number. If you cannot verify one, put it in an "Unverified"
  section with what you know and why it is unverified.
- Today is 2026-11-16. Include seminal works AND recent work (2023-2026).

## Output (write both files; your final reply should be a 10-15 line summary only)
1. <scratchpad>/survey/<family>.md with:
   - A synthesis of the family (300-600 words): its shared assumptions, where decisions live,
     where enforcement happens, what it does well, and what it leaves to others.
   - One entry per work: BibTeX key; one-line citation; verification (DOI / arXiv id / URL);
     what it is (2-4 sentences); D1-D7 in short bullets; "Overlap with Dagents" and
     "What it does that Dagents does not"; any exact quotes or numbers worth citing, with page
     or section where possible.
   - An "Unverified" section.
2. <scratchpad>/survey/<family>.bib with a BibTeX entry for every verified work. Use keys of the
   form firstauthorYEARword (e.g. mcmahan2017communication), prefixed with your family code to
   avoid clashes is NOT needed; keep keys plain and unique.
Aim for 15-25 works per family, chosen for relevance, not volume.
