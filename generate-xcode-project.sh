#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${POLLIA_PYTHON:-python3}"

cmake -S "$repo_dir" -B "$repo_dir/build-xcode" -G Xcode \
  -DPython_EXECUTABLE="$python_bin" \
  -DMI_DEFAULT_VARIANTS=scalar_rgb,scalar_mono_polarized,metal_ad_mono_polarized \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5

echo "Xcode project: $repo_dir/build-xcode/mitsuba.xcodeproj"
