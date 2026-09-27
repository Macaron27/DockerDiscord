"""Host metrics (psutil), ZFS pool state (OpenZFS kstat), UPS (NUT protocol), Wake-on-LAN."""

from __future__ import annotations

import asyncio
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

import psutil

ZFS_KSTAT = Path("/proc/spl/kstat/zfs")  # OpenZFS creates <pool>/state here (module/zfs/spa_stats.c)
CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
NUT_VAR_RE = re.compile(r'^VAR \S+ (\S+) "((?:[^"\\]|\\.)*)"$')


@dataclass(frozen=True, slots=True)
class HostMetrics:
    cpu_pct: float
    mem_pct: float
    mem_used: int
    mem_total: int
    load: tuple[float, float, float]
    cpus: int
    cpu_temp: float | None


def bar(pct: float, width: int = 10) -> str:
    filled = round(max(0.0, min(pct, 100.0)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def level(value: float, warn: float = 70, crit: float = 90) -> str:
    return "🔴" if value >= crit else "🟡" if value >= warn else "🟢"


def cpu_temp() -> float | None:
    read = getattr(psutil, "sensors_temperatures", None)  # Linux/FreeBSD only
    temps = read() if read else {}
    for key in CPU_SENSORS:
        if temps.get(key):
            return float(max(t.current for t in temps[key]))
    readings = [t.current for group in temps.values() for t in group]
    return float(max(readings)) if readings else None


def host_metrics() -> HostMetrics:
    """Blocking (~0.5 s CPU sample): call through asyncio.to_thread."""
    mem = psutil.virtual_memory()
    return HostMetrics(
        cpu_pct=psutil.cpu_percent(interval=0.5),
        mem_pct=mem.percent,
        mem_used=mem.total - mem.available,
        mem_total=mem.total,
        load=os.getloadavg(),
        cpus=psutil.cpu_count() or 1,
        cpu_temp=cpu_temp(),
    )


def zfs_pools(root: Path = ZFS_KSTAT) -> dict[str, str]:
    """Pool name -> state (ONLINE, DEGRADED, FAULTED, ...). Empty if ZFS isn't visible."""
    try:
        return {
            p.name: (p / "state").read_text().strip()
            for p in sorted(root.iterdir())
            if (p / "state").is_file()
        }
    except OSError:
        return {}


def zfs_level(state: str) -> str:
    return {"ONLINE": "🟢", "DEGRADED": "🟡"}.get(state, "🔴")


async def nut_vars(host: str, port: int, ups: str, wait_s: float = 3.0) -> dict[str, str]:
    """`LIST VAR <ups>` over the NUT network protocol (networkupstools docs/net-protocol.txt)."""
    async with asyncio.timeout(wait_s):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(f"LIST VAR {ups}\n".encode())
            await writer.drain()
            found: dict[str, str] = {}
            while line := (await reader.readline()).decode(errors="replace").strip():
                if line.startswith("ERR"):
                    raise ConnectionError(f"NUT said {line}")
                if line.startswith("END LIST VAR"):
                    break
                if m := NUT_VAR_RE.match(line):
                    found[m[1]] = re.sub(r"\\(.)", r"\1", m[2])
            writer.write(b"LOGOUT\n")
            return found
        finally:
            writer.close()


def ups_level(status: str) -> str:
    flags = status.split()
    return "🔴" if "LB" in flags else "🟡" if "OB" in flags else "🟢" if "OL" in flags else "🟡"


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, broadcast: str, port: int = 9) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet(mac), (broadcast, port))


async def is_up(ip: str, port: int, wait_s: float = 2.0) -> bool:
    """TCP reachability probe; needs no privileges (unlike ICMP under host networking + cap_drop)."""
    try:
        async with asyncio.timeout(wait_s):
            _, writer = await asyncio.open_connection(ip, port)
    except ConnectionRefusedError:
        return True  # a RST means the host's network stack answered: it is up
    except OSError:  # includes TimeoutError, EHOSTUNREACH, ENETUNREACH
        return False
    writer.close()
    return True
