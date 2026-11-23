#!/usr/bin/env bash
# Validate all six Helm release renderings without modifying EKS.
set -euo pipefail
for env in dev prod; do
  if [ "$env" = dev ]; then ns=ecommerce; else ns=ecommerce-prod; fi
  for service in frontend user-service product-service inventory-service order-service payment-service; do
    base="charts/ecommerce-workload/values/base/${service}.yaml"
    specific="charts/ecommerce-workload/values/${env}/${service}.yaml"
    helm lint charts/ecommerce-workload -f "$base" -f "$specific" >/dev/null
    rendered="/tmp/${env}-${service}-rendered.yaml"
    helm template "$service" charts/ecommerce-workload \
      --namespace "$ns" -f "$base" -f "$specific" > "$rendered"
    kubectl create --dry-run=client -f "$rendered" -o name >/dev/null
    echo "PASS: $env/$service rendered and parsed"
  done
done
echo 'PASS: all 12 Helm renderings; no cluster resources were applied.'
echo 'REVIEW: use kubectl diff or Argo CD Diff before any migration sync.'
