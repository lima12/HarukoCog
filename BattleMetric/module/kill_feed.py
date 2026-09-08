"""Live Hell Let Loose: Vietnam kill-feed module.

The module reads the game server's RCON admin log directly. Polling and Discord
delivery are separate so bursts of kills become one message every three seconds
instead of one Discord request per event.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import tasks
from redbot.core import commands

from ..authorization import requires_authorized_user

log = logging.getLogger("red.BattleMetric.kill_feed")

try:
    from hllrcon import HLLVRcon
    from hllrcon.admin_logs import HLLVPlayerKillAdminLog, HLLVPlayerTeamKillAdminLog
    from hllrcon.exceptions import (
        RconAuthError,
        RconCommandError,
        RconConnectionClosedError,
        RconConnectionError,
        RconConnectionRefusedError,
        RconMessageError,
    )
    from hllrcon.protocol import RconProtocol
except Exception as exc:  # noqa: BLE001 - keep unrelated BattleMetric modules loadable
    HLLVRcon = None
    HLLVPlayerKillAdminLog = None
    HLLVPlayerTeamKillAdminLog = None
    RconAuthError = None
    RconCommandError = None
    RconConnectionClosedError = None
    RconConnectionError = None
    RconConnectionRefusedError = None
    RconMessageError = None
    RconProtocol = None
    HLLRCON_IMPORT_ERROR: Exception | None = exc
else:
    HLLRCON_IMPORT_ERROR = None


@dataclass(frozen=True)
class KillFeedEvent:
    """A formatted event waiting to be delivered to Discord."""

    line: str
    team_kill: bool


class KillFeedConnectionTestError(RuntimeError):
    """A sanitized RCON test failure that is safe to show in Discord."""


def _install_uvloop_transport_compatibility() -> None:
    """Allow hllrcon 2.0.0.4 to use the transport supplied by Red's uvloop."""
    if RconProtocol is None:
        return

    current_handler = RconProtocol.connection_made
    if getattr(current_handler, "__battlemetric_uvloop_compatible__", False):
        return

    def connection_made(protocol: Any, transport: asyncio.BaseTransport) -> None:
        try:
            current_handler(protocol, transport)
        except TypeError as exc:
            is_uvloop_transport = type(transport).__module__.startswith("uvloop.")
            if str(exc) != "Transport must be an instance of asyncio.Transport" or not is_uvloop_transport:
                raise

            # uvloop's TCP transport implements the asyncio transport contract but
            # does not inherit asyncio.Transport on supported Red/Python versions.
            protocol.logger.info("Accepted uvloop TCP transport for HLL RCON")
            protocol._transport = transport

    connection_made.__battlemetric_uvloop_compatible__ = True
    RconProtocol.connection_made = connection_made


