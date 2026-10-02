"""Python client for a Genestack Console the customer runs.

The SDK is this package. It calls that console's HTTP API. It is not a
hosted service, and it does not talk to my.genestack.dev.
"""

from __future__ import annotations

from genestack_sdk.client import Client
from genestack_sdk.tracing import trace

__all__ = ["Client", "trace"]
