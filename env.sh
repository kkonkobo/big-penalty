# Source this before running anything: keeps every cache/download on the SSD.
export BP_ROOT="/Volumes/SSD/Dev/big-penalty"
export UV_CACHE_DIR="/Volumes/SSD/Dev/big-penalty-scratch/uv-cache"
export UV_PROJECT_ENVIRONMENT="$BP_ROOT/.venv"
export PLAYWRIGHT_BROWSERS_PATH="$BP_ROOT/.browsers"
export PIP_CACHE_DIR="/Volumes/SSD/Dev/pip-cache"
export XDG_CACHE_HOME="/Volumes/SSD/Dev/big-penalty-scratch/xdg-cache"
export TMPDIR="/Volumes/SSD/Dev/big-penalty-scratch/tmp/"
export JOBHUNT_DATA_DIR="$BP_ROOT/data"
mkdir -p "$UV_CACHE_DIR" "$XDG_CACHE_HOME" "$TMPDIR" "$JOBHUNT_DATA_DIR"
if [ -f "$BP_ROOT/.env" ]; then set -a; . "$BP_ROOT/.env"; set +a; fi
