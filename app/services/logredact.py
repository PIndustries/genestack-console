"""Access-log redaction for credential-bearing query params.

Realtime endpoints take single-use tickets (and agents take enrollment
tokens) as query params, and uvicorn's access log writes the full request
line. This filter rewrites ``token=`` / ``ticket=`` values in every
``uvicorn.access`` record to ``<redacted>`` so credentials never persist in
logs — defense in depth on top of the ticket mechanism.

``redact_secret_line`` does the same for persisted job logs: every line the
job runner appends is scrubbed before it lands in ``jobs.log_text``, so a
command that echoes a password, API key, token, or enrollment credential can
never leave the secret in the stored log.
"""

from __future__ import annotations

import logging
import re

# (?&)(token|ticket)=<value>  →  value redacted; value runs to the next &,
# whitespace, or quote (end of the request line in the access-log format).
_PARAM_RE = re.compile(r"([?&](?:token|ticket)=)[^&\s\"']*", re.IGNORECASE)

# `Authorization: <scheme> <token>` (e.g. an echoed auth header) → the whole
# value (scheme included) collapses to a single `<redacted>`. Handled before
# the key-value pass so it is not double-redacted.
_AUTH_RE = re.compile(
    r"(?i)\b(authorization)\s*[:=]\s*(?:Bearer\s+\S+|Basic\s+\S+|\S+)"
)

# Standalone `Bearer <token>` / `Basic <token>` with no preceding
# `authorization` key (e.g. a curl -H fragment) → token collapsed.
_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")

# Known secret keys immediately followed by `=` or `:` (with optional
# whitespace) and a value. The value is a quoted string or a single token.
# The leading `\b` keeps e.g. `max_token=` (a count) out; requiring `[:=]`
# right after the key keeps prose like "token is required" out.
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret|token)"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)

# Agent enrollment tokens and fernet-ciphertext blobs: mask the payload, keep
# the prefix so the line still reads as a token/secret placeholder.
_GSCA_RE = re.compile(r"\bgsca_[A-Za-z0-9._=-]+")
_FERNET_RE = re.compile(r"\bfernet:[A-Za-z0-9._=-]+")


class RedactCredentialsFilter(logging.Filter):
    """Redact token=/ticket= query values from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _PARAM_RE.sub(r"\1<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install(logger_name: str = "uvicorn.access") -> None:
    """Attach the filter to the given logger (idempotent)."""
    logger = logging.getLogger(logger_name)
    if any(isinstance(f, RedactCredentialsFilter) for f in logger.filters):
        return
    logger.addFilter(RedactCredentialsFilter())


def redact_secret_line(line: str) -> str:
    """Scrub credential-like values from a single job-log line.

    Applied to every line before it is persisted to ``jobs.log_text``:

      - ``Authorization: Bearer <token>``    → ``Authorization: <redacted>``
      - ``Bearer <token>`` / ``Basic <token>`` (standalone) → token → ``<redacted>``
      - ``password= / api_key: / token: …``  → value → ``<redacted>``
      - ``gsca_<token>`` (enrollment)        → ``gsca_***``
      - ``fernet:<ciphertext>``              → ``fernet:***``

    Non-matching lines are returned unchanged; the transform is idempotent.
    """
    if not line:
        return line
    out = _AUTH_RE.sub(r"\1: <redacted>", line)
    out = _SCHEME_RE.sub(r"\1 <redacted>", out)
    out = _KV_RE.sub(r"\1\2<redacted>", out)
    out = _GSCA_RE.sub("gsca_***", out)
    out = _FERNET_RE.sub("fernet:***", out)
    return out
