# Repository development

[中文](../cn/development.md)

Run repository commands from the checkout root. Python 3.11 or newer and uv are required.

See [Contributing](../../CONTRIBUTING.md) for pull requests and routine Python and Studio Web checks.

## Install the workspace

```bash
uv sync --locked --all-packages --group dev
```

Development dependencies are grouped by task: `test` provides the test runner and
contract fixtures, `lint` provides Ruff and Pyright, `integration` provides Docker
and database clients, and `packaging` provides wheel verification tools. `dev`
includes all four groups. These groups are not published as package dependencies.

Use `--no-default-groups --group NAME` to install selected groups. The current
package/Studio suites need `test`, `lint`, and `integration`: type-contract tests
invoke Pyright, and shared fixture collection imports the integration clients.
Installing those clients does not start Docker services. The packaging suite can
run independently with `--noconftest` after installing the `packaging` group and
all workspace packages. Use `uv run --no-sync` after a selective installation to
keep the selected environment.

### Use the Tsinghua mirror locally

The default installation uses official PyPI. To use the mirror locally, run these commands from the repository root. For a first installation, create `.venv` with `uv venv` first:

```bash
uv export --locked --all-packages --group dev --output-file /tmp/tinkerfin-dev-requirements.txt > /dev/null
uv pip sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple /tmp/tinkerfin-dev-requirements.txt
```

This installs locked versions and verifies the recorded package hashes without changing `uv.lock` or the global index configuration.

## Validate Studio Web

Run `pnpm test:browser` from `apps/studio/web` to build and run the browser suite.
To run a selected test file directly, build the application first:

```bash
pnpm build
pnpm exec playwright test tests/browser/todo-trace.spec.ts --workers=1
```

## Build wheels

The shared build command produces the ten framework wheels and the Studio server wheel:

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py --out-dir dist
```

Choose an output directory without existing wheels. To build selected projects, pass their
repository-relative paths:

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py \
  packages/tinkerfin-contracts packages/tinkerfin-native-stream \
  --out-dir dist/selected
```

The command copies current project files into temporary directories, including uncommitted
edits and untracked source files. It excludes build directories, caches, and generated
package metadata. Builds do not change or remove those files in the working tree.
Avoid editing source files while preparing a release so that all projects use one consistent
set of inputs.

Every wheel must match its copied source files byte for byte, including Python modules,
stubs, typing markers, and package resources. All selected projects pass this check before
any wheel is written to the output directory. A missing, extra, or changed file fails the
command. Add `--offline` when build requirements are already present in the uv cache.

Local builds, packaging tests, and the Studio Dockerfile use this entry point.
The Dockerfile exports locked production dependencies and builds wheels inside Docker.

## Validate packaging

```bash
mkdir -p .cache
uv export --locked --all-packages --no-dev --group packaging --no-emit-workspace \
  --no-header --output-file .cache/test-requirements.txt
uv run --no-sync python -m pip download --require-hashes --no-deps --only-binary=:all: \
  -r .cache/test-requirements.txt --dest .cache/test-wheels
uv run --locked --no-sync python -m pytest --noconftest tests/packaging -m packaging_e2e
```

The suite checks contaminated build directories, current source contents, wheel metadata,
licenses, declared dependencies, and installation into isolated environments. CI runs the
core installation cases on Python 3.11–3.14. Python 3.11 also runs every optional dependency
combination and the complete Studio deployment wheel set.


The preparation commands download wheels for the current Python and platform and
verify their lockfile hashes. Isolated installations then run offline against
`.cache/test-wheels`, with no package index access. Run preparation again when the
lockfile, Python version, or platform changes. The local wheelhouse also contains
the tools required by isolated builds.

## Validate Docker integrations

Start Docker, then run tests that create and clean up their own disposable services:

```bash
uv run pytest -m docker_integration
```

The main quality workflow runs on pushes and pull requests and excludes Docker
integration tests by default. Run them locally with the command above when you need
to validate against real services.
