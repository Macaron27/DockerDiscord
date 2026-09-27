"""POST /webhook/alert for Uptime Kuma, Prometheus Alertmanager and Grafana, Bearer-protected."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from bot.services.incidents import Incident

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
DETAIL_MAX = 1500
CONTAINER_LABELS = ("container", "container_name", "name")  # cAdvisor/docker_sd style labels
KUMA_DOWN = 0  # uptime-kuma src/util.ts: DOWN=0 UP=1 PENDING=2 MAINTENANCE=3

OnIncident = Callable[[Incident], Awaitable[object]]


def _labels(value: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _alertmanager(payload: dict[str, Any]) -> list[Incident]:
    """Alertmanager webhook v4 and Grafana (same shape, Grafana adds orgId/title/message)."""
    source = "grafana" if "orgId" in payload else "alertmanager"
    incidents = []
    for alert in payload["alerts"]:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue  # resolved notifications are not incidents
        labels, notes = _labels(alert.get("labels")), _labels(alert.get("annotations"))
        name = labels.get("alertname") or str(payload.get("title") or "Alert")
        container = next((labels[k] for k in CONTAINER_LABELS if labels.get(k)), None)
        target = container or labels.get("service") or labels.get("instance") or labels.get("job")
        detail = notes.get("summary") or notes.get("description") or str(payload.get("message") or "")
        incidents.append(
            Incident(
                service=f"{target}:{name}" if target else name,  # same alert on same target = duplicate
                title=f"{name} on {target}" if target else name,
                detail=(detail or "(no description)")[:DETAIL_MAX],
                source=source,
                container=container,
            )
        )
    return incidents


def _uptime_kuma(payload: dict[str, Any]) -> list[Incident]:
    heartbeat, monitor = payload.get("heartbeat"), payload.get("monitor")
    if not isinstance(heartbeat, dict) or heartbeat.get("status") != KUMA_DOWN:
        return []  # UP/PENDING/MAINTENANCE, or the "Test" button (heartbeat is null)
    name = str((monitor or {}).get("name") or "uptime-kuma")
    detail = str(heartbeat.get("msg") or payload.get("msg") or "Monitor is DOWN")
    return [Incident(name, f"{name} is DOWN", detail[:DETAIL_MAX], "uptime-kuma", container=name)]


def parse_payload(payload: dict[str, Any]) -> list[Incident] | None:
    """Firing incidents in the payload, or None if the format is not recognised."""
    if isinstance(payload.get("alerts"), list):
        return _alertmanager(payload)
    if "heartbeat" in payload or "monitor" in payload:
        return _uptime_kuma(payload)
    return None


def make_app(secret: str, on_incident: OnIncident) -> web.Application:
    expected = f"Bearer {secret}".encode()

    async def alert(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            log.warning("webhook: rejected request with bad/missing bearer from %s", request.remote)
            return web.json_response(
                {"error": "unauthorized"}, status=401, headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            payload = await request.json()  # bodies over MAX_BODY are rejected with 413 by aiohttp
        except json.JSONDecodeError, UnicodeDecodeError:
            return web.json_response({"error": "body must be JSON"}, status=400)
        incidents = parse_payload(payload) if isinstance(payload, dict) else None
        if incidents is None:
            return web.json_response({"error": "unsupported payload format"}, status=422)
        for incident in incidents:
            await on_incident(incident)  # a failure surfaces as 500 so the sender retries
        return web.json_response({"accepted": len(incidents)}, status=202)

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_post("/webhook/alert", alert)
    return app
