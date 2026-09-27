import asyncio

from bot.services.docker import DockerService, memory_used, strip_ansi
from tests.helpers import FakeAPI


def test_strip_ansi_removes_colors_hyperlinks_and_cr() -> None:
    assert strip_ansi("\x1b[1;31mERR\x1b[0m boom\r\n\x1b]8;;http://a\x07x\x1b]8;;\x07") == "ERR boom\nx"


def test_memory_used_matches_docker_cli() -> None:
    assert memory_used({"usage": 1000, "stats": {"total_inactive_file": 300}}) == 700  # cgroup v1
    assert memory_used({"usage": 1000, "stats": {"inactive_file": 200}}) == 800  # cgroup v2
    assert memory_used({"usage": 1000, "stats": {"inactive_file": 5000}}) == 1000


def test_list_parses_state_and_health() -> None:
    svc = DockerService(FakeAPI())  # type: ignore[arg-type]
    by_name = {c.name: c for c in asyncio.run(svc.containers())}
    assert by_name["plex"].health == "healthy" and by_name["plex"].dot == "🟢"
    assert by_name["db"].dot == "🔴"
    assert by_name["old"].dot == "🔴" and by_name["old"].health is None


def test_resolve_rejects_unknown_and_malformed_names() -> None:
    svc = DockerService(FakeAPI())  # type: ignore[arg-type]
    assert asyncio.run(svc.resolve("plex")) == "plex"
    assert asyncio.run(svc.resolve("nope")) is None
    assert asyncio.run(svc.resolve("plex; rm -rf /")) is None
    assert asyncio.run(svc.resolve("../plex")) is None
    assert asyncio.run(svc.resolve(None)) is None


def test_logs_are_ansi_free() -> None:
    svc = DockerService(FakeAPI())  # type: ignore[arg-type]
    assert asyncio.run(svc.logs("plex", 30)) == "INFO ready\nlink"


def test_top_memory_ranks_and_skips_vanished() -> None:
    svc = DockerService(FakeAPI())  # type: ignore[arg-type]
    top = asyncio.run(svc.top_memory(3))
    assert [(m.name, m.used) for m in top] == [("plex", 900), ("cache", 700), ("db", 500)]


def test_events_since_is_formatted_for_the_docker_api() -> None:
    seen: dict[str, object] = {}
    api = FakeAPI()
    api.events = lambda **kw: seen.update(kw) or iter(())  # type: ignore[attr-defined]
    svc = DockerService(api)  # type: ignore[arg-type]
    svc.events()
    assert seen["since"] is None and seen["decode"] is True
    svc.events(since_ns=1_700_000_000_000_000_042)
    assert seen["since"] == "1700000000.000000042"  # seconds.nanoseconds, as moby parses it
