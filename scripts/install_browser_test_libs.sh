#!/usr/bin/env bash
#
# Install the system shared libraries required by the headless-browser
# acceptance test (tests/test_console_real_daemon_e2e.py::
# test_render_paradigm_in_headless_browser).
#
# That test launches a real Chromium via Playwright. Chromium needs a set of
# system libraries (libnspr4, libnss3, libgbm1, libasound2, ...) that are NOT
# bundled with the Playwright browser download. The canonical way to install
# them is, with root:
#
#     playwright install --with-deps chromium      # needs sudo / apt
#
# On hosts WITHOUT root this script provides a userspace fallback: it downloads
# the required Debian/Ubuntu packages with `apt-get download` (no root needed)
# and extracts their shared objects into a gitignored `.browser-libs/lib`
# directory at the repo root. tests/conftest.py prepends that directory to
# LD_LIBRARY_PATH so the Chromium child process can find the libs.
#
# Usage:
#     scripts/install_browser_test_libs.sh
#
# Idempotent: re-running re-downloads and re-extracts into the same directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/.browser-libs"
DEBS="$OUT/debs"
EXTRACT="$OUT/extract"
LIB="$OUT/lib"

if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
  echo "ERROR: this fallback needs apt-get + dpkg (Debian/Ubuntu)." >&2
  echo "       On other distros, install Chromium's system deps with your" >&2
  echo "       package manager, or run 'playwright install --with-deps chromium'." >&2
  exit 1
fi

# Direct + transitive shared-library dependency closure for headless Chromium
# on Debian 13 (trixie). t64 names fall back to their pre-t64 names so the
# script also works on older Debian/Ubuntu releases.
PKGS=(
  libnspr4
  libnss3
  "libatk1.0-0t64|libatk1.0-0"
  "libatk-bridge2.0-0t64|libatk-bridge2.0-0"
  "libatspi2.0-0t64|libatspi2.0-0"
  libxcomposite1
  libxdamage1
  libxfixes3
  libxrandr2
  libgbm1
  libxkbcommon0
  "libasound2t64|libasound2"
  libxi6
  libdrm2
  "libcups2t64|libcups2"
  libavahi-client3
  libavahi-common3
)

rm -rf "$DEBS" "$EXTRACT"
mkdir -p "$DEBS" "$EXTRACT" "$LIB"

echo ">> Downloading packages into $DEBS"
cd "$DEBS"
for spec in "${PKGS[@]}"; do
  ok=0
  IFS='|' read -ra names <<<"$spec"
  for name in "${names[@]}"; do
    if apt-get download "$name" >/dev/null 2>&1; then
      echo "   ok: $name"
      ok=1
      break
    fi
  done
  [ "$ok" -eq 1 ] || echo "   WARN: could not download any of: $spec" >&2
done

echo ">> Extracting shared objects into $LIB"
for deb in "$DEBS"/*.deb; do
  dpkg -x "$deb" "$EXTRACT/"
done
find "$EXTRACT" -name "*.so*" -exec cp -a {} "$LIB/" \; 2>/dev/null || true

# Keep the directory lean: the .so files in lib/ are all that is needed at
# runtime; the raw .deb archives and the extraction tree are not.
rm -rf "$DEBS" "$EXTRACT"

echo ">> Done. $(ls "$LIB" | wc -l) shared objects installed in $LIB"

# Best-effort verification against the Playwright Chromium binary, if present.
SHELL_BIN="$(find "${HOME}/.cache/ms-playwright" -name chrome-headless-shell -type f 2>/dev/null | head -1 || true)"
if [ -n "$SHELL_BIN" ] && command -v ldd >/dev/null 2>&1; then
  missing="$(LD_LIBRARY_PATH="$LIB" ldd "$SHELL_BIN" 2>/dev/null | grep "not found" || true)"
  if [ -n "$missing" ]; then
    echo ">> WARNING: some libraries are still unresolved:" >&2
    echo "$missing" >&2
    echo "   Install the missing packages and re-run this script." >&2
  else
    echo ">> Verified: chrome-headless-shell resolves all libraries."
  fi
fi
