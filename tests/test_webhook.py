import asyncio
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from bot.services.incidents import Incident
from bot.webhooks.server import make_app

SECRET = "s" * 32
AUTH = {"Authorization": f"Bearer {SECRET}"}


def post(
    body: Any = None, headers: dict[str, str] | None = None, raw: bytes | None = None
) -> tuple[int, list[Incident]]:
    got: list[Incident] = []

    async def on_incident(incident: Incident) -> None:
        got.append(incident)

    async def scenario() -> int:
        async with TestClient(TestServer(make_app(SECRET, on_incident))) as client:
            kw: dict[str, Any] = {"data": raw} if raw is not None else {"json": body}
            resp = await client.post("/webhook/alert", headers=headers if headers is not None else AUTH, **kw)
            return resp.status

    return asyncio.run(scenario()), got


ALERTMANAGER = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighMemory", "name": "plex"},
            "annotations": {"summary": "RAM 95%"},
        },
        {
            "status": "resolved",
            "labels": {"alertname": "DiskFull", "instance": "nas:9100"},
            "annotations": {},
        },
    ],
}


def test_rejects_missing_or_wrong_bearer() -> None:
    assert post(ALERTMANAGER, headers={})[0] == 401
    assert post(ALERTMANAGER, headers={"Authorization": "Bearer nope"})[0] == 401
    assert post(ALERTMANAGER, headers={"Authorization": SECRET})[0] == 401


def test_rejects_bad_json_unknown_format_and_huge_body() -> None:
    assert post(raw=b"{not json")[0] == 400
    assert post({"hello": "world"})[0] == 422
    assert post(["a", "list"])[0] == 422
    assert post(raw=b'{"x": "' + b"a" * 70_000 + b'"}')[0] == 413


def test_alertmanager_firing_only() -> None:
    status, got = post(ALERTMANAGER)
    assert status == 202 and len(got) == 1
    assert got[0].source == "alertmanager" and got[0].container == "plex"
    assert got[0].service == "plex:HighMemory" and got[0].detail == "RAM 95%"


def test_grafana_detected_and_falls_back_to_message() -> None:
    body = {
        "orgId": 1,
        "title": "[FIRING:1]",
        "message": "CPU hot",
        "status": "firing",
        "alerts": [
            {"status": "firing", "labels": {"alertname": "CPU", "instance": "nas"}, "annotations": {}}
        ],
    }
    status, got = post(body)
    assert (
        status == 202
        and got[0].source == "grafana"
        and got[0].detail == "CPU hot"
        and got[0].container is None
    )


def test_uptime_kuma_down_only() -> None:
    down = {"heartbeat": {"status": 0, "msg": "timeout"}, "monitor": {"name": "jellyfin"}, "msg": "x"}
    status, got = post(down)
    assert status == 202 and got[0].title == "jellyfin is DOWN" and got[0].detail == "timeout"
    assert post({**down, "heartbeat": {"status": 1, "msg": "ok"}}) == (202, [])
    assert post({"heartbeat": None, "monitor": None, "msg": "Test"}) == (202, [])  # Kuma "Test" button
