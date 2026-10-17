# Step 9: Test your integration

A test that passes must show that your integration works. This page lists the
rules Dagents follows for its own tests, and shows how the worked example's test
follows them.

## 1. Test against the real planner

Every Guard and round decision comes from `dagentsc`. A test with a stub planner
only shows that your code calls something. Run your governance and federation
tests against the real binary.

Find the planner, and skip with a clear message when it is missing:

<!-- source: tests/test_getting_started_example.py -->
```python
def find_planner() -> str | None:
    """The planner the scripts should use: DAGENTSC_BIN, then PATH, then the dune build."""
    configured = os.environ.get("DAGENTSC_BIN")
    if configured and os.path.isfile(configured):
        return configured
    on_path = shutil.which("dagentsc")
    if on_path:
        return on_path
    if DUNE_BUILT_BINARY.is_file():
        return str(DUNE_BUILT_BINARY)
    return None


PLANNER = find_planner()
```

<!-- source: tests/test_getting_started_example.py -->
```python
@unittest.skipUnless(PLANNER, "dagentsc binary is not available")
class ExampleRunTests(unittest.TestCase):
```

Use the exact reason `dagentsc binary is not available`. The CI guard in
section 3 recognises it.

This test runs the example in a clone, so it looks in `DAGENTSC_BIN`, on `PATH`
and in the dune build folder. A test in an application that installs the
package can ask the framework instead: `find_planner()` in
`dagents.common.infrastructure.dagents_runner` returns the planner's path and
the rule that found it (`DAGENTSC_BIN`, `bundled`, `PATH` or `none`), in the
order the Guard uses.

## 2. Test the refusals

The Guard fails closed, and your tests should prove it for your own code paths.
This test runs the example without a planner and expects three denials:

<!-- source: tests/test_getting_started_example.py -->
```python
class FailClosedTests(unittest.TestCase):
    """Without a planner the example's Guard denies every read."""

    def test_the_guard_denies_every_read_without_the_planner(self) -> None:
        result = run_script("examples/getting-started/step2_guard_check.py", "/nonexistent/dagentsc")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, result.stdout.count("The planner could not be reached, so the Guard denied the request."))
        self.assertNotIn("permitted True", result.stdout)
```

Also test, for your application:

- a request whose purpose is not approved is denied;
- a table request below the minimum cohort is denied;
- a source on another contract version is excluded from a round;
- a release gate whose metric is missing gives `reject`;
- one source's store cannot read another source's records (Step 3).

## 3. Count the skips

A suite that skipped its governance tests is green without testing anything.
Run the example's tests with the planner:

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest tests.test_getting_started_example -v
```

Expected output, the last lines:

```text
Ran 14 tests in 2.664s

OK
```

Without the planner, the seven tests that run the example skip:

```text
Ran 14 tests in 0.279s

OK (skipped=7)
```

The run is still green, so CI reads the log with a guard script that fails on
a forbidden skip:

```bash
.venv/bin/python -m unittest tests.test_getting_started_example -v 2>&1 | tee example.log
.venv/bin/python scripts/ci_no_skipped_tests.py --forbid planner \
  --require-ran tests.test_getting_started_example.ExampleRunTests example.log
```

With the planner missing, the guard exits with status 1. Its output, with the
list of skipped tests shortened:

```text
example.log: 6 planner skips, because the OCaml planner (dagentsc) was not reachable. The job downloads the planner artifact and sets DAGENTSC_BIN, so check those steps.
  test_every_step_runs (tests.test_getting_started_example.ExampleRunTests.test_every_step_runs) ... skipped 'dagentsc binary is not available'
  ...
example.log: no test from tests.test_getting_started_example.ExampleRunTests ran; all of its tests skipped.
```

`--require-ran` also fails when a test class did not run at all, for example
because it was renamed. Use the same guard in your own CI, with
`--forbid database` for tests that need a database.

## 4. Run the same assertions against every store

If your application has an in-memory store and a Postgres store, write the
tests once and run them against both. The stroke demo's
[`tests/test_site_store.py`](../../apps/healthcare-demo/tests/test_site_store.py)
defines the assertions in one class and runs them for `MemorySiteStore` and
`PostgresSiteStore`. The Postgres run needs a database URL, and skips with a
reason that starts `database test skipped` without one (Step 3). Two
implementations tested with different assertions drift apart. Dagents has had
that bug.

## 5. Keep your documentation in step with your code

The worked example's test also checks this guide:

- every code block marked `<!-- source: PATH -->` must appear, line for line,
  in that file;
- every Python block in the guide must have such a marker;
- every output block marked `<!-- output: PATH -->` must equal what the script
  prints with the real planner;
- every file of the example must appear in the guide at least once.

If you change the example, run the test and copy the new code or output into
the block it names. Apply the same idea to your own documentation: a copy of
code that no test checks goes out of date.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `OK (skipped=7)` in CI | The job has no planner | Download or build `dagentsc` in the job and set `DAGENTSC_BIN`; run the guard script so a skip fails |
| `this block differs from its source file` | The example changed and the guide did not | Copy the block again from the file |
| `run the script and paste its output` | The script's output changed | Run the script and replace the output block |
| A Postgres test class shows no tests run | It skipped in `setUpClass` | Add it to `--require-ran` in your CI |

Next: [Checklist and common mistakes](10-checklist.md).
