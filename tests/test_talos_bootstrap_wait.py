import pytest

from app.services import talos


def test_bootstrap_waits_through_install_and_reboot_but_does_not_repeat_after_success(monkeypatch):
    results = iter([{'returncode': 1, 'stderr': 'bootstrap is not available yet'},
                    {'returncode': 1, 'stderr': 'connection refused'}, {'returncode': 0}])
    calls = []
    monkeypatch.setattr(talos.bridge, 'run_command', lambda *a, **k: calls.append(k) or next(results))
    monkeypatch.setattr(talos.time, 'sleep', lambda seconds: None)
    checked = []
    result = talos._bootstrap_with_retry(['talosctl', 'bootstrap'], timeout=900,
        check_cancel=lambda: checked.append(True), log=lambda line: None)
    assert result['returncode'] == 0 and len(calls) == 3 and len(checked) == 3
    assert all(call['timeout'] <= 30 for call in calls)


def test_bootstrap_fatal_error_does_not_retry(monkeypatch):
    monkeypatch.setattr(talos.bridge, 'run_command', lambda *a, **k: {'returncode': 1, 'stderr': 'certificate signed by unknown authority'})
    monkeypatch.setattr(talos.time, 'sleep', lambda seconds: pytest.fail('Must not retry a different identity'))
    result = talos._bootstrap_with_retry(['talosctl', 'bootstrap'], timeout=900)
    assert result['returncode'] == 1


def test_bootstrap_deadline_and_cancellation_are_bounded(monkeypatch):
    monkeypatch.setattr(talos.time, 'monotonic', lambda: 10)
    monkeypatch.setattr(talos.bridge, 'run_command', lambda *a, **k: pytest.fail('Expired job must not run'))
    assert talos._bootstrap_with_retry(['talosctl', 'bootstrap'], timeout=900, deadline=9)['returncode'] == 124
    def cancel(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        talos._bootstrap_with_retry(['talosctl', 'bootstrap'], timeout=900, check_cancel=cancel)



def test_guided_firewall_allows_its_pods_without_broadening_node_ip_selection():
    import yaml

    doc = {'talos': {'cni': 'kube-ovn', 'cluster_cidrs': ['10.77.28.0/24']},
           'servers': {'owned': {'ip': '10.77.28.191', 'private_ip': '10.77.28.191', 'roles': ['k8s_control_plane']}}}
    firewall = talos.firewall_yaml(doc)
    assert '10.236.0.0/14' in firewall
    assert '100.64.0.0/16' in firewall
    assert '0.0.0.0/0' not in firewall
    patch = list(yaml.safe_load_all(talos.network_patch_yaml(doc)))
    node = next(d for d in patch if d.get('kind') == 'KubeNodeConfig')
    assert node['nodeIP']['validSubnets'] == ['10.77.28.0/24']
