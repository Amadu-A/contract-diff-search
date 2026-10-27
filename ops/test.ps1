# ops/test.ps1
# Windows-прогон тестов; отсутствующие системные инструменты отмечаются как skipped.
param([switch]$RealDocuments)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
New-Item -ItemType Directory -Force .cache | Out-Null
$ErrorActionPreference = 'Continue'
$taskPytestArgs = @('run', '--frozen', 'pytest', '-q', '-m', 'not real and not external', '--cov=app', '--cov-branch', '--cov-report=term-missing')
if ($RealDocuments) { $taskPytestArgs[5] = 'not external'; $taskPytestArgs += '--real-documents' }
$taskConsoleEncoding = [Console]::OutputEncoding
$taskPythonEncoding = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    $env:PYTHONIOENCODING = 'utf-8'
    uv @taskPytestArgs *> .cache/tests-windows.log
    $taskExit = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $taskConsoleEncoding
    $env:PYTHONIOENCODING = $taskPythonEncoding
}
Get-Content .cache/tests-windows.log -Tail 25
if ($taskExit -ne 0) { exit $taskExit }
$taskFrontendTests = @(Get-ChildItem tests/frontend/*.test.js | ForEach-Object { $_.FullName })
$taskConsoleEncoding = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    node --test --test-reporter=tap @taskFrontendTests *> .cache/tests-frontend.log
    $taskExit = $LASTEXITCODE
} finally { [Console]::OutputEncoding = $taskConsoleEncoding }
Get-Content .cache/tests-frontend.log -Tail 8
if ($taskExit -ne 0) { exit $taskExit }
