import asyncio
import logging

import pytest

from bot.config import ConfigError
from bot.guards import ConfirmView, ask_confirm, authorized
from tests.helpers import config, interaction


def test_config_parses_env() -> None:
    cfg = config()
    assert cfg.allowed_user_ids == {111, 222}
    assert cfg.lockdown_containers == ("cloudflared", "nginx-proxy-manager")
    assert cfg.wol_hosts["nas"].port == 22
    assert cfg.guild_id is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ALLOWED_USER_IDS", ""),
        ("ALLOWED_USER_IDS", "abc"),
        ("WEBHOOK_SECRET", "short"),
        ("ALERT_CHANNEL_ID", "x"),
        ("WOL_HOSTS", '{"nas": {"mac": "nope", "ip": "192.168.1.10"}}'),
        ("WOL_HOSTS", '{"nas": {"mac": "AA:BB:CC:DD:EE:FF", "ip": "999.1.1.1"}}'),
        ("LOCKDOWN_CONTAINERS", "ok,bad name"),
        ("NUT_UPS", "ups\nLOGOUT"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_config_rejects_bad_values(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        config(**{key: value})


def test_authorized_allows_listed_user() -> None:
    assert authorized(interaction(user_id=222))


def test_authorized_rejects_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert not authorized(interaction(user_id=666, name="docker"))
    assert "user=666" in caplog.text and "'docker'" in caplog.text


def _run_confirm(click: str | None, timeout: float = 5) -> tuple[bool, object]:
    async def scenario() -> tuple[bool, object]:
        itx = interaction()
        task = asyncio.create_task(ask_confirm(itx, "Stop it?", expires_after=timeout))
        await asyncio.sleep(0)
        view: ConfirmView = itx.response.send_message.call_args.kwargs["view"]
        assert itx.response.send_message.call_args.kwargs["ephemeral"] is True
        if click:
            await getattr(view, click).callback(interaction())
        return await task, itx

    return asyncio.run(scenario())


def test_confirm_true_on_confirm() -> None:
    assert _run_confirm("confirm")[0] is True


def test_confirm_false_on_cancel() -> None:
    assert _run_confirm("cancel")[0] is False


def test_confirm_expires() -> None:
    ok, itx = _run_confirm(None, timeout=0.05)
    assert ok is False
    assert "Expired" in itx.edit_original_response.call_args.kwargs["content"]


def test_confirm_buttons_reject_strangers() -> None:
    async def scenario() -> bool:
        return await ConfirmView(timeout=1).interaction_check(interaction(user_id=666))

    assert asyncio.run(scenario()) is False


def test_tree_error_replies_ephemerally_but_not_to_autocomplete() -> None:
    from types import SimpleNamespace

    import discord
    from discord import app_commands

    from bot.guards import GuardedTree

    async def scenario(kind: discord.InteractionType) -> object:
        itx = interaction()
        itx.type = kind
        itx.response.is_done = lambda: False
        tree = SimpleNamespace()  # on_error doesn't touch tree state
        await GuardedTree.on_error(tree, itx, app_commands.AppCommandError("boom"))  # type: ignore[arg-type]
        return itx.response.send_message

    sent = asyncio.run(scenario(discord.InteractionType.application_command))
    assert sent.call_args.kwargs["ephemeral"] is True and "AppCommandError" in sent.call_args.args[0]
    asyncio.run(scenario(discord.InteractionType.autocomplete)).assert_not_called()
