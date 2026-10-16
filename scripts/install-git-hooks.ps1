$ErrorActionPreference = 'Stop'

$rootDir = Split-Path -Parent $PSScriptRoot
git -C $rootDir config --local core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Enabled repository Git hooks: $rootDir/.githooks"
