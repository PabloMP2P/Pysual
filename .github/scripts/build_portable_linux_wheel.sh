#!/usr/bin/env bash
# Build a manylinux wheel whose SDL libraries travel inside the archive.
# Run this inside quay.io/pypa/manylinux_2_28_x86_64 with the repository mounted.
# AlmaLinux 8's compiler and system libraries stay on the baseline x86-64 ISA,
# which is what the manylinux_2_28 tag allows. Its HarfBuzz is too old for
# SDL_ttf, so this script builds a pinned HarfBuzz into the same prefix.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

dnf install -y dnf-plugins-core
dnf config-manager --set-enabled powertools \
  || dnf config-manager --set-enabled crb \
  || true
dnf install -y \
  gcc gcc-c++ make cmake pkgconf-pkg-config xz \
  libX11-devel libXext-devel libXrender-devel libXrandr-devel libXcursor-devel \
  libXi-devel libXfixes-devel libXScrnSaver-devel libXtst-devel \
  mesa-libGL-devel \
  freetype-devel libpng-devel libjpeg-turbo-devel zlib-devel
# Wayland and DRM let SDL open a native window. X11 remains enough for the wheel probe.
dnf install -y wayland-devel libxkbcommon-devel libdrm-devel mesa-libgbm-devel \
  || echo "Continuing without optional Wayland or DRM development packages"

export PATH="/opt/python/cp312-cp312/bin:${PATH}"
# Keep every binary we compile on the baseline ISA. A newer default, such as
# x86-64-v2, cannot be tagged manylinux_2_28_x86_64.
export CFLAGS="-march=x86-64"
export CXXFLAGS="-march=x86-64"
python -m pip install -U pip 'build>=1.2'
if ! cmake --version | head -n 1 | grep -Eq 'version 3\.(2[2-9]|[3-9][0-9])'; then
  python -m pip install 'cmake>=3.28'
fi
hash -r

prefix="$root/.build/sdk"
python - <<'PY'
import hashlib
import tarfile
import urllib.request
from pathlib import Path

url = "https://github.com/harfbuzz/harfbuzz/releases/download/10.4.0/harfbuzz-10.4.0.tar.xz"
expected = "480b6d25014169300669aa1fc39fb356c142d5028324ea52b3a27648b9beaad8"
destination = Path(".build/deps/harfbuzz-10.4.0.tar.xz")
destination.parent.mkdir(parents=True, exist_ok=True)
if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
    urllib.request.urlretrieve(url, destination)
actual = hashlib.sha256(destination.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"HarfBuzz checksum mismatch: {actual}")
with tarfile.open(destination, "r:xz") as archive:
    archive.extractall(destination.parent, filter="data")
PY
cmake -S "$root/.build/deps/harfbuzz-10.4.0" -B "$root/.build/harfbuzz" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DBUILD_SHARED_LIBS=ON \
  -DHB_BUILD_SUBSET=OFF \
  -DHB_BUILD_UTILS=OFF \
  -DHB_HAVE_FREETYPE=ON \
  -DHB_HAVE_GLIB=OFF \
  -DHB_HAVE_GOBJECT=OFF \
  -DHB_HAVE_ICU=OFF \
  -DHB_HAVE_CAIRO=OFF \
  -DHB_HAVE_GRAPHITE2=OFF
cmake --build "$root/.build/harfbuzz" --parallel 2
cmake --install "$root/.build/harfbuzz"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

bash "$root/.github/scripts/build_pinned_sdl.sh"
# shellcheck disable=SC1091
source "$root/.build/sdk/env.sh"

PYTHONPATH="$root/src" python -m pysual native
PYTHONPATH="$root/src" python -m pysual package --wheel

rm -rf "$root/wheelhouse"
mkdir -p "$root/wheelhouse"
if ! command -v auditwheel >/dev/null 2>&1; then
  python -m pip install auditwheel
fi
auditwheel repair --plat manylinux_2_28_x86_64 -w "$root/wheelhouse" "$root"/dist/*.whl
auditwheel show "$root"/wheelhouse/*.whl

shopt -s nullglob
wheels=("$root"/wheelhouse/*manylinux*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "auditwheel did not produce exactly one manylinux wheel" >&2
  exit 1
fi