class KillFeedModule:
    """Poll HLL: Vietnam RCON and batch kill events into Discord messages."""

    RCON_SERVICE_NAME = "hllrcon"
    RCON_PASSWORD_NAME = "password"

    POLL_INTERVAL_SECONDS = 3
    DELIVERY_INTERVAL_SECONDS = 3
    MAX_LOOKBACK_SECONDS = 300
    MAX_QUEUE_SIZE = 500
    MAX_SEEN_EVENTS = 2000
    MAX_BATCH_EVENTS = 50
    MAX_BATCH_CHARACTERS = 3800
    RCON_TIMEOUT_SECONDS = 15

    _EMPTY_SETTINGS: ClassVar[dict[str, bool | int | str | None]] = {
        "enabled": False,
        "channel_id": None,
        "host": None,
        "port": None,
    }

    def __init__(self, cog: commands.Cog):
        self.cog = cog
        self._password: str | None = None
        self._clients: dict[int, tuple[tuple[str, int, str], Any]] = {}
        self._client_locks: dict[int, asyncio.Lock] = {}
        self._queues: dict[int, deque[KillFeedEvent]] = {}
        self._seen: dict[int, OrderedDict[str, None]] = {}
        self._last_poll_at: dict[int, datetime] = {}
        self._next_poll_at: dict[int, float] = {}
        self._failure_counts: dict[int, int] = {}
        self._dropped_counts: dict[int, int] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(kill_feed=dict(self._EMPTY_SETTINGS))

    async def start(self) -> None:
        await self.refresh_password()
        if not self.is_available():
            log.error(
                "HLL: Vietnam kill feed is unavailable because hllrcon could not be imported: %s",
                HLLRCON_IMPORT_ERROR,
            )
            return
        _install_uvloop_transport_compatibility()
        if not self.log_poller.is_running():
            self.log_poller.start()
        if not self.queue_worker.is_running():
            self.queue_worker.start()

    def stop(self) -> None:
        self.log_poller.cancel()
        self.queue_worker.cancel()
        self._disconnect_all()

    async def refresh_password(self) -> None:
        tokens = await self.cog.bot.get_shared_api_tokens(self.RCON_SERVICE_NAME)
        self.set_password(tokens.get(self.RCON_PASSWORD_NAME))

    def set_password(self, password: str | None) -> None:
        normalized = password.strip() if isinstance(password, str) and password.strip() else None
        if normalized == self._password:
            return
        self._password = normalized
        self._disconnect_all()

    def has_password(self) -> bool:
        return bool(self._password)

    @staticmethod
    def is_available() -> bool:
        return HLLRCON_IMPORT_ERROR is None

    @staticmethod
    def dependency_error() -> str | None:
        return str(HLLRCON_IMPORT_ERROR) if HLLRCON_IMPORT_ERROR is not None else None

    async def get_settings(
        self,
        guild: discord.Guild,
    ) -> dict[str, bool | int | str | None]:
        settings = await self.cog.config.guild(guild).kill_feed()
        if not isinstance(settings, Mapping):
            return dict(self._EMPTY_SETTINGS)

        channel_id = settings.get("channel_id")
        port = settings.get("port")
        host = settings.get("host")
        return {
            "enabled": bool(settings.get("enabled", False)),
            "channel_id": self._optional_int(channel_id),
            "host": str(host).strip() if host else None,
            "port": self._optional_int(port),
        }

    async def set_endpoint(self, guild: discord.Guild, host: str, port: int) -> None:
        settings = await self.get_settings(guild)
        settings.update({"enabled": False, "host": host, "port": port})
        await self.cog.config.guild(guild).kill_feed.set(settings)
        self.reset_guild(guild.id)

    async def enable(self, guild: discord.Guild, channel_id: int) -> None:
        settings = await self.get_settings(guild)
        settings.update({"enabled": True, "channel_id": channel_id})
        await self.cog.config.guild(guild).kill_feed.set(settings)
        self._queues[guild.id] = deque()
        self._seen[guild.id] = OrderedDict()
        self._last_poll_at[guild.id] = discord.utils.utcnow()
        self._next_poll_at.pop(guild.id, None)
        self._failure_counts.pop(guild.id, None)
        self._dropped_counts.pop(guild.id, None)

    async def disable(self, guild: discord.Guild) -> None:
        settings = await self.get_settings(guild)
        settings["enabled"] = False
        await self.cog.config.guild(guild).kill_feed.set(settings)
        self.reset_guild(guild.id)

    def reset_guild(self, guild_id: int) -> None:
        client_state = self._clients.pop(guild_id, None)
        if client_state is not None:
            client_state[1].disconnect()
        self._client_locks.pop(guild_id, None)
        self._queues.pop(guild_id, None)
        self._seen.pop(guild_id, None)
        self._last_poll_at.pop(guild_id, None)
        self._next_poll_at.pop(guild_id, None)
        self._failure_counts.pop(guild_id, None)
        self._dropped_counts.pop(guild_id, None)

    async def test_connection(self, guild: discord.Guild) -> None:
        if not self.is_available():
            raise RuntimeError("The hllrcon dependency is unavailable.")
        _install_uvloop_transport_compatibility()
        settings = await self.get_settings(guild)
        host = settings.get("host")
        port = settings.get("port")
        if not host or port is None:
            raise ValueError("Configure the RCON host and port first.")
        if not self._password:
            raise ValueError("The HLL RCON password is not configured in Red's API-token vault.")

        client, lock = self._get_client(guild.id, host, port, self._password)
        stage = "RCON V2 handshake"
        try:
            async with lock:
                await asyncio.wait_for(
                    client.connect(),
                    timeout=self.RCON_TIMEOUT_SECONDS,
                )
                stage = "GetAdminLog request"
                await asyncio.wait_for(
                    client.get_admin_log(1),
                    timeout=self.RCON_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            client.disconnect()
            current = self._clients.get(guild.id)
            if current is not None and current[1] is client:
                self._clients.pop(guild.id, None)
                self._client_locks.pop(guild.id, None)
            log.warning(
                "HLL: Vietnam RCON test failed for guild %s during %s (%s): %s",
                guild.id,
                stage,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise KillFeedConnectionTestError(
                self._connection_failure_message(exc, stage)
            ) from exc

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def log_poller(self) -> None:
        try:
            await self._poll_all_guilds()
        except Exception:
            log.exception("Unhandled error in the HLL: Vietnam kill-feed poller")

    @log_poller.before_loop
    async def before_log_poller(self) -> None:
        await self.cog.bot.wait_until_ready()

    @tasks.loop(seconds=DELIVERY_INTERVAL_SECONDS)
    async def queue_worker(self) -> None:
        try:
            await self._deliver_all_guilds()
        except Exception:
            log.exception("Unhandled error in the HLL: Vietnam kill-feed queue worker")

    @queue_worker.before_loop
    async def before_queue_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _poll_all_guilds(self) -> None:
        if not self.is_available() or not self._password:
            return

        coroutines = []
        now_monotonic = time.monotonic()
        for guild in self.cog.bot.guilds:
            settings = await self.get_settings(guild)
            if not settings.get("enabled"):
                continue
            if now_monotonic < self._next_poll_at.get(guild.id, 0):
                continue
            host = settings.get("host")
            port = settings.get("port")
            if isinstance(host, str) and isinstance(port, int):
                coroutines.append(self._poll_guild(guild, host, port))

        if coroutines:
            await asyncio.gather(*coroutines)

    async def _poll_guild(self, guild: discord.Guild, host: str, port: int) -> None:
        if not self._password:
            return

        now = discord.utils.utcnow()
        previous_poll = self._last_poll_at.get(guild.id, now - timedelta(seconds=1))
        elapsed = max(0.0, (now - previous_poll).total_seconds())
        lookback = min(
            self.MAX_LOOKBACK_SECONDS,
            max(6, math.ceil(elapsed) + self.POLL_INTERVAL_SECONDS),
        )

        try:
            client, lock = self._get_client(guild.id, host, port, self._password)
            async with lock:
                response = await asyncio.wait_for(
                    client.get_admin_log(lookback),
                    timeout=self.RCON_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one RCON endpoint from other guilds
            self._record_poll_failure(guild.id, exc)
            return

        self._last_poll_at[guild.id] = now
        self._next_poll_at.pop(guild.id, None)
        if self._failure_counts.pop(guild.id, 0):
            log.info("HLL: Vietnam RCON polling recovered for guild %s", guild.id)

        earliest = previous_poll - timedelta(seconds=self.POLL_INTERVAL_SECONDS)
        for entry in response.entries:
            if entry.timestamp < earliest:
                continue
            if not isinstance(entry, (HLLVPlayerKillAdminLog, HLLVPlayerTeamKillAdminLog)):
                continue
            fingerprint = f"{entry.timestamp.isoformat()}\0{entry.raw_message}"
            if not self._mark_seen(guild.id, fingerprint):
                continue
            self._enqueue(guild.id, self._format_event(entry))

    def _record_poll_failure(self, guild_id: int, exc: Exception) -> None:
        failure_count = self._failure_counts.get(guild_id, 0) + 1
        self._failure_counts[guild_id] = failure_count
        retry_delay = min(60, self.POLL_INTERVAL_SECONDS * (2 ** min(failure_count - 1, 5)))
        self._next_poll_at[guild_id] = time.monotonic() + retry_delay
        log.warning(
            "Could not poll HLL: Vietnam RCON for guild %s; retrying in %s seconds: %s",
            guild_id,
            retry_delay,
            exc,
        )

    async def _deliver_all_guilds(self) -> None:
        for guild in self.cog.bot.guilds:
            queue = self._queues.get(guild.id)
            if not queue:
                continue

            settings = await self.get_settings(guild)
            if not settings.get("enabled"):
                self.reset_guild(guild.id)
                continue

            channel_id = settings.get("channel_id")
            channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
            if not isinstance(channel, discord.TextChannel):
                log.warning("Disabling kill feed for guild %s because its channel is unavailable", guild.id)
                await self.disable(guild)
                continue

            batch = self._take_batch(guild.id)
            if not batch:
                continue
            dropped = self._dropped_counts.get(guild.id, 0)
            embed = self._build_batch_embed(batch, dropped)

            try:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                current_dropped = self._dropped_counts.get(guild.id, 0)
                remaining_dropped = max(0, current_dropped - dropped)
                if remaining_dropped:
                    self._dropped_counts[guild.id] = remaining_dropped
                else:
                    self._dropped_counts.pop(guild.id, None)
            except discord.Forbidden:
                log.warning("Disabling kill feed for guild %s because messages cannot be sent", guild.id)
                await self.disable(guild)
            except discord.HTTPException as exc:
                self._restore_batch(guild.id, batch)
                log.warning("Could not deliver kill-feed batch for guild %s: %s", guild.id, exc)

    def _get_client(
        self,
        guild_id: int,
        host: str,
        port: int,
        password: str,
    ) -> tuple[Any, asyncio.Lock]:
        if HLLVRcon is None:
            raise RuntimeError("The hllrcon dependency is unavailable.")
        signature = (host, port, password)
        state = self._clients.get(guild_id)
        if state is None or state[0] != signature:
            if state is not None:
                state[1].disconnect()
            self._clients[guild_id] = (
                signature,
                HLLVRcon(host=host, port=port, password=password, logger=log),
            )
            self._client_locks[guild_id] = asyncio.Lock()
        return self._clients[guild_id][1], self._client_locks[guild_id]

    @staticmethod
    def _connection_failure_message(exc: Exception, stage: str) -> str:
        if (
            (RconAuthError is not None and isinstance(exc, RconAuthError))
            or (
                RconCommandError is not None
                and isinstance(exc, RconCommandError)
                and getattr(exc, "status_code", None) == 401
            )
        ):
            return "The HLL server rejected the RCON password. Update the vault value and try again."

        if RconConnectionRefusedError is not None and isinstance(exc, RconConnectionRefusedError):
            return (
                "The RCON endpoint refused the TCP connection. Verify the RCON port and the "
                "game host's firewall or IP allowlist."
            )

        if RconConnectionClosedError is not None and isinstance(exc, RconConnectionClosedError):
            if stage == "RCON V2 handshake":
                return (
                    "The TCP port accepted the connection, but the game server closed it during "
                    "the RCON V2 handshake. Verify RCON is enabled, restart the game server after "
                    "changing its RCON settings, and temporarily disconnect BattleMetrics or "
                    "other RCON clients to test for a concurrent-connection limit."
                )
            return (
                "RCON authenticated, but the server closed the connection before it answered "
                "GetAdminLog. Temporarily disconnect other RCON clients and try again."
            )

        if isinstance(exc, TimeoutError):
            return f"The {stage} timed out after 15 seconds. Check the RCON firewall and server status."

        if RconMessageError is not None and isinstance(exc, RconMessageError):
            return (
                f"The {stage} returned data that is not valid HLL: Vietnam RCON V2. "
                "Verify that this is the game server's RCON port, not its game or query port."
            )

        if RconCommandError is not None and isinstance(exc, RconCommandError):
            return (
                f"The HLL server rejected the {stage} with RCON status "
                f"{getattr(exc, 'status_code', 'unknown')}."
            )

        if (
            (RconConnectionError is not None and isinstance(exc, RconConnectionError))
            or isinstance(exc, OSError)
        ):
            return (
                f"The {stage} could not connect to the configured endpoint. Check the host, "
                "RCON port, firewall, and IP allowlist."
            )

        return f"The {stage} failed. The bot owner should check the Red service log for details."

    def _disconnect_all(self) -> None:
        for _, client in self._clients.values():
            client.disconnect()
        self._clients.clear()
        self._client_locks.clear()

    def queue_size(self, guild_id: int) -> int:
        return len(self._queues.get(guild_id, ()))

    def _mark_seen(self, guild_id: int, fingerprint: str) -> bool:
        seen = self._seen.setdefault(guild_id, OrderedDict())
        if fingerprint in seen:
            return False
        seen[fingerprint] = None
        while len(seen) > self.MAX_SEEN_EVENTS:
            seen.popitem(last=False)
        return True

    def _enqueue(self, guild_id: int, event: KillFeedEvent) -> None:
        queue = self._queues.setdefault(guild_id, deque())
        if len(queue) >= self.MAX_QUEUE_SIZE:
            queue.popleft()
            self._dropped_counts[guild_id] = self._dropped_counts.get(guild_id, 0) + 1
        queue.append(event)

    def _take_batch(self, guild_id: int) -> list[KillFeedEvent]:
        queue = self._queues.setdefault(guild_id, deque())
        batch: list[KillFeedEvent] = []
        characters = 0
        while queue and len(batch) < self.MAX_BATCH_EVENTS:
            event = queue[0]
            added = len(event.line) + (1 if batch else 0)
            if batch and characters + added > self.MAX_BATCH_CHARACTERS:
                break
            batch.append(queue.popleft())
            characters += added
        return batch

    def _restore_batch(self, guild_id: int, batch: list[KillFeedEvent]) -> None:
        queue = self._queues.setdefault(guild_id, deque())
        for event in reversed(batch):
            queue.appendleft(event)
        while len(queue) > self.MAX_QUEUE_SIZE:
            queue.popleft()
            self._dropped_counts[guild_id] = self._dropped_counts.get(guild_id, 0) + 1

    def _build_batch_embed(self, batch: list[KillFeedEvent], dropped: int) -> discord.Embed:
        lines = [event.line for event in batch]
        if dropped:
            lines.insert(0, f"*{dropped} older events were dropped while the queue was full.*")
        return discord.Embed(
            title="Live Kill Feed",
            description="\n".join(lines)[:4096],
            color=discord.Color.orange() if any(event.team_kill for event in batch) else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

    @classmethod
    def _format_event(
        cls,
        entry: Any,
    ) -> KillFeedEvent:
        team_kill = HLLVPlayerTeamKillAdminLog is not None and isinstance(
            entry,
            HLLVPlayerTeamKillAdminLog,
        )
        action = "team-killed" if team_kill else "killed"
        attacker = cls._safe_text(entry.instigator_name, 80)
        victim = cls._safe_text(entry.victim_name, 80)
        attacker_team = cls._safe_text(entry.instigator_team_name, 16)
        victim_team = cls._safe_text(entry.victim_team_name, 16)
        weapon = str(entry.weapon_id).replace("`", "'")[:100]
        timestamp = int(entry.timestamp.timestamp())
        return KillFeedEvent(
            line=(
                f"<t:{timestamp}:T> **{attacker}** ({attacker_team}) {action} "
                f"**{victim}** ({victim_team}) with `{weapon}`"
            ),
            team_kill=team_kill,
        )

    @staticmethod
    def _safe_text(value: object, limit: int) -> str:
        return discord.utils.escape_markdown(str(value), as_needed=False)[:limit]

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class KillFeedCommandsMixin:
    """Commands for configuring the HLL: Vietnam kill-feed module."""

    @commands.hybrid_group(name="killfeed", invoke_without_command=True)
    @commands.guild_only()
    @requires_authorized_user()
    async def killfeed(self, ctx: commands.Context) -> None:
        """Manage this guild's HLL: Vietnam RCON kill feed."""
        await ctx.send_help()

    @killfeed.command(name="configure", aliases=["setrcon"])
    @app_commands.describe(
        host="Hostname or IP address of the HLL: Vietnam RCON server.",
        port="RCON port, which may differ from the public game port.",
    )
    @commands.guild_only()
    async def killfeed_configure(self, ctx: commands.Context, host: str, port: int) -> None:
        """Configure the HLL: Vietnam RCON endpoint without storing its password."""
        if ctx.guild is None:
            return

        host = host.strip()
        if not host or not all(char.isalnum() or char in ".:-_" for char in host):
            await ctx.send("Provide only an RCON hostname or IP address, without a URL scheme or path.")
            return
        if not 1 <= port <= 65535:
            await ctx.send("The RCON port must be between 1 and 65535.")
            return

        await self.kill_feed.set_endpoint(ctx.guild, host, port)
        await ctx.send(
            f"HLL: Vietnam RCON endpoint set to `{host}:{port}`. "
            "The kill feed is disabled until `killfeed setup` completes a connection test."
        )

    @killfeed.command(name="setup")
    @app_commands.describe(channel="Channel that should receive pooled kill-feed messages.")
    @commands.guild_only()
    async def killfeed_setup(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Test RCON and enable pooled kill-feed messages in a channel."""
        if ctx.guild is None:
            return

        settings = await self.kill_feed.get_settings(ctx.guild)
        if not self.kill_feed.is_available():
            await ctx.send(
                "The `hllrcon` dependency could not be loaded. Ask the bot owner to "
                "update the cog dependencies and restart Red."
            )
            return
        await self.kill_feed.refresh_password()
        if not settings.get("host") or not settings.get("port"):
            await ctx.send("Configure the RCON endpoint first with `killfeed configure <host> <port>`.")
            return
        if not self.kill_feed.has_password():
            await ctx.send(
                "Ask the bot owner to set the RCON password with Red's shared "
                f"API-token command: `{ctx.clean_prefix}set api hllrcon "
                "password,YOUR_RCON_PASSWORD`."
            )
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("The bot member is unavailable in this server.")
            return
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            await ctx.send("I need Send Messages and Embed Links in that channel.")
            return

        try:
            await self.kill_feed.test_connection(ctx.guild)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        except KillFeedConnectionTestError as exc:
            await ctx.send(str(exc))
            return
        except Exception:
            log.exception("Unexpected HLL: Vietnam RCON test failure for guild %s", ctx.guild.id)
            await ctx.send(
                "The RCON connection test failed unexpectedly. "
                "Ask the bot owner to check the Red service log."
            )
            return

        try:
            await channel.send(
                embed=discord.Embed(
                    title="Live Kill Feed",
                    description="Connected. Waiting for HLL: Vietnam kill events.",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow(),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await ctx.send("I cannot send the kill-feed embed in that channel.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Could not create the kill-feed message: {exc}")
            return

        await self.kill_feed.enable(ctx.guild, channel.id)
        await ctx.send(f"HLL: Vietnam kill feed enabled in {channel.mention}.")

    @killfeed.command(name="stop", aliases=["disable"])
    @commands.guild_only()
    async def killfeed_stop(self, ctx: commands.Context) -> None:
        """Stop this guild's kill feed and discard queued events."""
        if ctx.guild is None:
            return
        await self.kill_feed.disable(ctx.guild)
        await ctx.send("HLL: Vietnam kill feed disabled and its pending queue cleared.")

    @killfeed.command(name="status")
    @commands.guild_only()
    async def killfeed_status(self, ctx: commands.Context) -> None:
        """Show this guild's kill-feed configuration and queue state."""
        if ctx.guild is None:
            return

        settings = await self.kill_feed.get_settings(ctx.guild)
        channel_id = settings.get("channel_id")
        channel = ctx.guild.get_channel(channel_id) if isinstance(channel_id, int) else None
        endpoint = (
            f"`{settings['host']}:{settings['port']}`"
            if settings.get("host") and settings.get("port")
            else "not configured"
        )
        queue_size = self.kill_feed.queue_size(ctx.guild.id)
        lines = [
            f"Enabled: `{'yes' if settings.get('enabled') else 'no'}`",
            f"hllrcon dependency: `{'ready' if self.kill_feed.is_available() else 'unavailable'}`",
            f"Channel: {channel.mention if isinstance(channel, discord.TextChannel) else 'not configured'}",
            f"RCON endpoint: {endpoint}",
            f"RCON password: `{'configured' if self.kill_feed.has_password() else 'not configured'}`",
            f"Queued events: `{queue_size}`",
            "Delivery interval: `3 seconds`",
        ]
        await ctx.send("HLL: Vietnam kill-feed settings:\n" + "\n".join(f"- {line}" for line in lines))
