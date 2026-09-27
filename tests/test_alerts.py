import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from bot.cogs.alerts import (
    AckButton,
    AlertsCog,
    LogsButton,
    RestartButton,
    incident_embed,
    incident_view,
    stamp,
)
from bot.services.incidents import Incident
from tests.helpers import fake_bot, interaction


def buttons(view: discord.ui.View) -> list[Any]:
    return [getattr(i, "item", i) for i in view.children]  # DynamicItem wraps the real Button


INCIDENT = Incident("plex", "plex crashed (exit 139)", "`plex` exited with code **139**.", "docker", "plex")


class FakeChannel(discord.abc.Messageable):
    def __init__(self) -> None:
        self.send = AsyncMock()  # type: ignore[method-assign]

    async def _get_channel(self) -> Any:
        return self


def alert_bot(channel: Any = None) -> Any:
    bot = fake_bot()
    bot.channel = channel or FakeChannel()
    bot.wait_until_ready = AsyncMock()
    bot.get_channel = lambda cid: bot.channel if cid == 999 else None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "x"))
    bot.cog = AlertsCog(bot)
    bot.get_cog = lambda name: bot.cog if name == "AlertsCog" else None
    return bot


def test_view_has_three_persistent_buttons_for_known_container() -> None:
    async def scenario() -> list[Any]:
        return [(b.custom_id, b.disabled) for b in buttons(incident_view("plex", "plex"))]

    assert asyncio.run(scenario()) == [
        ("alert:restart:plex", False),
        ("alert:logs:plex", False),
        ("alert:ack:plex", False),
    ]


def test_view_disables_container_buttons_without_container() -> None:
    async def scenario() -> list[bool]:
        return [b.disabled for b in buttons(incident_view("jellyfin", None))]

    assert asyncio.run(scenario()) == [True, True, False]


def test_stamp_appends_and_caps_field() -> None:
    embed = incident_embed(INCIDENT)
    for i in range(100):
        embed = stamp(embed, f"line {i:03d} " + "x" * 20)
    actions = next(f for f in embed.fields if f.name == "Actions")
    assert len(actions.value or "") <= 1024 and (actions.value or "").endswith("line 099 " + "x" * 20)
    assert sum(f.name == "Actions" for f in embed.fields) == 1


def test_raise_incident_posts_then_dedupes() -> None:
    bot = alert_bot()
    assert asyncio.run(bot.cog.raise_incident(INCIDENT)) is True
    kwargs = bot.channel.send.call_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["embed"].title == "🚨 plex crashed (exit 139)"
    assert asyncio.run(bot.cog.raise_incident(INCIDENT)) is False  # cooldown
    assert bot.channel.send.call_count == 1


def test_failed_delivery_does_not_silence_the_retry() -> None:
    bot = alert_bot()
    bot.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "down")
    with pytest.raises(discord.HTTPException):
        asyncio.run(bot.cog.raise_incident(INCIDENT))
    assert not bot.cog.silencer.silenced("plex")


def test_alert_still_posts_when_docker_is_down() -> None:
    bot = alert_bot()
    bot.docker.resolve = AsyncMock(side_effect=ConnectionError("proxy down"))
    assert asyncio.run(bot.cog.raise_incident(INCIDENT)) is True
    view = bot.channel.send.call_args.kwargs["view"]
    assert [b.disabled for b in buttons(view)] == [True, True, False]


def _click(button: Any, bot: Any, user_id: int = 111) -> Any:
    itx = interaction(user_id=user_id, bot=bot)
    itx.message = SimpleNamespace(embeds=[incident_embed(INCIDENT)])

    async def scenario() -> None:
        if await button.interaction_check(itx):
            await button.callback(itx)

    asyncio.run(scenario())
    return itx


def test_restart_button_restarts_and_stamps_embed() -> None:
    bot = alert_bot()
    itx = _click(RestartButton("plex"), bot)
    assert bot.api.actions == [("restart", "plex")]
    actions = itx.edit_original_response.call_args.kwargs["embed"].fields[-1].value
    assert actions.startswith("🟢 Restarted by <@111> at <t:")


def test_restart_button_reports_failure_in_embed() -> None:
    bot = alert_bot()
    bot.docker.restart = AsyncMock(side_effect=ConnectionError("proxy down"))
    itx = _click(RestartButton("plex"), bot)
    assert (
        "🔴 Restart failed: proxy down"
        in itx.edit_original_response.call_args.kwargs["embed"].fields[-1].value
    )


