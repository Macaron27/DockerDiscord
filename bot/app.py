"""Wiring: one asyncio loop runs the Discord gateway, the webhook server and the Docker event callbacks."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from bot.config import Config, ConfigError, load
from bot.guards import GuardedTree
from bot.services.docker import DockerService

log = logging.getLogger(__name__)

EXTENSIONS = ("bot.cogs.docker", "bot.cogs.system", "bot.cogs.alerts")


class HomelabBot(commands.Bot):
    docker: DockerService

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True  # channel cache for alerts; slash commands need no privileged intents
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, tree_cls=GuardedTree, help_command=None
        )
        self.config = config

    async def setup_hook(self) -> None:
        # Fails fast if the socket proxy is unreachable; the compose restart policy retries.
        self.docker = await DockerService.connect(self.config.docker_host)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        if self.config.guild_id:  # guild-scoped sync: commands appear in that server right away
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)


async def _run(config: Config) -> None:
    bot = HomelabBot(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # `docker stop` sends SIGTERM: shut down cleanly
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
    async with bot:
        await bot.start(config.discord_token)


def main() -> None:
    try:
        config = load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    discord.utils.setup_logging(level=logging.getLevelName(config.log_level))
    asyncio.run(_run(config))
