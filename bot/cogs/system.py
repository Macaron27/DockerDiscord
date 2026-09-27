"""/status, /wake and /lockdown."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

import discord
import docker
from discord import app_commands
from discord.ext import commands

from bot.config import WolHost
from bot.guards import ask_confirm
from bot.services import system
from bot.services.docker import ContainerMemory
from bot.services.system import HostMetrics, bar, level

if TYPE_CHECKING:
    from bot.app import HomelabBot

log = logging.getLogger(__name__)

WAKE_POLL_S = 5.0
WAKE_TIMEOUT_S = 90.0
RANK = {"🟢": 0, "🟡": 1, "🔴": 2}
COLOUR = {"🟢": discord.Colour.green(), "🟡": discord.Colour.gold(), "🔴": discord.Colour.red()}
Field = tuple[str, str, str | None]  # embed field name, value, status dot (None = not scored)


def _size(n: float) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


def host_field(m: HostMetrics) -> tuple[str, str]:
    load_pct = m.load[0] / m.cpus * 100
    lines = [
        f"{level(m.cpu_pct)} CPU  `{bar(m.cpu_pct)}` {m.cpu_pct:4.0f}%",
        f"{level(m.mem_pct)} RAM  `{bar(m.mem_pct)}` {m.mem_pct:4.0f}%"
        f"  ({_size(m.mem_used)} / {_size(m.mem_total)})",
        f"{level(load_pct)} Load `{m.load[0]:.2f} {m.load[1]:.2f} {m.load[2]:.2f}` on {m.cpus} threads",
    ]
    dots = [level(m.cpu_pct), level(m.mem_pct), level(load_pct)]
    if m.cpu_temp is not None:
        dots.append(level(m.cpu_temp, warn=70, crit=85))
        lines.append(f"{dots[-1]} Temp `{m.cpu_temp:.0f}°C`")
    return "\n".join(lines), max(dots, key=RANK.__getitem__)


def top_field(top: list[ContainerMemory]) -> str:
    return (
        "\n".join(f"`{i}.` **{c.name}**: {_size(c.used)}" for i, c in enumerate(top, 1))
        or "No running containers"
    )


def zfs_field(pools: dict[str, str]) -> tuple[str, str]:
    if not pools:
        return f"🟡 No pools visible at `{system.ZFS_KSTAT}`", "🟡"
    dots = {name: system.zfs_level(state) for name, state in pools.items()}
    text = "\n".join(f"{dots[name]} **{name}**: `{state}`" for name, state in pools.items())
    return text, max(dots.values(), key=RANK.__getitem__)


def ups_field(ups: dict[str, str]) -> tuple[str, str]:
    status = ups.get("ups.status", "?")
    dot = system.ups_level(status)
    parts = [f"{dot} `{status}`"]
    if "battery.charge" in ups:
        parts.append(f"🔋 {ups['battery.charge']}%")
    if "battery.runtime" in ups:
        parts.append(f"⏱️ {int(float(ups['battery.runtime'])) // 60} min")
    if "ups.load" in ups:
        parts.append(f"load {ups['ups.load']}%")
    return " · ".join(parts), dot


class SystemCog(commands.Cog):
    def __init__(self, bot: HomelabBot) -> None:
        self.bot = bot
        self._tasks: set[asyncio.Task[None]] = set()

    async def cog_unload(self) -> None:
        for task in self._tasks:
            task.cancel()

    # --- /status -------------------------------------------------------------------------------

    @app_commands.command(name="status", description="Host, ZFS, UPS and container health")
    @app_commands.describe(target="What to check (default: all)")
    async def status(
        self,
        interaction: discord.Interaction[HomelabBot],
        target: Literal["all", "host", "zfs", "ups"] = "all",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        sections = {"host": self._host_section, "zfs": self._zfs_section, "ups": self._ups_section}
        wanted = sections.values() if target == "all" else [sections[target]]
        # Sections are independent: total latency is the slowest one, not the sum.
        results = await asyncio.gather(*(section() for section in wanted))

        embed = discord.Embed(title="🖥️ Homelab status", timestamp=discord.utils.utcnow())
        dots: list[str] = []
        for fields in results:
            for name, value, dot in fields:
                embed.add_field(name=name, value=value, inline=False)
                if dot is not None:
                    dots.append(dot)
        embed.colour = COLOUR[max(dots, key=RANK.__getitem__, default="🟢")]
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _host_section(self) -> list[Field]:
        metrics, top = await asyncio.gather(
            asyncio.to_thread(system.host_metrics), self.bot.docker.top_memory(3), return_exceptions=True
        )
        if isinstance(metrics, BaseException):
            raise metrics
        text, dot = host_field(metrics)
        if isinstance(top, BaseException):
            log.warning("top containers unavailable: %r", top)
            return [("Host", text, dot), ("Top containers (RAM)", "🔴 Docker API unreachable", "🔴")]
        return [("Host", text, dot), ("Top containers (RAM)", top_field(top), None)]

    async def _zfs_section(self) -> list[Field]:
        text, dot = zfs_field(await asyncio.to_thread(system.zfs_pools))
        return [("ZFS", text, dot)]

    async def _ups_section(self) -> list[Field]:
        cfg = self.bot.config
        if cfg.nut_host is None:
            return [("UPS", "⚪ Not configured (set `NUT_HOST`)", None)]
        try:
            text, dot = ups_field(await system.nut_vars(cfg.nut_host, cfg.nut_port, cfg.nut_ups))
        except (OSError, ValueError) as e:  # unreachable, NUT ERR, or junk values
            text, dot = f"🔴 NUT unreachable: `{type(e).__name__}`", "🔴"
        return [("UPS", text, dot)]

    # --- /wake ---------------------------------------------------------------------------------

    @app_commands.command(name="wake", description="Send a Wake-on-LAN packet and wait for the host to boot")
    @app_commands.describe(host="Configured host (WOL_HOSTS)")
    async def wake(self, interaction: discord.Interaction[HomelabBot], host: str) -> None:
        target = self.bot.config.wol_hosts.get(host)
        if target is None:
            await interaction.response.send_message(
                "❓ Unknown host. Pick one from the list.", ephemeral=True
            )
            return
        system.send_wol(target.mac, self.bot.config.wol_broadcast)
        log.info("user=%s woke %s (%s)", interaction.user.id, host, target.mac)
        await interaction.response.send_message(
            f"📡 Magic packet sent to **{host}** (`{target.mac}`). Waiting for `{target.ip}`…", ephemeral=True
        )
        task = asyncio.create_task(self.wait_for_boot(interaction, host, target))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for_boot(
        self,
        interaction: discord.Interaction[HomelabBot],
        host: str,
        target: WolHost,
        poll: float = WAKE_POLL_S,
        deadline: float = WAKE_TIMEOUT_S,
    ) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while (elapsed := loop.time() - start) <= deadline:
            if await system.is_up(target.ip, target.port):
                await interaction.edit_original_response(
                    content=f"🟢 **{host}** is online after {elapsed:.0f}s."
                )
                return
            await asyncio.sleep(poll)
        await interaction.edit_original_response(
            content=f"🔴 **{host}** did not answer on `{target.ip}:{target.port}` within {deadline:.0f}s."
        )

    @wake.autocomplete("host")
    async def _host_autocomplete(
        self, interaction: discord.Interaction[HomelabBot], current: str
    ) -> list[app_commands.Choice[str]]:
        hosts = self.bot.config.wol_hosts
        return [app_commands.Choice(name=n, value=n) for n in hosts if current.lower() in n.lower()][:25]

    # --- /lockdown -----------------------------------------------------------------------------

    @app_commands.command(name="lockdown", description="EMERGENCY: stop every external ingress container")
    async def lockdown(self, interaction: discord.Interaction[HomelabBot]) -> None:
        names = self.bot.config.lockdown_containers
        if not names:
            await interaction.response.send_message("⚪ `LOCKDOWN_CONTAINERS` is empty.", ephemeral=True)
            return
        listing = ", ".join(f"`{n}`" for n in names)
        if not await ask_confirm(interaction, f"🚨 **LOCKDOWN**: stop {listing}? LAN access stays up."):
            return
        log.warning("user=%s triggered LOCKDOWN of %s", interaction.user.id, ",".join(names))
        results = await asyncio.gather(*(self._stop_one(n) for n in names))
        await interaction.edit_original_response(
            content="🔒 **Lockdown finished**\n"
            + "\n".join(results)
            + "\nUndo per container with `/docker restart`."
        )

    async def _stop_one(self, name: str) -> str:
        # No pre-listing: in an emergency, try every stop even if listing containers fails.
        try:
            await self.bot.docker.stop(name)
        except docker.errors.NotFound:
            return f"⚪ `{name}`: no such container"
        except Exception as e:
            log.exception("lockdown: stopping %s failed", name)
            return f"🔴 `{name}`: {type(e).__name__}"
        return f"🟢 `{name}`: stopped"


async def setup(bot: HomelabBot) -> None:
    await bot.add_cog(SystemCog(bot))
