"""Check the GitHub release for a newer compiled Console (no source clone)."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.config import Settings
from app.paths import frozen, host_prefix

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = (
    "https://github.com/PIndustries/genestack-console/releases/latest/download/version.json"
)
CONSOLE_REPO = "PIndustries/genestack-console"
_GITHUB_API = f"https://api.github.com/repos/{CONSOLE_REPO}"
_GITHUB_WEB = f"https://github.com/{CONSOLE_REPO}/"
_CHANNEL_TTL = 45.0
_CHANNEL_MISS_TTL = 20.0
_FEED_TTL = 600.0
_FEED_MISS_TTL = 60.0
_ELF_HOLD = 6 * 3600.0
_UA = {"User-Agent": "genestack-console", "Accept": "application/json"}
_GH = {
    "User-Agent": "genestack-console",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_cache_lock = threading.Lock()
_apply_lock = threading.Lock()
_channel_cache: dict[str, Any] = {"at": 0.0, "url": "", "data": None, "ok": False}
_feed_cache: dict[str, Any] = {"at": 0.0, "data": None}
_apply_hold_until = 0.0
_wait_logged: set[str] = set()


def clear_caches() -> None:
    """Drop cached channel and release data. Tests use this."""
    global _apply_hold_until
    with _cache_lock:
        _channel_cache.update(at=0.0, url="", data=None, ok=False)
        _feed_cache.update(at=0.0, data=None)
        _apply_hold_until = 0.0
        _wait_logged.clear()


def parse_calver(raw: str) -> tuple[int, int, int, int, int, str]:
    """Return ``(year, month, day, build, release, suffix)``.

    A three-part version has build 0, so ``2026.10.03`` is older than
    ``2026.10.04.1``. A hyphen suffix is a prerelease (release 0) and sorts
    below the same numbers. A leading ``v`` is ignored.
    """
    text = str(raw or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    main, _, suf = text.partition("-")
    parts: list[int] = []
    for bit in main.split("."):
        if not bit:
            parts.append(0)
            continue
        try:
            parts.append(int(bit))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    release = 0 if suf else 1
    return (parts[0], parts[1], parts[2], parts[3], release, suf)


def is_newer(latest: str, current: str) -> bool:
    if not latest or not current:
        return False
    return parse_calver(latest) > parse_calver(current)


def fetch_channel(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            res = client.get(url, headers=_UA)
            res.raise_for_status()
            data = res.json()
    except Exception as exc:  # noqa: BLE001 — channel is optional
        log.debug("update channel unreachable: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def cached_channel(url: str) -> dict[str, Any] | None:
    """Return the published version.json, reusing a fresh success."""
    now = time.time()
    with _cache_lock:
        age = now - float(_channel_cache["at"])
        same = _channel_cache["url"] == url
        if same and _channel_cache["ok"] and age < _CHANNEL_TTL:
            data = _channel_cache["data"]
            return data if isinstance(data, dict) else None
        if same and not _channel_cache["ok"] and age < _CHANNEL_MISS_TTL:
            return None
    data = fetch_channel(url)
    with _cache_lock:
        _channel_cache.update(at=time.time(), url=url, data=data, ok=data is not None)
    return data


def status(settings: Settings) -> dict[str, Any]:
    url = (settings.update_url or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    channel = cached_channel(url)
    current = __version__
    latest = str((channel or {}).get("version") or "")
    binary = str((channel or {}).get("binary") or "")
    if binary.startswith("/"):
        binary = "https://genestack.dev" + binary
    available = bool(channel) and is_newer(latest, current)
    return {
        "current": current,
        "latest": latest or None,
        "update_available": available,
        "auto": bool(settings.update_auto),
        "watch": bool(getattr(settings, "update_watch", True)),
        "channel": url,
        "binary": binary or None,
        "reachable": channel is not None,
    }


def apply_binary(settings: Settings) -> dict[str, Any]:
    if not _apply_lock.acquire(blocking=False):
        info = status(settings)
        return {
            **info,
            "ok": False,
            "applied": False,
            "message": "an update is already running",
        }
    try:
        return _apply_binary_locked(settings)
    finally:
        _apply_lock.release()


def _apply_binary_locked(settings: Settings) -> dict[str, Any]:
    info = status(settings)
    if not info.get("update_available"):
        return {**info, "ok": True, "applied": False, "message": "already current"}
    url = info.get("binary")
    if not url:
        return {
            **info,
            "ok": False,
            "applied": False,
            "message": "channel has no binary URL",
        }
    prefix = host_prefix()
    dest = prefix / "bin" / "genestack-console"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="gsc-update-", dir=str(dest.parent))
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            with client.stream("GET", url) as res:
                res.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in res.iter_bytes():
                        fh.write(chunk)
        with tmp.open("rb") as fh:
            magic = fh.read(4)
        if magic != b"\x7fELF":
            tmp.unlink(missing_ok=True)
            return {
                **info,
                "ok": False,
                "applied": False,
                "message": "download is not a Linux ELF",
            }
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {**info, "ok": False, "applied": False, "message": str(exc)[:400]}
    restarted, message, ok = _restart_services()
    return {
        **info,
        "ok": ok,
        "applied": True,
        "restarted": restarted,
        "path": str(dest),
        "message": message,
    }


def _restart_services() -> tuple[bool, str, bool]:
    """Restart both units. Returns ``(restarted, message, ok)``."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "binary replaced; systemctl not found", True
    unit = Path("/etc/systemd/system/genestack-console.service")
    if not unit.is_file():
        return False, "binary replaced; genestack-console.service is not installed", True
    cmd = [
        systemctl,
        "restart",
        "genestack-console.service",
        "genestack-console-worker.service",
    ]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return False, "binary replaced; restart needs root", False
        # A timer has no tty, so it must not sit on a password prompt.
        cmd = [sudo, *cmd] if os.isatty(0) else [sudo, "-n", *cmd]
    proc = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return True, "binary replaced and services restarted", True
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if detail:
        return False, f"binary replaced; restart failed: {detail[:200]}", False
    return False, "binary replaced; restart failed", False


