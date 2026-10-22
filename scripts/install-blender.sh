#!/usr/bin/env bash
# Install Blender (official LTS tarball) into ~/vendor and clone CLI-Anything beside it (#925).
# User-local, no sudo. Blender is a separate program we call, never linked, so its GPL does not
# reach NOMAD code. Remove with: rm -rf ~/vendor/blender-* ~/vendor/CLI-Anything ~/bin/blender
set -euo pipefail

SERIES="${BLENDER_SERIES:-4.5}"            # 4.5 is the current LTS line
VENDOR="$HOME/vendor"
BASE="https://download.blender.org/release/Blender${SERIES}"
mkdir -p "$VENDOR" "$HOME/bin"
cd "$VENDOR"

# Newest patch release of the series, read from the release index.
TARBALL=$(curl -fsSL "$BASE/" | grep -oE "blender-${SERIES}\.[0-9]+-linux-x64\.tar\.xz" | sort -uV | tail -1)
[ -n "$TARBALL" ] || { echo "no linux-x64 tarball under $BASE" >&2; exit 1; }
DIR="${TARBALL%.tar.xz}"                   # blender-4.5.N-linux-x64
REL="${DIR%-linux-x64}"                    # blender-4.5.N

if [ ! -x "$VENDOR/$DIR/blender" ]; then
  curl -fLO "$BASE/$TARBALL"
  curl -fLO "$BASE/$REL.sha256"
  # Fails if the tarball line is missing from the checksum file, not only on a mismatch.
  grep " $TARBALL\$" "$REL.sha256" | sha256sum -c -
  tar xf "$TARBALL"
  rm -f "$TARBALL" "$REL.sha256"
fi
ln -sfn "$VENDOR/$DIR/blender" "$HOME/bin/blender"

# Shared libraries a headless WSL box often lacks. Background mode (-b) still loads them.
MISSING=$(ldd "$VENDOR/$DIR/blender" | awk '/not found/{print $1}' || true)
if [ -n "$MISSING" ]; then
  echo "Missing libraries:"; echo "$MISSING"
  echo "Fix: sudo apt install -y libxi6 libxkbcommon0 libsm6 libxrender1 libxxf86vm1 libxfixes3 libgl1 libegl1"
fi
"$HOME/bin/blender" -b --version | head -1

# CLI-Anything: clone (or update), then report what #925 needs to know.
if [ -d "$VENDOR/CLI-Anything/.git" ]; then
  git -C "$VENDOR/CLI-Anything" pull --ff-only
else
  git clone https://github.com/HKUDS/CLI-Anything "$VENDOR/CLI-Anything"
fi
echo "CLI-Anything HEAD: $(git -C "$VENDOR/CLI-Anything" rev-parse --short HEAD)"
echo "Licence: $(head -3 "$VENDOR"/CLI-Anything/LICENSE* 2>/dev/null | tr '\n' ' ')"
echo "Blender-related paths:"
find "$VENDOR/CLI-Anything" -maxdepth 4 -iname '*blender*' -not -path '*/.git/*' | head -20
