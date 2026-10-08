#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build_dir="${POLLIA_BUILD_DIR:-$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime}"
if [[ -x "$repo_dir/.venv/bin/python" ]]; then
  python_bin="${POLLIA_PYTHON:-$repo_dir/.venv/bin/python}"
else
  python_bin="${POLLIA_PYTHON:-python3}"
fi

# MI_DEFAULT_VARIANTS only applies when mitsuba.conf does not already exist.
# Update its enabled list as well, preserving all variant definitions.
"$python_bin" "$repo_dir/scripts/configure-pollia-xcode.py" "$repo_dir" --build-dir "$build_dir"

cmake -S "$repo_dir" -B "$build_dir" -G Xcode \
  -DPython_EXECUTABLE="$python_bin" \
  -DMI_DEFAULT_VARIANTS=scalar_rgb,scalar_mono_polarized,metal_ad_mono_polarized,metal_rgb_polarized \
  -DCMAKE_XCODE_GENERATE_SCHEME=OFF \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5

"$python_bin" "$repo_dir/scripts/configure-pollia-xcode.py" "$repo_dir" --build-dir "$build_dir" --workspace
echo "Xcode workspace: $repo_dir/pollia.xcworkspace"
