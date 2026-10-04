#!/usr/bin/env bash
# Build a release tarball with the web UI prebuilt, so target hosts only need Python (no Node.js).
#   scripts/package.sh [version]   ->  dist/vm-manager-<version>.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' backend/pyproject.toml)}"
NAME="vm-manager-$VERSION"

echo "Building the web UI…"
(cd frontend && npm ci --legacy-peer-deps --no-audit --no-fund --loglevel=error && CI=true npm run build --silent)

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"

# Only what a host needs to install and run, plus sources and examples
tar -c \
  --exclude='backend/venv' --exclude='backend/data' --exclude='__pycache__' --exclude='backend/.env' \
  --exclude='frontend/node_modules' \
  --exclude='opentofu_provider/terraform-provider-vmmanager' \
  --exclude='.terraform' --exclude='*.tfstate*' --exclude='.terraform.lock.hcl' \
  --exclude='e2e/node_modules' --exclude='e2e/screenshots' \
  README.md CLAUDE.md HANDOFF.md future-features.md run.sh scripts backend frontend opentofu_provider examples e2e \
  | tar -x -C "$STAGE/$NAME"
echo "$VERSION" > "$STAGE/$NAME/VERSION"

mkdir -p dist
tar -czf "dist/$NAME.tar.gz" -C "$STAGE" "$NAME"
echo "dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
echo "Install on a host:  tar xzf $NAME.tar.gz && cd $NAME && scripts/setup.sh"
