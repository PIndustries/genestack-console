"""Console version — single source of truth.

VERSION is CalVer year.month.day.build, rewritten by scripts/bump-version.sh
on each release. BUILD is the git short hash injected at image build time
via the GSC_BUILD build arg/env var ("dev" for local runs).
"""

from __future__ import annotations

import os

VERSION = "2026.10.07.12"

BUILD = os.environ.get("GSC_BUILD", "dev")
