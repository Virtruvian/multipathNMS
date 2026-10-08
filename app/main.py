from contextlib import asynccontextmanager
import ipaddress
from pathlib import Path
import re
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select

from .database import SessionLocal, init_db
from .config import settings
from .models import DiagnosticIncident, Event, Target, ServiceState, iso_utc
from .services.monitor import MonitorService, monitor
from .services.topology import topology, topology_payload
from .services.services import services, service_payload, service_config, service_enabled
from .services.service_health import validate_https_path
from .services.availability import target_availability
from .services.diagnostics import diagnostics, incident_payload, latest_diagnostic
from .websocket import manager
from .auth import AdminAuthMiddleware, valid_session
from .public_nms import public_target, public_availability, public_checks, public_summary, public_event

BASE_DIR = Path(__file__).resolve().parent
HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_target_address(value: str) -> str:
    value = value.strip().rstrip(".")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass

    if not value or len(value) > 253:
        raise ValueError("Enter a valid IPv4/IPv6 address or DNS hostname")
    labels = value.split(".")
    if any(not HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("Enter a valid IPv4/IPv6 address or DNS hostname")
    return value


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    tcp_port: int = Field(default=settings.tcp_port, ge=1, le=65535)
    tcp_check_enabled: bool = True
    https_enabled: bool = False
    https_path: str = Field(default="/", min_length=1, max_length=1024)

    @field_validator("https_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_https_path(value)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        return validate_target_address(value)


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    tcp_port: int = Field(default=settings.tcp_port, ge=1, le=65535)
    tcp_check_enabled: bool = True
    https_enabled: bool = False
    https_path: str = Field(default="/", min_length=1, max_length=1024)

    @field_validator("https_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_https_path(value)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Address cannot be null")
        return validate_target_address(value)

    @field_validator("name", "enabled")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Provided values cannot be null")
        return value


def serialize_target(target: Target, db) -> dict:
    checks = service_payload(db, target)
    return dict(MonitorService._serialize(target), service_checks=checks,
                availability=target_availability(target, checks), diagnostic=latest_diagnostic(db, target))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    diagnostics.start()
    monitor.start()
    topology.start()
    services.start()
    yield
    await monitor.stop()
    await services.stop()
    await topology.stop()
    await diagnostics.stop()


app = FastAPI(title="multipathNMS", version="0.4.0", lifespan=lifespan)
app.add_middleware(AdminAuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["utc_iso"] = iso_utc


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/nms")


@app.get("/healthz")
def healthz() -> JSONResponse:
    workers = {
        "icmp": monitor.is_running,
        "services": services.is_running,
        "topology": topology.is_running,
    }
    running = all(workers.values())
    return JSONResponse({"status": "ok" if running else "degraded", "workers": workers},
                        status_code=200 if running else 503)


@app.get("/nms", response_class=HTMLResponse)
def nms_page(request: Request):
    with SessionLocal() as db:
        targets = list(db.scalars(select(Target).order_by(Target.name)))
        events = list(
            db.scalars(
                select(Event)
                .order_by(desc(Event.created_at))
                .limit(25)
            )
        )
        route_summaries: dict[int, dict] = {}
        service_checks: dict[int, list] = {}
        availability: dict[int, dict] = {}
        for target in targets:
            checks = service_payload(db, target)
            service_checks[target.id] = public_checks(checks)
            availability[target.id] = public_availability(target_availability(target, checks))
            route_summaries[target.id] = public_summary(topology_payload(
                db,
                target.id,
            )["summary"])
        events = [public_event(event, {target.id: target.name for target in targets}) for event in events]

    return templates.TemplateResponse(
        request=request,
        name="nms.html",
        context={
            "targets": targets,
            "events": events,
            "route_summaries": route_summaries,
            "service_checks": service_checks,
            "availability": availability,
        },
    )


@app.get("/topology", response_class=HTMLResponse)
def topology_page(request: Request):
    with SessionLocal() as db:
        targets = list(
            db.scalars(select(Target).order_by(Target.name))
        )
    return templates.TemplateResponse(
        request=request,
        name="topology.html",
        context={"targets": targets},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with SessionLocal() as db:
        targets = list(
            db.scalars(select(Target).order_by(Target.name))
        )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"targets": targets},
    )


@app.get("/api/targets")
def get_targets() -> list[dict]:
    with SessionLocal() as db:
        return [
            serialize_target(target, db)
            for target in db.scalars(
                select(Target).order_by(Target.name)
            )
        ]


@app.get("/api/nms")
def get_nms() -> list[dict]:
    with SessionLocal() as db:
        result = []
        for target in db.scalars(select(Target).order_by(Target.name)):
            checks = service_payload(db, target)
            value = dict(MonitorService._serialize(target), service_checks=checks,
                         availability=target_availability(target, checks))
            item = public_target(value)
            item['route_summary'] = public_summary(topology_payload(db, target.id)['summary'])
            result.append(item)
        return result


@app.post("/api/targets", status_code=201)
def create_target(payload: TargetCreate) -> dict:
    with SessionLocal() as db:
        if db.scalar(
            select(Target).where(
                Target.address == payload.address
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Target address already exists",
            )
        target = Target(
            name=payload.name,
            address=payload.address,
            enabled=payload.enabled,
            tcp_port=payload.tcp_port,
            tcp_check_enabled=payload.tcp_check_enabled,
            https_enabled=payload.https_enabled,
            https_path=payload.https_path,
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return serialize_target(target, db)


@app.patch("/api/targets/{target_id}")
async def update_target(
    target_id: int,
    payload: TargetUpdate,
) -> dict:
    with SessionLocal() as db:
        target = db.get(Target, target_id)
        if not target:
            raise HTTPException(
                status_code=404,
                detail="Target not found",
            )
        previous = {method: (service_config(target, method), service_enabled(target, method)) for method in ("tcp", "https")}
        for field, value in payload.model_dump(
            exclude_unset=True
        ).items():
            setattr(target, field, value)
        changed = False
        for method, old in previous.items():
            if old != (service_config(target, method), service_enabled(target, method)):
                changed = True
                state = db.scalar(select(ServiceState).where(ServiceState.target_id == target_id, ServiceState.method == method))
                if state:
                    db.delete(state)
        if changed:
            target.service_revision += 1
        db.commit()
        db.refresh(target)
        result = serialize_target(target, db)
    if changed:
        await diagnostics.cancel_target(target_id)
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target:
                raise HTTPException(status_code=404, detail="Target not found")
            result = serialize_target(target, db)
    await manager.broadcast({"type": "target_health", "target": result})
    await manager.broadcast({"type": "service_health", "target_id": target_id, "service_checks": result["service_checks"], "availability": result["availability"], "diagnostic": result["diagnostic"]})
    return result


@app.delete("/api/targets/{target_id}", status_code=204)
async def delete_target(target_id: int) -> Response:
    with SessionLocal() as db:
        target = db.get(Target, target_id)
        if not target:
            raise HTTPException(
                status_code=404,
                detail="Target not found",
            )
        db.delete(target)
        db.commit()
    await diagnostics.cancel_target(target_id)
    return Response(status_code=204)


@app.post("/api/targets/{target_id}/check-services")
async def check_target_services(target_id: int) -> dict:
    with SessionLocal() as db:
        if not db.get(Target, target_id):
            raise HTTPException(status_code=404, detail="Target not found")
    await services.check_target(target_id)
    with SessionLocal() as db:
        target = db.get(Target, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
        return serialize_target(target, db)


@app.get("/api/events")
def get_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        events = list(
            db.scalars(
                select(Event)
                .order_by(desc(Event.created_at))
                .limit(limit)
            )
        )
        return [
            {
                "id": event.id,
                "target_id": event.target_id,
                "created_at": iso_utc(event.created_at),
                "severity": event.severity,
                "event_type": event.event_type,
                "message": event.message,
            }
            for event in events
        ]


@app.get("/api/topology/{target_id}")
def get_topology(target_id: int) -> dict:
    with SessionLocal() as db:
        if not db.get(Target, target_id):
            raise HTTPException(
                status_code=404,
                detail="Target not found",
            )
        return topology_payload(db, target_id)


@app.get("/api/targets/{target_id}/diagnostics")
def get_diagnostics(target_id: int, limit: int = 20) -> list[dict]:
    with SessionLocal() as db:
        target = db.get(Target, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
        incidents = db.scalars(select(DiagnosticIncident).where(DiagnosticIncident.target_id == target_id)
                              .order_by(desc(DiagnosticIncident.id)).limit(max(1, min(limit, 100))))
        return [incident_payload(incident, target) for incident in incidents]


@app.get("/api/targets/{target_id}/diagnostics/{incident_id}")
def get_diagnostic(target_id: int, incident_id: int, download: bool = False):
    with SessionLocal() as db:
        target, incident = db.get(Target, target_id), db.get(DiagnosticIncident, incident_id)
        if not target or not incident or incident.target_id != target_id:
            raise HTTPException(status_code=404, detail="Diagnostic incident not found")
        payload = incident_payload(incident, target, evidence=True)
    if download:
        return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="diagnostic-{incident_id}.json"'})
    return payload


@app.post("/api/topology/{target_id}/discover")
async def discover_topology(
    target_id: int, protocol: Literal["all", "icmp", "tcp"] = "all",
) -> dict:
    try:
        return await topology.discover(
            target_id,
            include_raw=True,
            protocol=protocol,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@app.websocket("/ws/live")
@app.websocket("/ws/admin")
async def websocket_live(
    websocket: WebSocket,
) -> None:
    authorize = (lambda: valid_session(websocket.scope.get('admin_session'))) if websocket.url.path == '/ws/admin' else None
    if not await manager.connect(websocket, authorize=authorize):
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
