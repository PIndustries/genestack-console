"""Single compiled entry: ``genestack-console serve`` / ``genestack-console worker``."""

from __future__ import annotations

import argparse
import os
import sys


def listen_addr(cli_host: str | None, cli_port: int | None) -> tuple[str, int]:
    """Resolve listen host/port: CLI, then env, then config.yaml (no recompile)."""
    from app.config import get_settings

    settings = get_settings()
    host = (
        cli_host or os.environ.get("GSC_HOST") or settings.host or "127.0.0.1"
    ).strip()
    if cli_port is not None:
        port = cli_port
    elif os.environ.get("GSC_PORT"):
        port = int(os.environ["GSC_PORT"])
    else:
        port = int(settings.port or 8080)
    return host, port


def main(argv: list[str] | None = None) -> int:
    from app.version import VERSION

    argv = list(sys.argv[1:] if argv is None else argv)
    prefix = (os.environ.get("GSC_PREFIX") or "/opt/genestack-console").rstrip("/")
    os.environ.setdefault("CONSOLE_CONFIG", f"{prefix}/config.yaml")
    os.environ.setdefault("GSC_PREFIX", prefix)
    cli_cmds = {
        "list-ops",
        "create-env",
        "create-tenant",
        "create-user",
        "add-member",
        "health",
        "make-config",
        "seed-demo",
    }
    if argv and argv[0] in cli_cmds:
        from app.cli import main as cli_main

        return cli_main(argv)

    parser = argparse.ArgumentParser(prog="genestack-console")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="HTTP API + UI")
    serve.add_argument(
        "--host",
        default=None,
        help="Listen address (default: config.yaml server.host)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Listen port (default: config.yaml server.port)",
    )
    worker = sub.add_parser("worker", help="job worker")
    worker.add_argument("--daemon", action="store_true", default=True)
    worker.add_argument("--interval", type=int, default=5)
    upd = sub.add_parser("update", help="check or apply a published binary")
    upd.add_argument("--check", action="store_true", help="print status only")
    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"

    if cmd == "update":
        from app.config import get_settings
        from app.services import updatecheck

        info = updatecheck.status(get_settings())
        if args.check or not info.get("update_available"):
            print(
                f"current={info.get('current')} latest={info.get('latest') or '-'} "
                f"available={info.get('update_available')}"
            )
            print(f"channel={info.get('channel')}")
            return 0
        result = updatecheck.apply_binary(get_settings())
        print(result.get("message") or result)
        return 0 if result.get("ok") else 1

    if cmd == "worker":
        from app.worker.runner import main as worker_main

        wargs = ["--daemon", "--interval", str(args.interval)]
        return worker_main(wargs)

    import uvicorn

    from app.main import app

    host, port = listen_addr(getattr(args, "host", None), getattr(args, "port", None))
    uvicorn.run(app, host=host, port=port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
