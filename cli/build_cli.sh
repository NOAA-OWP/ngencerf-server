#!/usr/bin/env bash
set -euo pipefail

# Move to script directory
cd "$(dirname "$0")"

APP_NAME="ngencerf"
ENTRY_POINT="ngencerf/run_cli.py"
BUILD_VENV=".venv-build"

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"

PYINSTALLER_PLATFORM_ARGS=()

case "$OS_NAME" in
  Linux)
    PLATFORM_DIR="linux"
    ;;
  Darwin)
    PLATFORM_DIR="macos"
    PYINSTALLER_PLATFORM_ARGS+=(--target-arch x86_64)
    ;;
  *)
    echo "ERROR: Unsupported OS: $OS_NAME"
    echo "This script supports Linux and macOS only."
    exit 1
    ;;
esac

cleanup() {
  echo "==> Cleaning up..."
  command -v deactivate &>/dev/null && deactivate || true
  rm -rf "$BUILD_VENV" ngencerf.spec build/
}

trap cleanup EXIT

echo "==> Building $APP_NAME for $PLATFORM_DIR ($ARCH_NAME)..."

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
echo "==> Checking CalibrationSortField consistency..."

if [[ ! -f "./check_enum_consistency.py" ]]; then
    echo "Error: check_enum_consistency.py not found in $(pwd)"
    echo "This script must be run from the CLI directory."
    exit 1
fi

python "./check_enum_consistency.py" || {
    echo "Enum consistency check failed. Fix mismatch before building."
    exit 1
}

cleanup

echo "==> Creating build virtual environment..."
python -m venv "$BUILD_VENV"
source "$BUILD_VENV/bin/activate"

if [[ "$OS_NAME" == "Darwin" ]]; then
  PYTHON_ARCH="$(python -c 'import platform; print(platform.machine())')"

  if [[ "$PYTHON_ARCH" != "x86_64" ]]; then
    echo "WARNING: macOS Intel build requested, but active Python reports architecture: $PYTHON_ARCH"
    echo "For the most reliable Intel-compatible macOS build, run this script with an x86_64 Python under Rosetta."
    echo "The build will continue, but verify the result with:"
    echo "  file ../downloads/latest/macos/$APP_NAME"
  fi
fi

echo "==> Upgrading pip and installing PyInstaller..."
python -m pip install --upgrade pip
python -m pip install pyinstaller

# On Linux, also install staticx. PyInstaller bundles the build host's libpython, which
# links against the host's glibc, so a binary built on a modern runner fails on older
# machines ("GLIBC_2.xx not found"). staticx wraps the onefile into a fully static
# executable that carries its own libc, so it runs on any glibc (Amazon Linux 2, RHEL 7+)
# and on musl. It is Linux-only and needs objcopy (binutils) + patchelf on PATH, which
# the CI job installs.
if [[ "$OS_NAME" == "Linux" ]]; then
  python -m pip install staticx
fi

echo "==> Installing build dependencies from pyproject.toml..."
python -m pip install .

echo "Virtual environment: $VIRTUAL_ENV"

echo "==> Generating CLI git info..."

git fetch --force --tags origin '+refs/tags/*:refs/tags/*' 2>/dev/null || true

jq -n \
  --arg commit_hash "$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
  --arg branch "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" \
  --arg tags "$(git tag --points-at HEAD 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]*$//')" \
  --arg author "$(git log -1 --pretty=format:'%an' 2>/dev/null || echo unknown)" \
  --arg commit_date "$(git log -1 --pretty=format:'%cI' 2>/dev/null || echo unknown)" \
  --arg message "$(git log -1 --pretty=format:'%s' 2>/dev/null | tr '\n' ';' || echo unknown)" \
  --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
  '{
    "ngencerf-cli": {
      commit_hash: $commit_hash,
      branch: $branch,
      tags: $tags,
      author: $author,
      commit_date: $commit_date,
      message: $message,
      build_date: $build_date
    }
  }' \
  > ngencerf/git_info.json

# staticx rejects the absolute DT_RUNPATH that actions/setup-python's Python and
# its stdlib extension modules carry (it points at the hostedtoolcache lib dir), because
# staticx cannot rewrite libraries that are already inside the PyInstaller archive. Strip
# that RUNPATH from the libs PyInstaller is about to bundle so staticx accepts the result.
# Linux-only (staticx is Linux-only); a no-op on libs that carry no rpath.
if [[ "$OS_NAME" == "Linux" ]]; then
  PY_LIBDIR="$(python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")')"
  if [[ -n "$PY_LIBDIR" && -d "$PY_LIBDIR" ]]; then
    echo "==> Stripping absolute RPATH/RUNPATH from Python libs under $PY_LIBDIR (staticx #188)..."
    find "$PY_LIBDIR" -name '*.so*' -exec patchelf --remove-rpath {} + 2>/dev/null || true
  fi
fi

echo "==> Running PyInstaller..."

if ! pyinstaller --onefile \
  --name "$APP_NAME" \
  "${PYINSTALLER_PLATFORM_ARGS[@]}" \
  --add-data "ngencerf/git_info.json:ngencerf" \
  "$ENTRY_POINT"; then
  echo "❌ PyInstaller build failed."
  exit 1
fi

mkdir -p "../downloads/latest/$PLATFORM_DIR"

if [[ "$OS_NAME" == "Linux" ]]; then
  # Wrap the onefile into a fully static binary so it runs on old glibc and musl.
  # The build host's glibc no longer matters; staticx bundles it into the binary.
  echo "==> Wrapping with staticx for broad Linux compatibility..."
  staticx "dist/$APP_NAME" "../downloads/latest/$PLATFORM_DIR/$APP_NAME"
else
  cp "dist/$APP_NAME" \
     "../downloads/latest/$PLATFORM_DIR/$APP_NAME"
fi

echo "==> Build complete. Executable located at: ../downloads/latest/$PLATFORM_DIR/$APP_NAME"

if [[ "$OS_NAME" == "Darwin" ]]; then
  echo "==> macOS executable architecture:"
  file "../downloads/latest/$PLATFORM_DIR/$APP_NAME"
fi
