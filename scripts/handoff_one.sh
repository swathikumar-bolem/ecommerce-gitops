#!/usr/bin/env bash
# Remove OLD Argo CD resource tracking only after ecommerce-dev excludes a service.
# Not a deployment script. Synchronize the new per-service Application manually afterward.
set -euo pipefail
SERVICE="${1:-}"
case "$SERVICE" in frontend|user-service|product-service|inventory-service|order-service|payment-service) ;; *)
  echo 'Usage: bash scripts/handoff_one.sh frontend|user-service|product-service|inventory-service|order-service|payment-service' >&2
  exit 2;;
esac
APP="ecommerce-dev-${SERVICE}"
CURRENT=$(kubectl get application ecommerce-dev -n argocd -o jsonpath='{.spec.syncPolicy.automated.enabled}')
if [ "$CURRENT" != false ]; then
  echo 'STOP: old ecommerce-dev must have auto-sync disabled before handoff.' >&2; exit 1
fi
if ! kubectl get application "$APP" -n argocd >/dev/null 2>&1; then
  echo "STOP: new Application $APP has not been created." >&2; exit 1
fi
if kubectl get application ecommerce-dev -n argocd -o jsonpath='{.spec.sources[*].path}' | grep -q 'charts/'; then
  echo 'STOP: old ecommerce-dev still points to a Helm chart; apply core-only Application first.' >&2; exit 1
fi
if grep -Eq "^[[:space:]]*-[[:space:]]*${SERVICE}/(deployment|service|hpa|pdb)\.yaml([[:space:]]*|$)" environments/dev/kustomization.yaml; then
  echo 'STOP: old DEV Kustomization still defines this service.' >&2; exit 1
fi
for item in "deployment/$SERVICE" "service/$SERVICE"; do
  kubectl get "$item" -n ecommerce >/dev/null
  current=$(kubectl get "$item" -n ecommerce -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}' || true)
  case "$current" in
    ecommerce-dev:*) echo "Owner: $item -> $current" ;;
    "") echo "No old tracking-id on $item" ;;
    *) echo "STOP: $item belongs to a different app: $current" >&2; exit 1;;
  esac
done
for item in "horizontalpodautoscaler/$SERVICE" "poddisruptionbudget/${SERVICE}-pdb"; do
  if kubectl get "$item" -n ecommerce >/dev/null 2>&1; then
    current=$(kubectl get "$item" -n ecommerce -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}' || true)
    case "$current" in ecommerce-dev:*|"") echo "Owner: $item -> $current" ;; *) echo "STOP: $item belongs to $current" >&2; exit 1;; esac
  fi
done
echo "About to remove ONLY Argo CD tracking annotation from existing $SERVICE resources."
echo 'Existing Pods, Services and PVCs will NOT be deleted by this script.'
read -r -p "Type HANDOFF to continue: " CONFIRM
[ "$CONFIRM" = HANDOFF ] || exit 1
for item in "deployment/$SERVICE" "service/$SERVICE" "horizontalpodautoscaler/$SERVICE" "poddisruptionbudget/${SERVICE}-pdb"; do
  if kubectl get "$item" -n ecommerce >/dev/null 2>&1; then
    kubectl annotate "$item" -n ecommerce argocd.argoproj.io/tracking-id- --overwrite >/dev/null
    # Only remove the instance label when exactly the previous owner, and not selected by the Service.
    instance=$(kubectl get "$item" -n ecommerce -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}' || true)
    if [ "$instance" = ecommerce-dev ]; then
      kubectl label "$item" -n ecommerce app.kubernetes.io/instance- >/dev/null
    fi
    echo "Untracked from old Application: $item"
  fi
done
echo "NEXT: In Argo CD manually SYNC $APP (Prune OFF, Force OFF, Replace OFF)."
echo 'Confirm it is Healthy, and check that the resource tracking-id now names the new Application.'
