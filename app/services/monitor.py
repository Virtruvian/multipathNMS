import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Event, Sample, Target, iso_utc
from ..websocket import manager
from .health import RollingStats, ping_target
from .diagnostics import diagnostics, latest_diagnostic
from .availability import target_availability
from .services import service_payload


logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stats: dict[int, RollingStats] = defaultdict(RollingStats)
        self._success_window: dict[int, deque[bool]] = defaultdict(lambda: deque(maxlen=30))

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self._task or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name='health-monitor')

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    target_ids = list(db.scalars(select(Target.id).where(Target.enabled.is_(True))))
                results = await asyncio.gather(
                    *(self._check_target(target_id) for target_id in target_ids),
                    return_exceptions=True,
                )
                for target_id, result in zip(target_ids, results):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, Exception):
                        logger.error("ICMP health check failed for target id %s; retrying next round", target_id,
                                     exc_info=(type(result), result, result.__traceback__))
            except Exception:
                logger.exception("ICMP monitor round failed; retrying next round")
            await asyncio.sleep(settings.health_interval_seconds)

    async def _check_target(self, target_id: int) -> None:
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not target.enabled:
                return
            address = target.address
            revision = target.service_revision

        result = await ping_target(address)
        now = datetime.now(timezone.utc)

        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not target.enabled or target.address != address or target.service_revision != revision:
                return

            previous_status = target.status
            self._success_window[target_id].append(result.success)

            if result.success:
                target.consecutive_failures = 0
                target.last_seen = now
                target.latency_ms = result.latency_ms
                self._stats[target_id].add(result.latency_ms)
                target.jitter_ms = self._stats[target_id].jitter
                average = self._stats[target_id].average
                if target.baseline_latency_ms is None and average is not None:
                    target.baseline_latency_ms = average
                elif average is not None and target.status in {'healthy', 'unknown'}:
                    target.baseline_latency_ms = (target.baseline_latency_ms * 0.95) + (average * 0.05)
            else:
                target.consecutive_failures += 1
                target.latency_ms = None

            window = self._success_window[target_id]
            target.loss_percent = round(100 * (1 - (sum(window) / len(window))), 2) if window else 0.0
            target.status = self._status_for(target)
            target.updated_at = now
            db.add(Sample(target_id=target.id, address=address, success=result.success, latency_ms=result.latency_ms))

            if previous_status != target.status:
                db.add(Event(
                    target_id=target.id,
                    severity='critical' if target.status == 'down'
                    else 'warning' if target.status in {'suspect', 'degraded'}
                    else 'info',
                    event_type='status_change',
                    message=f'{target.name}: {previous_status} -> {target.status}',
                ))

            db.commit()
            payload = {'type': 'target_health', 'target': self._serialize(target)}

        if target.status in {'suspect', 'down', 'degraded'}:
            diagnostics.request(target_id, f"ICMP {target.status}: loss {target.loss_percent:g}%, failures {target.consecutive_failures}")
        elif target.status == 'healthy' and previous_status in {'suspect', 'down', 'degraded'}:
            diagnostics.note_recovery(target_id, 'icmp')
        with SessionLocal() as db:
            current = db.get(Target, target_id)
            payload['diagnostic'] = latest_diagnostic(db, current) if current else None
            payload['availability'] = target_availability(current, service_payload(db, current)) if current else None
        await manager.broadcast(payload)

    @staticmethod
    def _status_for(target: Target) -> str:
        if target.consecutive_failures >= settings.down_after_failures:
            return 'down'
        if target.consecutive_failures >= settings.suspect_after_failures:
            return 'suspect'
        if target.loss_percent >= settings.degraded_loss_percent:
            return 'degraded'
        baseline = target.baseline_latency_ms
        if baseline and target.latency_ms and target.latency_ms >= baseline * settings.degraded_latency_multiplier:
            return 'degraded'
        return 'healthy'

    @staticmethod
    def _serialize(target: Target) -> dict:
        return {
            'id': target.id,
            'name': target.name,
            'address': target.address,
            'enabled': target.enabled,
            'tcp_port': target.tcp_port,
            'tcp_check_enabled': target.tcp_check_enabled,
            'https_enabled': target.https_enabled,
            'https_path': target.https_path,
            'status': target.status,
            'latency_ms': target.latency_ms,
            'loss_percent': target.loss_percent,
            'jitter_ms': target.jitter_ms,
            'baseline_latency_ms': target.baseline_latency_ms,
            'consecutive_failures': target.consecutive_failures,
            'last_seen': iso_utc(target.last_seen) if target.last_seen else None,
            'updated_at': iso_utc(target.updated_at) if target.updated_at else None,
        }


monitor = MonitorService()
