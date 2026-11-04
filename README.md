# E-commerce GitOps practice on Amazon EKS

Generated from uploaded `ecommerce-microservices` source ZIP (Git revision `f18759f1180ed8538f560a79e0ad499778adee2a`).

## Architecture

- **Frontend**: Nginx on `80`, image `kumarbolem/ecommerce-frontend:1.1.0`.
- **Backends**: Spring Boot user (`8081`), product (`8082`), inventory (`8083`), order (`8084`), payment (`8085`) images at tag `1.0.0`.
- **PostgreSQL 16**: one Postgres instance with five logical databases, initialized from `db/init.sql` content.
- **Kubernetes namespace**: `ecommerce`; all frontend and backend Services are `ClusterIP` (no AWS load balancer).
- **GitOps**: Argo CD Application `ecommerce-dev`, manual sync initially.

**Important image assumption**: `kumarbolem` is the Docker Hub namespace previously used in your lab; the source ZIP does not prove the actual published image owner or tag. Before syncing, verify the six image references are public and match your Docker Hub; update `image:` lines if necessary. If you want a different Docker Hub account use `./scripts/change-image-owner.sh NEW_USERNAME` before pushing.

## 0. Prerequisites

- A functioning EKS cluster `accounting-eks` in `us-east-1` with two Ready EC2 worker nodes.
- `kubectl` connected to EKS from administration EC2. This repo intentionally uses namespace `ecommerce`, not `accounting`.
- Your GitHub repo `swathikumar-bolem/ecommerce-gitops` created **public**, with the content of this project pushed to `main`.
- All six Docker Hub images public, or configure `imagePullSecrets` separately.

## 1. Verify cluster and images

```bash
aws eks update-kubeconfig --region us-east-1 --name accounting-eks
kubectl get nodes -o wide
kubectl get pods -A
# Check image names in manifests and verify the tags are published in Docker Hub.
grep -R 'image: kumarbolem' environments/dev/
```

Do not run `kubectl apply -k environments/dev` before Argo CD if you want Argo CD to perform the initial deployment. The commands below only bootstrap the namespace and an out-of-band lab Secret.

## 2. Bootstrap the namespace and database credential (not stored in Git)

```bash
kubectl apply -f environments/dev/namespace.yaml
read -rsp 'Enter temporary LAB PostgreSQL password: ' DB_PASSWORD; echo
kubectl -n ecommerce create secret generic ecommerce-db \
  --from-literal=DB_PASSWORD="$DB_PASSWORD"
unset DB_PASSWORD
kubectl -n ecommerce get secret ecommerce-db
```

Only use a **new lab password**; do not use production credentials. Kubernetes Secrets are not encrypted by default in every configuration; use dedicated secrets management in production. Never commit the Secret to Git. If the cluster was recreated, repeat this step before first Argo sync.

## 3. Install Argo CD into EKS

Official installation instructions: https://argo-cd.readthedocs.io/en/stable/getting_started/

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl get pods -n argocd
```

For a production system, pin the Argo CD release instead of using the moving `stable` branch, restrict RBAC, and secure UI access.

## 4. Register the GitOps Application (manual sync)

**Push this repo first**, and verify the Application YAML `repoURL` matches the actual GitHub repository URL.

```bash
kubectl apply -f argocd/ecommerce-dev-application.yaml
kubectl get applications -n argocd
```

Argo CD will discover Git resources. The app should initially be `OutOfSync` until you trigger its first Sync. No Jenkins is needed.

## 5. Open Argo CD via local port forwarding

On administration EC2 (SSH session 1):

```bash
kubectl -n argocd port-forward svc/argocd-server 8081:443 --address 127.0.0.1
```

Use a PuTTY SSH tunnel on Windows: local port **8081** → EC2 destination `127.0.0.1:8081`. Browse `https://localhost:8081` (a local TLS warning may appear). For the initial `admin` login password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 --decode; echo
```

Use the Argo CD web UI to find `ecommerce-dev`, inspect the proposed differences, and press **Sync**. Initial ordering uses Argo CD sync waves: namespace → config and database → backend Services → backend Deployments → frontend. If the app stays in Progressing, inspect events for image pull, missing secret, or health probe failures.

## 6. Check application and access frontend

```bash
kubectl get pods -n ecommerce -o wide
kubectl get deploy,sts,svc -n ecommerce
kubectl logs -n ecommerce deployment/order-service --tail=50
```

Frontend port-forward on EC2 (SSH session 2):

```bash
kubectl -n ecommerce port-forward svc/frontend 8080:80 --address 127.0.0.1
```

SSH tunnel from Windows: local port **8080** → EC2 destination `127.0.0.1:8080` and open `http://localhost:8080`. This does not need an AWS load balancer or open inbound security-group rule for 8080. Use the UI Admin tab to add a test user/product/stock, then place a simulated order.

You can also test on the administration EC2 in another SSH session using `curl http://localhost:8080/api/users` while port-forward is running.

## 7. GitOps OutOfSync experiment — manual sync

1. In `environments/dev/frontend/deployment.yaml`, change `spec.replicas: 1` to `spec.replicas: 2`.
2. Commit and push to GitHub `main`.
3. Refresh the Argo CD app (Git polling may take a few minutes). It should display `OutOfSync`.
4. Press **Sync**. Watch `kubectl get pods -n ecommerce -o wide`; two frontend Pods should run.
5. Commit `replicas: 1` again, and Sync to restore one frontend Pod.

## 8. Auto-sync and self-heal experiment (later)

Only after the manual Sync experiment, add to `spec.syncPolicy` in `argocd/ecommerce-dev-application.yaml`:

```yaml
  syncPolicy:
    automated:
      prune: false
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

**Important:** Because the Application YAML itself is bootstrapped by `kubectl apply` (not managed by itself), commit this change **and then re-apply the Application file**. Alternatively enable automated Sync via the Argo CD UI to test it without changing the app definition. Don't enable automated pruning in the first lab.

With self-healing enabled, run `kubectl -n ecommerce scale deployment/frontend --replicas=3` and watch Argo CD restore the Git-declared count. A Kubernetes Deployment replaces deleted Pods automatically, so deleting a Pod is *not* a good GitOps drift test.

## What this lab intentionally does NOT implement

- **Database persistence:** `postgres` uses `emptyDir` on purpose for inexpensive, simple practice. Its contents survive container restarts within the same Pod but are **lost when the Pod is replaced, rescheduled, or the cluster deleted**. Do not use for real data. Next lab: AWS EBS CSI + StatefulSet PVC or Amazon RDS.
- **No public ingress or AWS load balancer:** only private Services and SSH/port-forward for safe lab access.
- **No source-build pipeline:** Jenkins comes later; these YAML files reference Docker Hub images already built.
- **No multi-replica inventory backend:** current source uses JVM `synchronized`, unsafe with more than one inventory replica. Keep inventory at one until the database-level concurrency behavior is fixed.
- **No authentication:** frontend has an unauthenticated Admin tab; never expose it publicly or enter real customer/payment information.
- **No production Secrets/GitOps management:** database password is created manually out of Git for the first lab; later use External Secrets or an encryption tool with proper keys/access controls.

## Cost cleanup

Use `kubectl delete application ecommerce-dev -n argocd` with care, because that does not guarantee all deployed resources are removed. If doing a full lab teardown, delete app resources first (`kubectl delete -k environments/dev/` after Argo no longer reconciles them, and note database data loss), then delete the cluster with `eksctl delete cluster --name accounting-eks --region us-east-1 --wait`. Confirm CloudFormation and EC2 cleanup. Avoid deleting the administration EC2 unless you intend to.
