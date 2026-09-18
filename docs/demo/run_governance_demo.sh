#!/usr/bin/env bash
# Governance and federation demo. No Docker, no database, no model download.
#
# Two things are worth watching:
#   1. The same request resolves to a different protection strategy as the
#      requester's trust and the granularity change, and the decision comes
#      from the typed planner rather than from any service's own code.
#   2. A federated pilot produces a candidate that beats its baseline and is
#      still rejected, because two safety gates failed.
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

rule "1. The Ethical-Restriction Rails: one classification, four requests"
echo "Each call below changes exactly one thing. The strategy changes with it."

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
    "fieldSensitivity": {"nihss_total": "high"},
    "defaultSensitivity": "high",
    "regulations": ["HIPAA"],
    "minimumCohort": 20
  },
  "requestedFields": ["nihss_total"],
  "granularity": "${granularity}",
  "cohortSize": ${cohort},
  "declaredPurpose": "stroke_triage_research",
  "approvedPurposes": ["stroke_triage_research"]
}
JSON
}

probe "verified coordinator, one row"            true  row          140
probe "the same request, unverified"             false row          140
probe "verified, asking for a whole column"      true  column       140
probe "verified, sending a model update"         true  model_update 140
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
    "roundId": "round-0042", "studyId": "stroke-triage", "conditionId": "suspected_stroke",
    "phase": "training", "modelVersion": "seed-v3",
    "featureContractVersion": "stroke-triage-features-v2",
    "aggregation": {"kind": "secure_aggregation", "threshold": 3},
    "minimumParticipants": 2, "minimumCohortPerSite": 50,
    "requiredCapabilities": ["local_training"],
    "invitedSites": ["hospital-a", "hospital-b", "hospital-c", "hospital-d"]
  },
  "registrations": [
    {"siteId": "hospital-a", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v2", "approvedConditions": ["suspected_stroke"], "cohortSize": 420, "enrolled": true},
    {"siteId": "hospital-b", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v2", "approvedConditions": ["suspected_stroke"], "cohortSize": 310, "enrolled": true},
    {"siteId": "hospital-c", "capabilities": ["local_training"], "featureContractVersion": "stroke-triage-features-v1", "approvedConditions": ["suspected_stroke"], "cohortSize": 280, "enrolled": true},
    {"siteId": "hospital-d", "capabilities": [], "featureContractVersion": "stroke-triage-features-v2", "approvedConditions": ["suspected_stroke"], "cohortSize": 90, "enrolled": true}
  ]
}
JSON
echo
echo "  The secure-aggregation threshold of 3 overrode the manifest's stated"
echo "  minimum of 2. That threshold is the reason the coordinator cannot resolve"
echo "  any one site's update, so it is a floor on participation, not a hint."

rule "3. Release gates: a candidate that beats its baseline and is still refused"
cat <<'JSON' | "${DAGENTSC}" federation release evaluate --input - \
  | "${PYTHON}" "${REPO_ROOT}/docs/demo/_format_governance_demo.py" release
{
  "roundId": "round-0042", "candidateVersion": "candidate-v4", "rollbackVersion": "release-v3",
  "gates": [
    {"gateId": "discrimination", "metric": "auc", "comparison": {"kind": "at_least", "value": 0.80}, "blocking": true},
    {"gateId": "improves_on_current", "metric": "auc", "comparison": {"kind": "improves_on_baseline", "margin": 0.01}, "blocking": true},
    {"gateId": "subgroup_fairness", "metric": "subgroup_auc_gap", "comparison": {"kind": "at_most", "value": 0.05}, "blocking": true},
    {"gateId": "alert_burden", "metric": "alerts_per_1000", "comparison": {"kind": "at_most", "value": 400}, "blocking": false}
  ],
  "candidateMetrics": {"auc": 0.82, "subgroup_auc_gap": 0.14, "alerts_per_1000": 258},
  "baselineMetrics": {"auc": 0.78}
}
JSON
echo
echo "  Aggregation creates a candidate. It does not create a release."

rule "4. The same layers, driven by a real app"
if [[ -x "${REPO_ROOT}/apps/healthcare-demo/scripts/run_pilot.sh" ]]; then
  DAGENTS_HOME="${REPO_ROOT}" bash "${REPO_ROOT}/apps/healthcare-demo/scripts/run_pilot.sh" --cohort-size 300
else
  echo "  (healthcare demo not present)"
fi
