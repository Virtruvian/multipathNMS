# AGENTS.md — multipathNMS

This file is the source of truth for development in this repository.

## Product goal

multipathNMS is a lightweight Docker-based route monitoring application with two views over the same data:

1. `/nms` — compact NMS dashboard for operational monitoring, alarms and route health.
2. `/topology` — visual live topology view for route/multipath analysis.

The primary purpose is to detect route changes, degradation and outages with useful measurements such as latency, packet loss, jitter, last seen and availability.

## Architecture

- Docker / Docker Compose
- Python 3.12
- FastAPI backend
- Voyage / Paris MDA topology discovery behind an adapter
- WebSocket live updates
- Cytoscape.js topology rendering
- SQLite history for v1

Keep measurement engines behind interfaces so Voyage can later be supplemented or replaced by Scamper, MTR or another probe engine without redesigning the UI or database.

## Monitoring principles

- Fast health checks and slower topology discovery are separate jobs.
- A non-responsive intermediate hop does not automatically mean a route is down.
- A previously discovered path that is absent from the newest MDA scan is `missing`, not automatically proven failed.
- Route status and target reachability are separate concepts.
- Avoid excessive probing. Defaults should be conservative and configurable.
- Preserve historical events when routes appear, disappear, degrade or recover.
- Keep stable route identities based on their ordered TTL/IP signature.

Target health status model:

- `healthy`
- `degraded`
- `suspect`
- `down`

Topology route status model:

- `active`
- `degraded`
- `missing`

## Security

- Never execute user input through a shell.
- Use argument arrays with `asyncio.create_subprocess_exec`.
- Validate targets before probing.
- The application is intended for trusted/internal deployment unless authentication and access controls are explicitly added.
- Docker capabilities should be limited to what the probe engine needs.
- Pin external measurement-engine source revisions used in Docker builds.

## Code conventions

- Keep API, monitoring logic, persistence and frontend concerns separated.
- Type annotate Python code.
- Configuration belongs in environment variables/settings.
- Keep v1 dependencies small.
- Do not silently swallow probe errors.
- Do not call a missing intermediate hop a route outage without downstream evidence.
- Treat this AGENTS.md as authoritative for future changes.

## Branching

- `main` is stable.
- Development happens in feature branches.
- Changes go through pull requests.

## Implemented v1 foundation

- FastAPI application in Docker
- `/nms`, `/topology`, `/settings`
- target CRUD
- fast ICMP target-health monitoring
- SQLite samples/events/history
- WebSocket live updates
- Voyage flat-output parser
- stable multipath route IDs
- persistent nodes, links, routes and hops
- route discovery/recovery/missing events
- route RTT baseline and min/avg/max values
- Cytoscape live graph
- recent missing paths remain visible
- parser unit tests

## Next implementation step

Add faster route-specific verification when topology changes or target health becomes suspect, plus time-series charts for RTT/loss and route-change history. Keep these probes conservative and do not infer failure solely from an ICMP-silent intermediate hop.
