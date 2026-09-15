$ErrorActionPreference = 'Stop'

$rootDir = (git rev-parse --show-toplevel).Trim()
git -C $rootDir config --local core.hooksPath .githooks
Write-Output "Enabled repository Git hooks: $rootDir/.githooks"
