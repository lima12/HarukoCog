"""Display PostgreSQL server totals in persistent Discord voice channels."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

from .hll_database import HLLDatabaseUnavailableError
from .hll_group import HLLVN_COMMAND_GROUP

log = logging.getLogger("red.BattleMetric.discord_stats")


class DiscordStatsModule:
    UPDATE_INTERVAL_SECONDS = 600
    CHANNEL_FORMATS = (
        ("headcount_id", "total_unique_players", "👥 Head Count: {:,}"),
        ("kills_id", "total_kills", "⚔️ Kills: {:,}"),
        ("deaths_id", "total_deaths", "💀 Deaths: {:,}"),
    )
    DEFAULT_SETTINGS = {
        "enabled": False, "headcount_id": None, "kills_id": None, "deaths_id": None,
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._task: asyncio.Task[None] | None = None
        self._locks: dict[int, asyncio.Lock] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(discord_stats=self.DEFAULT_SETTINGS.copy())

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="BattleMetric Discord stats")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        # Retain the task reference until it exits so an immediate start cannot overlap it.

    async def configure(
        self, guild: discord.Guild, toggle: bool | None,
        headcount_channel: discord.VoiceChannel | None,
        kills_channel: discord.VoiceChannel | None,
        deaths_channel: discord.VoiceChannel | None,
    ) -> dict[str, Any]:
        async with self._lock(guild.id):
            value = self.cog.config.guild(guild).discord_stats
            settings = {**self.DEFAULT_SETTINGS, **await value()}
            supplied = (headcount_channel, kills_channel, deaths_channel)
            if toggle is None and all(channel is None for channel in supplied):
                return settings
            for (key, _, _), channel in zip(self.CHANNEL_FORMATS, supplied):
                if channel is not None:
                    if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != guild.id:
                        raise ValueError("Choose voice channels from this server.")
                    settings[key] = channel.id
            if toggle is not None:
                settings["enabled"] = toggle
            if settings["enabled"]:
                ids = [settings[key] for key, _, _ in self.CHANNEL_FORMATS]
                if None in ids or len(set(ids)) != 3:
                    raise ValueError("Configure three different voice channels before enabling stats.")
                member = guild.me
                if member is None:
                    raise ValueError("I could not resolve my server membership to check channel permissions. Try again shortly.")
                permission_errors = []
                for channel_id in ids:
                    channel = guild.get_channel(channel_id)
                    if not isinstance(channel, discord.VoiceChannel):
                        raise ValueError("A configured voice channel is unavailable. Select it again.")
                    permissions = channel.permissions_for(member)
                    if not permissions.view_channel:
                        permission_errors.append(f"<#{channel_id}>: View Channel")
                    elif not permissions.connect:
                        permission_errors.append(f"<#{channel_id}>: Connect")
                    elif not permissions.manage_channels:
                        permission_errors.append(f"<#{channel_id}>: Manage Channels")
                if permission_errors:
                    raise ValueError(
                        "My effective permissions are missing for these voice channels:\n- "
                        + "\n- ".join(permission_errors)
                        + "\nAllow the listed permissions for my bot role. Discord also removes "
                        "Manage Channels in voice channels when Connect is denied. Check each "
                        "channel's overrides and whether it is synced with its category."
                    )
            await value.set(settings)
            return settings

    async def _worker(self) -> None:
        await self.cog.bot.wait_until_ready()
        while True:
            try:
                await self._refresh_channels()
            except asyncio.CancelledError:
                raise
            except HLLDatabaseUnavailableError as exc:
                # The shared database boundary supplies a sanitized, actionable message.
                log.warning("Discord stats unavailable: %s Retrying in ten minutes.", exc)
            except Exception:
                # Avoid raw database/HTTP payloads in logs and keep retrying next cycle.
                log.warning("Discord stats cycle failed; retrying in ten minutes")
            # Sleep after completion: a slow refresh or rename cannot cause catch-up bursts.
            await asyncio.sleep(self.UPDATE_INTERVAL_SECONDS)

    async def _refresh_channels(self) -> None:
        enabled = []
        for guild in self.cog.bot.guilds:
            try:
                settings = await self.cog.config.guild(guild).discord_stats()
                if settings["enabled"]:
                    enabled.append(guild)
            except Exception:
                log.warning("Could not read Discord stats settings for guild %s", guild.id)
        if not enabled:
            return
        # All guilds use the same database: refresh and fetch only once per cycle.
        summary = await self.cog.hll_database.refresh_server_summary()
        for guild in enabled:
            try:
                async with self._lock(guild.id):
                    settings = await self.cog.config.guild(guild).discord_stats()
                    if not settings["enabled"]:
                        continue
                    for key, counter, template in self.CHANNEL_FORMATS:
                        channel = guild.get_channel(settings.get(key))
                        if not isinstance(channel, discord.VoiceChannel):
                            continue
                        name = template.format(getattr(summary, counter))
                        if name == channel.name:
                            continue
                        try:
                            await channel.edit(name=name, reason="HLL server statistics")
                        except discord.NotFound:
                            log.warning("Discord stats channel %s was deleted in guild %s", channel.id, guild.id)
                        except discord.Forbidden:
                            log.warning("Cannot rename Discord stats channel %s in guild %s", channel.id, guild.id)
                        except discord.HTTPException:
                            log.warning("Discord rejected stats rename for channel %s in guild %s", channel.id, guild.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Could not update Discord stats channels for guild %s", guild.id)


class DiscordStatsCommandsMixin:
    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(name="discordstat", description="Display server totals in three voice channel names.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        toggle="Enable or disable automatic updates; omit to keep the current setting.",
        headcount_channel="Voice channel for total unique players.",
        kills_channel="Voice channel for total kills.",
        deaths_channel="Voice channel for total deaths.",
    )
    async def hllvn_discordstat(
        self, interaction: discord.Interaction, toggle: bool | None = None,
        headcount_channel: discord.VoiceChannel | None = None,
        kills_channel: discord.VoiceChannel | None = None,
        deaths_channel: discord.VoiceChannel | None = None,
    ) -> None:
        # This command explicitly requires Discord Administrator, including at runtime.
        if interaction.guild is None or not interaction.permissions.administrator:
            await interaction.response.send_message(
                "You must have Administrator permission in this server to configure Discord stats.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            settings = await self.discord_stats.configure(
                interaction.guild, toggle, headcount_channel, kills_channel, deaths_channel,
            )
            state = "enabled" if settings["enabled"] else "disabled"
            channels = ", ".join(
                f"{key.removesuffix('_id')}: "
                + (f"<#{settings[key]}>" if settings[key] else "not configured")
                for key, _, _ in self.discord_stats.CHANNEL_FORMATS
            )
            message = (
                f"Discord stats are {state}. {channels}. "
                "Enabled channels update on the ten-minute worker cycle when PostgreSQL is available."
            )
        except ValueError as exc:
            message = str(exc)
        except Exception:
            log.warning("Could not configure Discord stats for guild %s", interaction.guild.id)
            message = "Could not save Discord stats settings. Try again later."
        await interaction.followup.send(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
