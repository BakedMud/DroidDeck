#!/usr/bin/env bash
# Dispatch one reviewed graphics component recipe from a selected source checkout.
set -euo pipefail

component=${1:?component id required}
source_arg=${2:?source directory required}
build_arg=${3:?build directory required}

source_dir=$(cd "${source_arg}" && pwd -P)
mkdir -p "${build_arg}"
build_dir=$(cd "${build_arg}" && pwd -P)

if find "${build_dir}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  printf 'error: build directory must be empty: %s\n' "${build_dir}" >&2
  exit 2
fi

require_recipe() {
  local recipe=$1
  if [[ ! -f "${source_dir}/${recipe}" ]]; then
    printf 'error: component %s is unsupported by the selected source; missing %s\n' \
      "${component}" "${recipe}" >&2
    exit 2
  fi
}

case "${component}" in
  hybris)
    require_recipe tools/hybris/build_runtime.sh
    bash "${source_dir}/tools/hybris/build_runtime.sh" "${build_dir}"
    ;;
  dxvk)
    require_recipe tools/dxvk/build.sh
    bash "${source_dir}/tools/dxvk/build.sh" "${build_dir}"
    ;;
  wsi)
    require_recipe tools/vulkan-wsi/build.sh
    bash "${source_dir}/tools/vulkan-wsi/build.sh" "${build_dir}"
    ;;
  dxvk-xclipse)
    require_recipe tools/dxvk/build_xclipse.sh
    bash "${source_dir}/tools/dxvk/build_xclipse.sh" "${build_dir}"
    ;;
  dxvk-mali)
    require_recipe tools/dxvk/build_mali.sh
    bash "${source_dir}/tools/dxvk/build_mali.sh" "${build_dir}"
    ;;
  gamescope)
    require_recipe tools/gamescope/build-in-arch.sh
    if ! command -v docker >/dev/null 2>&1; then
      printf 'error: docker is required to build gamescope in the ARM64 Arch container\n' >&2
      exit 2
    fi
    docker run --rm --pull=always \
      --volume "${source_dir}:/work" \
      --workdir /work \
      menci/archlinuxarm:base-devel \
      bash /work/tools/gamescope/build-in-arch.sh
    install -m 0644 "${source_dir}/gamescope.tzst" "${build_dir}/gamescope.tzst"
    ;;
  *)
    printf 'error: unsupported component: %s\n' "${component}" >&2
    exit 2
    ;;
esac
