#!/usr/bin/env python3
"""Generate environment-specific Argo CD applications and Helm values from CURRENT repo.

Uses stdlib and (if installed) PyYAML. If PyYAML is absent, kubectl converts YAML
into JSON via its local client-side dry-run. Does not communicate with AWS or
change Kubernetes resources. --preview does not write any files.
"""
import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

SERVICES = ("frontend", "user-service", "product-service", "inventory-service", "order-service", "payment-service")
REPO_URL = "https://github.com/swathikumar-bolem/ecommerce-gitops.git"
BASE_NS = "ecommerce"
PROD_NS = "ecommerce-prod"

try:
    import yaml
except ImportError:
    yaml = None


def read_obj(path):
    if not path.exists():
        raise RuntimeError(f"File missing: {path}")
    if yaml:
        obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        # Kubernetes client is already installed on this user's EKS jump box.
        p = subprocess.run(["kubectl", "create", "--dry-run=client", "-f", str(path), "-o", "json"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"Cannot parse {path} with kubectl: {p.stderr.strip()}")
        obj = json.loads(p.stdout)
    if not isinstance(obj, dict) or "kind" not in obj:
        raise RuntimeError(f"Expected a single Kubernetes object in {path}")
    return obj


def encode_obj(obj):
    if yaml:
        return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=1000)
    # JSON is syntactically valid YAML and accepted by Helm and kubectl.
    return json.dumps(obj, indent=2) + "\n"


def check_manifest(obj, kind, name, path):
    if obj.get("kind") != kind or obj.get("metadata", {}).get("name") != name:
        raise RuntimeError(f"Unexpected object in {path}: wanted {kind}/{name}")
    namespace = obj.get("metadata", {}).get("namespace", BASE_NS)
    if namespace != BASE_NS:
        raise RuntimeError(f"Incorrect namespace in {path}: {namespace}")
    if "status" in obj or "managedFields" in obj.get("metadata", {}):
        raise RuntimeError(f"{path} looks like a live kubectl export; use Git-tracked source files")


