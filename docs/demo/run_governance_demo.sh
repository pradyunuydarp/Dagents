#!/usr/bin/env bash
# Governance and federation demo. No Docker, no database, no model download.
#
# What to watch:
#   1. The same request for systolic_bp gets a different protection strategy
#      as the requester's trust and the granularity change. The decision comes
#      from the typed planner, not from any service's own code.
#   2. The planner decides which hospitals may join a round. Here ch-245 is
#      registered with an older feature contract, so two hospitals remain, and
#      the secure-aggregation threshold of 3 means the quorum is not met.
#   3. In a worked example, a candidate that beats its baseline is still
#      refused, because it fails the subgroup fairness gate.
#   4. The real pilot on the three hospitals' International Stroke Trial
#      records. On the committed extract its candidate is rejected: its AUC is
#      higher than the model in use, but not by the 0.02 margin that the
#      improves_on_current_release gate requires.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

DAGENTSC="${DAGENTSC_BIN:-${REPO_ROOT}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ ! -x "${DAGENTSC}" ]]; then
  echo "Building the planner first..."
  (cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe)
fi
export DAGENTSC_BIN="${DAGENTSC}"

PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"

rule() { printf '\n\033[1m%s\033[0m\n' "$*"; }

rule "1. The Ethical-Restriction Rails: one classification, five requests"
echo "Each call below changes one thing from an earlier call. The strategy changes with it."

probe() {
  local label="$1" verified="$2" granularity="$3" cohort="$4"
  printf '\n  %s\n' "${label}"
  cat <<JSON | "${DAGENTSC}" governance restrict --input - \
    | "${PYTHON}" "${REPO_ROOT}/docs/demo/_format_governance_demo.py" restriction
{
  "requestId": "demo",
  "boundary": "before_send",
  "requester": {
    "requesterId": "consortium-gma",
    "requesterKind": "coordinator",
    "complianceHistory": 0.9,
    "attributes": [
      {"attributeId": "verified_identity", "weight": 1.0, "verified": ${verified}},
      {"attributeId": "signed_dua", "weight": 1.0, "verified": ${verified}}
    ]
  },
  "classification": {
    "classificationId": "stroke-triage-phi",
    "fieldSensitivity": {"systolic_bp": "high"},
    "defaultSensitivity": "high",
    "regulations": ["HIPAA Privacy Rule"],
    "minimumCohort": 20
  },
  "requestedFields": ["systolic_bp"],
  "granularity": "${granularity}",
  "cohortSize": ${cohort},
  "declaredPurpose": "stroke_triage_research",
  "approvedPurposes": ["stroke_triage_research"]
}
JSON
}

# 198 is the number of records at ch-245, the smallest of the three hospitals.
probe "verified coordinator, one row"            true  row          198
probe "the same request, unverified"             false row          198
probe "verified, asking for a whole table"       true  table        198
probe "verified, sending a model update"         true  model_update 198
probe "verified, but the cohort is too small"    true  model_update 4

echo
echo "  Nothing above is a branch in a service. Every strategy is an exhaustive"
echo "  match in bindings/ocaml/lib/governance_compiler, so adding a sensitivity"
echo "  level fails to compile until every combination is handled."

rule "2. A federated round: who may take part"
cat <<'JSON' | "${DAGENTSC}" federation round plan --input - \
  | "${PYTHON}" "${REPO_ROOT}/docs/demo/_format_governance_demo.py" plan
{
  "manifest": {
    "roundId": "round-0042", "studyId": "stroke-triage-pilot", "conditionId": "suspected_stroke",
    "phase": "training", "modelVersion": "stroke-outcome-rule-v1",
    "featureContractVersion": "stroke-triage-features-v3",
    "aggregation": {"kind": "secure_aggregation", "threshold": 3},
    "minimumParticipants": 2, "minimumCohortPerSite": 20,
    "requiredCapabilities": ["local_training"],
    "invitedSites": ["uk-134", "it-002", "ch-245"]
  },
  "registrations": [
    {"siteId": "uk-134", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v3", "approvedConditions": ["suspected_stroke"], "cohortSize": 524, "enrolled": true},
    {"siteId": "it-002", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v3", "approvedConditions": ["suspected_stroke"], "cohortSize": 292, "enrolled": true},
    {"siteId": "ch-245", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v2", "approvedConditions": ["suspected_stroke"], "cohortSize": 198, "enrolled": true}
  ]
}
JSON
echo
echo "  Secure aggregation must not run with fewer than 3 sites, so its threshold"
echo "  raised the manifest's minimum of 2 to 3, and the quorum is not met. In the"
echo "  real pilot (step 4) all three hospitals use the v3 contract and take part."

rule "3. Release gates: a candidate that beats its baseline and is still refused"
echo "A worked example with the healthcare demo's five gates and invented metrics."
cat <<'JSON' | "${DAGENTSC}" federation release evaluate --input - \
  | "${PYTHON}" "${REPO_ROOT}/docs/demo/_format_governance_demo.py" release
{
  "roundId": "round-0042", "candidateVersion": "candidate-v4", "rollbackVersion": "stroke-outcome-rule-v1",
  "gates": [
    {"gateId": "discrimination", "metric": "auc", "comparison": {"kind": "at_least", "value": 0.70}, "blocking": true},
    {"gateId": "improves_on_current_release", "metric": "auc", "comparison": {"kind": "improves_on_baseline", "margin": 0.02}, "blocking": true},
    {"gateId": "subgroup_fairness", "metric": "subgroup_auc_gap", "comparison": {"kind": "at_most", "value": 0.10}, "blocking": true},
    {"gateId": "sensitivity_at_alert_threshold", "metric": "sensitivity", "comparison": {"kind": "at_least", "value": 0.80}, "blocking": true},
    {"gateId": "alert_burden", "metric": "alerts_per_1000", "comparison": {"kind": "at_most", "value": 750}, "blocking": false}
  ],
  "candidateMetrics": {"auc": 0.82, "subgroup_auc_gap": 0.14, "sensitivity": 0.86, "alerts_per_1000": 700},
  "baselineMetrics": {"auc": 0.78}
}
JSON
echo
echo "  Aggregation creates a candidate. It does not create a release."

rule "4. The same layers, driven by a real app"
if [[ -x "${REPO_ROOT}/apps/healthcare-demo/scripts/run_pilot.sh" ]]; then
  DAGENTS_HOME="${REPO_ROOT}" bash "${REPO_ROOT}/apps/healthcare-demo/scripts/run_pilot.sh"
else
  echo "  (healthcare demo not present)"
fi
