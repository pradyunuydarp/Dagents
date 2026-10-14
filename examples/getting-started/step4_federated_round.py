"""Step 4: run one federated evaluation round and decide whether to release the rule."""

from shop.coordinator import Coordinator
from shop.extension import install
from shop.store import SITE_IDS, load_store
from shop.store_site import StoreSite

install()
coordinator = Coordinator([StoreSite(load_store(site_id)) for site_id in SITE_IDS])

# 1. Plan and dispatch. The planner selects the eligible stores and checks quorum.
manifest = coordinator.manifest("returns-eval-1")
record = coordinator.run_round(manifest)
plan = record.plan
print(f"Round {plan.round_id}, digest {plan.round_digest}")
print(f"  selected: {', '.join(plan.selected_sites)}")
print(f"  excluded: {', '.join(f'{e.site_id} ({e.reason})' for e in plan.excluded_sites) or 'none'}")
print(f"  quorum met: {plan.quorum_met} ({len(plan.selected_sites)} of {plan.required_participants} required)")

# 2. What each store sent back: counts and metrics, never orders.
for result in record.results:
    metrics = ", ".join(f"{name} {value:.3f}" for name, value in sorted(result.metrics.items()))
    print(f"  {result.site_id}: {result.participation}, {result.contributed_examples} examples, {metrics}")

# 3. May the results be combined? The planner decides and gives each store a weight.
readiness = coordinator.controller.evaluate_readiness(plan.round_id)
weights = ", ".join(f"{site} {weight:.3f}" for site, weight in readiness.site_weights.items())
print(f"Aggregation permitted: {readiness.aggregation_permitted} (weights: {weights})")

# 4. The candidate: the weighted metrics. A candidate is not a release.
candidate = coordinator.controller.aggregate(plan.round_id)
metrics = ", ".join(f"{name} {value:.3f}" for name, value in sorted(candidate.metrics.items()))
print(f"Candidate {candidate.candidate_version}: {metrics}")

# 5. The Guard at the release boundary, then the release gates.
guard = coordinator.release_check(plan.round_id, candidate)
strategies = ", ".join(f"{r.field} {r.strategy}" for r in guard.plan.field_restrictions)
print(f"Release Guard: {guard.plan.decision} ({strategies}), permitted {guard.permitted}")
decision = coordinator.decide(plan.round_id, candidate)
for gate in decision.gate_results:
    print(f"  {gate.gate_id:<10} {gate.outcome:<7} {gate.detail}")
print(f"Decision: {decision.action}")
if decision.action == "release" and guard.permitted:
    print("A person on the release committee approves it before the stores use it.")
else:
    print(f"The stores keep using {decision.rollback_version}.")
