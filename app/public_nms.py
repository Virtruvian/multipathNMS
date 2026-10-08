"""Explicit allowlists for the anonymous dashboard and its live stream."""
from types import SimpleNamespace


def public_availability(value: dict | None) -> dict | None:
    if value is None:
        return None
    result = {key: value[key] for key in ('status', 'label', 'source', 'last_checked', 'valid_until') if key in value}
    result['detail'] = {
        'healthy': 'Recent check succeeded', 'down': 'Confirmed check failure',
        'pending': 'Verifying check failures', 'stale': 'No recent measurement',
        'disabled': 'Monitoring paused',
    }.get(value.get('status'), 'No measurement yet')
    return result


def public_checks(checks: list) -> list[dict]:
    return [{'status': check.get('status', 'unknown')} for check in checks]


def public_summary(value: dict) -> dict:
    return {key: value.get(key, 0) for key in ('active_routes', 'degraded_routes', 'missing_routes', 'pending_routes')}


def public_target(value: dict) -> dict:
    result = {key: value[key] for key in ('id', 'name', 'address', 'enabled', 'status', 'latency_ms', 'loss_percent', 'jitter_ms') if key in value}
    if 'availability' in value:
        result['availability'] = public_availability(value['availability'])
    if 'service_checks' in value:
        result['service_checks'] = public_checks(value['service_checks'])
    return result


def public_message(payload: dict) -> dict | None:
    kind = payload.get('type')
    if kind == 'target_health':
        result = {'type': kind, 'target': public_target(payload.get('target', {}))}
    elif kind == 'service_health':
        result = {'type': kind, 'target_id': payload.get('target_id'),
                  'service_checks': public_checks(payload.get('service_checks', []))}
    elif kind == 'topology_update':
        value = payload.get('topology', {})
        result = {'type': kind, 'target_id': payload.get('target_id'), 'topology': {
            'summary': public_summary(value.get('summary', {})),
            'service_checks': public_checks(value.get('service_checks', [])),
        }}
        if 'availability' in value:
            result['topology']['availability'] = public_availability(value['availability'])
    else:
        return None  # New message types stay private until explicitly reviewed.
    if 'availability' in payload:
        result['availability'] = public_availability(payload['availability'])
    return result


def public_event(event, names: dict[int, str]) -> SimpleNamespace:
    # Raw event messages can include routes, resolved IPs and probe errors.
    category = ('Service' if event.event_type.startswith('service_') else
                'Route' if event.event_type.startswith('route_') else
                'Topology' if event.event_type.startswith('topology_') else
                'Monitoring')
    return SimpleNamespace(created_at=event.created_at, severity=event.severity,
                           message=f"{names.get(event.target_id, 'Target')}: {category} status changed")
