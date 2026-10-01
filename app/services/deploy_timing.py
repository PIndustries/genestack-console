"""Structured deploy/greenfield timing lines.

Every stage and install item logs ``[timing] ... seconds=N`` so we can
measure a greenfield, find the slow steps, and compare runs. The Overview
pipe parses these lines; they are also returned on the job result.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

LogFn = Callable[[str], None]

TIMING_RE = re.compile(
    r"\[timing\]"
    r"(?:\s+phase=(?P<phase>\S+))?"
    r"(?:\s+stage=(?P<stage>\S+))?"
    r"(?:\s+item=(?P<item>\S+))?"
    r"(?:\s+host=(?P<host>\S+))?"
    r"\s+seconds=(?P<seconds>[0-9]+(?:\.[0-9]+)?)"
)


def now() -> float:
    return time.monotonic()


def elapsed(start: float) -> float:
    return max(0.0, now() - start)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    try:
        s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def log_timing(
    log: LogFn | None,
    *,
    seconds: float,
    stage: str = "",
    item: str = "",
    phase: str = "",
    host: str = "",
) -> None:
    if log is None:
        return
    bits: list[str] = []
    if phase:
        bits.append(f"phase={phase}")
    if stage:
        bits.append(f"stage={stage}")
    if item:
        bits.append(f"item={item}")
    if host:
        bits.append(f"host={host}")
    bits.append(f"seconds={float(seconds):.1f}")
    log("[timing] " + " ".join(bits))


def parse_timings(log_text: str | None) -> dict[str, Any]:
    """Pull ``[timing]`` lines out of a job log into a scoreboard."""
    stages: dict[str, dict[str, Any]] = {}
    phases: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    total_s: float | None = None
    for line in str(log_text or "").splitlines():
        match = TIMING_RE.search(line)
        if not match:
            continue
        try:
            seconds = float(match.group("seconds"))
        except (TypeError, ValueError):
            continue
        phase = match.group("phase") or ""
        stage = match.group("stage") or ""
        item = match.group("item") or ""
        host = match.group("host") or ""
        if phase == "total":
            total_s = seconds
            continue
        if host:
            hosts.append(
                {
                    "host": host,
                    "seconds": seconds,
                    "phase": phase or "metal",
                }
            )
            continue
        if phase and not stage:
            phases.append({"id": phase, "seconds": seconds})
            continue
        if not stage:
            continue
        rec = stages.setdefault(stage, {"id": stage, "seconds": None, "items": []})
        if item:
            rec["items"].append({"name": item, "seconds": seconds})
        else:
            rec["seconds"] = seconds
    out_stages = list(stages.values())
    if total_s is None:
        summed = 0.0
        have = False
        for row in phases:
            if row.get("seconds") is not None:
                summed += float(row["seconds"])
                have = True
        for row in out_stages:
            if row.get("seconds") is not None:
                summed += float(row["seconds"])
                have = True
        if have:
            total_s = round(summed, 1)
    return {
        "total_s": total_s,
        "phases": phases,
        "stages": out_stages,
        "hosts": hosts,
    }


def attach_stage_times(
    stages: list[dict[str, Any]], parsed: dict[str, Any] | None
) -> None:
    """Copy parsed seconds onto pipeline stage/item dicts (in place)."""
    if not parsed:
        return
    by_stage = {str(s.get("id")): s for s in (parsed.get("stages") or []) if s}
    for spec in stages:
        rec = by_stage.get(str(spec.get("id")))
        if not rec:
            continue
        if rec.get("seconds") is not None:
            spec["seconds"] = rec["seconds"]
        items = spec.get("items")
        if not isinstance(items, list):
            continue
        by_item = {
            str(it.get("name")): it.get("seconds")
            for it in (rec.get("items") or [])
            if it and it.get("name")
        }
        for item in items:
            name = str(item.get("name") or "")
            if name in by_item and by_item[name] is not None:
                item["seconds"] = by_item[name]
