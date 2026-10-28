"""Display PostgreSQL server totals in persistent Discord voice channels."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import discord
from discord import app_commands

from .hll_database import HLLDatabaseUnavailableError
from .hll_group import HLLVN_COMMAND_GROUP

log = logging.getLogger("red.BattleMetric.discord_stats")


class DiscordStatsModule:
    UPDATE_INTERVAL_SECONDS = 600
    MANUAL_REFRESH_INTERVAL_SECONDS = 30
    MAX_RENAME_DEADLINES = 256
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
        self._refresh_lock = asyncio.Lock()
        self._next_manual_refresh = 0.0

    def register_config(self) -> None:
        self.cog.config.register_guild(
            discord_stats=self.DEFAULT_SETTINGS.copy(), discord_stats_rename_after={},
        )

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
        # Serialize manual and scheduled database refreshes across all guilds.
        async with self._refresh_lock:
            summary = await self.cog.hll_database.refresh_server_summary()
        for guild in enabled:
            try:
                await self._update_guild(guild, summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Could not update Discord stats channels for guild %s", guild.id)

    async def sync_now(self, guild: discord.Guild) -> list[str]:
        async with self._lock(guild.id):
            settings = await self.cog.config.guild(guild).discord_stats()
            if not settings["enabled"]:
                raise ValueError("Enable Discord stats before requesting a sync.")
        if self._refresh_lock.locked():
            raise ValueError("A database refresh is already running. Try again shortly.")
        async with self._refresh_lock:
            remaining = self._next_manual_refresh - time.monotonic()
            if remaining > 0:
                raise ValueError(f"A manual database refresh was just attempted. Try again in {math.ceil(remaining)} seconds.")
            self._next_manual_refresh = time.monotonic() + self.MANUAL_REFRESH_INTERVAL_SECONDS
            summary = await self.cog.hll_database.refresh_server_summary()
        return await self._update_guild(guild, summary)

    async def _update_guild(self, guild: discord.Guild, summary: Any) -> list[str]:
        async with self._lock(guild.id):
            group = self.cog.config.guild(guild)
            settings = await group.discord_stats()
            if not settings["enabled"]:
                return ["Stats were disabled before channel updates could start."]
            results = []
            for key, counter, template in self.CHANNEL_FORMATS:
                channel_id = settings.get(key)
                label = key.removesuffix("_id")
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    results.append(f"{label}: channel unavailable")
                    continue
                name = template.format(getattr(summary, counter))
                if name == channel.name:
                    results.append(f"{label}: already current")
                    continue
                now = time.time()
                stored = await group.discord_stats_rename_after()
                deadlines = {
                    str(k): float(v) for k, v in stored.items()
                    if isinstance(v, (int, float)) and math.isfinite(v) and v > now
                }
                remaining = deadlines.get(str(channel_id), 0) - now
                if remaining > 0:
                    results.append(f"{label}: rename cooling down ({math.ceil(remaining)}s remaining)")
                    continue
                if len(deadlines) >= self.MAX_RENAME_DEADLINES:
                    results.append(f"{label}: too many recent renames; retry later")
                    continue
                # Persist the attempt before the HTTP request: a timeout or reload may
                # occur after Discord has applied the rename but before it replies.
                deadlines[str(channel_id)] = now + self.UPDATE_INTERVAL_SECONDS
                await group.discord_stats_rename_after.set(deadlines)
                try:
                    await channel.edit(name=name, reason="HLL server statistics")
                except discord.NotFound:
                    deadlines.pop(str(channel_id), None)
                    await group.discord_stats_rename_after.set(deadlines)
                    results.append(f"{label}: channel was deleted")
                    log.warning("Discord stats channel %s was deleted in guild %s", channel.id, guild.id)
                except discord.Forbidden:
                    deadlines.pop(str(channel_id), None)
                    await group.discord_stats_rename_after.set(deadlines)
                    results.append(f"{label}: Discord denied permission to rename")
                    log.warning("Cannot rename Discord stats channel %s in guild %s", channel.id, guild.id)
                except discord.HTTPException:
                    results.append(f"{label}: Discord request failed; retry after cooldown")
                    log.warning("Discord rejected stats rename for channel %s in guild %s", channel.id, guild.id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    results.append(f"{label}: rename could not be confirmed; retry after cooldown")
                    log.warning("Discord stats rename could not be confirmed for channel %s in guild %s", channel.id, guild.id)
                else:
                    results.append(f"{label}: updated to {name}")
                if str(channel_id) in deadlines:
                    deadlines[str(channel_id)] = time.time() + self.UPDATE_INTERVAL_SECONDS
                    await group.discord_stats_rename_after.set(deadlines)
            return results


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
        sync_now="Refresh PostgreSQL and update these channels immediately.",
    )
    async def hllvn_discordstat(
        self, interaction: discord.Interaction, toggle: bool | None = None,
        headcount_channel: discord.VoiceChannel | None = None,
        kills_channel: discord.VoiceChannel | None = None,
        deaths_channel: discord.VoiceChannel | None = None,
        sync_now: bool = False,
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
            if sync_now and toggle is False:
                raise ValueError("Use sync_now with enabled stats, not with toggle:false.")
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
            if sync_now:
                try:
                    results = await self.discord_stats.sync_now(interaction.guild)
                    message += "\nSync results:\n- " + "\n- ".join(results)
                except HLLDatabaseUnavailableError as exc:
                    message += f"\nSync failed: {exc}"
                except ValueError as exc:
                    message += f"\nSync failed: {exc}"
                except Exception:
                    log.warning("Discord stats sync failed for guild %s", interaction.guild.id)
                    message += "\nSync failed unexpectedly. Check bot logs and try again."
        except ValueError as exc:
            message = str(exc)
        except Exception:
            log.warning("Could not configure Discord stats for guild %s", interaction.guild.id)
            message = "Could not save Discord stats settings. Try again later."
        await interaction.followup.send(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
