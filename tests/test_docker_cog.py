import asyncio
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot.cogs.docker as docker_cog
from bot.cogs.docker import DockerCog, container_choices, container_embed, render_logs
from bot.services.docker import ContainerInfo
from tests.helpers import fake_bot, interaction


def _info(name: str, state: str = "running") -> ContainerInfo:
    return ContainerInfo(name=name, state=state, status="Up", image="img")


def test_choices_filter_prefix_first_and_cap_25() -> None:
    containers = [_info("nginx"), _info("npm-nginx"), _info("plex")] + [_info(f"x{i}") for i in range(40)]
    assert [c.value for c in container_choices(containers, "NGI")] == ["nginx", "npm-nginx"]
    assert len(container_choices(containers, "")) == 25


def test_render_logs_inline_then_attachment() -> None:
    small = render_logs("plex", "line\n```evil```", 30)
    assert "file" not in small and "```\nline\n`\u200b``evil`\u200b``\n```" in small["content"]
    big = render_logs("plex", "x" * 1901, 30)
    assert isinstance(big["file"], discord.File) and big["file"].filename == "plex-logs.txt"


def test_container_embed_stopped() -> None:
    embed = container_embed(fake_bot().api.inspect_container("plex"))
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🔴 plex" and fields["Exit code"] == "137" and fields["Started"].startswith("<t:")


def _call(bot: Any, action: str, name: str) -> Any:
    cog = DockerCog(bot)
    itx = interaction(bot=bot)
    asyncio.run(cog.docker_cmd.callback(cog, itx, action, name))  # type: ignore[arg-type]
    return itx


def test_unknown_container_is_rejected_without_echo() -> None:
    bot = fake_bot()
    itx = _call(bot, "stop", "`@everyone`")
    msg = itx.response.send_message.call_args
    assert "No such container" in msg.args[0] and "everyone" not in msg.args[0]
    assert bot.api.actions == []


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_state_changes_require_confirmation(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    bot = fake_bot()
    monkeypatch.setattr(docker_cog, "ask_confirm", AsyncMock(return_value=False))
    _call(bot, action, "plex")
    assert bot.api.actions == []

    monkeypatch.setattr(docker_cog, "ask_confirm", AsyncMock(return_value=True))
    itx = _call(bot, action, "plex")
    assert bot.api.actions == [(action, "plex")]
    assert "🟢" in itx.edit_original_response.call_args.kwargs["content"]


def test_logs_go_to_ephemeral_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    confirm = AsyncMock()
    monkeypatch.setattr(docker_cog, "ask_confirm", confirm)
    itx = _call(fake_bot(), "logs", "plex")
    sent = itx.followup.send.call_args.kwargs
    assert sent["ephemeral"] is True and "INFO ready" in sent["content"]
    confirm.assert_not_called()


def test_autocomplete_lists_live_containers() -> None:
    bot = fake_bot()
    cog = DockerCog(bot)
    choices = asyncio.run(cog._container_autocomplete(interaction(bot=bot), "pl"))  # type: ignore[arg-type]
    assert [c.value for c in choices] == ["plex"]


def test_autocomplete_reuses_one_listing_but_actions_revalidate() -> None:
    bot = fake_bot()
    cog = DockerCog(bot)

    async def scenario() -> None:
        for typed in ("p", "pl", "ple", "plex"):  # one keystroke each
            await cog._container_autocomplete(interaction(bot=bot), typed)  # type: ignore[arg-type]
        assert await bot.docker.resolve("plex") == "plex"

    asyncio.run(scenario())
    assert [c for c in bot.api.calls if c[0] == "list"] == [
        ("list", "*"),
        ("list", "*"),
    ]  # 1 cached + 1 fresh


def test_docker_api_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import docker

    bot = fake_bot()
    bot.docker.restart = AsyncMock(side_effect=docker.errors.APIError("boom", explanation="conflict"))
    monkeypatch.setattr(docker_cog, "ask_confirm", AsyncMock(return_value=True))
    itx = _call(bot, "restart", "plex")
    assert itx.edit_original_response.call_args.kwargs["content"] == "🔴 `plex`: restart failed: conflict"


def test_status_action_sends_embed() -> None:
    itx = _call(fake_bot(), "status", "plex")
    assert itx.followup.send.call_args.kwargs["embed"].title == "🔴 plex"
