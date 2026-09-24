from contextlib import asynccontextmanager
import ipaddress
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select

from .database import SessionLocal, init_db
from .models import Event, Target
from .services.monitor import MonitorService, monitor
from .services.topology import topology, topology_payload
from .websocket import manager

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

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        return validate_target_address(value)


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str | None) -> str | None:
        return validate_target_address(value) if value is not None else None


def serialize_target(target: Target) -> dict:
    return MonitorService._serialize(target)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    monitor.start()
    topology.start()
    yield
    await topology.stop()
    await monitor.stop()


app = FastAPI(title="multipathNMS", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/nms")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


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
        for target in targets:
            route_summaries[target.id] = topology_payload(
                db,
                target.id,
            )["summary"]

    return templates.TemplateResponse(
        request=request,
        name="nms.html",
        context={
            "targets": targets,
            "events": events,
            "route_summaries": route_summaries,
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
            serialize_target(target)
            for target in db.scalars(
                select(Target).order_by(Target.name)
            )
        ]


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
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return serialize_target(target)


@app.patch("/api/targets/{target_id}")
def update_target(
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
        for field, value in payload.model_dump(
            exclude_unset=True
        ).items():
            setattr(target, field, value)
        db.commit()
        db.refresh(target)
        return serialize_target(target)


@app.delete("/api/targets/{target_id}", status_code=204)
def delete_target(target_id: int) -> Response:
    with SessionLocal() as db:
        target = db.get(Target, target_id)
        if not target:
            raise HTTPException(
                status_code=404,
                detail="Target not found",
            )
        db.delete(target)
        db.commit()
    return Response(status_code=204)


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
                "created_at": event.created_at.isoformat(),
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


@app.post("/api/topology/{target_id}/discover")
async def discover_topology(target_id: int) -> dict:
    try:
        return await topology.discover(
            target_id,
            include_raw=True,
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
async def websocket_live(
    websocket: WebSocket,
) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
