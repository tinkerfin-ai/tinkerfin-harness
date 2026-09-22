# Contributing to TinkerFin

[中文](docs/CONTRIBUTING.cn.md)

Use [Issues](https://github.com/tinkerfin-ai/tinkerfin-harness/issues) for reproducible bugs and feature requests. Report security vulnerabilities privately to **1090116461@qq.com**; see [SECURITY.md](SECURITY.md).

## Submit a change

1. Fork the repository and create a branch from `main`.
2. Keep the change focused. Add regression coverage for bugs and update affected documentation.
3. Open a pull request against `tinkerfin-ai/tinkerfin-harness:main`, describing the behavior and the checks you ran.
4. Resolve review discussions and wait for the required checks. A maintainer reviews and merges the pull request.

Contributors do not need write access to this repository. Do not include credentials, private configuration, logs containing personal data, or generated build output.

## Run checks

From the repository root:

```bash
uv sync --locked --all-packages --group dev
uv run ruff check --no-cache .
uv run ruff format --no-cache --check packages apps/studio/server scripts tests
uv run pyright
uv run pytest
```

For Studio web changes, run these commands from `apps/studio/web`:

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm exec playwright install chromium
pnpm test:http
pnpm lint
pnpm test:browser
```

Browser tests build the application and start their own preview server. Keep port `4173` available. Docker integration and isolated wheel checks are described in [repository development](docs/en/development.md). Report any checks you could not run and why.

When running selected tests with `pnpm exec playwright test` directly, run `pnpm build` first.

## Local Git checks

Enable the repository hooks once after cloning:

```bash
./scripts/install-git-hooks.sh
```

Install Git, uv, Node.js and pnpm first, and install the workspace and web dependencies
as shown above. The complete check also needs Chromium. On Windows, install Git for
Windows for Git's hook shell; use PowerShell to enable the hooks:

```powershell
.\scripts\install-git-hooks.ps1
```

`git commit` then runs staged-file formatting, lint checks and directly related unit tests.
`git push` runs the complete Studio server and web verification, including the web build and
browser tests. The complete check can also be run manually from the repository root:

```bash
./scripts/verify-studio.sh
```

The equivalent PowerShell command is:

```powershell
.\scripts\verify-studio.ps1
```

Commit checks require the working tree to match the index, including files imported by
tests: stage or save any unstaged and untracked files first. Push checks require a clean
working tree and every non-deleted ref being pushed to point to the checked-out commit.
Ignored dependencies and local configuration remain available. The hooks do not stash
or rewrite files; manual verification scripts can check work in progress.

CI's `web` job runs unit tests, lint, build and packaging checks through the shared
Python checker. `web-browser` installs Chromium and splits UI tests across two jobs;
the first also runs `test:http`, which includes a real browser upload test.
`verify-studio-web.sh --skip-browser` (PowerShell: `verify-studio-web.ps1 -SkipBrowser`)
omits both HTTP and UI browser tests.

The hooks are local convenience checks and can be bypassed with Git's `--no-verify` option;
the repository's required CI checks remain authoritative for changes sent to the remote.

## License

Contributions use the license of the affected package or directory. Preserve third-party license and notice files; see [LICENSE](LICENSE).
