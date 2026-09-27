import asyncio
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import bot.cogs.system as system_cog
from bot.cogs.system import SystemCog, host_field, ups_field, zfs_field
from bot.config import WolHost
from bot.services import system
from bot.services.system import HostMetrics, bar, is_up, level, magic_packet, nut_vars, send_wol, zfs_pools
from tests.helpers import fake_bot, interaction

METRICS = HostMetrics(95.0, 50.0, 8 * 2**30, 16 * 2**30, (1.0, 0.5, 0.25), 8, 55.0)


def test_bar_and_levels() -> None:
    assert bar(0) == "░" * 10 and bar(100) == "█" * 10 and bar(42) == "████░░░░░░" and bar(250) == "█" * 10
    assert (level(10), level(75), level(95)) == ("🟢", "🟡", "🔴")


def test_host_field_worst_dot_wins() -> None:
    text, dot = host_field(METRICS)
    assert dot == "🔴" and "8.0 GiB / 16.0 GiB" in text and "55°C" in text and "on 8 threads" in text


def test_magic_packet_layout() -> None:
    pkt = magic_packet("AA:BB:CC:DD:EE:FF")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6 and pkt[6:] == bytes.fromhex("AABBCCDDEEFF") * 16
    assert magic_packet("aa-bb-cc-dd-ee-ff") == pkt
    with pytest.raises(ValueError):
        magic_packet("AA:BB:CC")


def test_send_wol_emits_udp_packet() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        send_wol("AA:BB:CC:DD:EE:FF", "127.0.0.1", rx.getsockname()[1])
        assert rx.recv(200) == magic_packet("AA:BB:CC:DD:EE:FF")


def test_is_up_open_refused_and_unreachable() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        open_port = server.sockets[0].getsockname()[1]
        with socket.socket() as s:  # grab a free port, then close it so it refuses
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        result = (
            await is_up("127.0.0.1", open_port),
            await is_up("127.0.0.1", closed_port),
            await is_up("192.0.2.1", 9, wait_s=0.3),  # RFC 5737 TEST-NET-1: never answers
        )
        server.close()
        return result

    assert asyncio.run(scenario()) == (True, True, False)


def test_zfs_pools_reads_kstat(tmp_path: Path) -> None:
    for pool, state in (("tank", "ONLINE\n"), ("backup", "DEGRADED\n")):
        (tmp_path / pool).mkdir()
        (tmp_path / pool / "state").write_text(state)
    (tmp_path / "arcstats").write_text("not a pool")  # sibling kstat files must be ignored
    pools = zfs_pools(tmp_path)
    assert pools == {"backup": "DEGRADED", "tank": "ONLINE"}
    assert zfs_field(pools)[1] == "🟡"
    assert zfs_pools(tmp_path / "missing") == {}


def test_nut_protocol_roundtrip() -> None:
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readline())
        writer.write(
            b"BEGIN LIST VAR ups\n"
            b'VAR ups battery.charge "87"\n'
            b'VAR ups ups.status "OB DISCHRG"\n'
            b'VAR ups ups.mfr "APC \\"Back\\" UPS"\n'
            b"END LIST VAR ups\n"
        )
        await writer.drain()
        writer.close()

    async def scenario() -> dict[str, str]:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            return await nut_vars("127.0.0.1", server.sockets[0].getsockname()[1], "ups")
        finally:
            server.close()

    found = asyncio.run(scenario())
    assert received == [b"LIST VAR ups\n"]
    assert found == {"battery.charge": "87", "ups.status": "OB DISCHRG", "ups.mfr": 'APC "Back" UPS'}
    assert ups_field(found) == ("🟡 `OB DISCHRG` · 🔋 87%", "🟡")


def test_status_command_builds_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "host_metrics", lambda: METRICS)
    monkeypatch.setattr(system, "zfs_pools", lambda: {"tank": "ONLINE"})
    bot = fake_bot()
    cog = SystemCog(bot)
    itx = interaction(bot=bot)
    asyncio.run(cog.status.callback(cog, itx, "all"))  # type: ignore[arg-type]
    embed = itx.followup.send.call_args.kwargs["embed"]
    names = [f.name for f in embed.fields]
    assert names == ["Host", "Top containers (RAM)", "ZFS", "UPS"]
    assert "**plex**" in embed.fields[1].value and "Not configured" in embed.fields[3].value
    assert embed.colour.value == 0xE74C3C  # red: CPU at 95%


def test_wake_rejects_unknown_host_and_waits_for_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = fake_bot()
    cog = SystemCog(bot)
    itx = interaction(bot=bot)
    asyncio.run(cog.wake.callback(cog, itx, "toaster"))  # type: ignore[arg-type]
    assert "Unknown host" in itx.response.send_message.call_args.args[0]

    probes = iter([False, False, True])
    monkeypatch.setattr(system, "is_up", AsyncMock(side_effect=lambda ip, port: next(probes)))
    target = WolHost("AA:BB:CC:DD:EE:FF", "192.168.1.10", 22)
    asyncio.run(cog.wait_for_boot(itx, "nas", target, poll=0, deadline=5))
    assert itx.edit_original_response.call_args.kwargs["content"].startswith("🟢 **nas** is online")

    monkeypatch.setattr(system, "is_up", AsyncMock(return_value=False))
    asyncio.run(cog.wait_for_boot(itx, "nas", target, poll=0.01, deadline=0.03))
    assert "did not answer" in itx.edit_original_response.call_args.kwargs["content"]


