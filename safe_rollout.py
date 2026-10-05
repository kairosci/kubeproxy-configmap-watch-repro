#!/usr/bin/env python3
"""Verify startup-only kube-proxy configuration in a dedicated three-node kind cluster."""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=['docker', 'podman'], default='docker')
    parser.add_argument('--kubectl', default='kubectl')
    parser.add_argument('--kubeconfig', type=pathlib.Path, required=True)
    parser.add_argument('--proxy-binary', type=pathlib.Path, required=True)
    parser.add_argument('--mode', choices=['baseline', 'patched'], required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = args.proxy_binary.resolve()
    k = [args.kubectl, '--kubeconfig', str(args.kubeconfig.resolve()),
         '--context', 'kind-kubeproxy-safe-rollout', '-n', 'kube-system']
    result = {'mode': args.mode, 'binarySHA256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'passed': False, 'observationSeconds': 60}

    binary_name = 'kube-proxy-' + result['binarySHA256'][:12]

    def run(command):
        return subprocess.check_output(command, text=True, timeout=180).strip()

    def kubectl(*command):
        return run(k + list(command))

    def log(message):
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z') + ' ' + message
        print(line, flush=True)
        with (output / 'run.log').open('a') as stream:
            stream.write(line + '\n')

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + '\n')

    def apply(obj):
        subprocess.run(k + ['apply', '-f', '-'], input=json.dumps(obj), text=True,
                       check=True, timeout=60, stdout=subprocess.DEVNULL)

    def patch(obj):
        kubectl('patch', 'ds', 'kube-proxy', '--type=strategic', '-p', json.dumps(obj))

    def pods():
        return json.loads(kubectl('get', 'pods', '-l', 'k8s-app=kube-proxy', '-o', 'json'))['items']

    def snapshot():
        states = {}
        for p in pods():
            if p['metadata'].get('deletionTimestamp'):
                continue
            node = p['spec'].get('nodeName')
            if not node:
                continue
            statuses = p['status'].get('containerStatuses', [{}])
            s = statuses[0]
            states[node] = {'pod': p['metadata']['name'], 'uid': p['metadata']['uid'],
                            'containerID': s.get('containerID'), 'restarts': s.get('restartCount', 0),
                            'ready': s.get('ready', False),
                            'configMap': next(v['configMap']['name'] for v in p['spec']['volumes']
                                              if v['name'] == 'kube-proxy')}
        return states

    def identity(states):
        return {node: {key: state[key] for key in ('uid', 'containerID', 'restarts')}
                for node, state in states.items()}

    def wait_for(predicate, description, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(2)
        raise RuntimeError('Timed out: ' + description)

    def settled(config_map=None):
        states = snapshot()
        ds = json.loads(kubectl('get', 'ds', 'kube-proxy', '-o', 'json'))
        status = ds.get('status', {})
        if (len(states) == 3 and all(s['ready'] for s in states.values())
                and status.get('observedGeneration', 0) >= ds['metadata']['generation']
                and status.get('numberAvailable') == 3 and status.get('updatedNumberScheduled') == 3
                and (not config_map or all(s['configMap'] == config_map for s in states.values()))):
            return states

    def configz(node):
        doc = json.loads(run([args.runtime, 'exec', node, 'curl', '-fsS', '--max-time', '5',
                              'http://127.0.0.1:10249/configz']))
        return doc['kubeproxy.config.k8s.io']

    def verbosity(node):
        return configz(node).get('logging', {}).get('verbosity', 0)

    def mounted_path(state):
        return (f'/var/lib/kubelet/pods/{state["uid"]}/volumes/'
                'kubernetes.io~configmap/kube-proxy/config.conf')

    def content(node, state):
        return run([args.runtime, 'exec', node, 'cat', mounted_path(state)])

    def observe_unchanged(before, label):
        for _ in range(12):
            time.sleep(5)
            current = snapshot()
            assert identity(current) == identity(before), label + ': unexpected restart'
            assert all(s['ready'] for s in current.values()), label + ': pod lost readiness'
            assert all(verbosity(n) == 0 for n in current), label + ': running config changed'
        log(label + ': unchanged identities, readiness and loaded verbosity for 60 seconds')

    def create_config(name, data):
        apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name},
               'immutable': True, 'data': data})

    def switch_config(name):
        patch({'spec': {'template': {'spec': {'volumes': [
            {'name': 'kube-proxy', 'configMap': {'name': name}}]}}}})

    def forwarding(states, service_ip):
        for node, s in states.items():
            if s['ready']:
                response = run([args.runtime, 'exec', node, 'curl', '-fsS', '--max-time', '5',
                                'http://' + service_ip])
                assert response == 'ready', 'Service forwarding failed on ' + node

    try:
        nodes = json.loads(kubectl('get', 'nodes', '-o', 'json'))['items']
        assert len(nodes) == 3, 'Use the dedicated three-node cluster'
        for node in nodes:
            name = node['metadata']['name']
            run([args.runtime, 'exec', name, 'mkdir', '-p', '/opt/kubeproxy-safe-rollout'])
            run([args.runtime, 'cp', str(binary), name + ':/opt/kubeproxy-safe-rollout/' + binary_name])
            run([args.runtime, 'exec', name, 'chmod', '755', '/opt/kubeproxy-safe-rollout/' + binary_name])
        patch({'spec': {'minReadySeconds': 10,
                        'updateStrategy': {'type': 'RollingUpdate', 'rollingUpdate': {
                            'maxUnavailable': 1, 'maxSurge': 0}},
                        'template': {'spec': {'containers': [{
                            'name': 'kube-proxy',
                            'command': ['/repro/' + binary_name, '--config=/var/lib/kube-proxy/config.conf',
                                        '--hostname-override=$(NODE_NAME)'],
                            'volumeMounts': [{'name': 'test-binary', 'mountPath': '/repro', 'readOnly': True}],
                            'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 10256},
                                               'periodSeconds': 2, 'failureThreshold': 3}}],
                            'volumes': [{'name': 'test-binary', 'hostPath': {
                                'path': '/opt/kubeproxy-safe-rollout', 'type': 'Directory'}}]}}}})
        before = wait_for(settled, 'patched binary deployment', timeout=300)
        assert all(verbosity(n) == 0 for n in before)
        result['initial'] = before
        save('daemonset.json', json.loads(kubectl('get', 'ds', 'kube-proxy', '-o', 'json')))
        save('version.json', json.loads(kubectl('version', '-o', 'json')))
        save('configz-before.json', {n: configz(n) for n in before})
        log('INSTALLED ' + json.dumps(before))

        # Fresh watches exist here on the baseline. Mutate all nodes before any projection swap.
        for node, state in before.items():
            run([args.runtime, 'exec', node, 'sh', '-c',
                 'printf "\\n# direct-write control\\n" >> "$1"', 'sh', mounted_path(state)])
        if args.mode == 'baseline':
            def all_restarted():
                current = snapshot()
                if len(current) == 3 and all(current[n]['uid'] == s['uid']
                                             and current[n]['restarts'] > s['restarts']
                                             and current[n]['ready'] for n, s in before.items()):
                    return current
            after = wait_for(all_restarted, 'baseline direct-write restarts', timeout=90)
            for node, state in after.items():
                logs = kubectl('logs', state['pod'], '--previous')
                (output / (node + '-previous.log')).write_text(logs + '\n')
                assert "content of the proxy server's configuration file was updated" in logs
            result['directWrite'] = after
            result['passed'] = True
            log('PASS: baseline direct writes restarted kube-proxy on all three nodes')
            return

        observe_unchanged(before, 'DIRECT WRITE')
        result['directWrite'] = snapshot()
        cm = json.loads(kubectl('get', 'cm', 'kube-proxy', '-o', 'json'))
        original = cm['data']['config.conf']
        assert original.count('  verbosity: 0') == 1
        updated = original.replace('  verbosity: 0', '  verbosity: 1')
        marker = '# safe-rollout projection ' + str(time.time_ns())
        updated += '\n' + marker + '\n'
        targets = {n: run([args.runtime, 'exec', n, 'readlink', '-f', mounted_path(s)])
                   for n, s in before.items()}
        kubectl('patch', 'cm', 'kube-proxy', '--type=merge', '-p',
                json.dumps({'data': {'config.conf': updated}}))
        def projected():
            assert identity(snapshot()) == identity(before), 'Restart during projection'
            return all(content(n, s) == updated.strip() for n, s in before.items())
        wait_for(projected, 'ConfigMap projection on all nodes', timeout=240)
        for node, state in before.items():
            assert run([args.runtime, 'exec', node, 'readlink', '-f', mounted_path(state)]) != targets[node]
        observe_unchanged(before, 'CONFIGMAP PROJECTION')
        result['afterProjection'] = snapshot()

        # Give healthy kube-proxy processes a real ClusterIP Service to forward.
        apply({'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'rollout-echo'},
               'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'rollout-echo'}},
                        'template': {'metadata': {'labels': {'app': 'rollout-echo'}}, 'spec': {
                            'containers': [{'name': 'echo', 'image': 'docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e',
                                            'command': ['sh', '-c', 'mkdir -p /www; echo ready > /www/index.html; exec httpd -f -p 8080 -h /www'],
                                            'readinessProbe': {'httpGet': {'path': '/', 'port': 8080}},
                                            'ports': [{'containerPort': 8080}]}]}}}})
        apply({'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'rollout-echo'},
               'spec': {'selector': {'app': 'rollout-echo'}, 'ports': [{'port': 80, 'targetPort': 8080}]}})
        kubectl('rollout', 'status', 'deployment/rollout-echo', '--timeout=180s')
        service_ip = kubectl('get', 'svc', 'rollout-echo', '-o', 'jsonpath={.spec.clusterIP}')
        time.sleep(5)
        forwarding(before, service_ip)
        save('echo-pod.json', json.loads(kubectl('get', 'pods', '-l', 'app=rollout-echo', '-o', 'json')))

        valid_data = dict(cm['data'], **{'config.conf': updated})
        create_config('kube-proxy-rollout-good', valid_data)
        switch_config('kube-proxy-rollout-good')
        samples = []
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = snapshot()
            unchanged = sum(n in current and identity({n: current[n]}) == identity({n: s})
                            for n, s in before.items())
            ready = sum(s['ready'] for s in current.values())
            samples.append({'time': time.time(), 'ready': ready, 'unchanged': unchanged, 'pods': current})
            assert ready >= 2, 'Good rollout made more than one node unavailable'
            forwarding(current, service_ip)
            if settled('kube-proxy-rollout-good'):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Good rollout did not complete')
        good = snapshot()
        assert all(good[n]['uid'] != before[n]['uid'] and verbosity(n) == 1 for n in good)
        result['goodRollout'] = good
        save('good-rollout-samples.json', samples)
        save('configz-after-good.json', {n: configz(n) for n in good})
        log('GOOD ROLLOUT: three replacements, loaded verbosity 1, at least two ready nodes')

        # Invalid startup config fails readiness, so the controller must retain the other two.
        invalid_data = dict(valid_data)
        invalid_data['config.conf'], count = re.subn(r'^mode:.*$', 'mode: definitely-invalid', updated, flags=re.MULTILINE)
        assert count == 1, 'Expected one proxy mode setting'
        create_config('kube-proxy-rollout-bad', invalid_data)
        switch_config('kube-proxy-rollout-bad')
        def first_bad():
            current = snapshot()
            bad = [s for s in current.values() if s['configMap'] == 'kube-proxy-rollout-bad']
            if len(bad) == 1 and not bad[0]['ready'] and bad[0]['restarts'] >= 1:
                return current
        blocked = wait_for(first_bad, 'bad config startup failure', timeout=120)
        bad_node = next(n for n, s in blocked.items() if s['configMap'] == 'kube-proxy-rollout-bad')
        survivors = {n: s for n, s in good.items() if n != bad_node}
        logs = kubectl('logs', blocked[bad_node]['pod'], '--previous')
        assert 'definitely-invalid' in logs and 'Invalid' in logs, 'Unexpected startup failure'
        (output / 'invalid-config.log').write_text(logs + '\n')
        for _ in range(12):
            time.sleep(5)
            current = snapshot()
            assert len(current) == 3
            assert sum(s['configMap'] == 'kube-proxy-rollout-bad' for s in current.values()) == 1
            for node, state in survivors.items():
                assert identity({node: current[node]}) == identity({node: state})
                assert current[node]['ready'] and verbosity(node) == 1
            forwarding({n: current[n] for n in survivors}, service_ip)
        result['blockedBadRollout'] = snapshot()
        log('BAD ROLLOUT: stopped at one failing pod for 60 seconds; two original pods still forward Service traffic')
        switch_config('kube-proxy-rollout-good')
        recovered = wait_for(lambda: settled('kube-proxy-rollout-good'), 'rollback recovery', timeout=240)
        assert all(verbosity(n) == 1 for n in recovered)
        forwarding(recovered, service_ip)
        result['rollback'] = recovered
        result['passed'] = True
        log('PASS: rollback restored three ready kube-proxy pods')
    finally:
        save('result.json', result)
        for p in pods():
            try:
                (output / (p['metadata']['name'] + '.log')).write_text(kubectl('logs', p['metadata']['name']) + '\n')
            except subprocess.CalledProcessError:
                pass


if __name__ == '__main__':
    main()