def active_jobs() -> int:
    """Queued or running jobs. Raises when the database cannot be read."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import SessionLocal
    from app.models import Job, JobStatus

    db = SessionLocal()
    try:
        return int(
            db.query(Job)
            .filter(Job.status.in_((JobStatus.queued, JobStatus.running)))
            .count()
            or 0
        )
    except SQLAlchemyError:
        log.exception("could not count active jobs")
        raise
    finally:
        db.close()


def can_apply_here() -> bool:
    """True for the published binary. A source checkout only checks."""
    return frozen()


def watch_once(
    settings: Settings,
    *,
    info: dict[str, Any] | None = None,
    installed: bool | None = None,
    busy: Any = None,
    apply: Any = None,
) -> dict[str, Any]:
    """Install a newer published build when this console is idle.

    ``update.watch: false`` leaves the install to the update button.
    A queued or running job waits. A laptop checkout does not download
    the Linux binary.
    """
    global _apply_hold_until
    current = info if info is not None else status(settings)
    if not bool(getattr(settings, "update_watch", True)):
        return {**current, "applied": False, "skipped": "watch off"}
    if not current.get("update_available"):
        return {**current, "applied": False, "skipped": "current"}
    here = can_apply_here() if installed is None else bool(installed)
    if not here:
        return {**current, "applied": False, "skipped": "not installed"}
    now = time.time()
    with _cache_lock:
        hold = _apply_hold_until
    if now < hold:
        return {**current, "applied": False, "skipped": "held"}
    try:
        count = int((busy or active_jobs)())
    except Exception:
        log.exception("update check could not read jobs")
        return {**current, "applied": False, "skipped": "jobs unread"}
    if count:
        latest = str(current.get("latest") or "")
        with _cache_lock:
            first = latest not in _wait_logged
            if first and latest:
                _wait_logged.add(latest)
        if first:
            log.info(
                "newer console %s is waiting; %d job(s) queued or running",
                latest or "build",
                count,
            )
        return {**current, "applied": False, "skipped": "jobs", "active_jobs": count}
    result = (apply or apply_binary)(settings)
    message = str((result or {}).get("message") or "")
    if "not a Linux ELF" in message:
        with _cache_lock:
            _apply_hold_until = time.time() + _ELF_HOLD
    return result


async def watch_loop(stop: Any, settings_fn: Any) -> None:
    """Check the published build on a short interval until ``stop`` is set."""
    import asyncio

    try:
        await asyncio.wait_for(stop.wait(), 15)
        return
    except TimeoutError:
        pass
    while True:
        try:
            result = await asyncio.to_thread(watch_once, settings_fn())
        except Exception:
            log.exception("console update check failed")
        else:
            if result.get("applied"):
                log.info("console update applied: %s", result.get("message"))
        try:
            await asyncio.wait_for(stop.wait(), 60)
            return
        except TimeoutError:
            continue


def _clip(text: str, limit: int = 180) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    import re

    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(text or ""))
    return cleaned.replace("`", "").replace("**", "").strip()


def notes_from_body(body: str) -> dict[str, Any]:
    """Split a release body into a summary and its bullet lines."""
    summary: list[str] = []
    items: list[str] = []
    for raw in str(body or "").splitlines():
        line = _plain(raw)
        if not line or line.startswith("#"):
            continue
        if line.startswith(("* ", "- ")):
            item = _clip(line[2:].strip(), 400)
            if item:
                items.append(item)
            continue
        rest = line
        if rest[:1].isdigit():
            while rest and rest[0].isdigit():
                rest = rest[1:]
            if rest[:1] in {".", ")"}:
                item = _clip(rest[1:].strip(), 400)
                if item:
                    items.append(item)
                continue
        summary.append(line)
    return {"summary": _clip(" ".join(summary), 600), "items": items[:40]}


def github_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.startswith(_GITHUB_WEB):
        return text
    return None


def fetch_github(url: str, timeout: float = 8.0) -> Any:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            res = client.get(url, headers=_GH)
            res.raise_for_status()
            return res.json()
    except Exception as exc:  # noqa: BLE001 — the panel still opens
        log.debug("github unreachable: %s", exc)
        return None


def _releases(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("draft"):
            continue
        tag = str(row.get("tag_name") or "").strip()
        version = tag[1:] if tag[:1] in ("v", "V") else tag
        notes = notes_from_body(str(row.get("body") or ""))
        rows.append(
            {
                "version": version,
                "tag": tag,
                "published_at": str(row.get("published_at") or ""),
                "html_url": github_url(row.get("html_url")),
                "prerelease": bool(row.get("prerelease")),
                "summary": notes["summary"],
                "items": notes["items"],
            }
        )
        if len(rows) >= 20:
            break
    return rows


def _jobs(payload: Any) -> list[dict[str, Any]]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return []
    rows: list[dict[str, Any]] = []
    for job in jobs[:30]:
        if not isinstance(job, dict):
            continue
        rows.append(
            {
                "name": _clip(str(job.get("name") or "job"), 80),
                "status": str(job.get("status") or ""),
                "conclusion": str(job.get("conclusion") or "") or None,
                "html_url": github_url(job.get("html_url")),
            }
        )
    return rows


def _pipeline(payload: Any) -> list[dict[str, Any]]:
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return []
    rows: list[dict[str, Any]] = []
    wanted: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "")
        row = {
            "name": name,
            "title": _clip(str(run.get("display_title") or "")),
            "status": str(run.get("status") or ""),
            "conclusion": str(run.get("conclusion") or "") or None,
            "event": str(run.get("event") or ""),
            "sha": str(run.get("head_sha") or "")[:7],
            "branch": _clip(str(run.get("head_branch") or ""), 80),
            "html_url": github_url(run.get("html_url")),
            "started_at": str(run.get("run_started_at") or run.get("created_at") or ""),
            "jobs": [],
        }
        rows.append(row)
        key = name.lower()
        run_id = run.get("id")
        if key in {"release", "ci"} and key not in seen and isinstance(run_id, int):
            seen.add(key)
            wanted.append((run_id, row))
        if len(rows) >= 15:
            break
    for run_id, row in wanted:
        row["jobs"] = _jobs(
            fetch_github(f"{_GITHUB_API}/actions/runs/{run_id}/jobs?per_page=30")
        )
    return rows


def load_feed() -> dict[str, Any]:
    releases_raw = fetch_github(f"{_GITHUB_API}/releases?per_page=30")
    runs_raw = fetch_github(f"{_GITHUB_API}/actions/runs?per_page=15")
    releases_ok = isinstance(releases_raw, list)
    runs_ok = isinstance(runs_raw, dict) and isinstance(runs_raw.get("workflow_runs"), list)
    return {
        "releases": _releases(releases_raw),
        "pipeline": _pipeline(runs_raw),
        "github_reachable": releases_ok or runs_ok,
    }


def feed(settings: Settings) -> dict[str, Any]:
    """Published release notes and the public CI runs. Cached."""
    info = status(settings)
    now = time.time()
    with _cache_lock:
        cached = _feed_cache.get("data")
        age = now - float(_feed_cache.get("at") or 0)
        ttl = _FEED_TTL
        if isinstance(cached, dict) and not cached.get("github_reachable"):
            ttl = _FEED_MISS_TTL
        if isinstance(cached, dict) and age < ttl:
            return {**info, **cached}
    payload = load_feed()
    with _cache_lock:
        _feed_cache.update(at=time.time(), data=payload)
    return {**info, **payload}