def test_lockdown_confirms_then_stops_listed_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = fake_bot(LOCKDOWN_CONTAINERS="plex,ghost")
    cog = SystemCog(bot)

    def run(confirmed: bool) -> Any:
        monkeypatch.setattr(system_cog, "ask_confirm", AsyncMock(return_value=confirmed))
        itx = interaction(bot=bot)
        asyncio.run(cog.lockdown.callback(cog, itx))  # type: ignore[arg-type]
        return itx

    run(False)
    assert bot.api.actions == []
    itx = run(True)
    assert bot.api.actions == [("stop", "plex")]
    report = itx.edit_original_response.call_args.kwargs["content"]
    assert "🟢 `plex`: stopped" in report and "⚪ `ghost`: no such container" in report


def test_status_sections_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def slow_metrics() -> HostMetrics:
        time.sleep(0.3)
        return METRICS

    def slow_pools() -> dict[str, str]:
        time.sleep(0.3)
        return {"tank": "ONLINE"}

    async def slow_nut(*_: object) -> dict[str, str]:
        await asyncio.sleep(0.3)
        return {"ups.status": "OL"}

    monkeypatch.setattr(system, "host_metrics", slow_metrics)
    monkeypatch.setattr(system, "zfs_pools", slow_pools)
    monkeypatch.setattr(system, "nut_vars", slow_nut)
    bot = fake_bot(NUT_HOST="10.0.0.2")
    cog = SystemCog(bot)
    itx = interaction(bot=bot)
    started = time.perf_counter()
    asyncio.run(cog.status.callback(cog, itx, "all"))  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started
    assert elapsed < 0.6, f"sections ran sequentially ({elapsed:.2f}s for 3 x 0.3s)"
    names = [f.name for f in itx.followup.send.call_args.kwargs["embed"].fields]
    assert names == ["Host", "Top containers (RAM)", "ZFS", "UPS"]  # order stays stable


def test_status_single_target_runs_only_that_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "host_metrics", lambda: pytest.fail("host section must not run"))
    monkeypatch.setattr(system, "zfs_pools", lambda: {"tank": "FAULTED"})
    bot = fake_bot()
    cog = SystemCog(bot)
    itx = interaction(bot=bot)
    asyncio.run(cog.status.callback(cog, itx, "zfs"))  # type: ignore[arg-type]
    embed = itx.followup.send.call_args.kwargs["embed"]
    assert [f.name for f in embed.fields] == ["ZFS"] and embed.colour.value == 0xE74C3C


def test_real_psutil_metrics_are_sane() -> None:
    m = system.host_metrics()
    assert 0 <= m.cpu_pct <= 100 and 0 < m.mem_used <= m.mem_total and m.cpus >= 1 and len(m.load) == 3


def test_cpu_temp_prefers_cpu_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace as T

    temps = {"nvme": [T(current=71.0)], "k10temp": [T(current=48.0), T(current=52.5)]}
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: temps, raising=False)
    assert system.cpu_temp() == 52.5
    monkeypatch.setattr(
        system.psutil, "sensors_temperatures", lambda: {"nvme": [T(current=71.0)]}, raising=False
    )
    assert system.cpu_temp() == 71.0
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: {}, raising=False)
    assert system.cpu_temp() is None


def test_wake_sends_packet_and_tracks_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(system, "send_wol", lambda mac, bcast: sent.append((mac, bcast)))
    monkeypatch.setattr(system, "is_up", AsyncMock(return_value=True))
    bot = fake_bot()
    cog = SystemCog(bot)
    itx = interaction(bot=bot)

    async def scenario() -> None:
        await cog.wake.callback(cog, itx, "nas")  # type: ignore[arg-type]
        assert len(cog._tasks) == 1  # strong reference kept while it runs
        await asyncio.gather(*cog._tasks)

    asyncio.run(scenario())
    assert sent == [("AA:BB:CC:DD:EE:FF", "255.255.255.255")]
    assert itx.edit_original_response.call_args.kwargs["content"].startswith("🟢 **nas** is online")
    assert not cog._tasks  # discarded when done


def test_host_autocomplete_and_empty_lockdown() -> None:
    bot = fake_bot(LOCKDOWN_CONTAINERS="")
    cog = SystemCog(bot)
    choices = asyncio.run(cog._host_autocomplete(interaction(bot=bot), "NA"))  # type: ignore[arg-type]
    assert [c.value for c in choices] == ["nas"]
    itx = interaction(bot=bot)
    asyncio.run(cog.lockdown.callback(cog, itx))  # type: ignore[arg-type]
    assert "empty" in itx.response.send_message.call_args.args[0]


def test_lockdown_reports_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = fake_bot(LOCKDOWN_CONTAINERS="plex")
    bot.docker.stop = AsyncMock(side_effect=ConnectionError("proxy down"))
    monkeypatch.setattr(system_cog, "ask_confirm", AsyncMock(return_value=True))
    cog = SystemCog(bot)
    itx = interaction(bot=bot)
    asyncio.run(cog.lockdown.callback(cog, itx))  # type: ignore[arg-type]
    assert "🔴 `plex`: ConnectionError" in itx.edit_original_response.call_args.kwargs["content"]
