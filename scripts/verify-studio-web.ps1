[CmdletBinding()]
param(
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'

$rootDir = Split-Path -Parent $PSScriptRoot
$mode = if ($SkipBrowser) { 'web-no-browser' } else { 'web' }
& uv run --project $rootDir python (Join-Path $rootDir 'scripts/studio_checks.py') $mode
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
