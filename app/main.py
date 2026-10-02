"""FastAPI application factory for Genestack Console.

Run with:
    uvicorn app.main:app --reload --app-dir .
from the genestack-console/ directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import (
    DEFAULT_DEV_API_KEYS,
    DEFAULT_SECRET_KEY,
    assert_safe_bind,
    get_settings,
)
from app.db import init_db
from app.deps import require_viewer
from fastapi.responses import RedirectResponse

from app.routers import (
    agents,
    alerts,
    app_hooks,
    apps,
    audit,
    auth,
    baremetal,
    cloud,
    descriptor,
    discovery,
    envconfig,
    environments,
    fleet,
    genestack_services,
    hardware_accounts,
    health,
    hostvms,
    ilo_console,
    jobs,
    k8s,
    livestate,
    metrics,
    notify,
    novnc,
    observe,
    obs_proxy,
    operations,
    overlays,
    ovh,
    platform,
    pxe,
    state,
    stream,
    tenants,
    terminal,
    ui,
    update,
    vms,
    workflow,
)
from app.routers import native, native_consoles, native_kubernetes
from app.schemas import Principal
from app.services import events
from app.services import genestack_bridge as bridge
from app.services import logredact
from app.services.catalog import get_operation_catalog

log = logging.getLogger(__name__)

_startup_time: float | None = None


def _ensure_single_worker() -> None:
    """Refuse to start when the launcher runs more than one web worker.

    The console is single-process by design: the SSE/agent relays and the
    in-memory ticket store (app/services/tickets.py) do not survive across
    uvicorn workers, so a ``--workers N > 1`` deployment would duplicate
    events and break stream/terminal tickets. We cannot reliably detect the
    worker count from inside a worker (each only sees itself), so the best
    signals are the environment variables the launcher sets: GSC_UVICORN_WORKERS
    (our own convention) and WEB_CONCURRENCY (gunicorn/uvicorn-common).
    """
    for var in ("GSC_UVICORN_WORKERS", "WEB_CONCURRENCY"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 1:
            raise RuntimeError(
                "Refusing to start: %s=%d — the console is NOT safe with "
                "multiple uvicorn workers (the in-memory ticket/relay state "
                "is single-process). Run with a single worker (uvicorn ... "
                "--workers 1 or omit --workers)." % (var, count)
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables, recover stale jobs, warn on default secret."""
    global _startup_time
    _startup_time = time.time()
    _ensure_single_worker()
    init_db()
    # Uvicorn may (re)configure logging after app import — make sure the
    # access-log redaction filter survives either ordering.
    logredact.install()
    # Record the running loop so sync worker threads (collector, alerts,
    # metrics, job runner) can publish SSE events; publish_sync is a no-op
    # until this happens.
    events.init(asyncio.get_running_loop())
    from app.db import SessionLocal
    from app.services.envcontext import sweep_staged_kubeconfigs
    from app.services.job_runner import recover_stale_jobs

    db = SessionLocal()
    try:
        recovered = recover_stale_jobs(db)
        if recovered:
            log.warning(
                "recovered %d stale job(s) left by a previous process", recovered
            )
        # Remove decrypted kubeconfig blobs leaked by a previous process
        # (per-job cleanup only covers jobs that reached their finally).
        swept_kube = sweep_staged_kubeconfigs(get_settings().data_dir)
        if swept_kube:
            log.warning(
                "swept %d staged kubeconfig file(s) left by a previous process",
                swept_kube,
            )
        # Seed the local-agent credential so the docker-compose local-agent
        # container can pick up its token from the shared volume on boot.
        try:
            from app.services.agents import seed_default_local_agent

            seed_default_local_agent(db, Path("/app/data"))
            log.info("local-agent seed complete")
        except Exception:
            log.exception("local-agent seed failed (non-fatal)")
    finally:
        db.commit()
        db.close()
    if get_settings().secret_key == DEFAULT_SECRET_KEY:
        log.warning(
            "secret_key is the built-in default — stored secrets (kubeconfig_data, "
            "notification credentials) are encrypted with a publicly known key. "
            "Set a unique secret_key in config.yaml."
        )
    dev_keys_in_use = set(DEFAULT_DEV_API_KEYS) & set(get_settings().parsed_api_keys())
    if dev_keys_in_use:
        log.warning(
            "default dev API key(s) active: %s — these are platform-admin "
            "break-glass credentials with publicly known values. Replace them "
            "in config.yaml before any real deployment.",
            ", ".join(sorted(dev_keys_in_use)),
        )
    if get_settings().dev_auto_login:
        log.warning(
            "auth.dev_auto_login is ON — every request is authenticated as "
            "platform-admin with NO credentials. This is a development-only "
            "toggle; disable it before exposing the console anywhere."
        )
    # The event bus is in-process, but the publishers (collector, alerts,
    # job runner) live in the worker daemon process. The DB relay polls the
    # shared SQLite database and re-publishes their changes onto this
    # process's bus so SSE subscribers actually receive them.
    relay_task = None
    if get_settings().stream_relay_enabled:
        from app.services.relay import start_relay

        relay_task = await start_relay(
            app, SessionLocal, get_settings().stream_relay_interval_seconds
        )
    # Agent command relay: the worker daemon is a separate process from this
    # one, where agents hold their WebSockets. Worker-side agent execution
    # inserts agent_commands rows (agent_relay.agent_exec); this relay polls
    # them and dispatches the frames to the connected agents.
    agent_relay_task = None
    if get_settings().agent_relay_enabled:
        from app.services.agent_relay import start_agent_relay

        agent_relay_task = await start_agent_relay(
            app, SessionLocal, get_settings().agent_relay_interval_seconds
        )
    try:
        from app.services.pxe_runtime import start_from_db

        start_from_db()
    except Exception:
        log.exception("pxe runtime start failed (non-fatal)")
    yield
    try:
        from app.services.pxe_runtime import stop_all

        stop_all()
    except Exception:
        log.exception("pxe runtime stop failed")
    if relay_task is not None:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
    if agent_relay_task is not None:
        agent_relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await agent_relay_task


