import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Event, Sample, Target
from ..websocket import manager
from .health import RollingStats, ping_target


class MonitorService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stats: dict[int, RollingStats] = defaultdict(RollingStats)
        self._success_window: dict[int, deque[bool]] = defaultdict(lambda: deque(maxlen=30))

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
            with SessionLocal() as db:
                target_ids = list(db.scalars(select(Target.id).where(Target.enabled.is_(True))))
            await asyncio.gather(*(self._check_target(target_id) for target_id in target_ids))
            await asyncio.sleep(settings.health_interval_seconds)

    async def _check_target(self, target_id: int) -> None:
        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target or not target.enabled:
                return
            address = target.address

        result = await ping_target(address)
        now = datetime.now(timezone.utc)

        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target:
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
            db.add(Sample(target_id=target.id, success=result.success, latency_ms=result.latency_ms))

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
            'status': target.status,
            'latency_ms': target.latency_ms,
            'loss_percent': target.loss_percent,
            'jitter_ms': target.jitter_ms,
            'baseline_latency_ms': target.baseline_latency_ms,
            'consecutive_failures': target.consecutive_failures,
            'last_seen': target.last_seen.isoformat() if target.last_seen else None,
            'updated_at': target.updated_at.isoformat() if target.updated_at else None,
        }


monitor = MonitorService()
