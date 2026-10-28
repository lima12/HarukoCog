"""Private welcome popups from the shared HLL connection-log stream."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_ban import HLLBanModule
from .hll_group import HLLVN_COMMAND_GROUP

try:
    from hllrcon.admin_logs import HLLVPlayerConnectAdminLog
except Exception:  # Keep unrelated features loadable without optional RCON models.
    HLLVPlayerConnectAdminLog = None


log = logging.getLogger("red.BattleMetric.join_notice")


@dataclass(frozen=True)
class JoinNotice:
    eos_id: str
    endpoint: tuple[str, int]
    ready_at: float
    expires_at: float


class HLLJoinNoticeModule:
    JOIN_DELAY_SECONDS = 5
    DELIVERY_INTERVAL_SECONDS = 2
    MAX_QUEUE_SIZE = 200
    MAX_SEEN_EVENTS = 5000
    MAX_NOTICE_AGE_SECONDS = 600
    MAX_BACKOFF_SECONDS = 60

    def __init__(self, cog: Any):
        self.cog = cog
        self._running = False
        self._started_at = datetime.now(timezone.utc)
        self._disabled_guilds: set[int] = set()
        self._enabled_since: dict[int, datetime] = {}
        self._queues: dict[int, deque[JoinNotice]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._locks: dict[int, asyncio.Lock] = {}
        self._next_delivery_at: dict[int, float] = {}
        self._failures: dict[int, int] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(hll_join_notice_enabled=True)

    async def start(self) -> None:
        if self._running:
            return
        settings = await self.cog.config.all_guilds()
        self._disabled_guilds = {
            int(guild_id) for guild_id, data in settings.items()
            if not data.get("hll_join_notice_enabled", True)
        }
        self._started_at = datetime.now(timezone.utc)
        self._running = True
        if not self.delivery_worker.is_running():
            self.delivery_worker.start()

    def stop(self) -> None:
        self._running = False
        self.delivery_worker.cancel()
        self._disabled_guilds.clear()
        self._enabled_since.clear()
        self._queues.clear()
        self._seen.clear()
        self._locks.clear()
        self._next_delivery_at.clear()
        self._failures.clear()

    def enabled(self, guild_id: int) -> bool:
        return guild_id not in self._disabled_guilds

    def should_poll(self, guild_id: int) -> bool:
        return (
            self._running and self.enabled(guild_id)
            and HLLVPlayerConnectAdminLog is not None
            and self.cog.hll_database.should_poll(guild_id)
        )

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def configure(self, guild: discord.Guild, enabled: bool) -> None:
        async with self._lock(guild.id):
            await self.cog.config.guild(guild).hll_join_notice_enabled.set(enabled)
            if enabled:
                if not self.enabled(guild.id):
                    self._enabled_since[guild.id] = datetime.now(timezone.utc)
                self._disabled_guilds.discard(guild.id)
            else:
                self._disabled_guilds.add(guild.id)
                self._queues.pop(guild.id, None)

    async def ingest_admin_logs(self, guild: discord.Guild, entries: Any) -> None:
        if not self.should_poll(guild.id):
            return
        settings = await self.cog.kill_feed.get_settings(guild)
        endpoint = (settings.get("host"), settings.get("port"))
        # Recheck after awaiting settings: a concurrent disable discards pending work.
        if not self.should_poll(guild.id):
            return
        cutoff = self._enabled_since.get(guild.id, self._started_at)
        now = time.monotonic()
        for entry in entries:
            if not isinstance(entry, HLLVPlayerConnectAdminLog) or entry.timestamp < cutoff:
                continue
            eos_id = HLLBanModule.normalize_eos_id(str(entry.player_id))
            if eos_id is None:
                continue
            # Include the endpoint so duplicate guild configurations do not greet twice.
            fingerprint = f"{endpoint}\0{entry.timestamp.isoformat()}\0{entry.raw_message}"
            if fingerprint in self._seen:
                continue
            self._seen[fingerprint] = None
            while len(self._seen) > self.MAX_SEEN_EVENTS:
                self._seen.popitem(last=False)
            queue = self._queues.setdefault(guild.id, deque())
            if len(queue) < self.MAX_QUEUE_SIZE:
                queue.append(JoinNotice(
                    eos_id, endpoint, now + self.JOIN_DELAY_SECONDS,
                    now + self.MAX_NOTICE_AGE_SECONDS,
                ))
            else:
                log.warning("HLL join-notice queue full in guild %s; dropping notice", guild.id)

    async def _message(self, eos_id: str) -> str:
        try:
            stats = await asyncio.wait_for(
                self.cog.hll_database.get_stats_by_eos(eos_id), timeout=15,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # An outage must not masquerade as a player with zero recorded stats.
            log.warning("Join-notice statistics unavailable; sending welcome without counters")
            kills = wounded = "Unavailable"
        else:
            kills = f"{stats.kills:,}" if stats is not None else "0"
            wounded = f"{stats.deaths:,}" if stats is not None else "0"
        return (
            "Welcome to SLH.\n"
            "We tracked your performance\n"
            f"Kill: {kills} Confirmed\n"
            f"Wounded: {wounded} Times\n"
            "Want to show off? Join our Discord at discord.gg/slh4theppl\n"
            "You can also customize your own dogtag there!"
        )

    async def _send(self, guild: discord.Guild, eos_id: str) -> str:
        message = await self._message(eos_id)
        try:
            await self.cog.kill_feed.execute_rcon(
                guild, "JoinNotice MessagePlayer request",
                lambda client: client.message_player(eos_id, message),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            failures = min(self._failures.get(guild.id, 0) + 1, 6)
            self._failures[guild.id] = failures
            self._next_delivery_at[guild.id] = time.monotonic() + min(
                self.DELIVERY_INTERVAL_SECONDS * 2 ** failures, self.MAX_BACKOFF_SECONDS,
            )
            raise
        else:
            self._failures.pop(guild.id, None)
            self._next_delivery_at[guild.id] = time.monotonic() + self.DELIVERY_INTERVAL_SECONDS
        return message

    async def test(self, guild: discord.Guild, eos_id: str) -> str:
        normalized = HLLBanModule.normalize_eos_id(eos_id)
        if normalized is None:
            raise ValueError("Provide a valid 17-digit or 32-character EOS ID.")
        async with self._lock(guild.id):
            delay = self._next_delivery_at.get(guild.id, 0) - time.monotonic()
            if delay > 0:
                raise ValueError(f"Join notices are cooling down. Try again in {int(delay) + 1} seconds.")
            return await self._send(guild, normalized)

    @tasks.loop(seconds=1)
    async def delivery_worker(self) -> None:
        await self._deliver_notices()

    @delivery_worker.before_loop
    async def before_delivery_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _deliver_notices(self) -> None:
        for guild in self.cog.bot.guilds:
            try:
                async with self._lock(guild.id):
                    if not self.should_poll(guild.id):
                        self._queues.pop(guild.id, None)
                        continue
                    queue = self._queues.get(guild.id)
                    now = time.monotonic()
                    while queue and queue[0].expires_at <= now:
                        queue.popleft()
                    if (
                        not queue or queue[0].ready_at > now
                        or self._next_delivery_at.get(guild.id, 0) > now
                    ):
                        continue
                    # Claim before sending. A lost RCON acknowledgement must not resend a popup.
                    notice = queue.popleft()
                    settings = await self.cog.kill_feed.get_settings(guild)
                    if notice.endpoint != (settings.get("host"), settings.get("port")):
                        continue
                    await self._send(guild, notice.eos_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Could not confirm HLL join-notice delivery in guild %s", guild.id)


class HLLJoinNoticeCommandsMixin:
    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(name="joinnotice", description="Inspect, enable, disable, or test private join notices.")
    @app_commands.describe(
        test="Send the welcome popup to this online EOS ID without changing the toggle.",
        toggle="True enables automatic join notices; false disables them.",
    )
    @app_commands.guild_only()
    async def hllvn_joinnotice(
        self, interaction: discord.Interaction, test: str | None = None,
        toggle: bool | None = None,
    ) -> None:
        if not await self.is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use HLL: Vietnam administration commands.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return
        if test is not None and toggle is not None:
            await interaction.followup.send("Use either test or toggle in one command.", ephemeral=True)
            return
        try:
            if test is not None:
                message = await self.join_notice.test(guild, test)
                result = f"Join notice sent to `{HLLBanModule.normalize_eos_id(test)}`:\n{message}"
            else:
                if toggle is not None:
                    await self.join_notice.configure(guild, toggle)
                state = "enabled" if self.join_notice.enabled(guild.id) else "disabled"
                result = (
                    f"Automatic join notices are {state}. Delivery requires the configured "
                    "RCON endpoint/password and database credentials. "
                    "Use `test:EOS_ID` with an online player to try the popup."
                )
        except ValueError as exc:
            result = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("HLL join-notice command failed in guild %s", guild.id)
            result = (
                "Could not confirm the join notice operation. Check the RCON/database setup "
                "and whether the player is online. Check the game before retrying a test."
            )
        await interaction.followup.send(
            result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
