import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.events import bus, outbox_dispatcher
from app.core.resources import resource_publisher
from app.db import models
from app.db.seed import seed_demo
from app.db.session import Base, SessionLocal, engine
from app.modules import (academic, analytics, certificates, governance, identity, ml, realtime, recommendations,
                         tracking)
from app.services import ml as ml_service
from app.services.live import live_inference_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("edumaster")
settings = get_settings()


def _bootstrap() -> None:
    Base.metadata.create_all(engine)
    if not settings.seed_demo:
        return
    with SessionLocal() as db:
        org = seed_demo(db, settings.demo_students)
        if org:
            log.info("Entorno DEMO cargado (%s)", org.name)
        demo = db.scalar(select(models.Organization).where(models.Organization.code == "DEMO"))
        has_model = demo and db.scalar(select(models.ModelVersion.id).where(
            models.ModelVersion.organization_id == demo.id).limit(1))
        if demo and not has_model and settings.auto_train_on_seed:
            ml_service.start_training(db, demo.id, None, {}, auto_promote=True)
            log.info("Entrenamiento inicial DEMO en segundo plano")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.bind(asyncio.get_running_loop())
    await asyncio.to_thread(_bootstrap)
    tasks = [asyncio.create_task(outbox_dispatcher()),
             asyncio.create_task(resource_publisher(settings.resource_interval_seconds))]
    if settings.live_inference_seconds > 0:
        tasks.append(asyncio.create_task(live_inference_loop(settings.live_inference_seconds)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="EduMaster.ai API", version="0.1.0", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/v1/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


def _error(request: Request, status_code: int, code: str, message, details=None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={
        "code": code, "message": message, "details": details,
        "correlation_id": getattr(request.state, "correlation_id", None)})


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return _error(request, exc.status_code, f"http_{exc.status_code}", exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"campo": ".".join(str(p) for p in e["loc"][1:]), "mensaje": e["msg"]} for e in exc.errors()]
    return _error(request, 422, "validation_error", "Datos inválidos", details)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Error no controlado")
    return _error(request, 500, "internal_error", "Error interno; reporte el correlation_id")


API = "/api/v1"
for module in (identity, academic, tracking, analytics, recommendations, certificates, ml, governance, realtime):
    app.include_router(module.router, prefix=API)


@app.get(f"{API}/health", tags=["operación"])
def health():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok", "subscribers": len(bus.subscribers)}


if settings.web_dist and settings.web_dist.exists():
    dist = settings.web_dist.resolve()
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = (dist / path).resolve()
        if path and target.is_file() and dist in target.parents:
            return FileResponse(target)
        return FileResponse(dist / "index.html")
