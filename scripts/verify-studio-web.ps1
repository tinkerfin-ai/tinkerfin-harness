param(
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'

$rootDir = (git rev-parse --show-toplevel).Trim()
$mode = if ($SkipBrowser) { 'web-no-browser' } else { 'web' }
& uv run python (Join-Path $rootDir 'scripts/studio_checks.py') $mode
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
