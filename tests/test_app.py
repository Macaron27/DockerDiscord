import asyncio
import threading
from typing import Any

import pytest
from aiohttp import ClientSession

from bot.app import EXTENSIONS, HomelabBot
from bot.config import ConfigError, load
from bot.guards import GuardedTree
from bot.services.docker import DockerService
from tests.helpers import ENV, FakeAPI, config


class BlockingStream:
    """Mimics docker's CancellableStream: iterating blocks until close()."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self) -> Any:
        self.closed.wait()
        return iter(())

    def close(self) -> None:
        self.closed.set()


def test_bot_loads_all_cogs_serves_webhook_and_shuts_down_cleanly() -> None:
    async def scenario() -> tuple[set[str], dict[str, Any], int, bool]:
        bot = HomelabBot(config(WEBHOOK_HOST="127.0.0.1", WEBHOOK_PORT="18765"))
        api = FakeAPI()
        stream = BlockingStream()
        api.events = lambda **kw: stream  # type: ignore[attr-defined]
        bot.docker = DockerService(api)  # type: ignore[arg-type]
        assert isinstance(bot.tree, GuardedTree)
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        commands = {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}
        async with ClientSession() as http:
            async with http.post("http://127.0.0.1:18765/webhook/alert", json={}) as resp:
                status = resp.status
        await bot.close()  # unloads extensions: stops the docker thread and the webhook server
        return set(commands), commands, status, stream.closed.is_set()

    names, commands, status, stream_closed = asyncio.run(scenario())
    assert names == {"docker", "status", "wake", "lockdown"}
    docker_opts = {o["name"]: o for o in commands["docker"]["options"]}
    assert [c["value"] for c in docker_opts["action"]["choices"]] == ["status", "restart", "stop", "logs"]
    assert docker_opts["container_name"]["autocomplete"] is True
    assert status == 401  # webhook is up and rejects unauthenticated calls
    assert stream_closed


def test_missing_env_fails_fast() -> None:
    with pytest.raises(ConfigError):
        load({k: v for k, v in ENV.items() if k != "DISCORD_TOKEN"})


@pytest.mark.parametrize("guild", ["", "123"])
def test_setup_hook_connects_loads_and_syncs(monkeypatch: pytest.MonkeyPatch, guild: str) -> None:
    from unittest.mock import AsyncMock

    import discord

    api = FakeAPI()
    stream = BlockingStream()
    api.events = lambda **kw: stream  # type: ignore[attr-defined]
    connect = AsyncMock(return_value=DockerService(api))  # type: ignore[arg-type]
    monkeypatch.setattr(DockerService, "connect", connect)

    async def scenario() -> Any:
        bot = HomelabBot(config(GUILD_ID=guild, WEBHOOK_HOST="127.0.0.1", WEBHOOK_PORT="18766"))
        bot.tree.sync = AsyncMock(return_value=[object()] * 4)  # type: ignore[method-assign]
        await bot.setup_hook()
        await bot.close()
        return bot.tree.sync

    sync = asyncio.run(scenario())
    connect.assert_awaited_once_with("tcp://127.0.0.1:2375")
    if guild:
        assert sync.call_args.kwargs["guild"] == discord.Object(id=123)
    else:
        assert sync.call_args.kwargs == {}


def test_main_exits_2_on_bad_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from bot.app import main

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and "config error: ALLOWED_USER_IDS" in capsys.readouterr().err
