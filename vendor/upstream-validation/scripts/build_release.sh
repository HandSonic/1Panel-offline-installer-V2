#!/usr/bin/env bash
set -euo pipefail
TOOLS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from validate_artifacts import architectures;architectures(sys.argv[2])' "$TOOLS" "$TARGET_ARCHES"
# A previous run must never contribute stale artifacts to this release.
if [[ -d dist ]] && [[ -n "$(ls -A dist)" ]]; then echo 'dist must be empty' >&2; exit 1; fi
mkdir -p build dist
for ARCH in $TARGET_ARCHES; do
    GOARCH="$ARCH"; GOARM=""
    if [[ "$ARCH" == armv7 ]]; then GOARCH=arm; GOARM=7; fi
    for component in core agent; do
        (cd "$component"; CGO_ENABLED=0 GOOS=linux GOARCH="$GOARCH" GOARM="$GOARM" \
            go build -trimpath -buildvcs=false -ldflags '-s -w' -o "../build/1panel-$component" ./cmd/server/main.go)
    done
    python3 "$TOOLS/package_release.py" package "$PWD" "$VERSION" "$ARCH"
done
python3 "$TOOLS/package_release.py" finalize "$PWD" "$VERSION" "$TARGET_ARCHES"
