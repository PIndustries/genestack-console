# Genestack Console — API + Ansible control host
# Pinned to bookworm: python:3.12-slim now floats to trixie (Debian 13),
# whose apt repo dropped the exact ansible pin below and would break the
# build on every future re-pull of the tag.
FROM python:3.12-slim-bookworm

ARG GSC_BUILD=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ANSIBLE_CONFIG=/app/ansible/ansible.cfg \
    GSC_BUILD=${GSC_BUILD}

WORKDIR /app

# apt ansible is pinned to the Debian bookworm (python:3.12-slim) version
# (ansible-core 2.14.x + the matching galaxy community collections): the
# bridge invokes `ansible-playbook` from PATH against the genestack repo's
# playbooks, so an unpinned `apt install ansible` lets distro updates swap
# the core version out from under the playbooks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ansible=7.7.0+dfsg-3+deb12u1 \
        openssh-client \
        curl \
        ca-certificates \
        git \
        jq \
        openssl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# kubectl + helm: the collector, livestate, drift/reconcile and component
# checks all shell out to them; without them every env probes as
# "executable not found" in the containerized deployment.
RUN ARCH="$(uname -m)" \
    && case "$ARCH" in x86_64) KARCH=amd64 ;; aarch64|arm64) KARCH=arm64 ;; *) KARCH=amd64 ;; esac \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.31.4/bin/linux/${KARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL https://get.helm.sh/helm-v3.16.4-linux-${KARCH}.tar.gz -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp \
    && mv "/tmp/linux-${KARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm.tgz "/tmp/linux-${KARCH}"

# talosctl: provider=talos bootstrap (genestack.deploy hosts stage). Pin to
# the factory image version the console BYOI's by default.
ARG TALOSCTL_VERSION=v1.13.9
RUN ARCH="$(uname -m)" \
    && case "$ARCH" in x86_64) TARCH=amd64 ;; aarch64|arm64) TARCH=arm64 ;; *) TARCH=amd64 ;; esac \
    && curl -fsSL -o /usr/local/bin/talosctl \
        "https://github.com/siderolabs/talos/releases/download/${TALOSCTL_VERSION}/talosctl-linux-${TARCH}" \
    && chmod +x /usr/local/bin/talosctl \
    && curl -fsSL -o /usr/local/bin/yq \
        "https://github.com/mikefarah/yq/releases/download/v4.44.6/yq_linux_${TARCH}" \
    && chmod +x /usr/local/bin/yq

# terraform: hardware.terraform.plan / apply for Rackspace, AWS, Azure, GCP.
ARG TERRAFORM_VERSION=1.9.8
RUN ARCH="$(uname -m)" \
    && case "$ARCH" in x86_64) TARCH=amd64 ;; aarch64|arm64) TARCH=arm64 ;; *) TARCH=amd64 ;; esac \
    && curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARCH}.zip" \
    && unzip -o /tmp/terraform.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform \
    && rm -f /tmp/terraform.zip

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY app /app/app
COPY ansible /app/ansible
COPY terraform /app/terraform
COPY config.yaml.example /app/config.yaml
COPY scripts /app/scripts
COPY agent /app/agent
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/scripts/*.sh 2>/dev/null || true \
    && chmod +x /app/entrypoint.sh

RUN mkdir -p /app/data \
    && useradd --create-home --uid 1000 --shell /bin/bash console \
    && chown -R console:console /app

USER console

EXPOSE 8080

# Prefer /health/live for liveness (always 200 once process is up).
# Startup readiness is /health/startup (503 until lifespan sets _startup_time).
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--access-log"]
