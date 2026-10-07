from datetime import datetime, timedelta, timezone

from ..config import settings
from ..models import Target, iso_utc


def target_availability(target: Target, checks: list[dict], *, now: datetime | None = None) -> dict:
    """Display reachability separately from the stored ICMP health status."""
    now = now or datetime.now(timezone.utc)
    if not target.enabled:
        return {"status": "disabled", "label": "PAUSED", "source": "", "detail": "Monitoring is paused", "valid_until": None}

    method = "https" if target.https_enabled else "tcp" if target.tcp_check_enabled else "icmp"
    source = "PING" if method == "icmp" else f"{method.upper()}:{target.tcp_port}"
    if method == "icmp":
        status = target.status
        checked = iso_utc(target.updated_at) if status != "unknown" else None
        max_age = max(30, settings.health_interval_seconds * 3 + 3)
        detail = "ICMP ping reachability; no TCP or HTTPS check enabled"
        if status in {"degraded", "suspect"}:
            status = "pending"
    else:
        check = next((item for item in checks if item["method"] == method), {})
        status = check.get("status", "unknown")
        checked = check.get("last_checked")
        max_age = settings.service_interval_seconds * 3 + settings.service_timeout_seconds
        detail = ("Verified HTTPS response (HTTP 200–399)" if method == "https"
                  else "TCP connection reachability; website content is not checked")
        if check.get("error"):
            detail += ": " + check["error"]
        if status == "pending":
            detail += f" ({check.get('consecutive_failures', 0)}/{check.get('confirmation_threshold', settings.service_failures_before_down)} failures)"

    valid_until = None
    if checked:
        timestamp = datetime.fromisoformat(checked).replace(tzinfo=timezone.utc)
        valid_until = timestamp + timedelta(seconds=max_age)
        if valid_until <= now:
            status = "stale"
    elif status not in {"unknown", "disabled"}:
        status = "unknown"  # An untimed result cannot claim current reachability.
    labels = {"healthy": "UP", "down": "DOWN", "pending": "VERIFYING", "unknown": "UNKNOWN",
              "stale": "STALE", "disabled": "UNKNOWN"}
    if status not in labels:
        status = "unknown"
    return {"status": status, "label": labels[status], "source": source, "detail": detail,
            "last_checked": checked, "valid_until": iso_utc(valid_until) if valid_until else None}
