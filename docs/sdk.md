# The Python client

The SDK is the `genestack_sdk` package in this repository. It is a Python client for the console HTTP API. The client calls the console the customer runs. `my.genestack.dev` is not a hosted console and it is not this library.

This is on `main`. It is not in the `v2026.10.03` binary. The license is Apache-2.0.

## Calling the API

```python
from genestack_sdk import Client

client = Client("http://127.0.0.1:8080", api_key="dev-admin-key")
response = client.request("GET", "traces")
```

`request(method, path)` sends one call. If `path` does not start with `/`, the client prefixes `/api/v1`. `traces` becomes `/api/v1/traces`. A path that already starts with `/` is sent as given.

`api_key` is sent as the header `X-API-Key`. `access_token` is sent as `Authorization: Bearer`. Pass either one to `Client`. Leave both out and call `login`.

```python
client = Client("http://127.0.0.1:8080")
client.login("you", "password")
client.refresh()
```

`login` posts form data to `/api/v1/oauth/token`. The fields are `grant_type=password`, `username`, `password`, `client_id=genestack-console`, and `scope=console`. The client stores `access_token` and `refresh_token` on itself. It does not log them. It does not send a client secret.

`refresh` posts `grant_type=refresh_token` and the stored refresh token to that same path. It replaces both tokens. The client does not implement the implicit grant.

## Spans

A span is one named piece of work: the name, how many milliseconds it took (`duration_ms`), whether it finished (`ok`) or raised (`error`), and when it started (`started_at`, UTC). The console keeps spans in a ring buffer of 500. A read returns at most 200, newest last. The buffer is in this process. A restart clears it. There is no database table.

An admin calls `GET /api/v1/traces?limit=50`. The body is `{"spans": [...]}`. `POST /api/v1/traces` appends one span:

```json
{"name": "hello.say", "duration_ms": 1.5, "status": "ok"}
```

`name` is at most 128 characters. `status` is only `ok` or `error`. Anything else is HTTP 400. The payload has no tokens. When the work raises, the stored error is the exception class name, at most 200 characters, not the message.

A module file can time its work in the console process:

```python
from app.services.traces import trace

with trace("hello.say"):
    log("hello")
```

The rest of `run` matches `examples/modules/hello/say.py`: `HANDLERS`, `OPERATION`, and the same argument list.

`genestack_sdk.tracing.trace` records a span in the calling process. That buffer is not the console's buffer. Pass a `Client` and the helper also posts the span to `/api/v1/traces`. The posted JSON is only `name`, `duration_ms`, and `status`. A transport error is ignored, so the caller's work still finishes. The span still has no tokens.

```python
from genestack_sdk.tracing import trace

with trace("hello.say", client=client):
    ...
```

## A module you ship

Modules already load two ways. `modules.paths` in `config.yaml` is a list of directories. Each directory has an `__init__.py` with one `Module` subclass. The class sets `name` and a `functions` tuple. Each function file sets `HANDLERS`, `OPERATION`, and `run(...)`. The worked copy is `examples/modules/hello/`. The longer description is [Modules](modules.md).

An entry point is a name an installed package publishes so another program can load a class. The group is `genestack_console.modules`. The value points at a `Module` subclass. The loader instantiates that class. A third-party package uses this entry point. There is no second plugin framework.

```toml
[project.entry-points."genestack_console.modules"]
hello = "my_hello:HelloModule"
```

```python
from app.modules.base import Module

class HelloModule(Module):
    name = "hello"
    functions = ("say",)
```

The console loads built-in modules, then each directory in `modules.paths`, then these entry points. A duplicate module name, handler, or operation id fails at startup.

Traces have no web page. Read them from the API.