def create_app() -> FastAPI:
    settings = get_settings()
    # Fail closed: a non-loopback bind guarded by publicly-known dev
    # credentials must never start (loopback stays dev-friendly).
    assert_safe_bind(settings)

    # Redact token=/ticket= query values from uvicorn access logs.
    logredact.install()

    app = FastAPI(
        title="Genestack Console",
        description=(
            "Operator fleet control plane for managing Genestack environments. "
            "Human catalog: GET /docs. Live OpenAPI UI: GET /swagger."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/swagger",
        redoc_url="/redoc",
        swagger_ui_parameters={"persistAuthorization": True},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Route map. Registration order stays as it is.
    #   probes and UI: health, update, ui, agent install script
    #   core: auth, tenants, operations, environments, config, jobs, audit
    #   bare metal: baremetal, ilo, pxe, discovery
    #   lifecycle: descriptor, workflow, fleet
    #   providers: ovh, hardware
    #   genestack reads: services, state, and the aliases registered below
    #   native Apple API: native, native_kubernetes, native_consoles
    #   observe: observe, dashboards, livestate, stream, alerts, metrics
    #   cloud and nodes: vms, cloud, novnc, k8s, platform, apps, host vms, agents, terminal

    # Probes. Health is unauthenticated.
    app.include_router(health.router)
    app.include_router(update.router)

    # Operator UI (static page; API calls use X-API-Key from the browser).
    app.include_router(ui.router)

    # Agent install script (unauthenticated; the curl-pipe one-liner).
    app.include_router(agents.install_router)

    # Core: accounts, tenants, environments, and config.
    app.include_router(auth.router)
    app.include_router(tenants.router)
    app.include_router(operations.router)
    app.include_router(environments.router)
    app.include_router(envconfig.router)
    app.include_router(overlays.router)

    # Bare metal and in-process PXE.
    app.include_router(baremetal.router)
    app.include_router(ilo_console.router)
    app.include_router(pxe.router)
    app.include_router(discovery.router)

    # Lifecycle and fleet views.
    app.include_router(descriptor.router)
    app.include_router(workflow.router)
    app.include_router(fleet.router)

    # Logs and dashboards, then jobs.
    app.include_router(observe.router)
    app.include_router(obs_proxy.router)
    app.include_router(jobs.router)

    # Providers: OVH and hardware accounts.
    app.include_router(ovh.router)
    app.include_router(hardware_accounts.router)

    # Audit and Genestack service/state reads.
    app.include_router(audit.router)
    app.include_router(genestack_services.router)
    app.include_router(state.router)

    # Native API the Apple apps use, through my.genestack.dev.
    app.include_router(native.router)
    app.include_router(native_kubernetes.router)
    app.include_router(native_consoles.router)

    # Live state, the event stream, alerts, and metrics.
    app.include_router(livestate.router)
    app.include_router(stream.router)
    app.include_router(alerts.router)
    app.include_router(notify.router)
    app.include_router(metrics.router)

    # OpenStack, Kubernetes, apps, host VMs, agents, and the terminal.
    app.include_router(vms.router)
    app.include_router(cloud.router)
    app.include_router(novnc.router)
    app.include_router(k8s.router)
    app.include_router(platform.router)
    app.include_router(apps.router)
    app.include_router(app_hooks.router)
    app.include_router(hostvms.router)
    app.include_router(agents.router)
    app.include_router(terminal.router)

    # Frontend static assets (check_dir=False: the dir may be absent pre-build)
    app.mount(
        "/static",
        StaticFiles(
            directory=Path(__file__).resolve().parent / "static", check_dir=False
        ),
        name="static",
    )

    # No-cache headers on static assets so browser never serves stale JS/CSS
    @app.middleware("http")
    async def _no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # Genestack convenience endpoints (read-only)
    gs_router = APIRouter(prefix="/api/v1/genestack", tags=["genestack"])

    @gs_router.get("/scripts")
    def list_scripts(principal: Principal = Depends(require_viewer)):
        settings_local = get_settings()
        root = bridge.resolve_genestack_root(settings_local)
        scripts = bridge.list_install_scripts(root)
        return {
            "genestack_root": str(root),
            "count": len(scripts),
            "scripts": scripts,
        }

    @gs_router.get("/components")
    def list_components(principal: Principal = Depends(require_viewer)):
        settings_local = get_settings()
        root = bridge.resolve_genestack_root(settings_local)
        return bridge.read_components_desired(root / "openstack-components.yaml")

    @gs_router.get("/operations")
    def genestack_ops_alias(principal: Principal = Depends(require_viewer)):
        """Alias hint — prefer GET /api/v1/operations."""
        return {
            "message": "Use GET /api/v1/operations for the full catalog",
            "count": len(get_operation_catalog()),
        }

    app.include_router(gs_router)

    @app.get("/")
    def root():
        """Send browsers to the UI; API clients can still use /health and /api/v1/*."""
        return RedirectResponse(url="/ui", status_code=307)

    @app.get("/api")
    def api_info():
        return {
            "service": "genestack-console",
            "version": __version__,
            "ui": "/ui",
            "docs": "/docs",
            "swagger": "/swagger",
            "openapi": "/openapi.json",
            "health": "/health",
            "dry_run": settings.dry_run,
        }

    return app


# Module-level app for `uvicorn app.main:app`
app = create_app()
