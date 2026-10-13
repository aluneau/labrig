"""FastAPI application entry point"""
import asyncio
import logging
from contextlib import asynccontextmanager

import libvirt
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import SessionLocal, init_db
from app.api.v1 import api_router
from app.models import CloudImage
from app.events import event_bus
from app.libvirt_client import libvirt_client, LibvirtUnavailable
from app.services.daemon_service import daemon_service, DaemonError
from app.services.helper_service import HelperError
from app.services.task_service import task_service
from app.services.cluster_service import cluster_service
from app.services import wireguard_service, auth_service
from app.auth_middleware import AuthMiddleware
from app.mcp.server import MCPEndpoint
from app.services.wireguard_relay import relay
from fastapi.concurrency import run_in_threadpool

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    init_db()
    auth_service.log_startup()
    with SessionLocal() as db:
        task_service.mark_interrupted(db)
        # Clusters whose task died with the previous process: resume (OpenShift install) or report
        try:
            cluster_service.recover_interrupted(db)
        except Exception:
            logging.getLogger(__name__).exception("Interrupted clusters")
        # Cloud images whose download died with the previous process
        db.query(CloudImage).filter(CloudImage.status == "downloading").update({CloudImage.status: "error"})
        db.commit()
        # WireGuard relays of the groups with remote access (DB only: never connects to libvirt)
        try:
            wireguard_service.reconcile(db)
        except Exception:
            logging.getLogger(__name__).exception("WireGuard relay")
        # Host DNS resolver for groupless / bare-metal OpenShift clusters (api / *.apps -> node IP)
        try:
            from app.services.dns_service import dns_service
            dns_service.reconcile(db)
        except Exception:
            logging.getLogger(__name__).exception("Host DNS resolver")
        # Redfish emulators of the groups with BMC members (DB only)
        try:
            from app.services import bmc_service
            bmc_service.reconcile(db)
        except Exception:
            logging.getLogger(__name__).exception("BMC emulators")
        # In-lab git server (Gitea) for GitOps, if it was bootstrapped before
        try:
            from app.services import git_server_service
            git_server_service.reconcile()
        except Exception:
            logging.getLogger(__name__).exception("Git server")

    async def watch_libvirt():
        """No keepalive: libvirt is connected on demand by requests. This loop only
        - closes the connection once idle with no browser attached, so socket-activated
          libvirtd / virtqemud can exit (their --timeout);
        - while browsers are attached, publishes daemon state changes (read from systemd,
          which never socket-activates the daemon)."""
        while True:
            await asyncio.sleep(5)
            try:
                if event_bus.subscriber_count():
                    if daemon_service.manageable():
                        await run_in_threadpool(daemon_service.status, False)
                elif settings.LIBVIRT_IDLE_TIMEOUT > 0 and not task_service.has_running():
                    await run_in_threadpool(libvirt_client.close_if_idle, settings.LIBVIRT_IDLE_TIMEOUT * 60)
            except Exception:
                logging.getLogger(__name__).exception("libvirt watcher")

    watcher = asyncio.create_task(watch_libvirt())
    yield
    watcher.cancel()
    relay.stop()
    from app.services.bmc_service import supervisor as bmc_supervisor
    bmc_supervisor.stop()
    from app.services.git_server_service import supervisor as git_supervisor
    git_supervisor.stop()
    libvirt_client.disconnect()


app = FastAPI(
    title="VM Manager API",
    description="A modern VM management platform built on libvirt",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Every /api request / WebSocket needs a session cookie or an API token (docs/auth.md).
# Added before CORS so CORS stays outermost (preflights and error responses keep their CORS headers).
app.add_middleware(AuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# The object vanished from libvirt (e.g. deleted while its page refetches) before the DB mirror caught up
_NOT_FOUND_CODES = {
    libvirt.VIR_ERR_NO_DOMAIN,
    libvirt.VIR_ERR_NO_NETWORK,
    libvirt.VIR_ERR_NO_STORAGE_POOL,
    libvirt.VIR_ERR_NO_STORAGE_VOL,
}

LIBVIRT_STOPPED = {"detail": "libvirt is stopped", "libvirt": "stopped"}


@app.exception_handler(LibvirtUnavailable)
async def libvirt_unavailable_handler(request: Request, exc: LibvirtUnavailable):
    return JSONResponse(status_code=503, content=LIBVIRT_STOPPED)


@app.exception_handler(libvirt.libvirtError)
async def libvirt_error_handler(request: Request, exc: libvirt.libvirtError):
    """Surface libvirt's own error message to the client"""
    if libvirt_client.conn is not None and not libvirt_client.is_alive():  # daemon went away mid-request
        return JSONResponse(status_code=503, content=LIBVIRT_STOPPED)
    status = 404 if exc.get_error_code() in _NOT_FOUND_CODES else 400
    return JSONResponse(status_code=status, content={"detail": exc.get_error_message() or str(exc)})


@app.exception_handler(DaemonError)
@app.exception_handler(HelperError)
async def privileged_error_handler(request: Request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(api_router, prefix="/api/v1")
# MCP server for AI assistants (docs/mcp.md), same port and authentication as the API
app.add_route("/api/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)


@app.get("/health")
def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION}


@app.api_route("/iso/{token}/agent.iso", methods=["GET", "HEAD"], include_in_schema=False)
def serve_agent_iso(token: str, request: Request):
    """Public, unguessable-token download of a bare-metal cluster's agent ISO, for the host's BMC to
    fetch as virtual media (the BMC can't send our auth header). The URL ends in .iso because Dell iDRAC
    picks the media type from the extension, and HEAD is handled because the BMC probes with HEAD before
    GET. Deleted after the install, so the URL stops working then. Outside /api, so AuthMiddleware leaves
    it public."""
    from app.services.openshift_installer import openshift_installer
    with SessionLocal() as db:
        iso = openshift_installer.iso_for_token(db, token)
    if iso is None:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if request.method == "HEAD":
        return Response(headers={"Content-Length": str(iso.stat().st_size),
                                 "Content-Type": "application/octet-stream", "Accept-Ranges": "bytes"})
    return FileResponse(str(iso), media_type="application/octet-stream", filename="agent.iso")


# Serve the built frontend (npm run build) from the same origin, if present
_frontend = settings.FRONTEND_DIR.resolve()
if (_frontend / "index.html").is_file():
    app.mount("/static", StaticFiles(directory=_frontend / "static"), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            # an API path no router knows (e.g. a UI newer than the running backend): JSON, not the page
            return JSONResponse({"detail": f"Not found: /{path} (is the backend up to date? restart it)"},
                                status_code=404)
        file = (_frontend / path).resolve()
        if path and file.is_file() and file.is_relative_to(_frontend):
            return FileResponse(file)
        # client-side routes; never cached: it names the hashed bundles, a stale copy keeps the old UI after an update
        return FileResponse(_frontend / "index.html", headers={"Cache-Control": "no-cache"})
else:
    @app.get("/")
    def root():
        return {"name": "VM Manager API", "version": settings.APP_VERSION, "docs": "/docs",
                "frontend": f"not built ({_frontend} has no index.html)"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
