"""Cloud-init user-data for Genestack nodes provisioned via MAAS.

Renders a Jinja2 template string suitable for MAAS machine deploy user-data.
Placeholders cover hostname, SSH authorized keys, and ``GENESTACK_ENV``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jinja2 import BaseLoader, Environment, StrictUndefined

# Genestack node cloud-init: sets hostname, installs baseline packages,
# injects SSH keys, and exports GENESTACK_ENV for later Ansible/bridge steps.
GENESTACK_NODE_USERDATA_TEMPLATE = """\
#cloud-config
# Genestack node user-data (MAAS deploy)
# Labels / roles (for reference; applied as MAAS tags / k8s labels later):
#   openstack-control-plane | compute | network | storage
hostname: {{ hostname }}
fqdn: {{ fqdn | default(hostname) }}
manage_etc_hosts: true
package_update: {{ package_update | default(true) | lower }}
package_upgrade: {{ package_upgrade | default(false) | lower }}
packages:
  - python3
  - python3-pip
  - openssh-server
  - curl
  - jq
  - lvm2
{% if extra_packages is defined and extra_packages %}
{% for pkg in extra_packages %}
  - {{ pkg }}
{% endfor %}
{% endif %}

users:
  - default
  - name: {{ admin_user | default('ubuntu') }}
    groups: [sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
{% if ssh_keys %}
{% for key in ssh_keys %}
      - {{ key }}
{% endfor %}
{% else %}
      # SSH keys placeholder — pass ssh_keys when rendering
      - ssh-ed25519 AAAA_REPLACE_ME genestack-console-placeholder
{% endif %}

write_files:
  - path: /etc/profile.d/genestack.sh
    permissions: '0644'
    content: |
      # Genestack Console environment marker
      export GENESTACK_ENV="{{ genestack_env | default('default') }}"
{% if genestack_role is defined and genestack_role %}
      export GENESTACK_ROLE="{{ genestack_role }}"
{% endif %}
  - path: /etc/genestack/env
    permissions: '0644'
    content: |
      GENESTACK_ENV={{ genestack_env | default('default') }}
{% if genestack_role is defined and genestack_role %}
      GENESTACK_ROLE={{ genestack_role }}
{% endif %}

runcmd:
  - [ mkdir, -p, /etc/genestack ]
  - [ bash, -c, "echo GENESTACK_ENV={{ genestack_env | default('default') }} >> /etc/environment" ]
{% if runcmd_extra is defined and runcmd_extra %}
{% for cmd in runcmd_extra %}
  - {{ cmd }}
{% endfor %}
{% endif %}

final_message: "Genestack node {{ hostname }} cloud-init complete (env={{ genestack_env | default('default') }})"
"""


def render_userdata(
    *,
    hostname: str,
    genestack_env: str = "default",
    ssh_keys: Sequence[str] | None = None,
    fqdn: str | None = None,
    genestack_role: str | None = None,
    admin_user: str = "ubuntu",
    extra_packages: Sequence[str] | None = None,
    package_update: bool = True,
    package_upgrade: bool = False,
    runcmd_extra: Sequence[Any] | None = None,
    extra_context: Mapping[str, Any] | None = None,
) -> str:
    """Render cloud-init user-data for a Genestack node.

    Parameters
    ----------
    hostname:
        Node hostname (also used as FQDN when ``fqdn`` is omitted).
    genestack_env:
        Value for ``GENESTACK_ENV`` (environment name in the console).
    ssh_keys:
        List of SSH public key strings; a placeholder key is used if empty.
    genestack_role:
        Optional role hint (control-plane, compute, network, storage).
    """
    env = Environment(
        loader=BaseLoader(),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    template = env.from_string(GENESTACK_NODE_USERDATA_TEMPLATE)
    context: dict[str, Any] = {
        "hostname": hostname,
        "fqdn": fqdn or hostname,
        "genestack_env": genestack_env,
        "ssh_keys": list(ssh_keys or []),
        "admin_user": admin_user,
        "package_update": package_update,
        "package_upgrade": package_upgrade,
    }
    if genestack_role:
        context["genestack_role"] = genestack_role
    if extra_packages:
        context["extra_packages"] = list(extra_packages)
    if runcmd_extra:
        context["runcmd_extra"] = list(runcmd_extra)
    if extra_context:
        context.update(dict(extra_context))
    return template.render(**context)


__all__ = [
    "GENESTACK_NODE_USERDATA_TEMPLATE",
    "render_userdata",
]
