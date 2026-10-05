#!/usr/bin/env python3
"""Reproduce #132877 in the dedicated local kind cluster."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', choices=['docker', 'podman'], default='docker')
parser.add_argument('--kubectl', default='kubectl')
parser.add_argument('--kubeconfig', type=pathlib.Path, required=True)
parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path('artifacts'))
args = parser.parse_args()
ROOT = args.output.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
NODE = "kubeproxy-watch-repro-control-plane"
K = [args.kubectl, "--kubeconfig", str(args.kubeconfig.resolve()), "--context", "kind-kubeproxy-watch-repro"]

def run(args):
    return subprocess.check_output(args, text=True, timeout=180).strip()

def kubectl(*args):
    return run(K + list(args))

def pod():
    items = json.loads(kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-proxy", "-o", "json"))["items"]
    assert len(items) == 1
    return items[0]

def state():
    p = pod()
    s = p["status"]["containerStatuses"][0]
    return {"pod": p["metadata"]["name"], "uid": p["metadata"]["uid"], "restarts": s["restartCount"], "containerID": s.get("containerID")}

def log(message):
    line = time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message
    print(line, flush=True)
    with (ROOT / "reproduction.log").open("a") as stream:
        stream.write(line + "\n")

kubectl("-n", "kube-system", "rollout", "status", "daemonset/kube-proxy", "--timeout=120s")
p = pod()
before = state()
log("BEFORE " + json.dumps(before))
log("VERSION " + kubectl("version", "-o", "json"))
(ROOT / "daemonset.json").write_text(kubectl("-n", "kube-system", "get", "ds", "kube-proxy", "-o", "json"))
cm = json.loads(kubectl("-n", "kube-system", "get", "cm", "kube-proxy", "-o", "json"))
(ROOT / "configmap-before.json").write_text(json.dumps(cm, indent=2))
path = f'/var/lib/kubelet/pods/{before["uid"]}/volumes/kubernetes.io~configmap/kube-proxy/config.conf'

def file_content():
    return run([args.runtime, "exec", NODE, "cat", path])

old = file_content()
old_target = run([args.runtime, "exec", NODE, "readlink", "-f", path])
log("MOUNT BEFORE " + old_target)
assert old == cm["data"]["config.conf"].strip()
# Change log verbosity without changing packet forwarding rules.
marker = "# kubeproxy-watch-repro ConfigMap update " + str(time.time_ns())
if old.count("  verbosity: 0") != 1:
    raise RuntimeError("Expected exactly one logging verbosity: 0 setting")
new = cm["data"]["config.conf"].replace("  verbosity: 0", "  verbosity: 1").rstrip() + "\n" + marker + "\n"
patch = json.dumps({"data": {"config.conf": new}})
kubectl("-n", "kube-system", "patch", "cm", "kube-proxy", "--type=merge", "-p", patch)
log("CONFIGMAP PATCHED logging.verbosity: 0 -> 1; " + marker)
projection_started = time.monotonic()
deadline = time.monotonic() + 180
while marker not in file_content():
    if time.monotonic() > deadline:
        raise RuntimeError("ConfigMap update never reached the mounted file")
    assert state() == before, "kube-proxy restarted before projection completed"
    time.sleep(5)
    log("WAITING FOR PROJECTION " + json.dumps(state()))
new_target = run([args.runtime, "exec", NODE, "readlink", "-f", path])
assert new_target != old_target, "Atomic projection target did not change"
assert file_content() == new.strip(), "Mounted content differs from the patched ConfigMap"
projection_seconds = round(time.monotonic() - projection_started, 2)
(ROOT / "mounted-config-after.yaml").write_text(file_content() + "\n")
log("MOUNT UPDATED " + new_target)
log("CONTENT SHA256 " + hashlib.sha256(file_content().encode()).hexdigest())
for _ in range(6):
    time.sleep(10)
    log("AFTER CONFIGMAP " + json.dumps(state()))
    assert state() == before, "kube-proxy restarted during observation"
after = state()
assert after == before, "kube-proxy restarted after ConfigMap update; bug not reproduced"
log("REPRODUCED: mounted config changed, container identity and restart count unchanged")
# The watcher remains attached to the original inode, which kubelet deletes.
# Recreate the pod so that the direct-write control has a freshly registered watch.
kubectl("-n", "kube-system", "delete", "pod", before["pod"], "--wait=true")
kubectl("-n", "kube-system", "rollout", "status", "daemonset/kube-proxy", "--timeout=120s")
control = state()
assert control["uid"] != before["uid"], "Control requires a fresh pod"
path = f'/var/lib/kubelet/pods/{control["uid"]}/volumes/kubernetes.io~configmap/kube-proxy/config.conf'
log("CONTROL BEFORE " + json.dumps(control))
control_started = time.monotonic()
run([args.runtime, "exec", NODE, "sh", "-c", 'printf "\n# direct-write control\n" >> "$1"', "sh", path])
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    current = state()
    if current["restarts"] > control["restarts"]:
        log("CONTROL RESTARTED " + json.dumps(current))
        previous = kubectl("-n", "kube-system", "logs", current["pod"], "--previous")
        (ROOT / "control-previous.log").write_text(previous + "\n")
        assert "content of the proxy server's configuration file was updated" in previous, "Unexpected reason for control restart"
        break
    time.sleep(2)
else:
    raise RuntimeError("Direct-write control did not restart kube-proxy")
(ROOT / "result.json").write_text(json.dumps({"reproduced": True, "before": before, "afterConfigMap": after, "controlBefore": control, "controlAfter": current, "marker": marker, "configChange": "logging.verbosity: 0 -> 1", "projectionSeconds": projection_seconds, "observationSeconds": 60, "controlRestartSeconds": round(time.monotonic() - control_started, 2), "oldTarget": old_target, "newTarget": new_target}, indent=2))
log("PASS: ConfigMap replacement missed, direct write detected")