def test_buttons_ignore_strangers() -> None:
    bot = alert_bot()
    for button in (RestartButton("plex"), LogsButton("plex"), AckButton("plex")):
        itx = _click(button, bot, user_id=666)
        itx.response.defer.assert_not_called()
        itx.response.edit_message.assert_not_called()
    assert bot.api.actions == []


def test_logs_button_replies_ephemerally() -> None:
    bot = alert_bot()
    itx = _click(LogsButton("plex"), bot)
    assert bot.api.actions == [("logs", "plex")]
    assert itx.followup.send.call_args.kwargs["ephemeral"] is True


def test_ack_silences_one_hour_and_stamps() -> None:
    bot = alert_bot()
    itx = _click(AckButton("plex"), bot)
    assert bot.cog.silencer.silenced("plex")
    embed = itx.response.edit_message.call_args.kwargs["embed"]
    assert "🔇 Acknowledged by <@111> at <t:" in embed.fields[-1].value
    assert embed.colour == discord.Colour.dark_grey()


def test_buttons_rebuild_from_custom_id_after_restart() -> None:
    async def scenario() -> str:
        match = RestartButton.__discord_ui_compiled_template__.fullmatch("alert:restart:my-app.1")
        assert match is not None
        return (await RestartButton.from_custom_id(None, None, match)).name  # type: ignore[arg-type]

    assert asyncio.run(scenario()) == "my-app.1"
    assert RestartButton.__discord_ui_compiled_template__.fullmatch("alert:restart:x;rm -rf") is None


def test_docker_watcher_turns_events_into_incidents() -> None:
    async def scenario() -> Any:
        bot = alert_bot()
        cog = bot.cog
        posted: list[Incident] = []

        async def raise_incident(incident: Incident) -> bool:
            posted.append(incident)
            cog._stop.set()
            return True

        cog.raise_incident = raise_incident
        events = [
            {"Action": "kill", "Actor": {"Attributes": {"name": "npm", "signal": "15"}}},
            {"Action": "die", "Actor": {"Attributes": {"name": "npm", "exitCode": "143"}}},
            {"Action": "die", "Actor": {"Attributes": {"name": "plex", "exitCode": "0"}}},
            {"Action": "oom", "Actor": {"Attributes": {"name": "db"}}},
        ]
        bot.docker.events = lambda since_ns: iter(events)
        await asyncio.to_thread(cog._watch_docker, asyncio.get_running_loop())
        await asyncio.sleep(0.05)
        return posted

    posted = asyncio.run(scenario())
    assert [i.service for i in posted] == ["db"]


def test_docker_watcher_reconnects_after_stream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import bot.cogs.alerts as alerts

    monkeypatch.setattr(alerts, "RECONNECT_MIN_S", 0.01)

    since: list[int | None] = []

    async def scenario() -> tuple[list[str], int]:
        bot = alert_bot()
        cog = bot.cog
        posted: list[str] = []
        attempts = 0

        def events(since_ns: int | None) -> Any:
            nonlocal attempts
            attempts += 1
            since.append(since_ns)
            if attempts == 1:
                raise ConnectionError("proxy restarting")
            if attempts == 2:  # one event, then the stream drops mid-way
                return iter([{"Action": "start", "timeNano": 1_700_000_000_000_000_000, "Actor": {}}])
            return iter(
                [
                    {
                        "Action": "oom",
                        "timeNano": 1_700_000_005_000_000_000,
                        "Actor": {"Attributes": {"name": "db"}},
                    }
                ]
            )

        async def raise_incident(incident: Incident) -> bool:
            posted.append(incident.service)
            cog._stop.set()
            return True

        bot.docker.events = events
        cog.raise_incident = raise_incident
        await asyncio.wait_for(asyncio.to_thread(cog._watch_docker, asyncio.get_running_loop()), 5)
        await asyncio.sleep(0.05)
        return posted, attempts

    posted, attempts = asyncio.run(scenario())
    assert posted == ["db"] and attempts == 3
    # first connect is live; after a drop it resumes 1 ns after the last event it saw
    assert since == [None, None, 1_700_000_000_000_000_001]


def test_restart_button_on_removed_container() -> None:
    bot = alert_bot()
    itx = _click(RestartButton("vanished"), bot)
    assert bot.api.actions == []
    assert (
        "⚪ Container no longer exists"
        in itx.edit_original_response.call_args.kwargs["embed"].fields[-1].value
    )


def test_logs_button_reports_docker_errors() -> None:
    bot = alert_bot()
    bot.docker.logs = AsyncMock(side_effect=ConnectionError("proxy down"))
    itx = _click(LogsButton("plex"), bot)
    assert itx.followup.send.call_args.args[0] == "🔴 Could not read logs: proxy down"
