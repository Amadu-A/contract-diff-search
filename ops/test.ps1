# ops/test.ps1
# Windows-прогон тестов; отсутствующие системные инструменты отмечаются как skipped.
param([switch]$RealDocuments)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
New-Item -ItemType Directory -Force .cache | Out-Null
$ErrorActionPreference = 'Continue'
$taskPytestArgs = @('run', '--frozen', 'pytest', '-q', '-m', 'not real and not external', '--cov=app', '--cov-branch', '--cov-report=term-missing')
if ($RealDocuments) { $taskPytestArgs[5] = 'not external'; $taskPytestArgs += '--real-documents' }
uv @taskPytestArgs *> .cache/tests-windows.log
$taskExit = $LASTEXITCODE
Get-Content .cache/tests-windows.log -Tail 25
if ($taskExit -ne 0) { exit $taskExit }
$taskFrontendTests = @(Get-ChildItem tests/frontend/*.test.js | ForEach-Object { $_.FullName })
node --test @taskFrontendTests *> .cache/tests-frontend.log
$taskExit = $LASTEXITCODE
Get-Content .cache/tests-frontend.log -Tail 8
if ($taskExit -ne 0) { exit $taskExit }
