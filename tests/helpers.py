"""Tiny fakes shared by tests: just enough of discord.Interaction to drive our code."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import docker

from bot.config import load

ENV = {
    "DISCORD_TOKEN": "token",
    "ALLOWED_USER_IDS": "111, 222",
    "ALERT_CHANNEL_ID": "999",
    "WEBHOOK_SECRET": "s" * 32,
    "LOCKDOWN_CONTAINERS": "cloudflared,nginx-proxy-manager",
    "WOL_HOSTS": '{"nas": {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.10"}}',
}


def config(**overrides: str) -> Any:
    return load({**ENV, **overrides})


_STORE = SimpleNamespace(remove_view=lambda view: None)


def _register_view(*_: Any, view: discord.ui.View | None = None, **__: Any) -> None:
    # A real send registers the view with the ViewStore, which is what starts its timeout.
    if view is not None:
        view._start_listening_from_store(_STORE)  # type: ignore[arg-type]


def interaction(user_id: int = 111, bot: Any = None, **data: Any) -> Any:
    bot = bot or SimpleNamespace(config=config())
    return SimpleNamespace(
        client=bot,
        user=SimpleNamespace(id=user_id, mention=f"<@{user_id}>", __str__=lambda self: "user"),
        type=discord.InteractionType.application_command,
        guild_id=1,
        data=data,
        message=None,
        response=SimpleNamespace(
            send_message=AsyncMock(side_effect=_register_view), edit_message=AsyncMock(), defer=AsyncMock()
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.containers_data = [
            {"Names": ["/plex"], "State": "running", "Status": "Up 2 hours (healthy)", "Image": "plex"},
            {"Names": ["/db"], "State": "running", "Status": "Up 1 hour (unhealthy)", "Image": "pg"},
            {"Names": ["/old"], "State": "exited", "Status": "Exited (1) 3 days ago", "Image": "x"},
            {"Names": ["/cache"], "State": "running", "Status": "Up 5 min", "Image": "redis"},
            {"Names": ["/gone"], "State": "running", "Status": "Up 1 sec", "Image": "y"},
        ]

    @property
    def actions(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "list"]

    def containers(self, all: bool) -> list[dict[str, Any]]:
        assert all is True
        self.calls.append(("list", "*"))
        return self.containers_data

    def logs(self, name: str, **kw: Any) -> bytes:
        self.calls.append(("logs", name))
        assert kw["stdout"] and kw["stderr"]
        return b"\x1b[32mINFO\x1b[0m ready\r\n\x1b]8;;http://x\x07link\x1b]8;;\x07\n"

    def restart(self, name: str, timeout: int) -> None:
        self.calls.append(("restart", name))

    def stop(self, name: str, timeout: int) -> None:
        if name not in {c["Names"][0].lstrip("/") for c in self.containers_data}:
            raise docker.errors.NotFound("no such container")
        self.calls.append(("stop", name))

    def inspect_container(self, name: str) -> dict[str, Any]:
        return {
            "Name": f"/{name}",
            "RestartCount": 2,
            "State": {
                "Status": "exited",
                "Running": False,
                "ExitCode": 137,
                "StartedAt": "2026-09-01T10:00:00.123456789Z",
            },
            "Config": {"Image": "plex:latest"},
        }

    def stats(self, name: str, stream: bool, one_shot: bool) -> dict[str, Any]:
        assert stream is False and one_shot is True
        if name == "gone":
            raise docker.errors.APIError("no such container")
        used = {"plex": 900, "db": 500, "cache": 700}[name]
        return {"memory_stats": {"usage": used + 100, "limit": 4096, "stats": {"inactive_file": 100}}}


def fake_bot(**overrides: str) -> Any:
    from bot.services.docker import DockerService

    api = FakeAPI()
    return SimpleNamespace(config=config(**overrides), docker=DockerService(api), api=api)  # type: ignore[arg-type]
