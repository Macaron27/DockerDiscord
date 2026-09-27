"""Async facade over docker-py's low-level APIClient (blocking calls run in threads)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import docker

from bot.config import CONTAINER_NAME_RE

# CSI/OSC escape sequences (colors, cursor moves, hyperlinks) as emitted by most loggers.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")

LIST_CACHE_S = 3.0  # autocomplete fires per keystroke; one API call serves a whole word
EVENT_FILTERS = {"type": ["container"], "event": ["die", "oom", "kill", "health_status"]}


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    name: str
    state: str  # running, exited, restarting, paused, created, dead
    status: str  # human text from the API, e.g. "Up 3 hours (healthy)"
    image: str

    @property
    def health(self) -> str | None:
        m = HEALTH_RE.search(self.status)
        return m.group(1).removeprefix("health: ") if m else None

    @property
    def dot(self) -> str:
        if self.state != "running":
            return "🔴"
        return {"unhealthy": "🔴", "starting": "🟡"}.get(self.health or "", "🟢")


@dataclass(frozen=True, slots=True)
class ContainerMemory:
    name: str
    used: int  # bytes, page cache excluded (same maths as `docker stats`)
    limit: int


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def memory_used(mem: dict[str, Any]) -> int:
    """Port of docker/cli calculateMemUsageUnixNoCache (cgroup v1 then v2)."""
    usage = int(mem.get("usage", 0))
    stats = mem.get("stats", {})
    if "total_inactive_file" in stats and stats["total_inactive_file"] < usage:
        return usage - int(stats["total_inactive_file"])
    if stats.get("inactive_file", usage) < usage:
        return usage - int(stats["inactive_file"])
    return usage


class DockerService:
    def __init__(self, api: docker.APIClient) -> None:
        self._api = api
        self._cached: tuple[float, list[ContainerInfo]] | None = None

    @classmethod
    async def connect(cls, base_url: str) -> DockerService:
        # version="auto" costs one GET /version; needed for stats(one_shot=...) (API >= 1.41).
        return cls(await asyncio.to_thread(docker.APIClient, base_url=base_url, version="auto", timeout=20))

    async def containers(self, max_age: float = 0.0) -> list[ContainerInfo]:
        """All containers by name. `max_age` > 0 allows a cached answer that recent."""
        now = time.monotonic()
        if max_age > 0 and self._cached and now - self._cached[0] <= max_age:
            return self._cached[1]
        raw = await asyncio.to_thread(self._api.containers, all=True)
        found = sorted(
            (
                ContainerInfo(
                    name=c["Names"][0].lstrip("/"), state=c["State"], status=c["Status"], image=c["Image"]
                )
                for c in raw
                if c.get("Names")
            ),
            key=lambda c: c.name,
        )
        self._cached = (now, found)
        return found

    async def resolve(self, name: str | None) -> str | None:
        """Return `name` only if it is a well-formed name of an existing container.

        Autocomplete is a suggestion; Discord lets users submit any string, so every
        action re-validates here before the name reaches the Docker API.
        """
        if not name or not CONTAINER_NAME_RE.match(name):
            return None
        return name if any(c.name == name for c in await self.containers()) else None

    async def inspect(self, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._api.inspect_container, name)

    async def logs(self, name: str, lines: int) -> str:
        raw: bytes = await asyncio.to_thread(self._api.logs, name, stdout=True, stderr=True, tail=lines)
        return strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\n")

    async def restart(self, name: str) -> None:
        await asyncio.to_thread(self._api.restart, name, timeout=10)

    async def stop(self, name: str) -> None:
        await asyncio.to_thread(self._api.stop, name, timeout=10)

    async def top_memory(self, n: int = 3) -> list[ContainerMemory]:
        running = [c.name for c in await self.containers() if c.state == "running"]

        def one(name: str) -> ContainerMemory | None:
            try:
                mem = self._api.stats(name, stream=False, one_shot=True).get("memory_stats", {})
            except docker.errors.APIError:  # container stopped between list and stats
                return None
            return ContainerMemory(name, memory_used(mem), int(mem.get("limit", 0)))

        gate = asyncio.Semaphore(8)  # docker-py's HTTP pool keeps 10 connections

        async def bounded(name: str) -> ContainerMemory | None:
            async with gate:
                return await asyncio.to_thread(one, name)

        results = await asyncio.gather(*(bounded(name) for name in running))
        return sorted((r for r in results if r), key=lambda r: r.used, reverse=True)[:n]

    def events(self, since_ns: int | None = None) -> Iterator[dict[str, Any]]:
        """Blocking, infinite event stream (no read timeout). Close it to stop iterating.

        `since_ns` replays buffered events from that Unix time in nanoseconds.
        """
        since = None if since_ns is None else f"{since_ns // 10**9}.{since_ns % 10**9:09d}"
        # docker-py passes non-datetime values through; the stubs only list int, which would drop
        # sub-second precision. moby documents "seconds.nanoseconds" (daemon/internal/timestamp).
        return self._api.events(decode=True, filters=EVENT_FILTERS, since=since)  # type: ignore[call-overload,no-any-return]
