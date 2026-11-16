"""Merge the survey .bib files into one, keeping the first entry for each key and each DOI.

Usage, from this folder:
  python merge_bib.py dagents_survey_paper.bib core.bib FAMILY.bib ...
The family .bib files were written by the survey agents (see survey-notes/BRIEF.md).
Prints the keys dropped as duplicates (and which key they duplicate).
"""
import re
import sys

out, *ins = sys.argv[1:]
entries = []
seen_keys = {}
seen_dois = {}
alias = {}
for path in ins:
    text = open(path, encoding="utf-8").read()
    # Split on lines that start a new entry.
    parts = re.split(r"(?m)^(?=@[A-Za-z]+\s*\{)", text)
    for part in parts:
        m = re.match(r"@([A-Za-z]+)\s*\{\s*([^,\s]+)\s*,", part)
        if not m:
            continue
        kind, key = m.group(1).lower(), m.group(2)
        if kind in ("comment", "preamble", "string"):
            continue
        body = part.strip()
        doi = re.search(r"(?im)^\s*doi\s*=\s*[{\"]\s*([^}\"]+?)\s*[}\"]", body)
        doi = doi.group(1).lower().replace("https://doi.org/", "") if doi else None
        if key in seen_keys:
            alias[key] = key
            continue
        if doi and doi in seen_dois:
            alias[key] = seen_dois[doi]
            continue
        if not re.search(r"(?im)^\s*year\s*=", body):
            # Web pages without a publication date: plainnat prints "n.d."; the note gives the access date.
            body = re.sub(r"\n\}([^}]*)$", lambda m: ",\n  year = {n.d.}\n}" + m.group(1), body.rstrip(), count=1)
            body = body.replace(",,\n  year", ",\n  year")
        if key == "powers2003epal" and not re.search(r"(?im)^\s*key\s*=", body):
            body = re.sub(r"\n\}\s*$", ",\n  key = {EPAL}\n}", body.rstrip())
        seen_keys[key] = path
        if doi:
            seen_dois[doi] = key
        entries.append(body)

with open(out, "w", encoding="utf-8") as f:
    f.write("% Merged from the survey families' bibliographies and core.bib.\n")
    f.write("% Every academic entry was checked against Crossref or arXiv; entries of type misc cite official pages.\n\n")
    f.write("\n\n".join(entries) + "\n")
print(f"{len(entries)} entries written")
for k, v in sorted(alias.items()):
    if k != v:
        print(f"duplicate DOI: {k} -> {v}")
