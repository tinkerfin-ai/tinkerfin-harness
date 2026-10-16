$ErrorActionPreference = 'Stop'

$rootDir = Split-Path -Parent $PSScriptRoot
& uv run --project $rootDir python (Join-Path $rootDir 'scripts/studio_checks.py') staged
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