def clean_resource(obj):
    obj = copy.deepcopy(obj)
    obj.pop("status", None)
    meta = obj.setdefault("metadata", {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
        meta.pop(key, None)
    meta.get("annotations", {}).pop("argocd.argoproj.io/tracking-id", None)
    meta.get("labels", {}).pop("app.kubernetes.io/instance", None)
    return obj


def make_application(name, ns, service, environment):
    app = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd", "labels": {"lab.env": environment, "lab.service": service}},
        "spec": {
            "project": "default",
            "source": {"repoURL": REPO_URL, "targetRevision": "main",
                "path": "charts/ecommerce-workload",
                "helm": {"releaseName": service, "valueFiles": [
                    f"values/base/{service}.yaml", f"values/{environment}/{service}.yaml"]}},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": ns},
            "syncPolicy": {"automated": {"enabled": False, "prune": False, "selfHeal": False},
                "syncOptions": ["FailOnSharedResource=true", "RespectIgnoreDifferences=true", "CreateNamespace=false"]},
        }
    }
    if service == "product-service":
        app["spec"]["ignoreDifferences"] = [{"group": "apps", "kind": "Deployment",
            "name": "product-service", "namespace": ns, "jsonPointers": ["/spec/replicas"]}]
    return app


def make_platform_app():
    return {
        "apiVersion":"argoproj.io/v1alpha1", "kind":"Application",
        "metadata":{"name":"ecommerce-prod-platform", "namespace":"argocd"},
        "spec":{"project":"default",
                "source":{"repoURL":REPO_URL, "targetRevision":"main", "path":"environments/prod"},
                "destination":{"server":"https://kubernetes.default.svc","namespace":PROD_NS},
                "syncPolicy":{"automated":{"enabled":False,"prune":False,"selfHeal":False},
                    "syncOptions":["CreateNamespace=true","FailOnSharedResource=true","ServerSideApply=true"]}}
    }


def require_safe_postgres(obj):
    if obj["kind"] != "StatefulSet" or obj["metadata"]["name"] != "postgres":
        return
    spec = obj["spec"]["template"]["spec"]
    for v in spec.get("volumes", []):
        if v.get("name") == "postgres-data" and "emptyDir" in v:
            raise RuntimeError("Your current postgres StatefulSet still uses emptyDir. "
                "Refusing to generate PROD with ephemeral DB storage. Migrate DEV to EBS PVC first.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Read and validate current manifests; show planned changes")
    mode.add_argument("--write", action="store_true", help="Write Helm values, Argo CD apps, prod platform")
    args = parser.parse_args()
    repo = Path.cwd()
    expected = repo / "environments/dev/kustomization.yaml"
    if not expected.is_file() or not (repo / "argocd/ecommerce-dev-application.yaml").is_file():
        sys.exit("ERROR: Run from the ROOT of your existing ecommerce-gitops checkout.")
    if not (repo / "charts/ecommerce-workload/Chart.yaml").is_file():
        sys.exit("ERROR: Unzip the bundle into the repo first (charts/ecommerce-workload is missing).")
    outputs = {}
    if args.write and (repo / "charts/ecommerce-workload/values/base/frontend.yaml").exists():
        raise RuntimeError("Generated Helm values already exist. This is a ONE-TIME migration generator; "
            "do not rerun --write and overwrite subsequent environment edits. Backup and review manually.")
    def stage(relative, obj):
        outputs[str(relative)] = obj if isinstance(obj, str) else encode_obj(obj)

    # Build base values from ACTUAL tracked application YAMLs, not stale starter files.
    for service in SERVICES:
        folder = repo / "environments/dev" / service
        deployment = read_obj(folder / "deployment.yaml")
        svc = read_obj(folder / "service.yaml")
        check_manifest(deployment, "Deployment", service, folder / "deployment.yaml")
        check_manifest(svc, "Service", service, folder / "service.yaml")
        if deployment["spec"]["selector"]["matchLabels"] != svc["spec"]["selector"]:
            raise RuntimeError(f"Selectors don't match for {service}; inspect before conversion")
        hpa = read_obj(folder / "hpa.yaml") if (folder / "hpa.yaml").exists() else None
        pdb = read_obj(folder / "pdb.yaml") if (folder / "pdb.yaml").exists() else None
        if hpa:
            check_manifest(hpa, "HorizontalPodAutoscaler", service, folder / "hpa.yaml")
        if pdb:
            check_manifest(pdb, "PodDisruptionBudget", f"{service}-pdb", folder / "pdb.yaml")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        if not containers or containers[0]["name"] != service:
            raise RuntimeError(f"Expected first container to be {service} in {folder / 'deployment.yaml'}")
        # Do not accidentally publish credentials stored as literal container env vars.
        for container in containers:
            for env in container.get("env", []):
                name = env.get("name", "").upper()
                if any(term in name for term in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "PRIVATE_KEY")) and "value" in env:
                    raise RuntimeError(f"Refusing literal credential {name} in {service}; use secretKeyRef")
        image = containers[0]["image"]
        base = {"image": {"reference": image},
                "replicaCount": deployment["spec"].get("replicas", 1),
                "autoscaling": {"enabled": bool(hpa)},
                "resources": {"deployment": clean_resource(deployment), "service": clean_resource(svc),
                    "hpa": clean_resource(hpa) if hpa else None,
                    "pdb": clean_resource(pdb) if pdb else None}}
        if hpa:
            hspec = hpa["spec"]
            cpu_metrics = [m for m in hspec.get("metrics", []) if m.get("type") == "Resource" and m.get("resource", {}).get("name") == "cpu"]
            if not cpu_metrics:
                raise RuntimeError(f"CPU HPA metric not found for {service}; edit template logic before migration")
            base["autoscaling"].update({"minReplicas": hspec["minReplicas"],
                "maxReplicas": hspec["maxReplicas"],
                "targetCPUUtilizationPercentage": cpu_metrics[0]["resource"]["target"]["averageUtilization"]})
        stage(f"charts/ecommerce-workload/values/base/{service}.yaml", base)
        stage(f"charts/ecommerce-workload/values/dev/{service}.yaml", {
            "image": {"reference": image},
            **({"autoscaling": {"minReplicas": base["autoscaling"]["minReplicas"],
                "maxReplicas": base["autoscaling"]["maxReplicas"],
                "targetCPUUtilizationPercentage": base["autoscaling"]["targetCPUUtilizationPercentage"]}} if hpa else
               {"replicaCount": max(3, deployment["spec"].get("replicas", 1))} if service == "frontend" else
               {"replicaCount": deployment["spec"].get("replicas", 1)})})
        # Suggested capacity for PROD-SIM; not automatically deployed.
        stage(f"charts/ecommerce-workload/values/prod/{service}.yaml", {
            "image": {"reference": image},
            **({"autoscaling": {"minReplicas": 3, "maxReplicas": 6,
                "targetCPUUtilizationPercentage": base["autoscaling"]["targetCPUUtilizationPercentage"]}} if hpa else
               {"replicaCount": 3 if service == "frontend" else 2})})
        stage(f"argocd/individual/dev/{service}.yaml", make_application(f"ecommerce-dev-{service}", BASE_NS, service, "dev"))
        stage(f"argocd/individual/prod/{service}.yaml", make_application(f"ecommerce-prod-{service}", PROD_NS, service, "prod"))

    # Safely preserve all existing non-service core resources in DEV.
    kustom_path = repo / "environments/dev/kustomization.yaml"
    original_kustom = kustom_path.read_text(encoding="utf-8")
    entries = re.findall(r'^\s*-\s*([^#\s]+)', original_kustom, flags=re.MULTILINE)
    if not entries:
        raise RuntimeError("Could not identify resources list in DEV kustomization.yaml")
    def workload_path(p):
        parts = p.strip("./").split("/")
        return len(parts) > 1 and parts[0] in SERVICES and parts[1] in ("deployment.yaml", "service.yaml", "hpa.yaml", "pdb.yaml")
    retained = [x for x in entries if not workload_path(x)]
    removed = [x for x in entries if workload_path(x)]
    if not removed:
        raise RuntimeError("No service resources found in DEV Kustomization. Did you already run the migration?")
    if not any("database/" in s for s in retained):
        raise RuntimeError("No database resources in core Kustomization; refusing to proceed")
    # Rewrite only resources-list entries, preserving all additional Kustomize fields and comments.
    new_kustom_lines = []
    for line in original_kustom.splitlines(keepends=True):
        match = re.match(r'^\s*-\s*([^#\s]+)', line)
        if match and workload_path(match.group(1)):
            continue
        new_kustom_lines.append(line)
    stage("environments/dev/kustomization.yaml", "".join(new_kustom_lines))

    # Keep current ecommerce-dev Application identity, but make it CORE-only and paused.
    old_app_path = repo / "argocd/ecommerce-dev-application.yaml"
    app = read_obj(old_app_path)
    if app.get("kind") != "Application" or app.get("metadata", {}).get("name") != "ecommerce-dev":
        raise RuntimeError("Existing Application is not ecommerce-dev")
    spec = app["spec"]
    candidates = list(spec.get("sources", [])) + ([spec["source"]] if "source" in spec else [])
    kustom_source = next((s for s in candidates if s.get("path") == "environments/dev"), None)
    if not kustom_source:
        raise RuntimeError("Existing Argo CD Application has no environments/dev source")
    spec["source"] = copy.deepcopy(kustom_source)
    spec.pop("sources", None)
    spec.pop("ignoreDifferences", None) # No app HPA objects remain in platform.
    sync = spec.setdefault("syncPolicy", {})
    sync["automated"] = {"enabled": False, "prune": False, "selfHeal": False}
    opts = sync.setdefault("syncOptions", [])
    if "PruneLast=true" in opts:
        opts.remove("PruneLast=true")
    spec.setdefault("destination", {"server": "https://kubernetes.default.svc", "namespace": BASE_NS})
    stage("argocd/ecommerce-dev-application.yaml", app)

    # PROD platform. Clone only currently tracked core resources; NEVER clone a secret.
    prod_entries = []
    pvc_found = False
    postgres_statefulset_found = False
    for item in retained:
        f = repo / "environments/dev" / item
        if not f.is_file():
            raise RuntimeError(f"Core Kustomize entry isn't a file: {item}. Inspect manually.")
        obj = read_obj(f)
        kind = obj["kind"]
        if kind == "StorageClass":  # Shared cluster-wide; do not duplicate in prod.
            continue
        if kind in ("PrometheusRule", "AlertmanagerConfig", "Ingress", "Secret"):
            continue  # Namespaced alert rules and credentials need separate environment policy.
        new_obj = clean_resource(obj)
        if kind == "Namespace":
            new_obj["metadata"]["name"] = PROD_NS
            new_obj["metadata"].setdefault("labels", {}).pop("alertmanager-configs", None)
        else:
            new_obj["metadata"]["namespace"] = PROD_NS
        if kind == "StatefulSet" and new_obj["metadata"]["name"] == "postgres":
            require_safe_postgres(new_obj)
            postgres_statefulset_found = True
        if kind == "PersistentVolumeClaim" and new_obj["metadata"]["name"] == "postgres-data":
            pvc_found = True
        # Keep original relative tree, with a generated .json extension for pure-stdlib compatibility.
        new_file = str(Path(item).with_suffix(".json"))
        stage(f"environments/prod/{new_file}", json.dumps(new_obj, indent=2) + "\n")
        prod_entries.append(new_file)
    if not postgres_statefulset_found or not pvc_found:
        raise RuntimeError("Missing postgres StatefulSet or 'postgres-data' PVC in current DEV core. "
            "PROD requires separate EBS-backed DB PVC; refusing to generate incomplete PROD.")
    stage("environments/prod/kustomization.yaml", {
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "namespace": PROD_NS, "resources": prod_entries})
    stage("argocd/individual/prod-platform.yaml", make_platform_app())

    print("\nMIGRATION PREVIEW - source files from your current working tree")
    print("DEV workload entries removed from old Kustomize:")
    for item in removed: print("  -", item)
    print("DEV core resources retained:")
    for item in retained: print("  -", item)
    print(f"\nWill {'WRITE' if args.write else 'GENERATE'} {len(outputs)} files across chart values, Argo CD and PROD platform.")
    print("All new Argo CD applications start with AUTO-SYNC DISABLED, PRUNING DISABLED.")
    print("DO NOT sync new apps until the old owner is removed from the GitOps desired state.")
    if args.write:
        for relative, contents in outputs.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        print("FILES WRITTEN. Inspect git diff before committing. No Kubernetes objects changed.")
    else:
        print("Preview only. Run --write after verifying these resources are correct.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError, FileNotFoundError) as exc:
        sys.exit(f"ERROR: {exc}")
