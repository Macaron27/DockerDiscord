"""Zero-trust auth: every command, autocomplete and button goes through `authorized()`."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

if TYPE_CHECKING:
    from bot.app import HomelabBot

log = logging.getLogger(__name__)


def authorized(interaction: discord.Interaction[Any]) -> bool:
    """True if the caller is in ALLOWED_USER_IDS. Otherwise log it and return False.

    Returning False without responding is the "silent ignore": Discord shows the caller
    a generic "did not respond" error and the bot reveals nothing.
    """
    bot: HomelabBot = interaction.client
    if interaction.user.id in bot.config.allowed_user_ids:
        return True
    data: Mapping[str, object] = interaction.data or {}
    log.warning(
        "unauthorized %s from user=%s (%s) guild=%s command=%r custom_id=%r",
        interaction.type.name,
        interaction.user.id,
        interaction.user,
        interaction.guild_id,
        data.get("name"),
        data.get("custom_id"),
    )
    return False


class GuardedTree(app_commands.CommandTree):
    # Runs before command *and* autocomplete dispatch, so strangers can't enumerate containers.
    async def interaction_check(self, interaction: discord.Interaction[Any], /) -> bool:
        return authorized(interaction)

    async def on_error(
        self, interaction: discord.Interaction[Any], error: app_commands.AppCommandError, /
    ) -> None:
        cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        log.error("command %r failed", interaction.data and interaction.data.get("name"), exc_info=cause)
        if interaction.type is discord.InteractionType.autocomplete:
            return  # autocomplete can't carry a message; the user just sees no suggestions
        text = f"🔴 Failed: `{type(cause).__name__}`. Details are in the bot logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction expired; the log line above is what matters


class GuardedView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction[Any], /) -> bool:
        return authorized(interaction)


class ConfirmView(GuardedView):
    def __init__(self, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.confirmed = False

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction[Any], _: discord.ui.Button[ConfirmView]) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Working…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction[Any], _: discord.ui.Button[ConfirmView]) -> None:
        await interaction.response.edit_message(content="❌ Cancelled.", view=None)
        self.stop()


async def ask_confirm(interaction: discord.Interaction[Any], prompt: str, expires_after: float = 30) -> bool:
    """Ephemeral ✅/❌ prompt. On True the caller edits the original response with the result."""
    view = ConfirmView(expires_after)
    await interaction.response.send_message(prompt, view=view, ephemeral=True)
    if await view.wait():  # True means the timeout fired
        await interaction.edit_original_response(
            content=f"{prompt}\n⌛ Expired, nothing was done.", view=None
        )
        return False
    return view.confirmed
