#!/usr/bin/env bash
# Keep the public CLI stable; discovery, validation and packaging use Python's stdlib.
set -euo pipefail
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required to prepare offline packages" >&2
    exit 1
fi
exec python3 "${BASE_DIR}/scripts/download_components.py" "$@"
