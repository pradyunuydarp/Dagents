"""Run one governed federated pilot and print a readable report.

Prints the evidence rather than a verdict: which sites were selected and which
were excluded and why, what each site contributed, what the candidate measured,
and how each release gate decided. A summary nobody can check is not evidence.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-size", type=int, default=400, help="synthetic records per hospital")
    parser.add_argument("--round-prefix", default="pilot", help="prefix for this run's round ids")
    parser.add_argument("--json", action="store_true", help="print the raw report instead of a summary")
    args = parser.parse_args()

    from app.services.consortium import Consortium

    consortium = Consortium(cohort_size=args.cohort_size)
    report = consortium.run_pilot(args.round_prefix)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Study      : {report['study_id']}")
    print(f"Condition  : {report['condition']['display_name']}")
    print(f"Engine     : {report['engine']}")
    print(f"Notice     : {report['notice']}")
    print()

    for name, round_report in report["rounds"].items():
        if round_report is None:
            continue
        plan = round_report["plan"]
        print(f"[{name}] round {round_report['round_id']}  digest {plan['round_digest']}")
        print(f"  quorum {plan['quorum_met']} ({len(plan['selected_sites'])}/{plan['required_participants']} required)")
        print(f"  selected: {', '.join(plan['selected_sites']) or 'none'}")
        for exclusion in plan["excluded_sites"]:
            print(f"  excluded: {exclusion['site_id']} - {exclusion['reason']}")
        readiness = round_report.get("readiness")
        if readiness:
            print(f"  aggregation permitted: {readiness['aggregation_permitted']}")
            for rejection in readiness["rejected_contributions"]:
                print(f"  rejected: {rejection['site_id']} - {rejection['reason']}")
        print()

    candidate = report.get("candidate")
    if not candidate:
        print("No candidate was produced; the round did not reach aggregation.")
        return 0

    print(f"Candidate  : {candidate['candidate_version']} (from {candidate['parent_version']})")
    print(f"  sites    : {', '.join(candidate['contributing_sites'])}")
    print(f"  examples : {candidate['contributed_examples']}")
    print("  metrics  :")
    for key in sorted(candidate["metrics"]):
        baseline = report["baseline_metrics"].get(key)
        suffix = f"   (baseline {baseline})" if baseline is not None else ""
        print(f"    {key:<22} {candidate['metrics'][key]:.4f}{suffix}")
    print()

    gates = report["release"]["gates"]
    print("Release gates")
    for result in gates["gate_results"]:
        marker = {"passed": "PASS", "failed": "FAIL", "not_evaluated": "????"}[result["outcome"]]
        print(f"  [{marker}] {result['gate_id']:<28} {result['detail']}")
    print()
    print(f"Decision   : {gates['action'].upper()}")
    for failure in gates["blocking_failures"]:
        print(f"  blocking : {failure}")
    if gates["action"] != "release":
        print(f"  rollback : {gates['rollback_version']}")
    print()

    print("Audit")
    for site_id, audit in report["site_audits"].items():
        print(f"  {site_id:<26} chain_intact={audit['chain_intact']}  decisions={len(audit['records'])}")
    coordinator = report["coordinator_audit"]
    print(f"  {'coordinator':<26} chain_intact={coordinator['chain_intact']}  decisions={len(coordinator['records'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
