import pytest
import yaml

from app.models import Environment
from app.services import envconfig


def test_guided_ovn_clears_base_bridge_pin_without_guessing_deployer_nic():
    doc = {'provider': 'talos', 'talos': {'cni': 'kube-ovn'}}
    files = envconfig.render_to_files(doc, Environment(id='owned', name='owned'))
    override = yaml.safe_load(files['helm-configs/kube-ovn/zz-talos-iface.yaml'])
    assert override['networking']['IFACE'] == ''
    assert override['networking']['vlan']['VLAN_INTERFACE_NAME'] == ''


def test_cluster_identity_requires_exact_reviewed_kubernetes_names_and_addresses():
    from app.services.cluster_network import verified_nodes
    doc = {'servers': {'owned': {'ip': '192.0.2.10', 'roles': ['k8s_control_plane', 'compute']}}}
    response = {'items': [{'metadata': {'name': 'owned', 'uid': 'fresh-cluster-node'},
                          'status': {'addresses': [{'type': 'InternalIP', 'address': '192.0.2.10'}]}}]}
    assert verified_nodes(doc, response) == ['owned']
    response['items'][0]['metadata']['name'] = 'foreign'
    with pytest.raises(ValueError, match='reviewed'): verified_nodes(doc, response)
