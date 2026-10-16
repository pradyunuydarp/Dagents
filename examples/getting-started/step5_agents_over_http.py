"""Step 5: run the round of step 4 again, this time through the LMA and GMA APIs."""

from agents.common.domain.federation import AggregationMethod, ModelVersionRegistration, RoundManifest, SiteResult
from agents.common.infrastructure.agent_client import AgentError, CallLog

from shop import rule
from shop.agents import build_agents
from shop.contract import CLASSIFICATION, CONDITION, CONTRACT_VERSION
from shop.coordinator import STUDY_ID
from shop.store import SITE_IDS

agents = build_agents()
print(f"Agents: {', '.join(agents.network.agents)}, called {agents.network.mode}")
gma = agents.gma()
with CallLog() as calls:
    # 1. Register the rule with the GMA. The GMA sends it with every job, and
    #    each LMA checks its digest before it reads an order.
    version = gma.register_model_version(
        rule.RULE_VERSION, ModelVersionRegistration(kind=rule.RuleKind.kind, params=rule.RULE_PARAMETERS)
    )
    print(f"Registered {version.version} as a {version.status}, digest matches the rule: {version.digest == rule.RULE_DIGEST}")

    # 2. The GMA asks each store's LMA for its enrolment, with its current order count.
    for registration in gma.refresh_sites():
        print(f"  enrolled {registration.site_id}: {registration.cohort_size} orders, {registration.feature_contract_version}")

    # 3. The same round contract as step 4, under a new round id. The GMA plans
    #    the round, then sends each selected store its job in one call: the
    #    store checks the job and runs it when it accepts.
    manifest = RoundManifest(
        round_id="returns-eval-http-1",
        study_id=STUDY_ID,
        condition_id=CONDITION.condition_id,
        phase="evaluation",
        model_version=rule.RULE_VERSION,
        model_artifact_digest=rule.RULE_DIGEST,
        training_code_digest=rule.RULE_DIGEST,
        feature_contract_version=CONTRACT_VERSION,
        privacy_profile="guarded-rows",
        aggregation=AggregationMethod(kind="fedavg"),
        minimum_participants=2,
        minimum_cohort_per_site=CLASSIFICATION.minimum_cohort,
        required_capabilities=["evaluate"],
        stop_conditions=["schema_failure", "quorum_not_met"],
        invited_sites=list(SITE_IDS),
    )
    record = gma.dispatch_round(manifest)
    print(f"Round {record.manifest.round_id}: {record.status}, selected {', '.join(record.plan.selected_sites)}")
    for call in sorted(record.dispatch, key=lambda item: item.site_id):
        print(f"  {call.site_id} {call.step} {call.status_code} {call.outcome}")

    # 4. What each store's LMA sent back: counts and metrics, never orders.
    for result in record.results:
        metrics = ", ".join(f"{name} {value:.3f}" for name, value in sorted(result.metrics.items()))
        print(f"  {result.site_id}: {result.participation}, {result.contributed_examples} examples, {metrics}")

    # 5. The GMA takes results only from the stores the round selected.
    try:
        gma.submit_result(
            manifest.round_id,
            SiteResult(
                round_id=manifest.round_id,
                site_id="store-east",
                job_digest=record.plan.round_digest,
                participation="completed",
                code_verified=True,
                privacy_checks_passed=True,
                contributed_examples=500,
                metrics={"precision": 1.0, "recall": 1.0, "flag_rate": 0.1},
            ),
        )
    except AgentError as error:
        print(f"A result from store-east, which the round did not select: {error.status} {error.code}")

    # 6. The planner decides whether the results may be combined; the GMA
    #    makes the candidate. A candidate is not a release.
    readiness = gma.readiness(manifest.round_id)
    weights = ", ".join(f"{site} {weight:.3f}" for site, weight in readiness.site_weights.items())
    print(f"Aggregation permitted: {readiness.aggregation_permitted} (weights: {weights})")
    candidate = gma.aggregate(manifest.round_id)
    metrics = ", ".join(f"{name} {value:.3f}" for name, value in sorted(candidate.metrics.items()))
    print(f"Candidate {candidate.candidate_version}: {metrics}")

    # 7. What each store reports to the GMA about itself: counts, the return
    #    rate only when the store's own Guard permits it, and its audit chain.
    print("What the stores report to the GMA:")
    for entry in gma.site_summaries(include_audit=True):
        summary, audit = entry.summary, entry.audit
        rate = f"{summary.label_rate:.3f}" if summary.label_rate is not None else "withheld"
        print(
            f"  {entry.site_id}: {summary.record_count} orders, {summary.labelled_count} with a known outcome, "
            f"return rate {rate} ({summary.label_rate_status}), "
            f"audit chain intact {audit.chain_intact} with {audit.record_count} records"
        )

    # 8. Each store reads its own job log from its own LMA, with its own token.
    print("What each store's job log holds:")
    for site_id in SITE_IDS:
        for job in agents.lma(site_id).job_log():
            print(f"  {site_id}: {job.round_id} {job.phase}, {job.participation}, {job.contributed_examples} examples")

# Every call this script made: the agent, the method, the route and the status.
# The calls the GMA made to the LMAs are the dispatch rows of step 3.
print(f"API calls ({len(calls.calls)}):")
for call in calls.calls:
    agent = "GMA" if call.service == "gma" else f"LMA {call.site_id}"
    print(f"  {agent} {call.method} {call.path} {call.status}")
agents.close()
