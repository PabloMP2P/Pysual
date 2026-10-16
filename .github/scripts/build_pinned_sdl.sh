#!/usr/bin/env bash
# Build pinned SDL3, SDL3_ttf, and SDL3_image into .build/sdk for a release wheel.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
prefix="$root/.build/sdk"
deps="$root/.build/deps"

python tools/fetch_native_deps.py --kind source

ttf_vendor=OFF
if [ -d "$deps/SDL3_ttf-3.2.2/external/freetype" ] && [ -d "$deps/SDL3_ttf-3.2.2/external/harfbuzz" ]; then
  ttf_vendor=ON
fi
image_vendor=OFF
if [ -d "$deps/SDL3_image-3.4.6/external/libpng" ]; then
  image_vendor=ON
fi
echo "SDL_ttf vendored=$ttf_vendor; SDL_image vendored=$image_vendor"

os_flags=()
if [ "$(uname)" = "Darwin" ] && [ -n "${MACOSX_DEPLOYMENT_TARGET:-}" ]; then
  os_flags+=("-DCMAKE_OSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET}")
fi

# Video and events are the subsystems the native host starts. Leaving audio and
# game-controller backends off keeps desktop session libraries out of the wheel.
cmake -S "$deps/SDL3-3.4.16" -B "$root/.build/sdl" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DSDL_SHARED=ON -DSDL_STATIC=OFF \
  -DSDL_TEST_LIBRARY=OFF -DSDL_TESTS=OFF -DSDL_EXAMPLES=OFF \
  -DSDL_AUDIO=OFF -DSDL_JOYSTICK=OFF -DSDL_HAPTIC=OFF -DSDL_HIDAPI=OFF \
  -DSDL_POWER=OFF -DSDL_CAMERA=OFF -DSDL_SENSOR=OFF \
  ${os_flags+"${os_flags[@]}"}
cmake --build "$root/.build/sdl" --parallel 2
cmake --install "$root/.build/sdl"

cmake -S "$deps/SDL3_ttf-3.2.2" -B "$root/.build/ttf" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_PREFIX_PATH="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DSDLTTF_VENDORED="$ttf_vendor" -DSDLTTF_HARFBUZZ=ON \
  -DSDLTTF_PLUTOSVG=OFF -DSDLTTF_SAMPLES=OFF \
  ${os_flags+"${os_flags[@]}"}
cmake --build "$root/.build/ttf" --parallel 2
cmake --install "$root/.build/ttf"

cmake -S "$deps/SDL3_image-3.4.6" -B "$root/.build/image" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_PREFIX_PATH="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DSDLIMAGE_VENDORED="$image_vendor" \
  -DSDLIMAGE_AVIF=OFF -DSDLIMAGE_JXL=OFF -DSDLIMAGE_WEBP=OFF -DSDLIMAGE_TIF=OFF \
  -DSDLIMAGE_PNG_SHARED=OFF -DSDLIMAGE_JPG_SHARED=OFF \
  -DSDLIMAGE_SAMPLES=OFF -DSDLIMAGE_TESTS=OFF \
  ${os_flags+"${os_flags[@]}"}
cmake --build "$root/.build/image" --parallel 2
cmake --install "$root/.build/image"

libdir="$prefix/lib"
if [ ! -d "$libdir/pkgconfig" ] && [ -d "$prefix/lib64/pkgconfig" ]; then
  libdir="$prefix/lib64"
fi
if [ ! -d "$libdir/pkgconfig" ]; then
  echo "SDL install did not produce a pkg-config directory under $prefix" >&2
  exit 1
fi

cat > "$prefix/env.sh" <<EOF
export PKG_CONFIG_PATH="$libdir/pkgconfig\${PKG_CONFIG_PATH:+:\$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$libdir\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export DYLD_LIBRARY_PATH="$libdir\${DYLD_LIBRARY_PATH:+:\$DYLD_LIBRARY_PATH}"
EOF

if [ -n "${GITHUB_ENV:-}" ]; then
  echo "PKG_CONFIG_PATH=$libdir/pkgconfig" >> "$GITHUB_ENV"
  echo "LD_LIBRARY_PATH=$libdir" >> "$GITHUB_ENV"
  echo "DYLD_LIBRARY_PATH=$libdir" >> "$GITHUB_ENV"
fi
