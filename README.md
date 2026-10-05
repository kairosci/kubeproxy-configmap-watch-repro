# kube-proxy ConfigMap watch reproduction

Reproduces the behavior reported in [kubernetes/kubernetes#132877](https://github.com/kubernetes/kubernetes/issues/132877), discussed in [PR #139204](https://github.com/kubernetes/kubernetes/pull/139204#issuecomment-5997426575).

On Kubernetes v1.33.1, updating the mounted ConfigMap delivers new bytes to the configuration file without restarting kube-proxy. Writing directly to the watched file causes kube-proxy to exit with its config-change error and restart.

The reproduction uses an **unmodified upstream kind node image**. It does not install the proposed PR patch.

## What the script verifies

The script records kube-proxy's pod UID, container ID and restart count, then changes `logging.verbosity` from `0` to `1` in the `kube-proxy` ConfigMap and adds a unique marker comment. Packet forwarding settings are unchanged.

It waits up to 180 seconds for kubelet to project the update, verifies the exact new content, and checks that the resolved timestamped directory behind the `..data` symlink changed. It then observes kube-proxy for a further 60 seconds, asserting that the pod UID, container ID and restart count remain unchanged.

For the control, the script recreates the test pod to establish a fresh file watch and appends a YAML comment directly to its projected configuration file through the kind node container. It requires an increased restart count within 60 seconds and confirms that the previous container logged:

```text
content of the proxy server's configuration file was updated
```

A successful run means **the mismatch was reproduced**, not that the bug was fixed. The control requires a fresh pod because the original watched inode is removed during the ConfigMap projection update.

## Run locally

Requirements: Python 3, kind v0.33.0, kubectl v1.33.1, and Docker or Podman. No Python dependencies are needed. Use a dedicated single-node cluster named `kubeproxy-watch-repro`; the script changes its kube-proxy ConfigMap and recreates its kube-proxy pod.

With Docker:

```bash
export KIND_EXPERIMENTAL_PROVIDER=docker
kind create cluster --name kubeproxy-watch-repro \
  --image kindest/node:v1.33.1@sha256:14ffd6ee8a3daa20cc934ba786626b181e1797268c5465f2c299a7cf54494c77 \
  --kubeconfig "$PWD/kubeconfig" --wait 120s
python3 -u reproduce.py --runtime docker --kubeconfig "$PWD/kubeconfig" --output artifacts
kind delete cluster --name kubeproxy-watch-repro --kubeconfig "$PWD/kubeconfig"
```

With rootless Podman on Linux/systemd, replace the create and reproduction commands with:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
systemd-run --scope --user -p Delegate=yes kind create cluster \
  --name kubeproxy-watch-repro \
  --image kindest/node:v1.33.1@sha256:14ffd6ee8a3daa20cc934ba786626b181e1797268c5465f2c299a7cf54494c77 \
  --kubeconfig "$PWD/kubeconfig" --wait 120s
python3 -u reproduce.py --runtime podman --kubeconfig "$PWD/kubeconfig" --output artifacts
kind delete cluster --name kubeproxy-watch-repro --kubeconfig "$PWD/kubeconfig"
```

The script always selects the `kind-kubeproxy-watch-repro` context explicitly. The kubeconfig is ignored by Git and is never uploaded as evidence.

See kind's [installation instructions](https://kind.sigs.k8s.io/docs/user/quick-start/) and [rootless requirements](https://kind.sigs.k8s.io/docs/user/rootless/) if needed.

## Evidence

The [initial local run](evidence/local-comment-2026-10-05/) used Linux amd64, rootless Podman and Kubernetes v1.33.1. This earlier run changed only a YAML comment. Projection took about 47 seconds, with no restart during 60 additional seconds. The direct-write control restarted within about 2 seconds.

The [configuration-change local run](evidence/local-verbosity-2026-10-05/) used the current script to change `logging.verbosity` from 0 to 1. Projection took 42.83 seconds, with no restart for another 60 seconds. The direct-write control restarted in 2.35 seconds.

The [successful CI run](https://github.com/kairosci/kubeproxy-configmap-watch-repro/actions/runs/37338417891) used Docker on Ubuntu 24.04 and the same unmodified v1.33.1 image. Projection took 72.31 seconds, with no restart for another 60 seconds. The direct-write control restarted in 2.22 seconds. The [permanent CI evidence](evidence/ci-docker-2026-10-05/) preserves the results and logs in this repository.

[GitHub Actions](https://github.com/kairosci/kubeproxy-configmap-watch-repro/actions/workflows/reproduce.yml) runs the current script on Ubuntu with Docker. It uploads logs, the mounted configuration and structured results as `kubeproxy-watch-evidence`, and deletes the cluster even after failures. Artifacts are retained for 30 days.

The node image is pinned by digest; tool versions and action commits are pinned. The checked-in evidence contains no kubeconfig credentials.

## Scope

This establishes a concrete config-change detection mismatch on v1.33.1. It does not establish that automatic kube-proxy restarts on ConfigMap updates are the desired rollout policy, nor validate the PR's implementation. Enabling those restarts could affect all nodes after a shared ConfigMap update; that behavior change needs separate consideration.

The reproduction code and documentation were prepared with assistance from Codex and verified against a real local cluster.
