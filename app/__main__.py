"""Allow `python -m app` to dispatch to the CLI."""

from app.cli import main

raise SystemExit(main())
