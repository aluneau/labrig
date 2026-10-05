"""FastAPI application entry point"""
import asyncio
import logging
from contextlib import asynccontextmanager

import libvirt
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import SessionLocal, init_db
from app.api.v1 import api_router
from app.models import CloudImage

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    init_db()
    from app.services.task_service import task_service
    with SessionLocal() as db:
        task_service.mark_interrupted(db)
        # Cloud images whose download died with the previous process
        db.query(CloudImage).filter(CloudImage.status == "downloading").update({CloudImage.status: "error"})
        db.commit()

    async def keep_connected():
        """Reconnect to libvirt after a daemon restart so live events keep flowing"""
        from fastapi.concurrency import run_in_threadpool
        from app.libvirt_client import libvirt_client
        while True:
            try:
                await run_in_threadpool(libvirt_client.connect)
            except Exception:
                pass  # logged by connect(); retried below
            await asyncio.sleep(5)

    watchdog = asyncio.create_task(keep_connected())
    yield
    watchdog.cancel()


app = FastAPI(
    title="VM Manager API",
    description="A modern VM management platform built on libvirt",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

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


@app.exception_handler(libvirt.libvirtError)
async def libvirt_error_handler(request: Request, exc: libvirt.libvirtError):
    """Surface libvirt's own error message to the client"""
    status = 404 if exc.get_error_code() in _NOT_FOUND_CODES else 400
    return JSONResponse(status_code=status, content={"detail": exc.get_error_message() or str(exc)})


app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health_check():
    return {"status": "healthy"}


# Serve the built frontend (npm run build) from the same origin, if present
_frontend = settings.FRONTEND_DIR.resolve()
if (_frontend / "index.html").is_file():
    app.mount("/static", StaticFiles(directory=_frontend / "static"), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        file = (_frontend / path).resolve()
        if path and file.is_file() and file.is_relative_to(_frontend):
            return FileResponse(file)
        return FileResponse(_frontend / "index.html")  # client-side routes
else:
    @app.get("/")
    def root():
        return {"name": "VM Manager API", "version": settings.APP_VERSION, "docs": "/docs",
                "frontend": f"not built ({_frontend} has no index.html)"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
