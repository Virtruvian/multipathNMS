"""Bounded incident evidence; diagnostic probes never change monitoring counters."""

import asyncio
import ipaddress
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import SessionLocal
from ..models import DiagnosticIncident, Event, RoutePath, Sample, ServiceSample, Target, TopologySnapshot, iso_utc
from ..websocket import manager
from .health import ping_target
from .service_health import check_service
from .tcp import parse_tcp_traces, run_tcp_trace
from .voyage import resolve_target


def target_config(target: Target) -> dict:
    return {"address": target.address, "port": target.tcp_port,
            "tcp_enabled": target.tcp_check_enabled, "https_enabled": target.https_enabled,
            "https_path": target.https_path}


def capture_baseline(db, target: Target) -> dict:
    """Freeze saved observations before extra probes; these are not proof of health."""
    snapshots = []
    for protocol, port in (("icmp", 0), ("tcp", target.tcp_port)):
        snapshot = db.scalar(select(TopologySnapshot).where(
            TopologySnapshot.target_id == target.id, TopologySnapshot.protocol == protocol,
            TopologySnapshot.destination_port == port,
        ).order_by(desc(TopologySnapshot.id)).limit(1))
        if snapshot:
            snapshots.append({"id": snapshot.id, "protocol": protocol, "port": port,
                              "resolved_ip": snapshot.resolved_ip, "time": iso_utc(snapshot.created_at)})
    routes = list(db.scalars(select(RoutePath).options(selectinload(RoutePath.hops)).where(
        RoutePath.target_id == target.id, RoutePath.active.is_(True),
        (RoutePath.protocol == "icmp") | ((RoutePath.protocol == "tcp") & (RoutePath.destination_port == target.tcp_port)),
    ).order_by(RoutePath.id).limit(65)))
    successful_services = []
    for method in ("tcp", "https"):
        path = target.https_path if method == "https" else ""
        sample = db.scalar(select(ServiceSample).where(
            ServiceSample.target_id == target.id, ServiceSample.method == method,
            ServiceSample.address == target.address, ServiceSample.port == target.tcp_port,
            ServiceSample.path == path, ServiceSample.success.is_(True),
        ).order_by(desc(ServiceSample.id)).limit(1))
        if sample:
            successful_services.append({"method": method, "time": iso_utc(sample.created_at),
                "resolved_ip": sample.resolved_ip, "latency_ms": sample.latency_ms,
                "phase_results": json.loads(sample.phase_results) if sample.phase_results else None})
    ping = db.scalar(select(Sample).where(Sample.target_id == target.id, Sample.success.is_(True), Sample.address == target.address)
                     .order_by(desc(Sample.id)).limit(1))
    return {"captured_at": iso_utc(datetime.now(timezone.utc)),
            "last_successful_ping": {"time": iso_utc(ping.created_at), "address": ping.address, "latency_ms": ping.latency_ms} if ping else None,
            "host": {"status": target.status, "latency_ms": target.latency_ms,
                     "loss_percent": target.loss_percent, "last_success": iso_utc(ping.created_at) if ping else None},
            "last_successful_services": successful_services, "snapshots": snapshots,
            "routes_truncated": len(routes) > 64,
            "routes": [{"id": route.id, "protocol": route.protocol, "port": route.destination_port,
                "resolved_ip": route.resolved_ip, "complete": route.complete, "last_seen": iso_utc(route.last_seen),
                "hops": [{"ttl": hop.ttl, "address": hop.address, "rtt_ms": hop.rtt_ms} for hop in route.hops]}
                for route in routes[:64]]}


