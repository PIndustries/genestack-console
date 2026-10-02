"""Console version — single source of truth.

VERSION is CalVer (year.month.day), rewritten by scripts/bump-version.sh on
each release day. BUILD is the git short hash injected at image build time
via the GSC_BUILD build arg/env var ("dev" for local runs).
"""

from __future__ import annotations

import os

VERSION = "2026.10.03"

BUILD = os.environ.get("GSC_BUILD", "dev")
