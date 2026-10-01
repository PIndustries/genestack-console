"""Build a fake genestack root for tests (install scripts, versions, components)."""

from __future__ import annotations

from pathlib import Path

import yaml


def _install_script(
    name: str,
    namespace: str,
    repo: str,
    repo_url: str,
    namespace_comment: str | None = None,
) -> str:
    ns_line = f'SERVICE_NAMESPACE="{namespace}"'
    if namespace_comment:
        ns_line += f" # {namespace_comment}"
    return (
        "#!/usr/bin/env bash\n"
        f'SERVICE_NAME_DEFAULT="{name}"\n'
        f"{ns_line}\n"
        f'HELM_REPO_NAME_DEFAULT="{repo}"\n'
        f'HELM_REPO_URL_DEFAULT="{repo_url}"\n'
        f'echo "install {name}"\n'
    )


# name -> (namespace, helm repo, repo url, optional inline comment on namespace)
_INSTALL_SCRIPTS = {
    "keystone": (
        "openstack",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
    "placement": (
        "openstack",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
    "glance": (
        "openstack",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
    "cinder": (
        "openstack",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
    "metallb": (
        "kube-system",
        "metallb",
        "https://metallb.github.io/metallb",
        "Note: metallb lives in kube-system",
    ),
    "grafana": ("monitoring", "grafana", "https://grafana.github.io/helm-charts", None),
    "mariadb-operator": (
        "mariadb-system",
        "mariadb-operator",
        "https://charts.example.com/mariadb",
        None,
    ),
    "tempest": (
        "tempest",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
    "service-template": (
        "openstack",
        "openstack-helm",
        "https://charts.example.com/openstack-helm",
        None,
    ),
}

_CHART_VERSIONS = {
    "keystone": "2026.1.8+db238e7c3",
    "metallb": "v0.14.9",
    "grafana": "8.5.1",
    # placement/cinder deliberately absent -> chart_version null
}

_COMPONENTS = {
    "keystone": True,
    "glance": True,
    "cinder": False,
    # placement/metallb/grafana deliberately absent -> desired null
}


def write_fake_genestack_root(root: Path) -> Path:
    """Create a minimal fake genestack checkout at ``root`` and return it."""
    root = Path(root)
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for name, (namespace, repo, repo_url, comment) in _INSTALL_SCRIPTS.items():
        script = bin_dir / f"install-{name}.sh"
        script.write_text(
            _install_script(name, namespace, repo, repo_url, comment),
            encoding="utf-8",
        )

    for helper in ("setup-hosts.sh", "setup-infrastructure.sh"):
        (bin_dir / helper).write_text(
            f'#!/usr/bin/env bash\necho "{helper}"\n', encoding="utf-8"
        )

    (root / "helm-chart-versions.yaml").write_text(
        yaml.safe_dump({"charts": _CHART_VERSIONS}), encoding="utf-8"
    )
    (root / "openstack-components.yaml").write_text(
        yaml.safe_dump({"components": _COMPONENTS}), encoding="utf-8"
    )

    for svc in ("keystone", "cinder"):
        (root / "base-helm-configs" / svc).mkdir(parents=True, exist_ok=True)
    (root / "base-kustomize" / "keystone").mkdir(parents=True, exist_ok=True)

    return root