def incident_payload(incident: DiagnosticIncident, target: Target, *, evidence: bool = False) -> dict:
    results = json.loads(incident.results_json) if incident.results_json else {}
    checks = results.get("services", [])
    parts = []
    if "ping" in results:
        ping = results["ping"]
        parts.append(f"ICMP {ping['received']}/{ping['sent']} replies")
    elif results.get("ping_error"):
        parts.append("ICMP probe error: " + results["ping_error"])
    for check in checks:
        label = check["method"].upper()
        parts.append(f"{label} reachable" if check["success"] else
                     f"{label}: {(check.get('failed_phase') or 'check').upper()} failed")
    trace = results.get("trace")
    if trace:
        parts.append("TCP trace: " + (trace.get("comparison", {}).get("message") or trace["status"]))
        if trace.get("error"):
            parts.append(trace["error"])
    recovery = json.loads(incident.recovery_json) if incident.recovery_json else {}
    payload = {"id": incident.id, "target_id": incident.target_id, "status": incident.status,
               "reason": incident.reason, "created_at": iso_utc(incident.created_at),
               "started_at": iso_utc(incident.started_at), "finished_at": iso_utc(incident.finished_at),
               "summary": "; ".join(parts) or "Waiting for diagnostic measurements",
               "error": incident.error,
               "matches_current_config": incident.revision == target.service_revision and json.loads(incident.config_json) == target_config(target),
               "recoveries": {method: value["time"] for method, value in recovery.items()},
               "download_url": f"/api/targets/{target.id}/diagnostics/{incident.id}?download=true"}
    if evidence:
        payload.update(config=json.loads(incident.config_json), baseline=json.loads(incident.baseline_json), results=results,
                       recovery_evidence=recovery)
    return payload


def latest_diagnostic(db, target: Target) -> dict | None:
    incident = db.scalar(select(DiagnosticIncident).where(DiagnosticIncident.target_id == target.id)
                         .order_by(desc(DiagnosticIncident.id)).limit(1))
    return incident_payload(incident, target) if incident else None


def compare_trace(baseline: dict, resolved_ip: str, paths: list[dict], port: int) -> dict:
    saved = [route for route in baseline["routes"] if route["protocol"] == "tcp" and route["port"] == port]
    comparable = [route for route in saved if route["resolved_ip"] == resolved_ip]
    if not saved:
        return {"status": "no-baseline", "message": "No saved TCP path for comparison"}
    if not comparable:
        return {"status": "different-destination", "message": "Destination differs from saved TCP paths; no route comparison"}
    if not paths or not any(path["complete"] for path in paths):
        return {"status": "inconclusive", "message": "No confirmed endpoint reply; gaps do not locate a faulty router"}
    for path in paths:
        signature = [(hop["ttl"], hop["address"]) for hop in path["hops"]]
        if path["complete"] and any(route["complete"] and signature == [(hop["ttl"], hop["address"]) for hop in route["hops"]] for route in comparable):
            return {"status": "matches-saved-path", "message": "Sample matches a saved TCP path"}
    return {"status": "different-sample", "message": "Different TCP hop sample; multipath or a route change is possible, not proven failure"}


