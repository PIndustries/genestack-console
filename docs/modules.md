# Modules

An operation is one job the console knows how to run. Power a server, push config, and deploy are operations. A module is the folder those operations live in.

The folder's `__init__.py` is a class. The class lists the other files in the folder, in order. It does not call them. The console calls the one file that matches the job.

```
app/modules/baremetal/
    __init__.py          the class. It lists the files below.
    node_register.py     one operation
    nodes_list.py        one operation
    bmc_scan.py          one operation
    node_actions.py      power, PXE, next boot, ISO, and provision
```

`node_actions.py` is one file because those five jobs share one body. Everything else is one operation per file.

## Why it is split this way

The job runner sets up the environment, then looks up the function for that job. The steps themselves are not in the runner. A new operation is a new file, plus one line on the class that lists it. You do not edit the runner to add a step.

Built-in modules and a module you write use the same shape.

## Where the built-in modules are

| Folder | What it does |
| --- | --- |
| `app/modules/console/` | Health check, backup, vacuum, and the local compile. These run on the console computer. |
| `app/modules/baremetal/` | Register a server, power it, and hand it a boot file. This is the console's own DHCP and boot files. |
| `app/modules/maas/` | Not used to boot a server. Installing Talos is the baremetal folder above. |
| `app/modules/genestack/` | Push config, run the install scripts, and check the cloud. |
| `app/modules/ansible/` | A host check, or an allow-listed playbook. |
| `app/modules/openstack/` | List servers, and start, stop, reboot, or delete one. |
| `app/modules/hostvm/` | Virtual machines on the console host. |
| `app/modules/agents/` | See an agent, run an allow-listed command, or install the agent. |
| `app/modules/platform/` | Day-2 actions on a node that is already installed, including Kubernetes drain and apply. |
| `app/modules/ovh/` | Reinstall an OVH server, or attach it to a vRack. |
| `app/modules/hardware/` | Terraform plan and apply for a hardware account. |
| `app/modules/apps/` | Deploy one application. |

The runner is `app/services/job_runner.py`. The catalog, which is the list the API returns, is assembled from these files. Built-in operations stay in the order they had before the split. An operation from a module you add is appended.

## What a function file contains

Open `examples/modules/hello/say.py`. Three names matter:

- `HANDLERS` is the internal name of the job. The runner looks this name up.
- `OPERATION` is the catalog entry: the id the API shows (`hello.say`), the name, who can run it, and the parameters. A file that serves two catalog entries uses `OPERATIONS` instead.
- `run` is the function. Its first argument is the job runner, so a step can call `self.write_audit`. The rest are the job, the environment, a log function, and the parameters. Copy the argument list from the example. Return a dict with `ok`.

`backend` has to be one of `internal`, `genestack`, `ansible`, `baremetal`, `maas`, or `agent`. A module that does not fit the others uses `internal`.

The operation id (`baremetal.node.power`) is what you see in the API. The handler (`baremetal_node_power`) is the name in `HANDLERS`. They match, except where two catalog entries share a file. `genestack.components.desired` and `genestack.components.list` both run `app/modules/genestack/components_desired.py`.

## Add an operation to a built-in module

1. Add a Python file in that module's folder. Start from `examples/modules/hello/say.py`.
2. Set `HANDLERS` and `OPERATION`. Pick an operation id and a handler name that are not already used. A duplicate fails at startup.
3. Add the file name, without `.py`, to the `functions` tuple in the folder's `__init__.py`.

The console picks it up the next time it starts. You do not register it anywhere else.

## Add your own module

Make a folder with the same shape. `__init__.py` subclasses `Module`, sets `name`, and lists the function files. One file per operation.

```python
from app.modules.base import Module

class HelloModule(Module):
    name = "hello"
    functions = ("say",)
```

Point the console at the folder. A relative path is from the directory of the config file.

```yaml
modules:
  paths:
    - /opt/genestack-console/modules/hello
```

The console loads built-in modules first, then each path, in order. A handler or an operation id that is already taken raises an error, so a custom module cannot silently replace a built-in. Give it a new name.

A package installed in the same Python environment can register itself instead of using a path:

```toml
[project.entry-points."genestack_console.modules"]
hello = "my_hello:HelloModule"
```

The value is the `Module` subclass. The worked copy of this layout is `examples/modules/hello/`. That folder is not loaded unless its path is in `modules.paths`.

## What the runner does

For every job, the runner resolves the environment, the deploy host, the timeout, and whether the command goes through an agent, SSH, or the console itself. Then it calls `run` in the file for that handler. The function returns the result dict. The runner stores the log and the audit row around that call.

A function file can import helpers from `app.services`. It should not import `app.services.job_runner` at the top of the file. The runner imports the modules, so a top-level import of the runner loops. If a step needs a helper that lives on the runner, import it inside `run`, the way `app/modules/genestack/k8s_upgrade.py` does.
