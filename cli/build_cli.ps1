$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$APP_NAME = "ngencerf"
$ENTRY_POINT = "ngencerf/run_cli.py"
$BUILD_VENV = ".venv-build"

function Cleanup {
    Write-Host "==> Cleaning up..."

    if (Get-Command deactivate -ErrorAction SilentlyContinue) {
        deactivate
    }

    Remove-Item -Recurse -Force $BUILD_VENV -ErrorAction SilentlyContinue
    Remove-Item -Force "ngencerf.spec" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "build" -ErrorAction SilentlyContinue
}

try {
    Write-Host "==> Checking CalibrationSortField consistency..."

    if (-not (Test-Path "./check_enum_consistency.py")) {
        Write-Host "Error: check_enum_consistency.py not found in $(Get-Location)"
        Write-Host "This script must be run from the CLI directory."
        exit 1
    }

    python "./check_enum_consistency.py"

    Cleanup

    Write-Host "==> Creating build virtual environment..."
    python -m venv $BUILD_VENV
    . "$BUILD_VENV\Scripts\Activate.ps1"

    Write-Host "==> Installing PyInstaller..."
    python -m pip install pyinstaller

    Write-Host "==> Installing build dependencies from pyproject.toml..."
    python -m pip install .

    Write-Host "Virtual environment: $env:VIRTUAL_ENV"

    Write-Host "==> Generating CLI git info..."

    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    if (-not $branch) { $branch = "unknown" }

    $commitHash = git rev-parse HEAD 2>$null
    if (-not $commitHash) { $commitHash = "unknown" }

    $commitDate = git log -1 --pretty=format:'%cI' 2>$null
    if (-not $commitDate) { $commitDate = "unknown" }

    $author = git log -1 --pretty=format:'%an' 2>$null
    if (-not $author) { $author = "unknown" }

    $message = git log -1 --pretty=format:'%s' 2>$null
    if (-not $message) { $message = "unknown" }

    $gitInfo = @{
        "ngencerf-cli" = @{
            release = "dev ($branch)"
            build_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            commit_hash = $commitHash
            commit_date = $commitDate
            author = $author
            message = $message
        }
    } | ConvertTo-Json -Depth 3

    $gitInfo | Set-Content -Encoding UTF8 "ngencerf/git_info.json"

    Write-Host "==> Running PyInstaller..."

    python -m PyInstaller --onefile `
        --name $APP_NAME `
        --add-data "ngencerf/git_info.json;ngencerf" `
        $ENTRY_POINT

    Write-Host "==> Build complete. Executable located at: dist\$APP_NAME.exe"
}
catch {
    Write-Host "Build failed."
    Write-Host $_
    exit 1
}
finally {
    Cleanup
}