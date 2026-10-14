# How the API reference is built

This folder holds the prose and the structure of the API reference that the
framework site shows at `#/api`. This page is for contributors: it says where
each fact comes from, how to place a new endpoint, and how to check your work.

## Where each part comes from

| Part | Source | Written by |
|---|---|---|
| Parameters, request bodies, responses, types, defaults, constraints | Each FastAPI app's OpenAPI document (`app.openapi()`), and the Spring controllers and records | `scripts/api_reference.py`, into `docs/reference/api-reference.json` |
| Field descriptions | `Field(description=...)` on pydantic models, or `` - `name`: text `` lines in a class docstring; for Spring, the `@param` lines of each record's and controller method's Javadoc | You, in the code |
| Endpoint summary | The first line of the handler's docstring, or of the Spring controller method's Javadoc | You, in the code |
| Spring error responses | `400` for any request body (Spring's own error body); `422` when the body is `@Valid` or the handler or the service it calls throws `IllegalArgumentException`, which each service's `ApiExceptionHandler` maps | `scripts/api_reference.py` |
| Handler, its file, and the classes it calls | The routing table and the handler's source | `scripts/api_reference.py` |
| dagentsc commands, their flags and defaults (read from each handler's `arg_value` calls, not the usage text), the library functions they call, the types they read and write | `bindings/ocaml/bin/dagentsc.ml` | `scripts/api_reference.py` |
| Modules, interfaces, which endpoint belongs where, descriptions, side effects, diagrams | The Markdown files in this folder | You |
| Example requests and responses | Real calls, made by `scripts/capture_api_examples.py` | You declare the requests; the script makes them |
| The endpoint list | `docs/reference/service-inventory.json` | `scripts/service_inventory.py` |

Put a fact in the code when the code can hold it. A field description in
`Field(description=...)` also appears on the service's own `/docs` page, and
it cannot drift from the field.

## The files

| File | What it holds |
|---|---|
| `overview.md` | Conventions shared by every service: addresses, versions, errors, limits. The site shows it at `#/api`. |
| `agents.md` | The LMA and the GMA |
| `framework-services.md` | core-service, pipeline-service, model-service |
| `spring-services.md` | spring-control-service, spring-core-service |
| `demo-apps.md` | The healthcare demo backend and the NL2SQL demo backend |
| `dagentsc.md` | The planner program and its commands |
| `examples/<service>.requests.json` | The example requests for one service, written by you |
| `examples/<service>.json` | The captured requests and responses. Generated; do not edit. |

The site lists the module pages in the order of `MODULES` in
`site/src/api/modules.ts`. A new module file must be added there too; a test
checks it.

## Addresses on the site

Other pages link to the reference with these routes. They are stable as long as
the heading or the path they come from does not change.

| Route | Shows |
|---|---|
| `#/api` | The overview and conventions (`overview.md`) |
| `#/api/<module>` | A module page: `agents`, `framework-services`, `spring-services`, `demo-apps`, `dagentsc` |
| `#/api/<module>#<interface>` | One interface. The anchor is the id of its `##` heading, for example `#/api/framework-services#workload-compiler` |
| `#/api/<module>#<endpoint>` | One endpoint. The anchor is the service, method and path joined by hyphens, for example `#/api/framework-services#core-service-post-api-v1-workloads-compile` |
| `#/api/dagentsc#<command>` | One planner command, for example `#/api/dagentsc#dagentsc-governance-restrict` |
| `#/api/<module>#<object>` | One object. The anchor is the id of its `###` heading, for example `#/api/framework-services#the-workloadcomponent-object` |

An endpoint anchor is lower case, with every run of characters other than
letters and digits replaced by one hyphen: `GET /api/v1/workload-plans/{plan_id}`
on core-service is `core-service-get-api-v1-workload-plans-plan-id`.
`docs/reference/api-reference.json` holds each endpoint's `id`.

From a page in `docs/learn/`, link to a module file with a relative path, such
as `[the workload compiler](../reference/api/framework-services.md#workload-compiler)`.
On GitHub the link opens the Markdown file; on the site it opens the module page.

## The format of a module page

A module page is ordinary Markdown with three kinds of fenced block. The site
and the tests read them; on GitHub they show as code.

### An interface

A `##` heading followed directly by an `api-interface` block starts an
interface: a group of endpoints that one router, controller or command group
owns.

````markdown
## Workload compiler

```api-interface
service: core-service
status: complete
code: services/core-service/app/api/routes.py
classes:
- ManifestService (services/core-service/app/services/manifest_service.py)
- InMemoryWorkloadPlanRepository (services/core-service/app/services/manifest_service.py)
endpoints:
- POST /api/v1/workloads:compile
- GET /api/v1/workload-plans/{plan_id}
- POST /api/v1/manifests/pods
```

Prose about the interface: what it is responsible for, and how its endpoints
fit together.
````

| Key | Required | Meaning |
|---|---|---|
| `service` | yes | The service name from the inventory, or `dagentsc` |
| `endpoints` | yes | One `METHOD /path` per line, exactly as the inventory writes it, or `dagentsc <command words>` |
| `code` | no | The files that define the routes. One path, a comma-separated list, or `- ` lines |
| `classes` | no | `Name (path/to/file)` per line: the classes, Spring controllers or OCaml modules that do the work |
| `status` | no | `draft` (the default) or `complete`. A test holds a `complete` interface to the rules in [Done for a module](#done-for-a-module) |

List both paths of an LMA or GMA alias pair (`GET /health` and
`GET /api/v1/health`) in the same interface. The site shows them on one card.

### An endpoint's prose

A `###` heading inside an interface, followed directly by an `api-endpoint`
block that names one of the interface's endpoints, holds that endpoint's prose.
For an alias pair, name either path.

````markdown
### Compile a workload plan

```api-endpoint
POST /api/v1/workloads:compile
```

One paragraph: what the endpoint does, what it stores, which planner command it
runs, which Guard boundary it passes.

**Errors.** When each error status happens.

**Related.** Links to the endpoints and objects it works with.
````

The prose runs to the next `##` or `###` heading. Use bold labels or `####`
headings inside it, not `###`.

### An object

A `###` heading followed directly by an `api-object` block documents one
object, in the style "The WorkloadComponent object". Put objects under a
`## Objects` heading at the end of the page.

````markdown
### The WorkloadComponent object

```api-object
class: app.models.WorkloadComponent
service: core-service
example: core-service compile-a-deployment /manifests/0
```

What the object is and where it is used.
````

| Key | Required | Meaning |
|---|---|---|
| `class` | yes | The dotted import path of the class, as the service imports it. For Spring, the record's full Java name. |
| `service` | yes | The service whose interpreter imports the class |
| `example` | no | `<service> <example id> <JSON pointer>`: a part of a captured response to show as the example |

The generator imports the class and writes its JSON schema, with the field
descriptions, into `api-reference.json`.

## Examples

Declare the requests in `examples/<service>.requests.json`:

```json
{
  "service": "core-service",
  "env": {"SOME_SETTING": "value"},
  "examples": [
    {
      "id": "compile-a-deployment",
      "endpoint": "POST /api/v1/workloads:compile",
      "title": "Compile an LMA Deployment",
      "description": "Optional. One sentence shown above the example.",
      "request": {
        "path": "/api/v1/workloads:compile",
        "query": {"limit": 3},
        "headers": {"Idempotency-Key": "example-1"},
        "json": {"plan_id": "plan-hospital-a", "components": []}
      },
      "status": 200,
      "volatile": ["/plan_id", "/records/*/created_at"],
      "trim": {"items": 3, "chars": 800}
    }
  ]
}
```

- The requests run in order against one fresh app, so a later request can read
  what an earlier one stored. Use fixed ids so the answers repeat.
- Where the service makes the id itself (a run or a job id from the clock), a
  later path can use it: `{from:<example id>:<JSON pointer>}`, for example
  `"/api/v1/runs/{from:run-pipeline:/run_id}"`. The stored example keeps the
  real path for the code samples and the template in `request_template`, and
  the replay compares the template. The page says where the value came from.
- `status` is the status you expect. The capture fails if the answer differs.
- `volatile` lists JSON pointers whose values change on every call, such as an
  id made from the clock. `*` matches any list item or key. The replay check
  ignores them.
- Long answers are trimmed: by default a list keeps 5 items and a string 1,200
  characters. The stored response lists each cut, and the page says so.
- `env` sets environment variables before the app is imported, for example to
  turn off a rate limit.
- An example of an error is as useful as a success. Capture one for each error
  status an endpoint can return.
- Set `"show": false` on a request that only prepares state for a later one.

For `dagentsc`, a request is `{"args": ["governance", "restrict", "--input", "-"], "stdin": {...}}`
and `status` is the exit code.

A service the script cannot import (the Spring services) needs a `runner`: a
command that starts it on a port between 9400 and 9499. `{port}` in the command
or its `env` is replaced with the port.

```json
"runner": {
  "command": ["java", "-jar", "services/spring-services/spring-core-service/target/spring-core-service-0.1.0-SNAPSHOT.jar"],
  "env": {"SERVER_PORT": "{port}"},
  "port": 9460,
  "ready": "/api/v1/health"
}
```

## Commands

Run them from the repository root. The planner must be built, because the
examples call it.

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe

.venv/bin/python scripts/api_reference.py --write          # facts from the code
.venv/bin/python scripts/capture_api_examples.py           # every examples/*.requests.json
.venv/bin/python scripts/capture_api_examples.py --service core-service
.venv/bin/python scripts/capture_api_examples.py --check   # replay; fails if an answer changed

.venv/bin/python -m unittest tests.test_api_reference tests.test_api_examples -v
cd site && npm run build && npm run check
```

Regenerate `api-reference.json` after any change to a route, a model, a
docstring or a module page's `api-object` blocks. Capture again after changing
a request list or the behaviour of an endpoint it calls.

## What the tests check

`tests/test_api_reference.py`:

- `api-reference.json` equals a fresh run of the generator.
- Every endpoint in the inventory, and every dagentsc command, is placed in
  exactly one interface, and every listed endpoint exists.
- The two paths of an alias pair are in the same interface.
- Every file in `code` and `classes` exists, and every class is defined in its file.
- Each `api-endpoint` block names an endpoint of its own interface, once.
- Anchors are unique on each page, and every module file is listed in `MODULES`.
- A `complete` interface meets the rules below.

`tests/test_api_examples.py`:

- Every stored example matches its declared request, its endpoint exists, its
  path fits the endpoint's path template, and its request and response match
  the schemas in `api-reference.json`. This needs no planner and no network.
- With the planner available, every request is made again and the answers must
  equal the stored ones, apart from the volatile values.

`site/check.mjs` opens every API page in Chromium and checks that each endpoint
appears where the hierarchy places it, every diagram draws, every anchor
resolves and the code tabs switch.

## Done for a module

An interface is `complete` when:

1. Every endpoint has an `api-endpoint` section. Its first paragraph says what
   the endpoint does and its side effects: what it stores, which planner
   command it runs, which Guard boundary it passes.
2. Every endpoint has at least one captured example, and every error status it
   can return has an example or a sentence under **Errors**.
3. Its parameters and the fields of its request and response models have
   descriptions in the code (`Field`, `Path`, `Query` or `Header` with
   `description=...`).
4. The objects it takes or returns that a reader needs to understand have an
   `api-object` section.

A module is done when all its interfaces are `complete`, its introduction says
what it is for, where its code is, its ports and how it relates to the other
modules, and it has a Mermaid diagram. `framework-services.md` has the core
service done this way; copy it.

## Adding an endpoint

1. Add the route, with a docstring and `Field(description=...)` on its models.
2. `.venv/bin/python scripts/service_inventory.py --write`
3. Add the endpoint to an interface in the module page, with an `api-endpoint`
   section.
4. Add a request, and an error request if it has errors, to
   `examples/<service>.requests.json`, then capture.
5. `.venv/bin/python scripts/api_reference.py --write`
6. Run the tests and the site check above.
