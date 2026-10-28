"""Exercise the real setup helper with a fake host NIC, without cluster access."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "app/assets/site-network.sh"


class SiteNetworkTests(unittest.TestCase):
    def run_setup(self, values):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "ip"
            fake.write_text('#!/bin/sh\nprintf called >> "$GS_TEST_IP_CALLS"\nexit 0\n')
            fake.chmod(0o755)
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(
                    ("CONTAINER_", "COMPUTE_", "OVN_", "GENESTACK_NETWORK_")
                )
            }
            env.update(
                PATH=directory + ":" + env["PATH"],
                GS_TEST_IP_CALLS=str(root / "calls"),
                **values,
            )
            code = 'set -e; source "$1"; genestack_site_network; printf "RESULT:%s|%s|%s\\n" "${CONTAINER_INTERFACE:-}" "${CONTAINER_VLAN_INTERFACE:-}" "${OVN_EXTERNAL_INTERFACE:-}"'
            result = subprocess.run(
                ["bash", "-c", code, "_", str(HELPER)],
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, (root / "calls").exists()

    def test_automatic_mode_does_not_guess_a_deployer_nic_or_provider_port(self):
        result, called = self.run_setup({"GENESTACK_NETWORK_INTERFACES": "automatic"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT:||", result.stdout)
        self.assertFalse(called)

    def test_manual_compute_interface_is_never_an_implicit_provider_port(self):
        result, called = self.run_setup(
            {"CONTAINER_INTERFACE": "br-overlay", "COMPUTE_INTERFACE": "management0"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT:br-overlay|br-overlay|", result.stdout)
        self.assertFalse(called)

    def test_explicit_provider_vlan_is_preserved(self):
        result, _ = self.run_setup(
            {
                "GENESTACK_NETWORK_INTERFACES": "automatic",
                "OVN_EXTERNAL_INTERFACE": "provider0.200",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT:||provider0.200", result.stdout)

    def test_partial_provider_vlan_and_unknown_mode_stop(self):
        for values in [
            {"GENESTACK_NETWORK_INTERFACES": "unknown"},
            {
                "GENESTACK_NETWORK_INTERFACES": "automatic",
                "OVN_EXTERNAL_VLAN_ID": "200",
            },
        ]:
            result, _ = self.run_setup(values)
            self.assertNotEqual(result.returncode, 0)

    def test_automatic_mode_rejects_conflicting_overrides(self):
        result, called = self.run_setup(
            {
                "GENESTACK_NETWORK_INTERFACES": "automatic",
                "CONTAINER_INTERFACE": "management0",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(called)

    def test_manual_mode_still_rejects_shared_interfaces(self):
        result, _ = self.run_setup(
            {"CONTAINER_INTERFACE": "management0", "COMPUTE_INTERFACE": "management0"}
        )
        self.assertNotEqual(result.returncode, 0)

    def test_provider_annotation_removes_stale_uplink_or_sets_explicit_port(self):
        for port in ["", "provider0.200"]:
            env = dict(os.environ, OVN_EXTERNAL_INTERFACE=port)
            code = 'source "$1"; kubectl() { printf "%s\\n" "$@"; }; genestack_annotate_provider_ports'
            result = subprocess.run(
                ["bash", "-c", code, "_", str(HELPER)],
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = (
                "ovn.openstack.org/ports=br-ex:" + port
                if port
                else "ovn.openstack.org/ports-"
            )
            self.assertIn(expected, result.stdout.splitlines())
            self.assertNotIn(
                "ovn.openstack.org/ports=br-ex:", result.stdout.splitlines()
            )


if __name__ == "__main__":
    unittest.main()
