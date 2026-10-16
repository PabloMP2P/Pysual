#!/usr/bin/env bash
# Build a manylinux wheel whose SDL libraries travel inside the archive.
# Run this inside quay.io/pypa/manylinux_2_34_x86_64 with the repository mounted.
# AlmaLinux 9 provides HarfBuzz 2.3.1 or newer, which SDL_ttf requires.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

dnf install -y dnf-plugins-core
dnf config-manager --set-enabled powertools \
  || dnf config-manager --set-enabled crb \
  || true
dnf install -y \
  gcc gcc-c++ make cmake pkgconf-pkg-config \
  libX11-devel libXext-devel libXrender-devel libXrandr-devel libXcursor-devel \
  libXi-devel libXfixes-devel libXScrnSaver-devel libXtst-devel \
  mesa-libGL-devel \
  freetype-devel harfbuzz-devel libpng-devel libjpeg-turbo-devel zlib-devel
# Wayland and DRM let SDL open a native window. X11 remains enough for the wheel probe.
dnf install -y wayland-devel libxkbcommon-devel libdrm-devel mesa-libgbm-devel \
  || echo "Continuing without optional Wayland or DRM development packages"

export PATH="/opt/python/cp312-cp312/bin:${PATH}"
python -m pip install -U pip 'build>=1.2'
if ! cmake --version | head -n 1 | grep -Eq 'version 3\.(2[2-9]|[3-9][0-9])'; then
  python -m pip install 'cmake>=3.28'
fi
hash -r

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
auditwheel repair --plat manylinux_2_34_x86_64 -w "$root/wheelhouse" "$root"/dist/*.whl
auditwheel show "$root"/wheelhouse/*.whl

shopt -s nullglob
wheels=("$root"/wheelhouse/*manylinux*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "auditwheel did not produce exactly one manylinux wheel" >&2
  exit 1
fi
