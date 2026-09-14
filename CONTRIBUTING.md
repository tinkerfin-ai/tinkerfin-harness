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
uv run ruff check .
uv run ruff format --check packages apps/studio/server scripts tests
uv run pyright
uv run pytest
```

For Studio web changes, run these commands from `apps/studio/web`:

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm exec playwright install chromium
pnpm test:proxy
pnpm lint
pnpm test:browser
```

Browser tests build the application and start their own preview server. Keep port `4173` available. Docker integration and isolated wheel checks are described in [repository development](docs/en/development.md). Report any checks you could not run and why.

When running selected tests with `pnpm exec playwright test` directly, run `pnpm build` first.

## License

Contributions use the license of the affected package or directory. Preserve third-party license and notice files; see [LICENSE](LICENSE).
