# Ansible library

This directory is a placeholder for Ansible modules (`library/<name>.py`).

The console does not boot servers from here. DHCP and boot files are the bare-metal code in `app/services/pxe.py` and `app/services/pxe_runtime.py`. A new console operation is a Python module under `app/modules/`, described in `docs/modules.md`. It is not an Ansible module in this folder.
