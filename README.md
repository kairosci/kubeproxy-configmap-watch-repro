# kube-proxy configuration: reproduction and explicit rollout

Reproduces the behavior reported in [kubernetes/kubernetes#132877](https://github.com/kubernetes/kubernetes/issues/132877), discussed in [PR #139204](https://github.com/kubernetes/kubernetes/pull/139204#issuecomment-5997426575).

On Kubernetes v1.33.1, updating the mounted ConfigMap delivers new bytes to the configuration file without restarting kube-proxy. Writing directly to the watched file causes kube-proxy to exit with its config-change error and restart.

The reproduction uses an **unmodified upstream kind node image**. It does not install the proposed PR patch.


## Explicit rollout verification

The proposed replacement for PR #139204 removes kube-proxy's config-file watcher. Configuration is loaded once at startup; updates to a mounted file, including ConfigMap projections, do not restart the process. Applying a new configuration requires an explicit restart. For a DaemonSet, use a new, immutable ConfigMap and change its reference in the pod template so the controller can roll out the update incrementally.

[`safe_rollout.py`](safe_rollout.py) verifies this approach in a dedicated three-node kind cluster. It installs two kube-proxy binaries built from the **same Kubernetes source revision**, first without the change and then with [`patches/kube-proxy-startup-config.patch`](patches/kube-proxy-startup-config.patch). The source is pinned to PR #139204's base, `7de79d40aae24d6baca355d7f2053e1f3c104a74`; the patch contains the implementation and unit-test changes. The kind control plane and container userspace remain v1.33.1. This is an isolated, mixed-version behavior test, not a supported production version-skew deployment or a full networking compatibility test.

The baseline must restart all three kube-proxy containers when their mounted files are edited directly. With the patch, direct writes and a ConfigMap projection must preserve pod UIDs, container IDs, restart counts, readiness, and the running verbosity reported by `/configz` for 60 seconds each. The ConfigMap test changes verbosity from 0 to 1 and verifies that the new file contents and symlink target reached every node while the running configuration remains at 0.

The script then creates an immutable ConfigMap with verbosity 1 and updates the DaemonSet reference. It sets `maxUnavailable: 1`, `maxSurge: 0`, a `/healthz` readiness probe and `minReadySeconds: 10`. It checks that the rollout replaces all three pods, loads verbosity 1, and keeps at least two nodes ready. It also checks ClusterIP Service forwarding from ready nodes.

Finally, it rolls out another immutable ConfigMap with an invalid proxy mode. The first replacement must fail startup, and the controller must retain the other two healthy processes for 60 seconds. Their identities and loaded configuration must remain unchanged, and both must still forward Service traffic. Switching back to the good ConfigMap must restore three ready pods.

A readiness probe is essential for this failure-containment check: the default kind kube-proxy DaemonSet has none. `/healthz` detects the startup failure used here; it does **not** prove that every syntactically valid configuration preserves application traffic. Such changes still require monitoring and an operator's decision to stop or reverse the rollout. Removing the watcher also changes existing direct-file-write behavior; it is a proposed policy change that needs upstream agreement.

The [successful local three-node run](evidence/explicit-rollout-local-2026-10-05/) used rootless Podman on Linux amd64. Both unit tests and race-detector tests passed. The baseline restarted all three containers on direct writes. The patched run passed both 60-second no-restart checks, the valid rollout (minimum two ready nodes observed), the invalid-config containment check, Service forwarding, and rollback to three ready pods. The evidence includes both binaries' hashes, the source revision, and the patch hash.

### Run the rollout verification locally

Use Go 1.27.1, the pinned kind/kubectl tools described below, and a dedicated clone of Kubernetes. Build the baseline before applying the patch:

```bash
git clone https://github.com/kubernetes/kubernetes.git kubernetes-source
git -C kubernetes-source checkout 7de79d40aae24d6baca355d7f2053e1f3c104a74
mkdir -p bin
(cd kubernetes-source && CGO_ENABLED=0 go build -p 2 -o ../bin/kube-proxy-baseline ./cmd/kube-proxy)
git -C kubernetes-source apply ../patches/kube-proxy-startup-config.patch
(cd kubernetes-source && go test -p 2 ./cmd/kube-proxy/app)
(cd kubernetes-source && CGO_ENABLED=0 go build -p 2 -o ../bin/kube-proxy-patched ./cmd/kube-proxy)
export KIND_EXPERIMENTAL_PROVIDER=docker
kind create cluster --name kubeproxy-safe-rollout --config kind-safe-rollout.yaml \
  --image kindest/node:v1.33.1@sha256:14ffd6ee8a3daa20cc934ba786626b181e1797268c5465f2c299a7cf54494c77 \
  --kubeconfig "$PWD/kubeconfig" --wait 180s
python3 -u safe_rollout.py --runtime docker --kubeconfig "$PWD/kubeconfig" \
  --proxy-binary bin/kube-proxy-baseline --mode baseline --output artifacts/baseline
python3 -u safe_rollout.py --runtime docker --kubeconfig "$PWD/kubeconfig" \
  --proxy-binary bin/kube-proxy-patched --mode patched --output artifacts/patched
kind delete cluster --name kubeproxy-safe-rollout --kubeconfig "$PWD/kubeconfig"
```

For rootless Podman, use `KIND_EXPERIMENTAL_PROVIDER=podman`, create the cluster inside `systemd-run --scope --user -p Delegate=yes`, and pass `--runtime podman` to both script invocations. The scripts explicitly target the `kind-kubeproxy-safe-rollout` context. They replace kube-proxy binaries, modify its DaemonSet and ConfigMaps, and deploy a small HTTP test service only in this dedicated cluster.

The [explicit-rollout workflow](.github/workflows/safe-rollout.yml) repeats the comparison with Docker on Ubuntu 24.04 and uploads logs, binary hashes, unit-test output, `/configz` snapshots, rollout samples, and results. Cluster deletion runs even after failures. The original mismatch reproduction below is preserved as a separate test against an unmodified upstream image.

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

The original unmodified-image reproduction establishes a concrete config-change detection mismatch on v1.33.1. By itself, it does not establish that automatic restarts are the desired rollout policy or validate a fix. The separate explicit-rollout verification above tests startup-only configuration and failure containment with new ConfigMap references. Both the policy change and its compatibility implications need upstream review.

The reproduction and rollout-verification code, patch, and documentation were prepared with assistance from Codex and verified against real local clusters.
