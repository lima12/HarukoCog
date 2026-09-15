"""In-game HLL admin requests delivered to a configured Discord role."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_database import HLLDatabaseError
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError

log = logging.getLogger("red.BattleMetric.admin_ping")

try:
    from hllrcon.admin_logs import HLLVPlayerSendMessageAdminLog
except Exception as exc:  # noqa: BLE001 - keep unrelated cog features loadable
    HLLVPlayerSendMessageAdminLog = None
    HLLRCON_MODEL_IMPORT_ERROR: Exception | None = exc
else:
    HLLRCON_MODEL_IMPORT_ERROR = None


@dataclass(frozen=True)
class HLLAdminAlert:
    player_name: str
    eos_id: str
    text: str
    occurred_at: datetime


class HLLAdminPingModule:
    """Queue in-game admin requests and deliver controlled Discord role pings."""

    COMMAND = "!admin"
    DELIVERY_INTERVAL_SECONDS = 3
    MAX_QUEUE_SIZE = 100
    MAX_SEEN_EVENTS = 2000
    MAX_RETRY_SECONDS = 60
    _EMPTY_SETTINGS: ClassVar[dict[str, bool | int | None]] = {
        "enabled": False,
        "role_id": None,
        "channel_id": None,
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._enabled_guilds: set[int] = set()
        self._queues: dict[int, deque[HLLAdminAlert]] = {}
        self._seen: dict[int, OrderedDict[str, None]] = {}
        self._failure_counts: dict[int, int] = {}
        self._next_delivery_at: dict[int, float] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(hll_admin_ping=dict(self._EMPTY_SETTINGS))

    async def start(self) -> None:
        all_guilds = await self.cog.config.all_guilds()
        self._enabled_guilds = {
            int(guild_id)
            for guild_id, data in all_guilds.items()
            if isinstance(data, Mapping)
            and isinstance(data.get("hll_admin_ping"), Mapping)
            and bool(data["hll_admin_ping"].get("enabled", False))
        }
        if not self.delivery_worker.is_running():
            self.delivery_worker.start()

    def stop(self) -> None:
        self.delivery_worker.cancel()
        self._enabled_guilds.clear()
        self._queues.clear()
        self._seen.clear()
        self._failure_counts.clear()
        self._next_delivery_at.clear()

    def should_poll(self, guild_id: int) -> bool:
        return guild_id in self._enabled_guilds

    async def get_settings(
        self,
        guild: discord.Guild,
    ) -> dict[str, bool | int | None]:
        stored = await self.cog.config.guild(guild).hll_admin_ping()
        if not isinstance(stored, Mapping):
            return dict(self._EMPTY_SETTINGS)
        return {
            "enabled": bool(stored.get("enabled", False)),
            "role_id": self._optional_id(stored.get("role_id")),
            "channel_id": self._optional_id(stored.get("channel_id")),
        }

    async def configure(
        self,
        guild: discord.Guild,
        *,
        enabled: bool,
        role_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        settings = await self.get_settings(guild)
        settings["enabled"] = enabled
        if enabled:
            settings["role_id"] = role_id
            settings["channel_id"] = channel_id
        await self.cog.config.guild(guild).hll_admin_ping.set(settings)

        if enabled:
            self._enabled_guilds.add(guild.id)
            self._queues.setdefault(guild.id, deque())
            self._seen.setdefault(guild.id, OrderedDict())
        else:
            self._enabled_guilds.discard(guild.id)
            self._reset_guild(guild.id)

    async def ingest_admin_logs(
        self,
        guild: discord.Guild,
        entries: Sequence[Any],
    ) -> None:
        if not self.should_poll(guild.id) or HLLVPlayerSendMessageAdminLog is None:
            return

        for entry in entries:
            if not isinstance(entry, HLLVPlayerSendMessageAdminLog):
                continue
            message = str(entry.message).strip()
            if not self._is_admin_command(message):
                continue

            fingerprint = f"{entry.timestamp.isoformat()}\0{entry.raw_message}"
            if not self._mark_seen(guild.id, fingerprint):
                continue
            self._enqueue(
                guild.id,
                HLLAdminAlert(
                    player_name=str(entry.player_name).strip() or "Unknown player",
                    eos_id=str(entry.player_id).strip(),
                    text=message[len(self.COMMAND) :].strip(),
                    occurred_at=entry.timestamp,
                ),
            )

    @classmethod
    def _is_admin_command(cls, message: str) -> bool:
        if message[: len(cls.COMMAND)].casefold() != cls.COMMAND:
            return False
        return len(message) == len(cls.COMMAND) or message[len(cls.COMMAND)].isspace()

    def _mark_seen(self, guild_id: int, fingerprint: str) -> bool:
        seen = self._seen.setdefault(guild_id, OrderedDict())
        if fingerprint in seen:
            return False
        seen[fingerprint] = None
        while len(seen) > self.MAX_SEEN_EVENTS:
            seen.popitem(last=False)
        return True

    def _enqueue(self, guild_id: int, alert: HLLAdminAlert) -> None:
        queue = self._queues.setdefault(guild_id, deque())
        if len(queue) >= self.MAX_QUEUE_SIZE:
            queue.popleft()
            log.warning(
                "Dropped the oldest HLL admin alert because guild %s reached its queue limit",
                guild_id,
            )
        queue.append(alert)

    @tasks.loop(seconds=DELIVERY_INTERVAL_SECONDS)
    async def delivery_worker(self) -> None:
        try:
            await self._deliver_all_guilds()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error in the HLL admin-alert delivery worker")

    @delivery_worker.before_loop
    async def before_delivery_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _deliver_all_guilds(self) -> None:
        now = time.monotonic()
        for guild in self.cog.bot.guilds:
            queue = self._queues.get(guild.id)
            if not queue or not self.should_poll(guild.id):
                continue
            if now < self._next_delivery_at.get(guild.id, 0):
                continue
            await self._deliver_one(guild, queue)

    async def _deliver_one(
        self,
        guild: discord.Guild,
        queue: deque[HLLAdminAlert],
    ) -> None:
        settings = await self.get_settings(guild)
        role_id = settings.get("role_id")
        channel_id = settings.get("channel_id")
        role = guild.get_role(role_id) if isinstance(role_id, int) else None
        channel = (
            guild.get_channel_or_thread(channel_id)
            if isinstance(channel_id, int)
            else None
        )
        if role is None or channel is None or not hasattr(channel, "send"):
            log.warning(
                "Disabling HLL admin alerts for guild %s because its role or channel is unavailable",
                guild.id,
            )
            await self.configure(guild, enabled=False)
            return

        alert = queue[0]
        discord_value = await self._linked_discord_value(alert.eos_id)
        embed = self._build_embed(alert, discord_value)
        try:
            await channel.send(
                content=role.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=[role],
                    users=False,
                    replied_user=False,
                ),
            )
        except asyncio.CancelledError:
            raise
        except discord.Forbidden:
            log.warning(
                "Disabling HLL admin alerts for guild %s because Discord denied delivery",
                guild.id,
            )
            await self.configure(guild, enabled=False)
        except discord.HTTPException as exc:
            failures = self._failure_counts.get(guild.id, 0) + 1
            self._failure_counts[guild.id] = failures
            delay = min(self.MAX_RETRY_SECONDS, 3 * (2 ** min(failures - 1, 5)))
            self._next_delivery_at[guild.id] = time.monotonic() + delay
            log.warning(
                "Could not deliver HLL admin alert for guild %s; retrying in %s seconds: %s",
                guild.id,
                delay,
                exc,
            )
        else:
            queue.popleft()
            self._failure_counts.pop(guild.id, None)
            self._next_delivery_at.pop(guild.id, None)

    async def _linked_discord_value(self, eos_id: str) -> str:
        try:
            stats = await self.cog.hll_database.get_stats_by_eos(eos_id)
        except HLLDatabaseError as exc:
            log.warning("Could not resolve Discord link for HLL admin alert: %s", exc)
            return "Link lookup unavailable"
        except Exception:
            log.exception("Unexpected Discord-link lookup failure for HLL admin alert")
            return "Link lookup unavailable"
        if stats is None or stats.discord_id is None:
            return "Account not linked"
        return f"<@{stats.discord_id}>"

    @classmethod
    def _build_embed(
        cls,
        alert: HLLAdminAlert,
        discord_value: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="HLLVN SOS",
            color=discord.Color.red(),
            timestamp=alert.occurred_at,
        )
        embed.add_field(
            name="Player report",
            value=cls._safe_embed_text(alert.player_name, 1024),
            inline=False,
        )
        embed.add_field(name="EOS_Id", value=f"`{alert.eos_id}`", inline=False)
        embed.add_field(name="Discord", value=discord_value, inline=False)
        embed.add_field(
            name="Text",
            value=cls._safe_embed_text(
                alert.text or "No additional details provided.",
                1024,
            ),
            inline=False,
        )
        return embed

    @staticmethod
    def _safe_embed_text(value: str, limit: int) -> str:
        value = discord.utils.escape_mentions(value.replace("\x00", "").strip())
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _reset_guild(self, guild_id: int) -> None:
        self._queues.pop(guild_id, None)
        self._seen.pop(guild_id, None)
        self._failure_counts.pop(guild_id, None)
        self._next_delivery_at.pop(guild_id, None)

    @staticmethod
    def _optional_id(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return parsed if parsed is not None and parsed > 0 else None


class HLLAdminPingCommandsMixin:
    """Restricted Discord configuration for in-game admin alerts."""

    @HLLVN_COMMAND_GROUP.command(
        name="adminping",
        description="Configure the Discord role pinged by the in-game !admin command.",
    )
    @app_commands.describe(
        role="Role to ping for in-game admin requests.",
        toggle="Enable or disable in-game admin requests.",
    )
    @app_commands.choices(
        toggle=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    @app_commands.guild_only()
    async def hllvn_adminping(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        toggle: app_commands.Choice[str],
    ) -> None:
        if not await self.is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use HLL: Vietnam administration commands.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        enabled = toggle.value == "enable"
        if enabled:
            if role.is_default():
                await interaction.followup.send(
                    "Choose a specific staff role instead of `@everyone`."
                )
                return
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send(
                    "Run this command in the text channel that should receive alerts."
                )
                return

            bot_member = guild.me
            if bot_member is not None:
                permissions = channel.permissions_for(bot_member)
                can_send = (
                    permissions.send_messages_in_threads
                    if isinstance(channel, discord.Thread)
                    else permissions.send_messages
                )
                if not can_send or not permissions.embed_links:
                    await interaction.followup.send(
                        "I need permission to send messages and embeds in this channel."
                    )
                    return
            try:
                await self.kill_feed.test_connection(guild)
            except (ValueError, KillFeedConnectionTestError) as exc:
                await interaction.followup.send(str(exc))
                return
            except RuntimeError:
                log.exception("HLL admin alerts are unavailable")
                await interaction.followup.send(
                    "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                    "update the cog dependencies and restart Red."
                )
                return

        await self.admin_ping.configure(
            guild,
            enabled=enabled,
            role_id=role.id if enabled else None,
            channel_id=channel.id if enabled else None,
        )
        embed = discord.Embed(
            title="HLL VN Admin Ping",
            description=(
                f"In-game `!admin` requests will ping {role.mention} in {channel.mention}."
                if enabled
                else "The in-game `!admin` command is disabled."
            ),
            color=discord.Color.green() if enabled else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if enabled:
            embed.set_footer(text="Alerts are queued and delivered one every 3 seconds.")
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
