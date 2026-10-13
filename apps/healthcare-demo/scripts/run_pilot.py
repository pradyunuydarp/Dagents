"""Run one governed federated pilot and print a readable report.

The report shows the evidence for each step: which sites each round selected
and excluded and why, what each site sent, the training loss and update norms
per round, what the candidate measured against the baseline, how each release
gate decided, and whether every audit chain verifies.

With ``HEALTHCARE_DEMO_DATABASE_URL`` set, the pilot reads each hospital's
records from its own schema in that database. Without it, each hospital keeps
its records in memory, loaded from the committed IST extract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _number(value: object) -> str:
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--round-prefix", default="pilot", help="start of this run's round ids")
    parser.add_argument("--training-rounds", type=int, default=None, help="training rounds to run (default 5)")
    parser.add_argument("--json", action="store_true", help="print the raw report instead of a summary")
    args = parser.parse_args()

    from app.domain import stroke_model
    from app.services.consortium import Consortium

    consortium = Consortium(database_url=os.environ.get("HEALTHCARE_DEMO_DATABASE_URL", ""))
    report = consortium.run_pilot(args.round_prefix, args.training_rounds or stroke_model.DEFAULT_ROUNDS)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Study      : {report['study_id']}")
    print(f"Condition  : {report['condition']['display_name']}")
    print(f"Engine     : {report['engine']}")
    print(f"Model used : {report['model_in_use']['version']} ({report['model_in_use']['kind']})")
    print(f"Notice     : {report['notice']}")
    print()

    rounds = report["rounds"]
    ordered = [("analytics", rounds["analytics"]), ("evaluation", rounds["evaluation"])]
    ordered += [(f"training {index}", item) for index, item in enumerate(rounds["training"], start=1)]
    ordered.append(("candidate validation", rounds["candidate_validation"]))
    for name, round_report in ordered:
        if round_report is None:
            continue
        plan = round_report["plan"]
        print(f"[{name}] {round_report['round_id']}  model {round_report['model_version']}")
        selected, required = len(plan["selected_sites"]), plan["required_participants"]
        print(f"  quorum {plan['quorum_met']} ({selected} selected, {required} required)")
        for exclusion in plan["excluded_sites"]:
            print(f"  excluded: {exclusion['site_id']} - {exclusion['reason']}")
        for result in round_report["results"]:
            norm = result["update_norm"]
            norm_text = f"  norm {norm:.4f}" if isinstance(norm, float) and name.startswith("training") else ""
            examples = result["contributed_examples"]
            print(f"  {result['site_id']:<8} {result['participation']:<9} examples {examples:>4}{norm_text}")
        readiness = round_report.get("readiness")
        if readiness:
            print(f"  aggregation permitted: {readiness['aggregation_permitted']}")
            for rejection in readiness["rejected_contributions"]:
                print(f"  rejected: {rejection['site_id']} - {rejection['reason']}")
        print()

    if report["training_curve"]:
        print("Training: log loss at the sites, weighted by the planner's weights")
        for point in report["training_curve"]:
            print(
                f"  round {point['round']}: started at {_number(point['start_loss'])}, "
                f"produced {_number(point['loss'])}, largest update norm {_number(point['max_update_norm'])}"
            )
        print()

    candidate = report.get("candidate")
    if not candidate:
        print(f"No candidate was produced: {report.get('stop_reason') or 'training did not reach aggregation'}.")
        return 0

    print(f"Candidate  : {candidate['candidate_version']} ({candidate['status']})")
    print(f"  digest   : {candidate['artifact_digest']}")
    print(f"  sites    : {', '.join(candidate['contributing_sites'])}")
    print(f"  examples : {candidate['contributed_examples']}")
    print("  metrics (candidate, then the model in use):")
    for key in sorted(candidate["metrics"]):
        baseline = report["baseline_metrics"].get(key)
        print(f"    {key:<22} {candidate['metrics'][key]:.4f}   {_number(baseline)}")
    print()

    gates = report["release"]["gates"]
    print("Release gates")
    for result in gates["gate_results"]:
        marker = {"passed": "PASS", "failed": "FAIL", "not_evaluated": "NONE"}[result["outcome"]]
        print(f"  [{marker}] {result['gate_id']:<32} {result['detail']}")
    print()
    print(f"Decision   : {gates['action'].upper()}")
    for failure in gates["blocking_failures"]:
        print(f"  blocking : {failure}")
    if gates["action"] != "release":
        print(f"  the hospitals keep using {gates['rollback_version']}")
    else:
        print("  the candidate stays a candidate until a person approves it")
    print()

    print("Audit")
    for site_id, audit in report["site_audits"].items():
        print(f"  {site_id:<12} chain_intact={audit['chain_intact']}  decisions={audit['record_count']}")
    coordinator = report["coordinator_audit"]
    print(f"  {'coordinator':<12} chain_intact={coordinator['chain_intact']}  decisions={coordinator['record_count']}")
    print()
    print(report["dataset_notice"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
