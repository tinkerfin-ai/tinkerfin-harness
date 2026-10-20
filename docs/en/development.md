# Repository development

[中文](../cn/development.md)

Run repository commands from the checkout root. Python 3.11 or newer and uv are required.

See [Contributing](../../CONTRIBUTING.md) for pull requests and routine Python and Studio Web checks.

## Install the workspace

```bash
uv sync --locked --all-packages --group dev
```

Development dependencies are grouped by task: `test` provides the test runner and
contract fixtures, `lint` provides Ruff and Pyright, and `packaging` provides package
verification tools. `dev` includes these three groups, which are not published as
package dependencies.

Use `--no-default-groups --group NAME` to install selected groups. Routine Python
and Studio checks need `test` and `lint`. The packaging suite runs independently
with `--noconftest` after installing `packaging` and all workspace packages.
Use `uv run --no-sync` after a selective installation to keep the selected environment.

### Use the Tsinghua mirror locally

The default installation uses official PyPI. To use the mirror locally, run these commands from the repository root. For a first installation, create `.venv` with `uv venv` first:

```bash
uv export --locked --all-packages --group dev --output-file /tmp/tinkerfin-dev-requirements.txt > /dev/null
uv pip sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple /tmp/tinkerfin-dev-requirements.txt
```

This installs locked versions and verifies the recorded package hashes without changing `uv.lock` or the global index configuration.

## Applications and development environments

Applications live in `apps/desktop`, `apps/studio-server` and `apps/studio-web`; framework libraries stay in `packages/`.

After preparing the [server configuration and dependencies](../../apps/studio-server/README.md), manage this worktree's services on macOS or Linux:

```bash
pnpm --dir apps/studio-web install --frozen-lockfile
uv run python -m scripts.dev start
uv run python -m scripts.dev status
uv run python -m scripts.dev stop
```

Choose configuration and ports on first startup with `--env-file`, `--api-port` and `--web-port`; subsequent starts reuse the recorded environment. Each worktree records its own process identities. Occupied ports cause an error without stopping another program. Logs live in `.cache/dev-environment/`.

`start --dependencies` creates isolated Docker dependencies from the selected env file. Assign distinct published database, Redis, MinIO and sandbox ports to each worktree first. Use ordinary `start` with existing external dependencies; the tool never adopts them. `stop` preserves data. `uv run python -m scripts.dev destroy` permanently removes containers, networks and volumes created for this environment while retaining shared images and other environments.

Develop the desktop client with `uv run python -m scripts.desktop_dev`. Server and unrelated web edits reuse the existing development app; desktop or bundled framework edits rebuild it. See the [desktop guide](../../apps/desktop/README.md) for downloads, installation and releases.

## CI and releases

Pull requests and main-branch pushes select checks from source paths and the `uv.lock` dependency graph. Web changes run web checks, server changes run server checks, and shared libraries select their consumers. The stable `Quality gate` aggregates selected jobs; failed or cancelled jobs cannot pass.

Checks and releases are separate. Ordinary pushes do not publish the client. A `desktop-vX.Y.Z` tag or a manual `Desktop release` run builds and publishes the desktop package. `Studio server image` independently verifies the server image and does not upload a desktop app.

## Validate Studio Web

Run `pnpm test:browser` from `apps/studio-web` to build and run the browser suite.
To run a selected test file directly, build the application first:

```bash
pnpm build
pnpm exec playwright test tests/browser/todo-trace.spec.ts --workers=1
```

## Build wheels

The shared build command produces wheels for framework libraries and Python applications:

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
The production Dockerfile builds only the Studio wheel, installs framework packages from
PyPI, and installs third-party dependencies from the lockfile. CI uses an explicit
`candidate` target to verify the current framework source.

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
licenses, declared dependencies, and installation into isolated environments. CI uses
Python 3.14 for the core installation cases, every optional dependency combination,
and the Studio candidate source wheel set.


The preparation commands download wheels for the current Python and platform and
verify their lockfile hashes. Isolated installations then run offline against
`.cache/test-wheels`, with no package index access. Run preparation again when the
lockfile, Python version, or platform changes. The local wheelhouse also contains
the tools required by isolated builds.
