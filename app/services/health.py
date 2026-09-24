import asyncio
import re
from collections import deque
from dataclasses import dataclass


PING_TIME = re.compile(r'time[=<]([0-9.]+)\s*ms', re.IGNORECASE)


@dataclass(slots=True)
class HealthResult:
    success: bool
    latency_ms: float | None
    error: str | None = None


async def ping_target(address: str, timeout_seconds: int = 1) -> HealthResult:
    process = await asyncio.create_subprocess_exec(
        'ping', '-n', '-c', '1', '-W', str(timeout_seconds), '--', address,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    text = stdout.decode(errors='replace')
    if process.returncode != 0:
        return HealthResult(False, None, stderr.decode(errors='replace').strip() or text.strip())

    match = PING_TIME.search(text)
    return HealthResult(True, float(match.group(1)) if match else None)


class RollingStats:
    def __init__(self, size: int = 30) -> None:
        self.values: deque[float] = deque(maxlen=size)

    def add(self, value: float | None) -> None:
        if value is not None:
            self.values.append(value)

    @property
    def average(self) -> float | None:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def jitter(self) -> float | None:
        if len(self.values) < 2:
            return 0.0 if self.values else None
        values = list(self.values)
        diffs = [abs(b - a) for a, b in zip(values, values[1:])]
        return sum(diffs) / len(diffs)
