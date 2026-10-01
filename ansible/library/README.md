# Ansible library (custom modules)

Placeholder directory for Genestack Console custom Ansible modules.

Modules placed here are automatically available when `ansible.cfg` sets
`library = library` (see `../ansible.cfg`).

## Intended modules (future)

- MAAS-aware helpers (machine wait, tag ensure)
- Genestack label helpers (`openstack-control-plane`, `compute`, `network`, `storage`)
- Bridge status reporters for the Console job worker

Add a module as `library/<name>.py` following Ansible module conventions.
