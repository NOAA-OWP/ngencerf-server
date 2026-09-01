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

#=======================================================================
# Verify CLI and server enums are in sync before building
#=======================================================================
# NOTE:
# This build step assumes we are executing from inside the CLI directory:
#     /ngencerf/ngencerf-server/cli
#
# That’s the default Docker build context (RUN cli/build_cli.sh).
# If the script is ever run manually from another directory, relative paths
# to `check_enum_consistency.py` will not resolve correctly.
#
# To prevent path errors, we reference the file explicitly as "./check_enum_consistency.py"
# and fail fast if it's missing.
#=======================================================================

try {
    Write-Host "==> Checking CalibrationSortField consistency..."

    if (-not (Test-Path "./check_enum_consistency.py")) {
        Write-Host "Error: check_enum_consistency.py not found in $(Get-Location)"
        Write-Host "This script must be run from the CLI directory."
        exit 1
    }

    python "./check_enum_consistency.py"
    # PowerShell does NOT throw on a native command's nonzero exit, even under
    # $ErrorActionPreference = "Stop", so check $LASTEXITCODE explicitly. Without
    # this, an enum mismatch (check_enum_consistency.py exits 1) is ignored and a
    # stale-enum Windows binary would ship. Mirrors `|| exit 1` in build_cli.sh.
    if ($LASTEXITCODE -ne 0) {
        throw "Enum consistency check failed. Fix mismatch before building."
    }

    Cleanup

    Write-Host "==> Creating build virtual environment..."
    python -m venv $BUILD_VENV
    . "$BUILD_VENV\Scripts\Activate.ps1"

    Write-Host "==> Upgrading pip and installing PyInstaller..."
    python -m pip install --upgrade pip
    python -m pip install pyinstaller

    Write-Host "==> Installing build dependencies from pyproject.toml..."
    python -m pip install .

    Write-Host "Virtual environment: $env:VIRTUAL_ENV"

    Write-Host "==> Generating CLI git info..."

    git fetch --force --tags origin "+refs/tags/*:refs/tags/*" 2>$null

    $commitHash = git rev-parse HEAD 2>$null
    if (-not $commitHash) { $commitHash = "unknown" }

    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    if (-not $branch) { $branch = "unknown" }

    $tags = git tag --points-at HEAD 2>$null
    if ($tags) {
        $tags = ($tags -join " ").Trim()
    } else {
        $tags = ""
    }

    $author = git log -1 --pretty=format:'%an' 2>$null
    if (-not $author) { $author = "unknown" }

    $commitTimestamp = git log -1 --pretty=format:'%ct' 2>$null
    if ($commitTimestamp) {
        $commitDate = ([DateTimeOffset]::FromUnixTimeSeconds([int64]$commitTimestamp)).
            UtcDateTime.
            ToString("yyyy-MM-dd HH:mm:ss 'UTC'")
    } else {
        $commitDate = "unknown"
    }

    $message = git log -1 --pretty=format:'%s' 2>$null
    if ($message) {
        $message = $message -replace "`r?`n", ";"
    } else {
        $message = "unknown"
    }

    $buildDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss 'UTC'")

    $gitInfo = @{
        "ngencerf-cli" = @{
            commit_hash = $commitHash
            branch = $branch
            tags = $tags
            author = $author
            commit_date = $commitDate
            message = $message
            build_date = $buildDate
        }
    } | ConvertTo-Json -Depth 3

    $gitInfo | Set-Content -Encoding UTF8 "ngencerf/git_info.json"

    Write-Host "==> Running PyInstaller..."

    python -m PyInstaller --onefile `
        --name $APP_NAME `
        --add-data "ngencerf/git_info.json;ngencerf" `
        $ENTRY_POINT

    New-Item -ItemType Directory `
        -Force `
        -Path "../downloads/latest/windows" | Out-Null

    Copy-Item `
        "dist/$APP_NAME.exe" `
        "../downloads/latest/windows/$APP_NAME.exe"

    Write-Host "==> Build complete. Executable located at: ../downloads/latest/windows/$APP_NAME.exe"
}
catch {
    Write-Host "Build failed."
    Write-Host $_
    exit 1
}
finally {
    Cleanup
}