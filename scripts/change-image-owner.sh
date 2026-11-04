#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo 'Usage: ./scripts/change-image-owner.sh DOCKERHUB_USERNAME' >&2
  exit 1
fi
cd "$(dirname "$0")/.."
old_owner=$(sed -n 's/^[[:space:]]*image: \([^/]*\)\/ecommerce-frontend:1.1.0$/\1/p' environments/dev/frontend/deployment.yaml)
if [[ -z "$old_owner" ]]; then echo 'Cannot detect current image owner' >&2; exit 1; fi
for f in environments/dev/*/deployment.yaml; do
  sed -i "s#image: ${old_owner}/#image: $1/#g" "$f"
done
printf 'Updated image owner from %s to %s. Verify tag availability before syncing.\n' "$old_owner" "$1"
