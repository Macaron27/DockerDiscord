"""Proactive alerts: Docker event listener + webhook server -> incident embeds with remediation buttons."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

import discord
from aiohttp import web
from discord.ext import commands

from bot.cogs.docker import render_logs
from bot.guards import authorized
from bot.services.incidents import EventClassifier, Incident, Silencer
from bot.webhooks.server import make_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bot.app import HomelabBot

log = logging.getLogger(__name__)

ACK_SILENCE_S = 3600
LOG_LINES = 50
NAME = r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"
CONTAINER_ID_MAX = 85  # "alert:restart:" + name must fit Discord's 100-char custom_id
ACTIONS_FIELD = "Actions"
RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 60.0


def _now_tag() -> str:
    return f"<t:{int(time.time())}:t>"  # renders as HH:MM in each viewer's own timezone


def incident_embed(incident: Incident) -> discord.Embed:
    embed = discord.Embed(
        title=f"🚨 {incident.title}"[:256],
        description=incident.detail,
        colour=discord.Colour.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Service", value=f"`{incident.service}`")
    embed.add_field(name="Source", value=incident.source)
    return embed


def stamp(embed: discord.Embed, line: str, colour: discord.Colour | None = None) -> discord.Embed:
    """Append a line to the embed's Actions field, keeping it under Discord's 1024-char limit."""
    embed = embed.copy()
    idx = next((i for i, f in enumerate(embed.fields) if f.name == ACTIONS_FIELD), None)
    lines = (embed.fields[idx].value or "").splitlines() if idx is not None else []
    lines.append(line)
    while len("\n".join(lines)) > 1024:
        lines.pop(0)
    if idx is None:
        embed.add_field(name=ACTIONS_FIELD, value="\n".join(lines), inline=False)
    else:
        embed.set_field_at(idx, name=ACTIONS_FIELD, value="\n".join(lines), inline=False)
    if colour is not None:
        embed.colour = colour
    return embed


def _explain(e: Exception) -> str:
    return str(getattr(e, "explanation", None) or e or type(e).__name__)[:300]


_Dynamic = discord.ui.DynamicItem[discord.ui.Button[Any]]


class _Guarded:
    """Auth for the persistent alert buttons (DynamicItems survive bot restarts)."""

    async def interaction_check(self, interaction: discord.Interaction[Any], /) -> bool:
        return authorized(interaction)


class RestartButton(_Guarded, _Dynamic, template=rf"alert:restart:(?P<name>{NAME})"):
    def __init__(self, name: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Restart Container",
                emoji="🔄",
                style=discord.ButtonStyle.primary,
                custom_id=f"alert:restart:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> RestartButton:
        return cls(match["name"])

    async def callback(self, interaction: discord.Interaction[HomelabBot]) -> None:  # type: ignore[override]
        await interaction.response.defer()
        bot = interaction.client
        log.warning("user=%s restart container=%s (alert button)", interaction.user.id, self.name)
        try:
            if await bot.docker.resolve(self.name) is None:
                result = "⚪ Container no longer exists"
            else:
                await bot.docker.restart(self.name)
                result = "🟢 Restarted"
        except Exception as e:  # docker-py also raises plain requests errors when the proxy is down
            result = f"🔴 Restart failed: {_explain(e)}"
        colour = discord.Colour.green() if result.startswith("🟢") else None
        assert interaction.message is not None
        embed = stamp(
            interaction.message.embeds[0], f"{result} by {interaction.user.mention} at {_now_tag()}", colour
        )
        await interaction.edit_original_response(embed=embed)


class LogsButton(_Guarded, _Dynamic, template=rf"alert:logs:(?P<name>{NAME})"):
    def __init__(self, name: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=f"Tail {LOG_LINES} Logs",
                emoji="📜",
                style=discord.ButtonStyle.secondary,
                custom_id=f"alert:logs:{name}",
            )
        )
        self.name = name

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> LogsButton:
        return cls(match["name"])

    async def callback(self, interaction: discord.Interaction[HomelabBot]) -> None:  # type: ignore[override]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await interaction.client.docker.logs(self.name, LOG_LINES)
        except Exception as e:
            await interaction.followup.send(f"🔴 Could not read logs: {_explain(e)}", ephemeral=True)
            return
        await interaction.followup.send(**render_logs(self.name, text, LOG_LINES), ephemeral=True)


class AckButton(_Guarded, _Dynamic, template=r"alert:ack:(?P<key>.+)"):
    def __init__(self, key: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Acknowledge / Silence 1h",
                emoji="🔇",
                style=discord.ButtonStyle.success,
                custom_id=f"alert:ack:{key}",
            )
        )
        self.key = key

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> AckButton:
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction[HomelabBot]) -> None:  # type: ignore[override]
        cog = interaction.client.get_cog(AlertsCog.__cog_name__)
        assert isinstance(cog, AlertsCog)
        cog.silencer.silence(self.key, ACK_SILENCE_S)
        log.info("user=%s acknowledged %s", interaction.user.id, self.key)
        assert interaction.message is not None
        line = f"🔇 Acknowledged by {interaction.user.mention} at {_now_tag()} (silenced 1 h)"
        await interaction.response.edit_message(
            embed=stamp(interaction.message.embeds[0], line, discord.Colour.dark_grey())
        )


def incident_view(key: str, container: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if container and len(container) <= CONTAINER_ID_MAX:
        view.add_item(RestartButton(container))
        view.add_item(LogsButton(container))
    else:  # no matching container (e.g. an external Uptime Kuma monitor): show why the buttons are inert
        view.add_item(discord.ui.Button(label="Restart Container", emoji="🔄", disabled=True))
        view.add_item(discord.ui.Button(label=f"Tail {LOG_LINES} Logs", emoji="📜", disabled=True))
    view.add_item(AckButton(key))
    return view


class AlertsCog(commands.Cog):
    def __init__(self, bot: HomelabBot) -> None:
        self.bot = bot
        self.silencer = Silencer()
        self.classifier = EventClassifier()
        self._stop = threading.Event()
        self._stream: Iterator[dict[str, Any]] | None = None
        self._runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        cfg = self.bot.config
        self.bot.add_dynamic_items(RestartButton, LogsButton, AckButton)
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._watch_docker, args=(loop,), name="docker-events", daemon=True).start()
        self._runner = web.AppRunner(make_app(cfg.webhook_secret, self.raise_incident))
        await self._runner.setup()
        await web.TCPSite(self._runner, cfg.webhook_host, cfg.webhook_port).start()
        log.info("webhook listening on %s:%s/webhook/alert", cfg.webhook_host, cfg.webhook_port)

    async def cog_unload(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.close()  # type: ignore[attr-defined]  # docker CancellableStream
        if self._runner is not None:
            await self._runner.cleanup()
        self.bot.remove_dynamic_items(RestartButton, LogsButton, AckButton)

    def _watch_docker(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking event loop in a thread; reconnects with capped exponential backoff.

        After a drop it resumes from the last event seen, so a crash that happened while the
        proxy or daemon connection was down is still reported (the daemon replays from its
        in-memory buffer of recent events). The first connect starts live: no history replay.
        """
        backoff = RECONNECT_MIN_S
        resume_ns: int | None = None
        while not self._stop.is_set():
            try:
                self._stream = self.bot.docker.events(since_ns=resume_ns)
                log.info("listening for docker events%s", " (resuming)" if resume_ns else "")
                backoff = RECONNECT_MIN_S
                for event in self._stream:
                    if isinstance(event.get("timeNano"), int):
                        resume_ns = event["timeNano"] + 1
                    incident = self.classifier.feed(event)
                    if incident is not None:
                        future = asyncio.run_coroutine_threadsafe(self.raise_incident(incident), loop)
                        future.add_done_callback(_log_failure)
            except Exception:
                if self._stop.is_set():
                    return
                log.exception("docker event stream failed; reconnecting in %.0fs", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    async def raise_incident(self, incident: Incident) -> bool:
        """Post an incident unless that service is in cooldown or acknowledged. True if posted."""
        key = incident.service
        if self.silencer.silenced(key):
            log.info("suppressed duplicate/silenced incident for %s", key)
            return False
        self.silencer.silence(key, self.bot.config.alert_cooldown_s)
        try:
            await self.bot.wait_until_ready()
            try:  # an alert must still go out when Docker itself is the thing that's down
                container = await self.bot.docker.resolve(incident.container)
            except Exception:
                container = None
            cid = self.bot.config.alert_channel_id
            channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError(f"ALERT_CHANNEL_ID {cid} is not a text channel")
            await channel.send(
                embed=incident_embed(incident),
                view=incident_view(key, container),
                allowed_mentions=discord.AllowedMentions.none(),  # payload text is untrusted
            )
        except BaseException:
            self.silencer.clear(key)  # not delivered: let the retry through
            raise
        log.info("posted incident %s (%s)", key, incident.source)
        return True


def _log_failure(future: Future[bool]) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.error("failed to post docker incident", exc_info=future.exception())


async def setup(bot: HomelabBot) -> None:
    await bot.add_cog(AlertsCog(bot))
