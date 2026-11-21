param(
    [Parameter(Mandatory=$true)]
    [string]$TargetRepo,
    [switch]$Commit,
    [switch]$Push
)

$ErrorActionPreference = "Stop"
$SrcRoot = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path (Join-Path $TargetRepo ".git"))) {
    Write-Error "Папка $TargetRepo не является git-репозиторием. Выполните: git clone https://github.com/ssrjkk/twinby-swipe-bot.git $TargetRepo"
    exit 1
}

Write-Host "Синхронизирую <3 v3.9.0 в $TargetRepo (сохраняя .git, .env, .env.bak, data и .venv)..."
robocopy $SrcRoot $TargetRepo /MIR /XD ".git" "data" ".venv" "venv" "__pycache__" ".pytest_cache" /XF ".env" ".env.bak" "*.pyc" | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Error "Ошибка robocopy (код $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Push-Location $TargetRepo
git status --short
if ($Commit) {
    git add -A
    git commit -m "feat: release <3 v3.9.0 (full verification, Windows+POSIX tests, zero tech-debt)"
    if ($Push) {
        git push origin HEAD
    } else {
        Write-Host "Коммит создан! Выполните: git push origin main"
    }
} else {
    Write-Host "Готово. Проверьте git diff и выполните: git add -A; git commit -m 'feat: release <3 v3.9.0'; git push"
}
Pop-Location
