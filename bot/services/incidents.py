"""Incident model plus the pure logic deciding what pages you: event triage and silencing."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

KEY_MAX = 80  # "alert:ack:" + key must fit Discord's 100-char custom_id
KILL_GRACE_S = 30.0  # a `die` this soon after a `kill` is a deliberate stop/restart


@dataclass(frozen=True, slots=True)
class Incident:
    service: str  # dedup / silence key
    title: str
    detail: str
    source: str  # docker | alertmanager | grafana | uptime-kuma
    container: str | None = None  # candidate for the Restart / Logs buttons

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", self.service[:KEY_MAX])


class Silencer:
    """Per-service suppression window. `silence` never shortens an existing window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}

    def silenced(self, key: str) -> bool:
        return self._until.get(key, 0.0) > self._clock()

    def silence(self, key: str, seconds: float) -> None:
        self._until[key] = max(self._until.get(key, 0.0), self._clock() + seconds)

    def clear(self, key: str) -> None:
        self._until.pop(key, None)


class EventClassifier:
    """Turns raw Docker events into incidents. Feed it every die/oom/kill/health_status event.

    Docker emits `kill` (with the signal) before `die` whenever something *asked* the container
    to stop: `docker stop`, compose down/up, our own /docker stop, /lockdown. A crash or the OOM
    killer produces `die` with no preceding `kill`, so that is what we alert on.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, kill_grace: float = KILL_GRACE_S) -> None:
        self._clock = clock
        self._grace = kill_grace
        self._kills: dict[str, float] = {}

    def feed(self, event: dict[str, Any]) -> Incident | None:
        action = str(event.get("Action", ""))
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        name = str(attrs.get("name") or str(event.get("id", "?"))[:12])
        image = attrs.get("image", "?")
        now = self._clock()
        self._kills = {n: t for n, t in self._kills.items() if now - t < self._grace}

        def incident(title: str, what: str) -> Incident:
            return Incident(name, f"{name} {title}", f"`{name}` ({image}) {what}", "docker", name)

        if action == "kill":
            self._kills[name] = now
            return None
        if action == "die":
            code = str(attrs.get("exitCode", "?"))
            if code == "0" or self._kills.pop(name, None) is not None:
                return None
            return incident(f"crashed (exit {code})", f"exited with code **{code}**.")
        if action == "oom":
            return incident("ran out of memory", "was hit by the OOM killer.")
        if action == "health_status: unhealthy":
            return incident("is unhealthy", "is failing its healthcheck.")
        return None
