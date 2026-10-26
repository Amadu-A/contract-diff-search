# ops/check-quality.ps1
# Одна команда качества на Windows; подробный успешный вывод сохраняется в .cache.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
New-Item -ItemType Directory -Force .cache | Out-Null
$taskLog = Join-Path (Get-Location).Path '.cache/quality-windows.log'
try { & {
    $ErrorActionPreference = 'Continue'
    uv run --frozen ruff check app tests
    if ($LASTEXITCODE -ne 0) { throw 'Ruff check завершился ошибкой' }
    uv run --frozen ruff format --check app tests
    if ($LASTEXITCODE -ne 0) { throw 'Ruff format завершился ошибкой' }
    uv run --frozen pytest -q tests/architecture
    if ($LASTEXITCODE -ne 0) { throw 'Нарушены архитектурные границы' }
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Ошибка git diff --check' }
} *> $taskLog } catch { Get-Content -LiteralPath $taskLog -Tail 80; throw }
Get-Content -LiteralPath $taskLog -Tail 8
Write-Output 'Качество проверено. Полный лог: .cache/quality-windows.log'