class DiagnosticMonitor:
    def __init__(self) -> None:
        self._active = False
        self._tasks: dict[int, asyncio.Task] = {}
        self._incident_ids: dict[int, int] = {}
        self._slots = asyncio.Semaphore(2)

    def start(self) -> None:
        with SessionLocal() as db:
            for incident in db.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status.in_(("queued", "running")))):
                incident.status = "interrupted"
                incident.finished_at = datetime.now(timezone.utc)
                incident.error = "Application restarted before the diagnostic round finished"
            db.commit()
        self._active = True

    async def stop(self) -> None:
        self._active = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._incident_ids.clear()
        with SessionLocal() as db:
            for incident in db.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status.in_(("queued", "running")))):
                incident.status, incident.finished_at = "interrupted", datetime.now(timezone.utc)
                incident.error = "Application stopped before the diagnostic round finished"
            db.commit()

    def request(self, target_id: int, reason: str) -> bool:
        if not self._active or not settings.diagnostics_enabled or target_id in self._tasks or len(self._tasks) >= 16:
            return False
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not target.enabled:
                return False
            previous = db.scalar(select(DiagnosticIncident).where(DiagnosticIncident.target_id == target_id)
                                 .order_by(desc(DiagnosticIncident.id)).limit(1))
            if previous and (now - previous.created_at.replace(tzinfo=timezone.utc)).total_seconds() < settings.diagnostic_cooldown_seconds:
                return False
            incident = DiagnosticIncident(target_id=target_id, created_at=now, status="queued", reason=reason[:255],
                revision=target.service_revision, config_json=json.dumps(target_config(target)),
                baseline_json=json.dumps(capture_baseline(db, target)))
            db.add(incident)
            db.commit()
            incident_id = incident.id
        task = asyncio.create_task(self._run(incident_id, target_id), name=f"diagnostic-{target_id}")
        self._tasks[target_id] = task
        self._incident_ids[target_id] = incident_id
        task.add_done_callback(lambda finished: self._task_done(target_id, finished))
        return True

    async def cancel_target(self, target_id: int) -> None:
        task = self._tasks.get(target_id)
        incident_id = self._incident_ids.get(target_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if incident_id:
            with SessionLocal() as db:
                incident, target = db.get(DiagnosticIncident, incident_id), db.get(Target, target_id)
                if incident and incident.status == "queued":
                    changed = not target or not target.enabled or target.service_revision != incident.revision
                    incident.status, incident.finished_at = "superseded" if changed else "interrupted", datetime.now(timezone.utc)
                    incident.error = "Diagnostic round cancelled before probes started"
                    db.commit()

    def _task_done(self, target_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(target_id) is task:
            self._tasks.pop(target_id, None)
            self._incident_ids.pop(target_id, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                logging.getLogger(__name__).error("Diagnostic task failed for target %s", target_id,
                                                  exc_info=(type(error), error, error.__traceback__))

    def note_recovery(self, target_id: int, method: str) -> None:
        if not self._active:
            return
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not target.enabled:
                return
            incident = db.scalar(select(DiagnosticIncident).where(DiagnosticIncident.target_id == target_id)
                                 .order_by(desc(DiagnosticIncident.id)).limit(1))
            if not incident or incident.revision != target.service_revision or json.loads(incident.config_json) != target_config(target):
                return
            recovery = json.loads(incident.recovery_json) if incident.recovery_json else {}
            if method not in recovery:
                observation = {"time": iso_utc(datetime.now(timezone.utc))}
                if method == "icmp":
                    observation.update(latency_ms=target.latency_ms, loss_percent=target.loss_percent,
                                       jitter_ms=target.jitter_ms)
                else:
                    sample = db.scalar(select(ServiceSample).where(ServiceSample.target_id == target_id,
                        ServiceSample.method == method, ServiceSample.success.is_(True), ServiceSample.address == target.address,
                        ServiceSample.port == target.tcp_port, ServiceSample.path == (target.https_path if method == "https" else ""))
                        .order_by(desc(ServiceSample.id)).limit(1))
                    if sample:
                        observation.update(time=iso_utc(sample.created_at), latency_ms=sample.latency_ms,
                            resolved_ip=sample.resolved_ip, http_status=sample.http_status,
                            phase_results=json.loads(sample.phase_results) if sample.phase_results else None)
                recovery[method] = observation
                incident.recovery_json = json.dumps(recovery)
                db.commit()

    @staticmethod
    async def _ping_round(address: str) -> dict:
        samples = []
        for index in range(3):
            result = await ping_target(address)
            samples.append({**asdict(result), "checked_at": iso_utc(datetime.now(timezone.utc))})
            if index < 2:
                await asyncio.sleep(0.3)
        received = sum(sample["success"] for sample in samples)
        return {"sent": len(samples), "received": received,
                "loss_percent": round(100 * (1 - received / len(samples)), 2), "samples": samples}

    @staticmethod
    async def _trace(config: dict, checks: list[dict], baseline: dict) -> dict:
        if not settings.tcp_enabled:
            return {"status": "disabled"}
        # Prefer the actual connected IPv4 address; otherwise an address from the recorded DNS result.
        checks = sorted(checks, key=lambda check: check["method"] != "tcp")
        connected = [check["resolved_ip"] for check in checks if check.get("resolved_ip")]
        candidates = connected or [address for check in checks for address in check.get("resolved_addresses", [])]
        if connected and ipaddress.ip_address(connected[0]).version != 4:
            return {"status": "unsupported", "error": "Connected service uses IPv6; topology engine is IPv4 only"}
        resolved = next((address for address in candidates if ipaddress.ip_address(address).version == 4), None)
        if not resolved:
            if checks:  # A failed DNS phase must not be disguised by a second lookup.
                return {"status": "unavailable", "error": "No IPv4 address from service checks"}
            resolved = await resolve_target(config["address"])
        result = await run_tcp_trace(resolved, config["port"], flows=1, hop_timeout_seconds=0.5)
        observation = parse_tcp_traces(result)
        paths = [{"complete": path.complete, "endpoint_response": path.endpoint_response,
                  "destination_rtt_ms": path.destination_rtt_ms,
                  "hops": [{"ttl": hop.ttl, "address": hop.address, "rtt_ms": hop.rtt_ms} for hop in path.hops]}
                 for path in observation.paths]
        return {"status": "measured", "resolved_ip": result.resolved_ip, "port": config["port"],
                "checked_at": iso_utc(datetime.now(timezone.utc)),
                "paths": paths, "reply_count": observation.probe_count, "raw_traces": result.traces,
                "comparison": compare_trace(baseline, result.resolved_ip, paths, config["port"])}

    async def _run(self, incident_id: int, target_id: int) -> None:
        results = {"services": []}
        status, error = "completed", None
        try:
            async with self._slots:
                with SessionLocal() as db:
                    incident, target = db.get(DiagnosticIncident, incident_id), db.get(Target, target_id)
                    if not incident or not target:
                        return
                    config = json.loads(incident.config_json)
                    if not target.enabled or target.service_revision != incident.revision or target_config(target) != config:
                        status = "superseded"
                        return
                    incident.status, incident.started_at = "running", datetime.now(timezone.utc)
                    baseline = json.loads(incident.baseline_json)
                    db.commit()

                async def ping():
                    try:
                        results["ping"] = await self._ping_round(config["address"])
                    except Exception as exc:
                        results["ping_error"] = str(exc)[:300]

                async def service(method):
                    try:
                        result = await check_service(config["address"], config["port"], https=method == "https",
                            path=config["https_path"], timeout=settings.service_timeout_seconds)
                        results["services"].append({"method": method, **asdict(result),
                                                    "checked_at": iso_utc(datetime.now(timezone.utc))})
                    except Exception as exc:
                        results["services"].append({"method": method, "success": False, "error": str(exc)[:300]})

                async with asyncio.timeout(settings.diagnostic_timeout_seconds):
                    await asyncio.gather(ping(), *(service(method) for method, enabled in
                        (("tcp", config["tcp_enabled"]), ("https", config["https_enabled"])) if enabled))
                    with SessionLocal() as db:
                        target = db.get(Target, target_id)
                        if not target or not target.enabled or target.service_revision != incident.revision or target_config(target) != config:
                            status = "superseded"
                            return
                    try:
                        results["trace"] = await self._trace(config, results["services"], baseline)
                    except (OSError, RuntimeError, ValueError) as exc:
                        results["trace"] = {"status": "failed", "error": str(exc)[:500]}
        except TimeoutError:
            status, error = "timeout", f"Diagnostic round exceeded {settings.diagnostic_timeout_seconds:g} seconds; completed checks retained"
            results.setdefault("trace", {"status": "timeout"})
        except asyncio.CancelledError:
            status, error = "interrupted", "Diagnostic round cancelled; completed checks retained"
            raise
        except Exception as exc:
            status, error = "failed", str(exc)[:500]
        finally:
            with SessionLocal() as db:
                incident, target = db.get(DiagnosticIncident, incident_id), db.get(Target, target_id)
                if incident and target:
                    if not target.enabled or target.service_revision != incident.revision or target_config(target) != json.loads(incident.config_json):
                        status, error = "superseded", "Target configuration changed during the diagnostic round"
                    incident.status, incident.error, incident.results_json = status, error, json.dumps(results)
                    incident.finished_at = datetime.now(timezone.utc)
                    db.add(Event(target_id=target_id, severity="info", event_type="diagnostic_completed",
                                 message=f"{target.name}: automatic diagnosis #{incident_id} {status}"))
                    db.commit()
                    payload = incident_payload(incident, target)
                else:
                    payload = None
            if payload:
                await manager.broadcast({"type": "diagnostic_update", "target_id": target_id, "diagnostic": payload})


diagnostics = DiagnosticMonitor()
