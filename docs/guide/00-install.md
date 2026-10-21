# Install the package

Dagents is on PyPI, the Python Package Index, as
[`dagents`](https://pypi.org/project/dagents/). This page installs it into your
own project. At the end, `python -m dagents` finds the planner, and an LMA and a
GMA answer their health checks.

The package is the `dagents/` folder of the repository: the LMA and GMA, the
Guard, the round controller and its worker, the extension interface and the
planner runner. Everything else is only in the repository:

| You want to | Use |
|---|---|
| Use Dagents in your own application (paths B and C in the [overview](README.md#three-ways-to-build-on-dagents)) | The package, from this page |
| Follow the worked example of this guide | The package and a clone of the repository ([section 5](#5-run-the-worked-example-on-the-package)), or a clone alone ([Step 1](01-setup.md)) |
| Call the framework services (path A), run the demos or change Dagents | A clone of the repository ([Step 1](01-setup.md)) |

## What you need

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 or later | The package |
| curl | any | Calling the agents on this page |
| git | any | Cloning the repository for the worked example ([section 5](#5-run-the-worked-example-on-the-package)) |

On Linux x86_64, Linux aarch64 and macOS arm64 you do not need OCaml, because
the package includes the planner. Other platforms need a planner built from the
repository ([section 2](#2-check-that-the-package-has-a-planner)).

## 1. Install the package

Create a virtual environment in your project and install the package with the
extras you need. `python3 --version` must print 3.11 or later. If it prints an
older version, use `python3.11` or a later version in place of `python3`.

```bash
python3 -m venv .venv
.venv/bin/pip install "dagents[lma,gma]"
```

An extra is an optional group of dependencies. The package alone needs only
pydantic. Each extra adds what one part of the framework needs:

| Install | Adds | Needed for |
|---|---|---|
| `pip install dagents` | pydantic | The Guard, the round controller and its in-process simulator, the extension registry and the planner runner, used as Python classes (path C) |
| `pip install "dagents[lma]"` or `"dagents[gma]"` | FastAPI, uvicorn and httpx | Serving an LMA or a GMA, and the client the agents use to call each other (path B) |
| `pip install "dagents[postgres]"` | psycopg 3 | The Postgres audit chain, job log, model registry, lease and source adapter |
| `pip install "dagents[charts]"` | matplotlib | The SVG chart renderer |
| `pip install "dagents[compose]"` | PyYAML | The appliers that start a service from a Docker Compose file that core-service rendered |
| `pip install "dagents[all]"` | All of the above | Everything in the package |

Extras combine, as in `pip install "dagents[lma,postgres]"`.

### Versions

Every version before 1.0.0 is a pre-release. The first, 0.1.0a1, is an alpha.
Before 1.0.0, the Python APIs, settings, routes and package layout can change
from one release to the next, so pin the version you tested:

```bash
.venv/bin/pip install "dagents[lma,gma]==0.1.0a1"
```

While every release is a pre-release, `pip install dagents` installs the newest
one. After 1.0.0, a pre-release needs `pip install --pre dagents` or an exact
version. The [GitHub Releases](https://github.com/pradyunuydarp/Dagents/releases)
page lists every release.

This guide describes the code on the repository's `main` branch, which can be
newer than the newest release.

## 2. Check that the package has a planner

The planner, `dagentsc`, makes every decision: what a request may see, which
sources may join a round and whether a model may be released. The Python code
starts it as a separate program.

When PyPI has a wheel for your platform, pip installs it. A wheel is a
ready-built file of the package for one platform. Otherwise pip builds the
package from its source distribution, which holds the Python code and no
planner. Whether you get a planner depends on which of the two pip used:

| Platform | Planner |
|---|---|
| Linux x86_64 and Linux aarch64 | In the wheel |
| macOS arm64 (Apple silicon), macOS 11 or later | In the wheel |
| Any other platform, such as Windows or macOS on Intel | None. pip builds the package from its source distribution |

The Linux planner is statically linked, so it needs no system library. pip
installs the Linux wheel when the system's C library is glibc 2.17 or later,
as on current Debian, Ubuntu and Fedora releases, or musl 1.1 or later, as on
Alpine.

Check what you installed:

```bash
.venv/bin/python -m dagents
```

When the wheel has the planner, the command prints the installed version, the
planner's path, the SHA-256 of the planner file and the limits of each planner
call (the path and the hash are shortened here):

```text
dagents 0.1.0a1
planner: .../site-packages/dagents/_bin/dagentsc (the planner bundled with this package)
planner sha256: ...
planner limits: 10 seconds per call, 16777216 bytes of input, 16777216 bytes of output
```

The hash is the one the release's `SHA256SUMS` file lists for your platform's
planner, because the wheel holds that same file. 0.1.0a1 prints only the first
two lines; the hash and the limits come with the next release.

Without a planner it prints:

```text
dagents 0.1.0a1
planner: none found. Set DAGENTSC_BIN, install a platform wheel, or put dagentsc on PATH.
Without a planner the Ethical Guard denies every request.
```

The command exits with status 0 when it finds a planner it can run, and 1 when
it does not. It also exits with status 1 when a planner setting is wrong
([Step 8, Planner settings](08-deploy.md#planner-settings)). It starts nothing
and reads no data except the planner file, which it hashes, so you can run it in a
container build or a CI job to stop when the planner is missing.

### Use a planner from somewhere else

The framework looks for the planner in this order:

1. `DAGENTSC_BIN`, when it is set. A path that does not exist is still used,
   so a wrong setting fails instead of quietly running another planner.
2. The planner in the package.
3. `dagentsc` on `PATH`.

On a platform without a planner in the wheel, build it from the repository with
OCaml, as in [Step 1, section 2](01-setup.md#2-build-the-planner), and set
`DAGENTSC_BIN` in every process that uses Dagents:

```bash
export DAGENTSC_BIN=/path/to/Dagents/bindings/ocaml/_build/default/bin/dagentsc.exe
.venv/bin/python -m dagents
```

Each GitHub Release also has the planners for the three platforms in the table,
as the files `dagentsc-linux-x86_64`, `dagentsc-linux-aarch64` and
`dagentsc-macos-arm64`, with their SHA-256 checksums in `SHA256SUMS`. To use
one, download it with the checksum file, check it, make it executable and set
`DAGENTSC_BIN` to its path. Take the files from the release whose version you
installed:

```bash
PLANNER=dagentsc-linux-x86_64   # or dagentsc-linux-aarch64, or dagentsc-macos-arm64
curl -LO https://github.com/pradyunuydarp/Dagents/releases/download/v0.1.0a1/$PLANNER
curl -LO https://github.com/pradyunuydarp/Dagents/releases/download/v0.1.0a1/SHA256SUMS
grep " $PLANNER\$" SHA256SUMS | sha256sum -c -
chmod +x $PLANNER
export DAGENTSC_BIN=$PWD/$PLANNER
.venv/bin/python -m dagents
```

`sha256sum -c` prints `dagentsc-linux-x86_64: OK` when the file matches its
checksum. On macOS, if `sha256sum` is missing, use `shasum -a 256 -c -` in its
place. A downloaded file cannot run until `chmod +x` makes it executable.
Before that, `python -m dagents` reports that `DAGENTSC_BIN` names a file it
cannot run.

Without a planner, the Ethical Guard denies every request. An LMA or a GMA
without one still starts and answers `/health`, and logs a warning with the
reason when it starts.

## 3. Start an LMA and a GMA

The package installs two commands, `dagents-lma` and `dagents-gma`. They need
the `lma` or `gma` extra. Without it, the command prints how to install the
extra and exits with status 2. Start each command in its own terminal:

```bash
# Terminal 1: the LMA, on 127.0.0.1:8010
.venv/bin/dagents-lma

# Terminal 2: the GMA, on 127.0.0.1:8020
.venv/bin/dagents-gma
```

Check that each one answers:

```bash
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8020/health
```

Expected output:

```text
{"status":"ok","agent_type":"LMA","deployed_bundles":0,"runs":0,"model_runs":0,"active_bundle":null}
{"status":"ok","agent_type":"GMA","engine":"in-process-simulator"}
```

Each command takes `--host`, `--port` and `--log-level`. Without the flags, the
LMA listens on `LMA_API_HOST` and `LMA_API_PORT`, and the GMA on `GMA_API_HOST`
and `GMA_API_PORT`. Without those, each listens on `127.0.0.1` and its own
port. `python -m dagents.lma` and `python -m dagents.gma` do the same as the
commands.

Started with no settings, the LMA serves no site: it has profiling, model runs
and the Guard, and its site and job routes answer `404`. The GMA runs rounds
with the in-process simulator. Neither checks tokens, and each logs a warning
that says so. Run them this way only on your own machine.

## 4. Configure the agents with environment variables

An installed package reads its settings only from the environment of the
process. It reads no env file. In a clone or a container image built from the
repository, the agents also read `env/.env.shared` and `env/.env.lma` or
`env/.env.gma`. An installed package does not, because any other package
installed in the same `site-packages` folder could add an `env/` folder there
and change an agent's settings.

Set the variables in your shell, in a container's environment or in a service
manager's unit file. These are the ones an LMA needs to serve one source in a
study over HTTP (path B):

| Variable | What it sets |
|---|---|
| `LMA_EXTENSIONS` | Your extension, as `module:attribute`. Several are separated by commas |
| `LMA_SITE_ID`, `LMA_CONDITION_ID` | The source this LMA serves, and the condition pack from your extension it plans against |
| `LMA_SITE_SOURCE` | The site source your extension contributes, which reads the source's records. Required with `LMA_SITE_ID` |
| `LMA_CAPABILITIES` | What the source can do, comma-separated, such as `evaluate`. The names are free text and must match the round's `required_capabilities` exactly. A round selects only the sources that have every capability it requires. Without this variable the LMA registers none, and such a round excludes it with `site lacks required capability ...` |
| `LMA_SITE_TOKEN`, `LMA_COORDINATOR_TOKEN` | The tokens the LMA accepts: `site` for the source's own application, `coordinator` for the GMA |
| `LMA_GMA_ENDPOINT`, `LMA_GMA_TOKEN` | The GMA's address, and the GMA token bound to this source |
| `LMA_REQUIRE_TOKENS` | `1` refuses to start without a token |

And the GMA that coordinates them:

| Variable | What it sets |
|---|---|
| `GMA_EXTENSIONS` | The same extension |
| `GMA_ENGINE` | `http` sends each round's jobs to the LMAs over their APIs. The default, `in-process`, is the simulator |
| `GMA_SITE_ENDPOINTS` | Each LMA's address, as `SITE=URL,...`. The GMA calls only these addresses |
| `GMA_ADMIN_TOKEN` | The token of the person or application that runs the study |
| `GMA_LMA_TOKENS` | `SITE=TOKEN,...`: the token the GMA sends to each LMA, which is that LMA's `LMA_COORDINATOR_TOKEN` |
| `GMA_SITE_TOKENS` | `SITE=TOKEN,...`: the token each LMA sends to the GMA, which is that LMA's `LMA_GMA_TOKEN` |
| `GMA_REQUIRE_TOKENS` | `1` refuses to start without a token |

The [LMA README](../../dagents/lma/README.md#settings) and the
[GMA README](../../dagents/gma/README.md#settings) list every setting with its
default, including the Postgres settings. The agents do not create the Postgres
tables those settings name, and the package has no migrations. Create the
tables first, as the "Postgres tables" section of each README shows.
[Step 8](08-deploy.md#3-configure-from-the-environment) covers configuration
for a deployment.

`dagents-lma` and `dagents-gma` import each extension from the packages
installed in the environment and from `PYTHONPATH`, not from the current
folder. Install your application in the same environment, or put the folder
that holds its modules on `PYTHONPATH`.

## 5. Run the worked example on the package

The worked example is in the repository, not in the package. Clone the
repository, install the package in a virtual environment there, and run the
example's scripts with that environment's Python:

```bash
git clone https://github.com/pradyunuydarp/Dagents.git
cd Dagents
python3 -m venv .venv
.venv/bin/pip install "dagents[lma,gma]"
.venv/bin/python examples/getting-started/step1_register.py
```

Python puts the script's folder on its path, so the script finds the example's
`shop` package, and it imports `dagents` from the installed package, with its
planner. On the three platforms with a planner in the wheel, you need neither
OCaml nor `DAGENTSC_BIN`. `step1_register.py` to `step4_federated_round.py` need
only `pip install dagents`. `step5_agents_over_http.py` also needs the `lma` and
`gma` extras.

The later pages are written for a clone without the package, so their commands
put the clone's root on Python's path. On this route, leave the root out:

| The pages write | On this route, write |
|---|---|
| `PYTHONPATH=. .venv/bin/python examples/getting-started/step1_register.py` | `.venv/bin/python examples/getting-started/step1_register.py` |
| `PYTHONPATH=.:examples/getting-started ...` | `PYTHONPATH=examples/getting-started ...` |
| `.venv/bin/uvicorn dagents.lma.main:app --port 8010` | `.venv/bin/dagents-lma --port 8010` |

With the root on the path, Python imports `dagents` from the clone, which has
no planner inside, and the Guard denies every request unless `DAGENTSC_BIN` is
set. `uvicorn`, `python -m` and `python -c` put the current folder on the path
themselves. So in the clone's root, start the agents with `dagents-lma` and
`dagents-gma`, and run `python -m dagents` from another folder.

For example, this LMA loads the example's extension and still imports
`dagents` from the package:

```bash
LMA_EXTENSIONS=shop.extension:ReturnsExtension PYTHONPATH=examples/getting-started \
  .venv/bin/dagents-lma --port 8010
```

`curl -s http://127.0.0.1:8010/api/v1/extensions` then lists what the extension
contributes, as in
[Step 2](02-describe-your-data.md#registering-with-the-lma-and-gma-over-http).

The example on `main` is written for the code on `main`. To run it as it was
at the release you installed, check out that release's tag, such as
`git checkout v0.1.0a1`.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| pip prints `(from versions: none)` and `No matching distribution found for dagents`. pip 23 and later first print a line that ends with `Requires-Python >=3.11` | The virtual environment uses Python 3.10 or older. `.venv/bin/python --version` shows which | Create the environment again with Python 3.11 or later, for example `python3.11 -m venv --clear .venv`, then install again |
| `python -m dagents` prints `planner: none found` and exits with status 1 | There is no wheel with a planner for your platform, `DAGENTSC_BIN` is not set, and `dagentsc` is not on `PATH` | Download or build the planner and set `DAGENTSC_BIN` (section 2) |
| `planner: DAGENTSC_BIN names /opt/dagentsc, which is not a file this process can run.` | `DAGENTSC_BIN` names a missing file, or one without permission to run | Correct the path. For a planner you downloaded, run `chmod +x` on it (section 2). Or unset `DAGENTSC_BIN` to use the planner in the package |
| An agent answers `503` with the code `store_not_set_up` | A table that a Postgres setting names does not exist. In 0.1.0a1 the message says to apply the migrations, but the package has none | Create the tables in the "Postgres tables" section of the [LMA README](../../dagents/lma/README.md#postgres-tables) or the [GMA README](../../dagents/gma/README.md#postgres-tables) |
| `The LMA needs uvicorn and FastAPI. Install them with: pip install 'dagents[lma]'` | The package was installed without the `lma` extra (or `gma` for the GMA) | Install the extra |
| `ExtensionError: extension spec 'shop.extension:ReturnsExtension': cannot import module shop.extension: No module named 'shop'` | The agent imports extensions from the environment and `PYTHONPATH`, not from the current folder | Install your application in the environment, or add its folder to `PYTHONPATH` (section 4) |
| A value in an `env/` file has no effect | An installed package reads no env file | Set the variable in the process's environment |
| In a clone, every Guard decision is `deny` with "The planner could not be reached, so the Guard denied the request.", or `python -m dagents` finds no planner | `PYTHONPATH=.`, `uvicorn`, `python -m` or `python -c` in the clone's root made Python import `dagents` from the clone, which has no planner inside | Leave the clone's root off the path (section 5), or set `DAGENTSC_BIN` |

Next: [Step 1: Set up](01-setup.md) works from a clone and starts the framework
services. If you ran the worked example in section 5, you can go on to
[Step 2: Describe your data](02-describe-your-data.md).
