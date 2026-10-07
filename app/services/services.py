import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Event, ServiceSample, ServiceState, Target, iso_utc
from ..websocket import manager
from .service_health import ServiceResult, check_service
from .diagnostics import diagnostics, latest_diagnostic


def service_config(target: Target, method: str) -> tuple[str, int, str]:
    return target.address, target.tcp_port, target.https_path if method == "https" else ""


def service_enabled(target: Target, method: str) -> bool:
    return target.enabled and (target.https_enabled if method == "https" else target.tcp_check_enabled)


def service_payload(db, target: Target) -> list[dict]:
    states = {state.method: state for state in db.scalars(select(ServiceState).where(ServiceState.target_id == target.id))}
    result = []
    now = datetime.now(timezone.utc)
    for method in ("tcp", "https"):
        address, port, path = service_config(target, method)
        state = states.get(method)
        enabled = service_enabled(target, method)
        if state and (state.address, state.port, state.path) != (address, port, path):
            state = None  # Previous configuration must not claim current reachability.
        status = state.status if state else "unknown"
        if not enabled:
            status = "disabled"
        elif state and state.last_checked and (now - state.last_checked.replace(tzinfo=timezone.utc)).total_seconds() > settings.service_interval_seconds * 3 + settings.service_timeout_seconds:
            status = "stale"
        result.append({
            "method": method, "port": port, "path": path, "enabled": enabled, "status": status,
            "latency_ms": state.latency_ms if state and enabled else None,
            "http_status": state.http_status if state and enabled else None,
            "resolved_ip": state.resolved_ip if state and enabled else None,
            "error": state.error if state and enabled else None,
            "consecutive_failures": state.consecutive_failures if state and enabled else 0,
            "confirmation_threshold": settings.service_failures_before_down,
            "last_checked": iso_utc(state.last_checked) if state and state.last_checked else None,
            "last_success": iso_utc(state.last_success) if state and state.last_success else None,
            **(json.loads(state.phase_results) if state and enabled and state.phase_results else
               {"phases": [], "failed_phase": None, "resolved_addresses": []}),
        })
    return result


class ServiceMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._slots = asyncio.Semaphore(8)

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run(), name="service-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            with SessionLocal() as db:
                ids = list(db.scalars(select(Target.id).where(Target.enabled.is_(True))))
            await asyncio.gather(*(self.check_target(target_id) for target_id in ids))
            await asyncio.sleep(settings.service_interval_seconds)

    async def check_target(self, target_id: int) -> None:
        async with self._slots, self._locks[target_id]:
            with SessionLocal() as db:
                target = db.get(Target, target_id)
                if not target:
                    return
                configs = [(method, service_config(target, method)) for method in ("tcp", "https") if service_enabled(target, method)]
                revision = target.service_revision
            results = await asyncio.gather(*(check_service(address, port, https=method == "https",
                path=path or "/", timeout=settings.service_timeout_seconds)
                for method, (address, port, path) in configs), return_exceptions=True)
            for (method, config), result in zip(configs, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    result = ServiceResult(False, error=f"Service check error: {str(result)[:250]}")
                self._record(target_id, method, config, result, expected_revision=revision)
            with SessionLocal() as db:
                target = db.get(Target, target_id)
                if target:
                    payload = service_payload(db, target)
                    diagnostic = latest_diagnostic(db, target)
                else:
                    return
            await manager.broadcast({"type": "service_health", "target_id": target_id, "service_checks": payload, "diagnostic": diagnostic})

    @staticmethod
    def _record(target_id: int, method: str, config: tuple[str, int, str], result: ServiceResult, *, expected_revision: int | None = None) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not service_enabled(target, method) or service_config(target, method) != config:
                return  # Ignore results that raced with edit/disable/delete.
            if expected_revision is not None and expected_revision != target.service_revision:
                return
            state = db.scalar(select(ServiceState).where(ServiceState.target_id == target_id, ServiceState.method == method))
            if not state:
                state = ServiceState(target_id=target_id, method=method, address=config[0], port=config[1], path=config[2], status="unknown", consecutive_failures=0)
                db.add(state)
            elif (state.address, state.port, state.path) != config:
                state.address, state.port, state.path = config
                state.status, state.consecutive_failures, state.last_success = "unknown", 0, None
            previous = state.status
            state.last_checked = now
            state.latency_ms, state.resolved_ip, state.http_status, state.error = result.latency_ms, result.resolved_ip, result.http_status, result.error
            phase_results = json.dumps({"phases": result.phases, "failed_phase": result.failed_phase,
                                        "resolved_addresses": result.resolved_addresses})
            state.phase_results = phase_results
            state.consecutive_failures = 0 if result.success else state.consecutive_failures + 1
            state.status = "healthy" if result.success else "down" if state.consecutive_failures >= settings.service_failures_before_down else "pending"
            if result.success:
                state.last_success = now
            label = f"{target.name} {method.upper()}:{config[1]}"
            if state.status == "down" and previous != "down":
                db.add(Event(target_id=target_id, severity="critical", event_type="service_down",
                             message=f"{label}: {state.consecutive_failures} consecutive failures ({result.error})"))
            elif previous == "down" and state.status == "healthy":
                db.add(Event(target_id=target_id, severity="info", event_type="service_recovered", message=f"{label} reachable again"))
            db.add(ServiceSample(target_id=target_id, created_at=now, method=method, address=config[0], port=config[1], path=config[2],
                                 success=result.success, latency_ms=result.latency_ms, resolved_ip=result.resolved_ip,
                                 http_status=result.http_status, error=result.error, phase_results=phase_results))
            db.commit()
        if not result.success:
            diagnostics.request(target_id, f"{method.upper()} {(result.failed_phase or 'check').upper()}: {result.error or 'failed'}")
        elif previous in {"pending", "down"}:
            diagnostics.note_recovery(target_id, method)


services = ServiceMonitor()
