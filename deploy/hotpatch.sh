#!/bin/sh
# Hot-patch deploy: push the local code + manifests to the cluster, then restart.
# The MQTT credentials Secret (hxlink-mqtt) is created separately and never stored in the repo.
set -eu
cd "$(dirname "$0")/.."

kubectl apply -f deploy/namespace.yaml
kubectl -n hxlink create configmap hxlink-code --from-file=hxlink/ --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/pvc.yaml -f deploy/deployment.yaml
kubectl -n hxlink rollout restart deploy/hxlink
kubectl -n hxlink rollout status deploy/hxlink --timeout=180s
