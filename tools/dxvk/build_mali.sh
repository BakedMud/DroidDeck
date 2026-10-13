#!/usr/bin/env bash
# Build the experimental Mali DXVK payload in a pinned LLVM-mingw container.
# This is a CI component build only; it neither modifies the APK nor publishes
# a release asset. The baseline payload is deliberately built before the Mali
# gates so native loading can be verified independently of those gates.
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd -P)
. "$root/tools/dxvk/source.env"
. "$root/tools/dxvk/mali-source.env"

die() {
  echo "error: $*" >&2
  exit 1
}

apply_patches() {
  local directory=$1 patch
  shopt -s nullglob
  for patch in "$directory"/*.patch; do
    git -C "$source" apply --check "$patch"
    git -C "$source" apply "$patch"
  done
  shopt -u nullglob
}

write_cross_file() {
  local name=$1 triplet=$2 family=$3 cpu=$4 target_args=$5
  local file="$crossfiles/$name.ini"
  cat > "$file" <<EOF
[binaries]
c = '$toolchain/${triplet}-clang'
cpp = '$toolchain/${triplet}-clang++'
ar = '$toolchain/${triplet}-ar'
strip = '$toolchain/${triplet}-strip'
windres = '$toolchain/${triplet}-windres'

[properties]
needs_exe_wrapper = true

[built-in options]
c_args = ['-O2', '-fwrapv', '-fno-strict-aliasing', '-ggdb', '-ffunction-sections', '-fdata-sections', '-fno-omit-frame-pointer', '-ffile-prefix-map=$source=.', $target_args]
cpp_args = ['-O2', '-fwrapv', '-fno-strict-aliasing', '-ggdb', '-ffunction-sections', '-fdata-sections', '-fno-omit-frame-pointer', '-ffile-prefix-map=$source=.', $target_args]

[host_machine]
system = 'windows'
cpu_family = '$family'
cpu = '$cpu'
endian = 'little'
EOF
}

check_payload() {
  local payload=$1
  test -x "$payload/run-proton.py" || die "managed payload wrapper is not executable: $payload/run-proton.py"
  mkdir -p "$payload/provenance"
  python3 - "$payload" "$toolchain/llvm-objdump" "$payload/provenance/pe-imports.json" <<'PY'
from pathlib import Path
import json
import re
import struct
import subprocess
import sys

payload = Path(sys.argv[1])
objdump = sys.argv[2]
report = Path(sys.argv[3])
machines = {
    'i386-windows': 0x014c,
    'x86_64-windows': 0x8664,
    'aarch64-windows': 0xaa64,
}
names = ('d3d8', 'd3d9', 'd3d10core', 'd3d11', 'dxgi')
runtime = re.compile(r'^(lib(c\+\+|gcc|stdc\+\+|winpthread)|winpthread)([-._]|$)', re.I)
entries = []
for architecture, expected in machines.items():
    for name in names:
        dll = payload / 'dxvk-mali' / architecture / f'{name}.dll'
        if not dll.is_file():
            raise SystemExit(f'missing required DLL: {dll}')
        data = dll.read_bytes()
        if data[:2] != b'MZ' or len(data) < 0x40:
            raise SystemExit(f'{dll}: not a PE file')
        offset, = struct.unpack_from('<I', data, 0x3c)
        if data[offset:offset + 4] != b'PE\0\0':
            raise SystemExit(f'{dll}: PE header not found')
        machine, = struct.unpack_from('<H', data, offset + 4)
        if machine != expected:
            raise SystemExit(f'{dll}: PE machine {machine:#06x}, expected {expected:#06x}')
        header = subprocess.run([objdump, '-p', str(dll)], check=True,
                                capture_output=True, text=True).stdout
        imports = re.findall(r'^\s*DLL Name: (.+)$', header, re.MULTILINE)
        bad = [library for library in imports if runtime.match(library)]
        if bad:
            raise SystemExit(f'{dll}: unexpected dynamic C++/GCC/pthread runtime imports: {bad}')
        entries.append({
            'architecture': architecture,
            'dll': str(dll.relative_to(payload)),
            'expected_machine': f'0x{expected:04x}',
            'machine': f'0x{machine:04x}',
            'imports': imports,
        })
report.write_text(json.dumps(entries, indent=2, sort_keys=True) + '\n')
PY
}

stamp_version() {
  local tag=$1
  python3 - "$source/version.h.in" "$tag" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
tag = sys.argv[2]
text = path.read_text()
replacement = f'#define DXVK_VERSION "{tag}"'
text, count = re.subn(r'^#define DXVK_VERSION ".*"$', replacement, text, flags=re.M)
if count != 1:
    raise SystemExit(f'{path}: expected one DXVK_VERSION declaration, found {count}')
path.write_text(text)
PY
}

write_payload_metadata() {
  local label=$1 source_epoch
  local payload="$out/$label"
  mkdir -p "$payload/dxvk-mali" "$payload/crossfiles"
  install -m 0755 "$root/tools/hybris/run-proton.py" "$payload/run-proton.py"
  cp -a "$crossfiles/." "$payload/crossfiles/"
  cp "$source/LICENSE" "$payload/LICENSE"
  printf '%s\n' "$DXVK_REVISION" > "$payload/source-commit"
  printf '%s-droiddeck-%s\n' "$DXVK_MALI_BASE_VERSION" "$label" > "$payload/build-version"
  printf '%s\n' "$DXVK_MALI_CONTAINER_IMAGE" > "$payload/container-image"
  printf '%s\n' "$DXVK_MALI_PROTON_RECIPE" > "$payload/proton-recipe-commit"
  git -C "$source" submodule status --recursive > "$payload/submodules.txt"
  git -C "$source" diff --binary > "$payload/downstream.patch"
  source_epoch=$(git -C "$source" show -s --format=%ct HEAD)
  tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
    --exclude-vcs --zstd -cf "$payload/sourcearchive.tar.zst" -C "$source" .
  {
    printf 'container=%s\n' "$DXVK_MALI_CONTAINER_IMAGE"
    printf 'toolchain=%s\n' "$toolchain"
    printf 'proton_recipe=%s\n' "$DXVK_MALI_PROTON_RECIPE"
    "$toolchain/i686-w64-mingw32-clang" --version
    "$toolchain/x86_64-w64-mingw32-clang" --version
    "$toolchain/arm64ec-w64-mingw32-clang" --version
  } > "$payload/compiler.txt"
  cp "$tool_versions" "$payload/tool-versions.txt"
  check_payload "$payload"
  (
    cd "$payload"
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  )
}

build_payload() {
  local label=$1 architecture triplet family cpu builddir payload
  while IFS=$'\t' read -r architecture triplet family cpu; do
    builddir="$work/build-$architecture"
    payload="$out/$label/dxvk-mali"
    mkdir -p "$payload/$architecture"
    if [ -d "$builddir" ]; then
      meson setup --reconfigure "$builddir" "$source" --cross-file "$crossfiles/$architecture.ini" \
        --buildtype plain --prefix "$payload" --bindir "$architecture" --libdir "$architecture" \
        -Db_ndebug=true --strip
    else
      meson setup "$builddir" "$source" --cross-file "$crossfiles/$architecture.ini" \
        --buildtype plain --prefix "$payload" --bindir "$architecture" --libdir "$architecture" \
        -Db_ndebug=true --strip
    fi
    meson compile -C "$builddir" -j 2
    meson install -C "$builddir"
  done <<'EOF'
i386-windows	i686-w64-mingw32	x86	x86
x86_64-windows	x86_64-w64-mingw32	x86_64	x86_64
aarch64-windows	arm64ec-w64-mingw32	aarch64	aarch64
EOF
}

package_payloads() {
  local archive="$out/dxvk-mali-payloads.tar.zst"
  # upload-artifact normalizes file modes. Keep both payloads in one tarball so
  # run-proton.py remains executable after a maintainer downloads and extracts it.
  tar --sort=name --mtime="@${SOURCE_DATE_EPOCH:?}" --owner=0 --group=0 --numeric-owner \
    --zstd -cf "$archive" -C "$out" baseline experimental
  (
    cd "$out"
    sha256sum "$(basename "$archive")" > dxvk-mali-payloads.SHA256SUMS
  )
}

inside_container() {
  root=$1
  out=$2
  work=$3
  . "$root/tools/dxvk/source.env"
  . "$root/tools/dxvk/mali-source.env"
  toolchain=$DXVK_MALI_TOOLCHAIN
  source="$work/source"
  crossfiles="$work/crossfiles"
  tool_versions="$work/tool-versions.txt"
  export LC_ALL=C TZ=UTC

  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git meson ninja-build glslang-tools pkg-config python3 zstd
  {
    git --version
    meson --version
    ninja --version
    glslangValidator --version
    pkg-config --version
    zstd --version
    "$toolchain/llvm-objdump" --version
  } > "$tool_versions"

  test -x "$toolchain/i686-w64-mingw32-clang" || die "missing i686 LLVM-mingw compiler"
  test -x "$toolchain/x86_64-w64-mingw32-clang" || die "missing x86_64 LLVM-mingw compiler"
  test -x "$toolchain/arm64ec-w64-mingw32-clang" || die "missing arm64ec LLVM-mingw compiler"

  mkdir -p "$source" "$crossfiles"
  git -C "$source" init -q
  git -C "$source" fetch --depth=1 https://github.com/doitsujin/dxvk.git "$DXVK_REVISION"
  git -C "$source" checkout -q --detach FETCH_HEAD
  test "$(git -C "$source" rev-parse HEAD)" = "$DXVK_REVISION"
  git -C "$source" submodule update --init --recursive --depth=1
  export SOURCE_DATE_EPOCH
  SOURCE_DATE_EPOCH=$(git -C "$source" show -s --format=%ct HEAD)

  write_cross_file i386-windows i686-w64-mingw32 x86 x86 "'-mstackrealign', '-march=nocona', '-mtune=core-avx2', '-mfpmath=sse'"
  write_cross_file x86_64-windows x86_64-w64-mingw32 x86_64 x86_64 "'-mcmodel=small', '-march=nocona', '-mtune=core-avx2', '-mfpmath=sse'"
  # Valve's pinned Proton recipe builds this ABI with arm64ec-w64-mingw32;
  # DXVK's PE artifact remains ordinary ARM64 (0xaa64) in aarch64-windows.
  write_cross_file aarch64-windows arm64ec-w64-mingw32 aarch64 aarch64 "'-march=armv8.2-a', '-mtune=cortex-x3'"

  apply_patches "$root/tools/dxvk/patches"
  python3 "$root/tools/dxvk/check_present_sync.py" "$source/src/dxvk/dxvk_presenter.cpp"
  stamp_version "$DXVK_MALI_BASE_VERSION-droiddeck-baseline"
  build_payload baseline
  write_payload_metadata baseline

  apply_patches "$root/tools/dxvk/mali-patches"
  stamp_version "$DXVK_MALI_BASE_VERSION-droiddeck-experimental"
  build_payload experimental
  write_payload_metadata experimental
  package_payloads
}

if [ "${1:-}" = --inside-container ]; then
  inside_container "${2:?source root required}" "${3:?output directory required}" "${4:?work directory required}"
  exit 0
fi

out_arg=${1:?output directory required}
mkdir -p "$out_arg"
out=$(cd "$out_arg" && pwd -P)
if find "$out" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  die "output directory must be empty: $out"
fi
if command -v podman >/dev/null 2>&1; then
  runner=podman
elif command -v docker >/dev/null 2>&1; then
  runner=docker
else
  die "podman or docker is required for the pinned LLVM-mingw build"
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/droiddeck-dxvk-mali.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
"$runner" run --rm --pull=always \
  --user 0:0 \
  --volume "$root:/src:ro" \
  --volume "$out:/out:rw" \
  --volume "$work:/work:rw" \
  --workdir /work \
  "$DXVK_MALI_CONTAINER_IMAGE" \
  bash /src/tools/dxvk/build_mali.sh --inside-container /src /out /work
