#!/bin/bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${1:-${POLLIA_BUILD_DIR:-$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime}}"
configuration="${POLLIA_SWIFT_CONFIGURATION:-release}"
if [[ ! -f "$build_dir/python/mitsuba/__init__.py" || ! -x "$repo_dir/.venv/bin/python" ]]; then
  echo "Build Pollia-Backend and prepare .venv before building the app." >&2
  exit 1
fi
swift build --package-path "$repo_dir/swift" --scratch-path "$build_dir/pollia-swift" \
  --configuration "$configuration" --product Pollia
swift_products="$(swift build --package-path "$repo_dir/swift" --scratch-path "$build_dir/pollia-swift" \
  --configuration "$configuration" --show-bin-path)"
"$repo_dir/.venv/bin/python" - "$repo_dir" "$build_dir" "$swift_products" <<'PY'
from pathlib import Path
import plistlib
import shutil
import sys

root, build, products = map(Path, sys.argv[1:])
app = build / "Pollia.app"
resources = app / "Contents/Resources"
executables = app / "Contents/MacOS"
resources.mkdir(parents=True, exist_ok=True)
executables.mkdir(parents=True, exist_ok=True)
shutil.copy2(products / "Pollia", executables / ".Pollia-new")
(executables / ".Pollia-new").replace(executables / "Pollia")
for bundle in products.glob("*.bundle"):
    shutil.copytree(bundle, resources / bundle.name, dirs_exist_ok=True)
# Stage launch inputs outside Documents, matching the existing runtime layout.
for source in (root / ".venv").rglob("*"):
    target = build / "pollia-python-env" / source.relative_to(root / ".venv")
    if source.is_symlink() and target.is_symlink():
        target.unlink()
shutil.copytree(root / ".venv", build / "pollia-python-env", symlinks=True, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(root / "rendering/pollia", build / "pollia-rendering/pollia", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"))
runtime = dict(python=str(build / "pollia-python-env/bin/python"),
               rendering=str(build / "pollia-rendering"), backend=str(build / "python"))
(resources / "PolliaRuntime.plist").write_bytes(plistlib.dumps(runtime))
info = dict(CFBundleName="Pollia", CFBundleDisplayName="Pollia", CFBundleExecutable="Pollia",
            CFBundleIdentifier="org.pollia.scene-renderer", CFBundlePackageType="APPL",
            CFBundleVersion="1", CFBundleShortVersionString="0.1", LSMinimumSystemVersion="14.0",
            NSHighResolutionCapable=True, NSPrincipalClass="NSApplication")
(app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
print(f"App: {app}")
PY
