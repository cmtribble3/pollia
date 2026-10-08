# Source this file from bash or zsh: source scripts/pollia-env.sh
if [ -n "${BASH_VERSION:-}" ]; then
    _pollia_env_file="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _pollia_env_file="${(%):-%x}"
else
    echo "Pollia environment setup supports bash and zsh." >&2
    return 1
fi
_pollia_env_root="$(cd "$(dirname "$_pollia_env_file")/.." && pwd)"
if [ ! -x "$_pollia_env_root/.venv/bin/python" ]; then
    echo "Create Pollia's .venv and install requirements-pollia.txt first." >&2
    return 1
fi
_pollia_env_build="${POLLIA_BUILD_DIR:-$HOME/Library/Developer/Xcode/DerivedData/pollia-runtime}"
if [ ! -f "$_pollia_env_build/python/drjit/__init__.py" ]; then
    echo "Build Pollia-Backend first, or set POLLIA_BUILD_DIR to an existing build." >&2
    return 1
fi
_pollia_env_build="$(cd "$_pollia_env_build" && pwd)"
source "$_pollia_env_root/.venv/bin/activate"
export POLLIA_PYTHON="$_pollia_env_root/.venv/bin/python"
export POLLIA_BUILD_DIR="$_pollia_env_build"
export PYTHONPATH="$_pollia_env_build/python${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$_pollia_env_build:$PATH"
unset _pollia_env_file _pollia_env_root _pollia_env_build
