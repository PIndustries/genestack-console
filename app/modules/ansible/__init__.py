"""What: Run a host check or an allow-listed playbook.
Where: app/modules/ansible/__init__.py. This class lists the function files below, in
order.
Why: Playbooks stay in their own files, next to the playbook allowlist.
"""

from __future__ import annotations

from app.modules.base import Module


class AnsibleModule(Module):
    name = "ansible"
    functions = (
        "host_preflight",
        "host_basic_ops",
        "playbook_run",
    )
