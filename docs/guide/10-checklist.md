# Checklist and common mistakes

Check each item before your application handles real data.

## Checklist

**Data description**

- [ ] Every field your sources hold is in the feature contract, or stays at the source.
- [ ] Every field in the contract has a sensitivity, and `default_sensitivity` is `high`.
- [ ] `model_update` has a sensitivity, if you run training rounds.
- [ ] The minimum cohort comes from a disclosure-risk review, not from a guess.
- [ ] Your configuration lists the approved purposes, and your code puts that
      list on every `RestrictionRequest` (`approved_purposes`).
- [ ] The condition pack lists the release gates, and says which gates are
      advisory.
- [ ] The extension is registered once, at start-up.

**Stores**

- [ ] Each source has its own store, and no code reads two sources' records together.
- [ ] A Postgres store runs every query through `acting_as`, as the source's role.
- [ ] The coordinator's role has no grant on any source's schema.
- [ ] Each source has its own database login in production.

**Governance**

- [ ] Every read of records goes through `GovernanceService.enforce`.
- [ ] Every requester attribute marked `verified=True` was checked by your application.
- [ ] Each source has its own audit sink, and the coordinator has one for releases.
- [ ] No code path permits a request when the planner fails.

**Rounds and release**

- [ ] Each source computes the manifest digest itself.
- [ ] The runner receives the Guard's payload, not the raw records.
- [ ] A training round sends updates through an `UpdateChannel`, never in a result.
- [ ] The runner puts only aggregate numbers in `metrics`. The Guard does not
      check them before they leave the source.
- [ ] Every metric a release gate names is reported, under the same name.
- [ ] A person approves each release, and the approval is recorded.

**Deployment and tests**

- [ ] `dagentsc` is on `PATH` in every container that decides.
- [ ] The LMA and GMA run with tokens and `LMA_REQUIRE_TOKENS=1` /
      `GMA_REQUIRE_TOKENS=1`. The framework services check no identity, so
      they run on a private network or behind a gateway that authenticates
      callers.
- [ ] No URL or port is written in the code.
- [ ] Governance and federation tests run against the real planner, and CI fails on a skip.
- [ ] Both store implementations pass the same tests.

## Common mistakes

| Mistake | Rule it breaks | Do this instead |
|---|---|---|
| A fallback that permits a request when the planner cannot be reached | The Guard fails closed | Let the Guard deny and record the request; fix the planner |
| Checking sensitivity, trust or quorum in your own Python code | Decisions live in the planner | Call the Guard or the round controller; read the planner's answer |
| Reading every source's records into one list to compute a metric | Records stay at their source | Compute at each source; combine what each source's Guard permits |
| Sending a profile snapshot to the coordinator | What leaves a source goes through `before_send` | Send the payload of a `table` request through the Guard |
| Putting the model update in `SiteResult.metrics` | The update vector never enters a result | Send it through the worker and an `UpdateChannel` |
| Treating `release` as a deployment | Aggregation produces a candidate, never a release | Record a person's approval before any source uses the candidate |
| A session-level `SET ROLE` on a pooled connection | Every database operation runs in one transaction that starts with `SET LOCAL ROLE` | Use `acting_as` |
| Patching a framework file to add your domain | Applications contribute through extensions | Register an extension; if it cannot express what you need, propose a change to the extension interface |
| A domain word, such as a field name, in `agents/` | Nothing in `agents/` knows an application's domain | Keep domain code in your application |
| A test with a stub planner | Governance and federation tests use the real planner | Find the planner and skip with `dagentsc binary is not available`; forbid the skip in CI |
| Marking an attribute verified because it was supplied | `verified` defaults to `False` on purpose | Mark it verified only after your application checked it |

## Where to go next

- The [learning guide](../learn/README.md) explains federated learning and the
  governance model behind these rules.
- The [API reference](../reference/api/overview.md) lists every endpoint, its
  fields and its errors.
- The stroke demo's [README](../../apps/healthcare-demo/README.md) describes a
  complete application with three sources, Postgres stores, training rounds and
  a user interface.
- [`AGENTS.md`](../../AGENTS.md) is the contributor guide, for changes to the
  framework itself.

Back to the [start of this guide](README.md).
