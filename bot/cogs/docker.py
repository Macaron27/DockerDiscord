"""/docker status | restart | stop | logs, with live container-name autocomplete."""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import discord
import docker
from discord import app_commands
from discord.ext import commands

from bot.guards import ask_confirm
from bot.services.docker import LIST_CACHE_S, ContainerInfo

if TYPE_CHECKING:
    from bot.app import HomelabBot

log = logging.getLogger(__name__)

LOG_LINES = 30
INLINE_LIMIT = 1900
PAST = {"restart": "restarted", "stop": "stopped"}


def container_choices(containers: list[ContainerInfo], current: str) -> list[app_commands.Choice[str]]:
    needle = current.lower()
    matches = sorted(
        (c for c in containers if needle in c.name.lower()),
        key=lambda c: (not c.name.lower().startswith(needle), c.name),
    )
    return [app_commands.Choice(name=f"{c.dot} {c.name} ({c.state})", value=c.name) for c in matches[:25]]


def render_logs(name: str, text: str, lines: int) -> dict[str, Any]:
    """Send kwargs: an inline code block, or a .txt attachment when it would exceed 1900 chars."""
    header = f"📜 Last {lines} lines of `{name}`"
    block = "```\n" + (text or "(no output)").replace("```", "`\u200b``") + "\n```"
    if len(block) <= INLINE_LIMIT:
        return {"content": f"{header}\n{block}"}
    return {
        "content": f"{header} (attached, too long to show inline)",
        "file": discord.File(io.BytesIO(text.encode()), filename=f"{name}-logs.txt"),
    }


def _started(iso: str) -> str:
    # Docker returns nanosecond RFC 3339 ("...:05.123456789Z") and year 1 for "never".
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return "n/a"
    return "never" if dt.year < 2000 else discord.utils.format_dt(dt, "R")


def container_embed(info: dict[str, Any]) -> discord.Embed:
    state = info.get("State", {})
    health = (state.get("Health") or {}).get("Status")
    running = bool(state.get("Running"))
    if running and health in (None, "healthy"):
        dot, colour = "🟢", discord.Colour.green()
    elif running:
        dot, colour = "🟡", discord.Colour.gold()
    else:
        dot, colour = "🔴", discord.Colour.red()
    embed = discord.Embed(title=f"{dot} {info.get('Name', '?').lstrip('/')}", colour=colour)
    embed.add_field(name="State", value=state.get("Status", "?"))
    embed.add_field(name="Health", value=health or "no healthcheck")
    embed.add_field(name="Restarts", value=str(info.get("RestartCount", 0)))
    embed.add_field(name="Started", value=_started(state.get("StartedAt", "")))
    if not running:
        embed.add_field(name="Exit code", value=str(state.get("ExitCode", "?")))
    embed.add_field(name="Image", value=f"`{info.get('Config', {}).get('Image', '?')}`", inline=False)
    return embed


class DockerCog(commands.Cog):
    def __init__(self, bot: HomelabBot) -> None:
        self.bot = bot

    @app_commands.command(name="docker", description="Inspect or control a Docker container")
    @app_commands.describe(action="What to do", container_name="Container (autocompletes as you type)")
    async def docker_cmd(
        self,
        interaction: discord.Interaction[HomelabBot],
        action: Literal["status", "restart", "stop", "logs"],
        container_name: str,
    ) -> None:
        name = await self.bot.docker.resolve(container_name)
        if name is None:  # never echo the raw input back
            await interaction.response.send_message(
                "❓ No such container. Pick one from the list.", ephemeral=True
            )
            return

        if action in PAST:
            if not await ask_confirm(interaction, f"⚠️ **{action.title()}** container `{name}`?"):
                return
            log.warning("user=%s %s container=%s", interaction.user.id, action, name)
            try:
                await (self.bot.docker.restart if action == "restart" else self.bot.docker.stop)(name)
            except docker.errors.APIError as e:
                await interaction.edit_original_response(
                    content=f"🔴 `{name}`: {action} failed: {e.explanation or e}"
                )
                return
            await interaction.edit_original_response(content=f"🟢 `{name}` {PAST[action]}.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if action == "logs":
            text = await self.bot.docker.logs(name, LOG_LINES)
            await interaction.followup.send(**render_logs(name, text, LOG_LINES), ephemeral=True)
        else:
            await interaction.followup.send(
                embed=container_embed(await self.bot.docker.inspect(name)), ephemeral=True
            )

    @docker_cmd.autocomplete("container_name")
    async def _container_autocomplete(
        self, interaction: discord.Interaction[HomelabBot], current: str
    ) -> list[app_commands.Choice[str]]:
        return container_choices(await self.bot.docker.containers(max_age=LIST_CACHE_S), current)


async def setup(bot: HomelabBot) -> None:
    await bot.add_cog(DockerCog(bot))
