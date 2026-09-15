$ErrorActionPreference = 'Stop'

$rootDir = (git rev-parse --show-toplevel).Trim()
& uv run python (Join-Path $rootDir 'scripts/studio_checks.py') server
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
