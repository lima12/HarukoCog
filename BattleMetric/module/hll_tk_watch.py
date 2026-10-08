"""Threshold-based HLL: Vietnam team-kill alerts and staff actions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError

log = logging.getLogger("red.BattleMetric.hll_tk_watch")

try:
    from hllrcon.admin_logs import HLLVPlayerTeamKillAdminLog
except Exception as exc:  # noqa: BLE001 - keep unrelated cog features loadable
    HLLVPlayerTeamKillAdminLog = None
    HLLRCON_MODEL_IMPORT_ERROR: Exception | None = exc
else:
    HLLRCON_MODEL_IMPORT_ERROR = None


TKAction = Literal["forgive", "warn_watch", "kick"]


class HLLTKWatchChannelUnavailable(RuntimeError):
    """Raised when a configured Discord alert channel can no longer be used."""


@dataclass(frozen=True)
class HLLTeamKillEvent:
    """One deduplicated team-kill entry from the shared RCON log."""

    player_name: str
    eos_id: str
    player_team: str
    victim_name: str
    victim_id: str
    weapon_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class HLLTKWatchRecord:
    """A persisted player watch created by the Warn & Watch action."""

    eos_id: str
    player_name: str
    expires_at: int
    starts_at: float = 0.0

    def to_config(self) -> dict[str, str | int | float]:
        return {
            "eos_id": self.eos_id,
            "player_name": self.player_name,
            "expires_at": self.expires_at,
            "starts_at": self.starts_at,
        }


@dataclass(frozen=True)
class HLLTKAlertRecord:
    """Persisted routing data for one 15-minute Discord action message."""

    message_id: int
    channel_id: int
    eos_id: str
    player_name: str
    threshold_count: int
    watch_duration_minutes: int
    expires_at: int
    status: str = "active"
    default_action_at: int = 0
    warning_sent: bool = False
    decision: str = ""

    def to_config(self) -> dict[str, str | int]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "eos_id": self.eos_id,
            "player_name": self.player_name,
            "threshold_count": self.threshold_count,
            "watch_duration_minutes": self.watch_duration_minutes,
            "expires_at": self.expires_at,
            "status": self.status,
            "default_action_at": self.default_action_at,
            "warning_sent": self.warning_sent,
            "decision": self.decision,
        }


class HLLTKWatchActionView(discord.ui.View):
    """Persistent staff actions routed by the alert message ID."""

    def __init__(self, module: HLLTKWatchModule, *, watching: bool = False):
        super().__init__(timeout=None)
        self.module = module
        self.warn_watch.disabled = watching

    @discord.ui.button(
        label="Forgive",
        style=discord.ButtonStyle.secondary,
        custom_id="hll_tk_watch:forgive",
    )
    async def forgive(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.module.handle_action(interaction, "forgive")

    @discord.ui.button(
        label="Warn & Watch",
        style=discord.ButtonStyle.primary,
        custom_id="hll_tk_watch:warn_watch",
    )
    async def warn_watch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.module.handle_action(interaction, "warn_watch")

    @discord.ui.button(
        label="Kick",
        style=discord.ButtonStyle.danger,
        custom_id="hll_tk_watch:kick",
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.module.handle_action(interaction, "kick")


class HLLTKWatchModule:
    """Track team-kill bursts and coordinate Discord/RCON moderation actions."""

    WINDOW_SECONDS = 60
    ACTION_WINDOW_SECONDS = 15 * 60
    DEFAULT_ACTION_SECONDS = 5 * 60
    MAX_TIMER_ACTIONS_PER_PASS = 20
    WORKER_INTERVAL_SECONDS = 1
    CLEANUP_INTERVAL_SECONDS = 30
    MAX_QUEUE_SIZE = 500
    MAX_SEEN_EVENTS = 4000
    MAX_EVENTS_PER_GUILD_PER_PASS = 25
    MAX_RETRY_SECONDS = 60
    OFFLINE_CHECK_INTERVAL_SECONDS = 3

    WARNING_MESSAGE = (
        "your Team Kill have been noticed by admin. Please avoid TK by all cost"
    )
    MANUAL_KICK_REASON = "Kicked by an administrator for team killing."
    WATCH_KICK_REASON = "Team killing while under an administrator watch."

    _EMPTY_SETTINGS: ClassVar[dict[str, Any]] = {
        "enabled": False,
        "channel_id": None,
        "role_id": None,
        "threshold_per_min": 3,
        "watch_duration_minutes": 15,
        "exclude_commander": True,
        "active_watches": {},
        "active_alerts": {},
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._enabled_guilds: set[int] = set()
        self._queues: dict[int, deque[HLLTeamKillEvent]] = {}
        self._seen: dict[int, OrderedDict[str, None]] = {}
        self._windows: dict[int, dict[str, deque[float]]] = {}
        self._watches: dict[int, dict[str, HLLTKWatchRecord]] = {}
        self._alerts: dict[int, dict[int, HLLTKAlertRecord]] = {}
        self._open_alert_players: dict[int, set[str]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._alert_locks: dict[int, asyncio.Lock] = {}
        self._failure_counts: dict[int, int] = {}
        self._next_process_at: dict[int, float] = {}
        self._watch_failure_notified: set[tuple[int, str]] = set()
        self._next_cleanup_at = 0.0
        self._registered_views: dict[int, HLLTKWatchActionView] = {}
        self._timer_failures: dict[int, int] = {}
        self._next_timer_at: dict[int, float] = {}
        self._next_offline_check_at: dict[int, float] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(hll_tk_watch=dict(self._EMPTY_SETTINGS))

    async def start(self) -> None:
        await self._load_state()
        now = int(discord.utils.utcnow().timestamp())
        for alerts in self._alerts.values():
            for alert in alerts.values():
                if alert.status not in {"active", "watching"} or alert.expires_at <= now:
                    continue
                view = HLLTKWatchActionView(self, watching=alert.status == "watching")
                self.cog.bot.add_view(view, message_id=alert.message_id)
                self._registered_views[alert.message_id] = view
        if not self.worker.is_running():
            self.worker.start()

    def stop(self) -> None:
        self.worker.cancel()
        for view in self._registered_views.values():
            view.stop()
        self._registered_views.clear()
        self._enabled_guilds.clear()
        self._queues.clear()
        self._seen.clear()
        self._windows.clear()
        self._watches.clear()
        self._alerts.clear()
        self._open_alert_players.clear()
        self._guild_locks.clear()
        self._alert_locks.clear()
        self._failure_counts.clear()
        self._next_process_at.clear()
        self._watch_failure_notified.clear()
        self._timer_failures.clear()
        self._next_timer_at.clear()
        self._next_offline_check_at.clear()

    def should_poll(self, guild_id: int) -> bool:
        return guild_id in self._enabled_guilds

    async def get_settings(self, guild: discord.Guild) -> dict[str, Any]:
        stored = await self.cog.config.guild(guild).hll_tk_watch()
        return self._normalize_settings(stored)

    async def delete_user_data(self, user_id: int) -> None:
        """Remove stored staff identity from temporary moderation decisions."""
        mentions = (f"<@{user_id}>", f"<@!{user_id}>")
        for guild_id in await self.cog.config.all_guilds():
            guild_id = int(guild_id)
            async with self._guild_lock(guild_id):
                value = self.cog.config.guild_from_id(guild_id).hll_tk_watch
                settings = self._normalize_settings(await value())
                changed = False
                for raw in settings["active_alerts"].values():
                    if not isinstance(raw, dict):
                        continue
                    decision = str(raw.get("decision", ""))
                    for mention in mentions:
                        decision = decision.replace(mention, "a deleted administrator")
                    if decision != raw.get("decision", ""):
                        raw["decision"] = decision
                        changed = True
                for message_id, alert in list(self._alerts.get(guild_id, {}).items()):
                    decision = alert.decision
                    for mention in mentions:
                        decision = decision.replace(mention, "a deleted administrator")
                    self._alerts[guild_id][message_id] = replace(alert, decision=decision)
                if changed:
                    await value.set(settings)

    async def configure(
        self,
        guild: discord.Guild,
        *,
        channel_id: int,
        threshold_per_min: int,
        watch_duration_minutes: int,
        exclude_commander: bool,
        role_id: int | None = None,
    ) -> None:
        async with self._guild_lock(guild.id):
            settings = await self.get_settings(guild)
            settings.update(
                {
                    "enabled": True,
                    "channel_id": channel_id,
                    "threshold_per_min": threshold_per_min,
                    "watch_duration_minutes": watch_duration_minutes,
                    "exclude_commander": exclude_commander,
                    "role_id": role_id,
                }
            )
            self._write_runtime_state(guild.id, settings)
            await self.cog.config.guild(guild).hll_tk_watch.set(settings)
            self._enabled_guilds.add(guild.id)
            self._queues.setdefault(guild.id, deque())
            self._seen.setdefault(guild.id, OrderedDict())
            self._windows.pop(guild.id, None)

    async def disable(self, guild: discord.Guild) -> None:
        async with self._guild_lock(guild.id):
            alerts = list(self._alerts.get(guild.id, {}).values())
            settings = await self.get_settings(guild)
            settings["enabled"] = False
            settings["active_watches"] = {}
            settings["active_alerts"] = {}
            await self.cog.config.guild(guild).hll_tk_watch.set(settings)
            self._enabled_guilds.discard(guild.id)
            self._reset_transient_guild(guild.id)
            self._watches.pop(guild.id, None)
            self._alerts.pop(guild.id, None)
            self._open_alert_players.pop(guild.id, None)
        for alert in alerts:
            async with self._alert_lock(alert.message_id):
                self._timer_failures.pop(alert.message_id, None)
                self._next_timer_at.pop(alert.message_id, None)
                await self._edit_alert_message(
                    guild, replace(alert, status="resolved", decision="TK watch disabled")
                )

    async def ingest_admin_logs(
        self,
        guild: discord.Guild,
        entries: Sequence[Any],
    ) -> None:
        """Queue team kills without slowing the shared RCON polling path."""
        if not self.should_poll(guild.id) or HLLVPlayerTeamKillAdminLog is None:
            return

        for entry in entries:
            if not isinstance(entry, HLLVPlayerTeamKillAdminLog):
                continue
            fingerprint = f"{entry.timestamp.isoformat()}\0{entry.raw_message}"
            if not self._mark_seen(guild.id, fingerprint):
                continue
            self._enqueue(
                guild.id,
                HLLTeamKillEvent(
                    player_name=self._safe_plain_text(entry.instigator_name, 100),
                    eos_id=str(entry.instigator_id),
                    player_team=self._safe_plain_text(entry.instigator_team_name, 32),
                    victim_name=self._safe_plain_text(entry.victim_name, 100),
                    victim_id=str(entry.victim_id),
                    weapon_id=self._safe_plain_text(entry.weapon_id, 100),
                    occurred_at=entry.timestamp,
                ),
            )

    @tasks.loop(seconds=WORKER_INTERVAL_SECONDS)
    async def worker(self) -> None:
        try:
            await self._process_all_guilds()
            await self._process_alert_timers()
            now = time.monotonic()
            if now >= self._next_cleanup_at:
                self._next_cleanup_at = now + self.CLEANUP_INTERVAL_SECONDS
                await self._cleanup_expired_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error in the HLL team-kill watch worker")

    @worker.before_loop
    async def before_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _process_all_guilds(self) -> None:
        now = time.monotonic()
        for guild in self.cog.bot.guilds:
            queue = self._queues.get(guild.id)
            if not queue or not self.should_poll(guild.id):
                continue
            if now < self._next_process_at.get(guild.id, 0):
                continue
            processed = 0
            while queue and processed < self.MAX_EVENTS_PER_GUILD_PER_PASS:
                event = queue.popleft()
                processed += 1
                try:
                    await self._process_event(guild, event)
                except asyncio.CancelledError:
                    queue.appendleft(event)
                    raise
                except HLLTKWatchChannelUnavailable:
                    log.warning(
                        "Disabling HLL team-kill watch for guild %s because its alert channel is unavailable",
                        guild.id,
                    )
                    await self.disable(guild)
                    break
                except discord.Forbidden:
                    log.warning(
                        "Disabling HLL team-kill watch for guild %s because Discord denied alert delivery",
                        guild.id,
                    )
                    await self.disable(guild)
                    break
                except discord.HTTPException as exc:
                    queue.appendleft(event)
                    self._record_process_failure(guild.id, exc)
                    break
                except Exception:
                    log.exception(
                        "Could not process an HLL team-kill event for guild %s",
                        guild.id,
                    )
                else:
                    self._failure_counts.pop(guild.id, None)
                    self._next_process_at.pop(guild.id, None)

    async def _process_event(
        self,
        guild: discord.Guild,
        event: HLLTeamKillEvent,
    ) -> None:
        settings = await self.get_settings(guild)
        if not settings["enabled"]:
            self._enabled_guilds.discard(guild.id)
            self._reset_transient_guild(guild.id)
            return

        watch = self._active_watch(guild.id, event.eos_id)
        if watch is not None:
            if event.occurred_at.timestamp() < watch.starts_at:
                return
            if settings["exclude_commander"]:
                role = await self._get_role(guild, event.eos_id)
                if role is None:
                    log.warning(
                        "Skipped an automatic TK-watch kick in guild %s because the player's commander status could not be verified",
                        guild.id,
                    )
                    return
                if role[0]:
                    return
            if not self.should_poll(guild.id):
                return
            await self._kick_watched_player(guild, event, watch, settings)
            return

        count = self._record_team_kill(guild.id, event.eos_id, event.occurred_at)
        threshold = settings["threshold_per_min"]
        if count < threshold:
            return
        if event.eos_id in self._open_alert_players.setdefault(guild.id, set()):
            return

        role = await self._get_role(guild, event.eos_id)
        if role is None and settings["exclude_commander"]:
            log.warning(
                "Skipped a TK threshold alert in guild %s because the player's commander status could not be verified",
                guild.id,
            )
            self._clear_window(guild.id, event.eos_id)
            return
        is_commander, role_name = role if role is not None else (False, "Unavailable")
        if is_commander and settings["exclude_commander"]:
            self._clear_window(guild.id, event.eos_id)
            return
        if not self.should_poll(guild.id):
            return

        await self._send_threshold_alert(
            guild,
            event,
            count=count,
            threshold=threshold,
            watch_duration_minutes=settings["watch_duration_minutes"],
            role_name=role_name,
            commander_included=is_commander and not settings["exclude_commander"],
            channel_id=settings["channel_id"],
            role_id=settings["role_id"],
        )
        self._clear_window(guild.id, event.eos_id)

    async def _send_threshold_alert(
        self,
        guild: discord.Guild,
        event: HLLTeamKillEvent,
        *,
        count: int,
        threshold: int,
        watch_duration_minutes: int,
        role_name: str,
        commander_included: bool,
        channel_id: int | None,
        role_id: int | None = None,
    ) -> None:
        channel = (
            guild.get_channel_or_thread(channel_id)
            if isinstance(channel_id, int)
            else None
        )
        if channel is None or not hasattr(channel, "send"):
            raise HLLTKWatchChannelUnavailable

        expires_at = int((discord.utils.utcnow() + timedelta(seconds=self.ACTION_WINDOW_SECONDS)).timestamp())
        default_action_at = expires_at - self.ACTION_WINDOW_SECONDS + self.DEFAULT_ACTION_SECONDS
        embed = discord.Embed(
            title="HLL VN Team-Kill Threshold",
            description=(
                f"This player reached **{count} team kills in a rolling minute**. "
                f"Automatic Warn & Watch begins <t:{default_action_at}:R> unless staff decide. "
                f"Forgive is available until <t:{expires_at}:R>."
            ),
            color=discord.Color.orange(),
            timestamp=event.occurred_at,
        )
        embed.add_field(
            name="Automatic warning",
            value="Pending delivery to the player.",
            inline=False,
        )
        embed.add_field(
            name="Player",
            value=self._safe_embed_text(event.player_name, 1024),
            inline=False,
        )
        embed.add_field(name="EOS ID", value=f"`{event.eos_id}`", inline=False)
        embed.add_field(name="Team", value=event.player_team, inline=True)
        embed.add_field(
            name="Threshold",
            value=f"{count} / {threshold} TKs per minute",
            inline=True,
        )
        embed.add_field(
            name="In-game role",
            value=(
                "Commander (included by configuration)"
                if commander_included
                else self._safe_embed_text(role_name, 1024)
            ),
            inline=True,
        )
        embed.add_field(
            name="Latest team kill",
            value=(
                f"Victim: **{self._safe_embed_text(event.victim_name, 700)}**\n"
                f"Weapon: `{self._safe_code_text(event.weapon_id, 200)}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Warn & Watch",
            value=(
                f"Warns the player and watches them for {watch_duration_minutes} minute(s). "
                "Their next team kill during that period triggers an automatic kick."
            ),
            inline=False,
        )
        embed.set_footer(text="Buttons expire after 15 minutes. This alert is kept as a record.")
        view = HLLTKWatchActionView(self)
        role = guild.get_role(role_id) if role_id is not None else None
        message = await channel.send(
            content=role.mention if role is not None and not role.is_default() else None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False,
                roles=[role] if role is not None and not role.is_default() else False,
                replied_user=False,
            ),
        )
        self._registered_views[message.id] = view

        record = HLLTKAlertRecord(
            message_id=message.id,
            channel_id=channel.id,
            eos_id=event.eos_id,
            player_name=event.player_name,
            threshold_count=count,
            watch_duration_minutes=watch_duration_minutes,
            expires_at=expires_at,
            default_action_at=default_action_at,
        )
        stored = False
        async with self._guild_lock(guild.id):
            if self.should_poll(guild.id):
                self._alerts.setdefault(guild.id, {})[message.id] = record
                self._open_alert_players.setdefault(guild.id, set()).add(event.eos_id)
                await self._persist_runtime_state(guild)
                stored = True
        if not stored:
            await self._edit_alert_message(
                guild, replace(record, status="resolved", decision="TK watch disabled"), message
            )
        else:
            await self._process_alert_timer(guild, record)

    async def handle_action(
        self,
        interaction: discord.Interaction,
        action: TKAction,
    ) -> None:
        """Execute one authorized button action against the alert's player."""
        if not await self.cog.is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use HLL: Vietnam administration controls.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        message = interaction.message
        if guild is None or message is None:
            await interaction.followup.send("This action is only available in the configured server.")
            return

        async with self._alert_lock(message.id):
            record = self._alerts.get(guild.id, {}).get(message.id)
            now = int(discord.utils.utcnow().timestamp())
            if record is None or record.expires_at <= now:
                if record is not None:
                    await self._expire_alert_while_locked(guild, record)
                await interaction.followup.send("This team-kill action window has expired.")
                return
            if record.status not in {"active", "watching"}:
                if record.status == "closed":
                    await self._edit_alert_message(guild, record, message)
                    await interaction.followup.send(record.decision)
                else:
                    await interaction.followup.send("Another administrator already handled this alert.")
                return
            if action == "warn_watch" and record.status == "watching":
                await interaction.followup.send("This player is already watched. You can still forgive or kick them.")
                return
            if not self.should_poll(guild.id):
                await self._expire_alert_while_locked(guild, record)
                await interaction.followup.send("Team-kill watch is disabled for this server.")
                return

            try:
                updated = await self._perform_action(
                    guild, record, action, actor=interaction.user.mention
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                updated = None if action == "forgive" else await self._close_if_player_offline(
                    guild, record, actor=interaction.user.mention
                )
                if updated is None:
                    if isinstance(exc, (ValueError, KillFeedConnectionTestError)):
                        await interaction.followup.send(str(exc))
                    else:
                        log.exception("HLL TK-watch button action failed in guild %s", guild.id)
                        await interaction.followup.send(
                            "The RCON action failed. The buttons remain available; check the bot log and retry."
                        )
                    return

            if updated is None:
                await interaction.followup.send(
                    "This alert is no longer available for that action."
                )
                return
            await self._edit_alert_message(guild, updated, message)
            label = "Case closed" if updated.status == "closed" else "Action completed"
            await interaction.followup.send(f"{label}: {updated.decision}")

    async def _close_if_player_offline(
        self, guild: discord.Guild, record: HLLTKAlertRecord, *, actor: str
    ) -> HLLTKAlertRecord | None:
        """Close a failed action only after a fresh, valid roster proves absence."""
        if not self.should_poll(guild.id):
            return None
        now = time.monotonic()
        if now < self._next_offline_check_at.get(guild.id, 0):
            return None
        # Claim before yielding; failures and simultaneous staff actions share pacing.
        self._next_offline_check_at[guild.id] = now + self.OFFLINE_CHECK_INTERVAL_SECONDS
        try:
            response = await self.cog.kill_feed.execute_rcon(
                guild, "GetPlayers TK offline check", lambda client: client.get_players()
            )
            players = getattr(response, "players", None)
            if not isinstance(players, (list, tuple)):
                return None
            target = record.eos_id.strip().casefold()
            for player in players:
                eos_id = getattr(player, "eos_id", None)
                if not isinstance(eos_id, str) or not eos_id.strip():
                    return None
                if eos_id.strip().casefold() == target:
                    return None

            async with self._guild_lock(guild.id):
                current = self._alerts.get(guild.id, {}).get(record.message_id)
                if (
                    not self.should_poll(guild.id) or current is None
                    or current.status not in {"active", "watching"}
                ):
                    return None
                decision = "Closed - player disconnected before action"
                if self._active_watch(guild.id, current.eos_id) is not None:
                    decision += "; existing watch remains until expiry"
                updated = replace(current, status="closed", decision=f"{decision} by {actor}")
                settings = await self.get_settings(guild)
                self._write_runtime_state(guild.id, settings)
                settings["active_alerts"][str(current.message_id)] = updated.to_config()
                # Persist before changing memory so a failed Config write is retryable.
                await self.cog.config.guild(guild).hll_tk_watch.set(settings)
                self._alerts[guild.id][current.message_id] = updated
                self._open_alert_players.setdefault(guild.id, set()).discard(current.eos_id)
                self._clear_window(guild.id, current.eos_id)
                self._timer_failures.pop(current.message_id, None)
                self._next_timer_at.pop(current.message_id, None)
                return updated
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Could not confirm/record TK player absence in guild %s", guild.id, exc_info=True)
            return None

    async def _perform_action(
        self,
        guild: discord.Guild,
        record: HLLTKAlertRecord,
        action: TKAction,
        *,
        actor: str = "Automatic five-minute default",
        automatic: bool = False,
    ) -> HLLTKAlertRecord | None:
        # Serialize watch changes and watched-player kicks against forgiveness.
        async with self._guild_lock(guild.id):
            current = self._alerts.get(guild.id, {}).get(record.message_id)
            if (
                not self.should_poll(guild.id)
                or current is None
                or current.status not in {"active", "watching"}
                or (action == "warn_watch" and current.status == "watching")
            ):
                return None
            now = int(discord.utils.utcnow().timestamp())
            if not automatic and current.expires_at <= now:
                return None
            excluded_commander = False
            if not automatic and action in {"warn_watch", "kick"}:
                settings = await self.get_settings(guild)
                if settings["exclude_commander"]:
                    role = await self._get_role(guild, current.eos_id)
                    if role is None:
                        raise ValueError(
                            "Cannot verify the player's role. No TK action was taken; retry when RCON is available."
                        )
                    excluded_commander = role[0]
                    if excluded_commander:
                        action = "forgive"
            watches = self._watches.setdefault(guild.id, {})
            if action == "forgive":
                watches.pop(current.eos_id, None)
                label = "Commander excluded; watch cancelled" if excluded_commander else "Forgiven; watch cancelled"
            elif action == "warn_watch":
                await self._warn_player(guild, current.eos_id)
                started_at = discord.utils.utcnow().timestamp()
                watches[current.eos_id] = HLLTKWatchRecord(
                    eos_id=current.eos_id,
                    player_name=current.player_name,
                    starts_at=started_at,
                    expires_at=int(started_at) + current.watch_duration_minutes * 60,
                )
                label = f"Warned and watched for {current.watch_duration_minutes} minute(s)"
            elif action == "kick":
                await self.cog.kill_feed.execute_rcon(
                    guild, "KickPlayer TK action",
                    lambda client: client.kick_player(current.eos_id, self.MANUAL_KICK_REASON),
                )
                watches.pop(current.eos_id, None)
                label = "Kicked"
            else:
                raise ValueError("Unknown team-kill action.")
            updated = replace(
                current,
                status="watching" if action == "warn_watch" else "resolved",
                warning_sent=current.warning_sent or action == "warn_watch",
                decision=f"{label} by {actor}",
            )
            self._alerts[guild.id][current.message_id] = updated
            self._watch_failure_notified.discard((guild.id, current.eos_id))
            if updated.status == "resolved":
                self._open_alert_players.setdefault(guild.id, set()).discard(current.eos_id)
                self._clear_window(guild.id, current.eos_id)
            await self._persist_runtime_state(guild)
            return updated

    async def _warn_player(self, guild: discord.Guild, eos_id: str) -> None:
        await self.cog.kill_feed.execute_rcon(
            guild, "MessagePlayer TK warning",
            lambda client: client.message_player(eos_id, self.WARNING_MESSAGE),
        )

    async def _process_alert_timers(self) -> None:
        actions = 0
        for guild in self.cog.bot.guilds:
            if not self.should_poll(guild.id):
                continue
            for record in list(self._alerts.get(guild.id, {}).values()):
                if record.status != "active":
                    continue
                if time.monotonic() < self._next_timer_at.get(record.message_id, 0):
                    continue
                now = int(discord.utils.utcnow().timestamp())
                if record.warning_sent and now < record.default_action_at:
                    continue
                if actions >= self.MAX_TIMER_ACTIONS_PER_PASS:
                    return
                actions += 1
                await self._process_alert_timer(guild, record)

    async def _process_alert_timer(self, guild: discord.Guild, record: HLLTKAlertRecord) -> None:
        async with self._alert_lock(record.message_id):
            current = self._alerts.get(guild.id, {}).get(record.message_id)
            if current is None or current.status != "active" or not self.should_poll(guild.id):
                return
            try:
                settings = await self.get_settings(guild)
                role = await self._get_role(guild, current.eos_id) if settings["exclude_commander"] else (False, "")
                if role is None:
                    raise RuntimeError("Could not verify commander status")
                if role[0]:
                    async with self._guild_lock(guild.id):
                        latest = self._alerts.get(guild.id, {}).get(current.message_id)
                        if latest is None or latest.status != "active" or not self.should_poll(guild.id):
                            return
                        current = replace(latest, status="resolved", decision="Commander excluded")
                        self._alerts[guild.id][current.message_id] = current
                        self._open_alert_players.setdefault(guild.id, set()).discard(current.eos_id)
                        await self._persist_runtime_state(guild)
                elif int(discord.utils.utcnow().timestamp()) >= current.default_action_at:
                    current = await self._perform_action(guild, current, "warn_watch", automatic=True)
                else:
                    current = await self._deliver_initial_warning(guild, current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                closed = await self._close_if_player_offline(
                    guild, current, actor="Automatic TK warning/watch"
                )
                if closed is not None:
                    await self._edit_alert_message(guild, closed)
                    return
                failures = self._timer_failures.get(record.message_id, 0) + 1
                self._timer_failures[record.message_id] = failures
                self._next_timer_at[record.message_id] = time.monotonic() + min(
                    self.MAX_RETRY_SECONDS, 2 ** min(failures, 6)
                )
                log.warning("TK warning/watch failed for alert %s; retrying: %s", record.message_id, exc)
                return
            self._timer_failures.pop(record.message_id, None)
            self._next_timer_at.pop(record.message_id, None)
            if current is not None:
                await self._edit_alert_message(guild, current)

    async def _deliver_initial_warning(
        self, guild: discord.Guild, record: HLLTKAlertRecord
    ) -> HLLTKAlertRecord | None:
        async with self._guild_lock(guild.id):
            current = self._alerts.get(guild.id, {}).get(record.message_id)
            if not self.should_poll(guild.id) or current is None or current.status != "active":
                return None
            if not current.warning_sent:
                await self._warn_player(guild, current.eos_id)
                current = replace(current, warning_sent=True)
                self._alerts[guild.id][current.message_id] = current
                await self._persist_runtime_state(guild)
            return current

    async def _kick_watched_player(
        self,
        guild: discord.Guild,
        event: HLLTeamKillEvent,
        watch: HLLTKWatchRecord,
        settings: Mapping[str, Any],
    ) -> None:
        changed_alerts = []
        success = False
        notify = False
        async with self._guild_lock(guild.id):
            if (
                not self.should_poll(guild.id)
                or self._active_watch(guild.id, event.eos_id) != watch
                or event.occurred_at.timestamp() < watch.starts_at
            ):
                return
            try:
                await self.cog.kill_feed.execute_rcon(
                    guild, "KickPlayer automatic TK watch",
                    lambda client: client.kick_player(event.eos_id, self.WATCH_KICK_REASON),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Automatic TK-watch kick failed for %s in guild %s: %s", event.eos_id, guild.id, exc)
                key = (guild.id, event.eos_id)
                if key not in self._watch_failure_notified:
                    self._watch_failure_notified.add(key)
                    notify = True
            else:
                success = notify = True
                self._watches.setdefault(guild.id, {}).pop(event.eos_id, None)
                self._watch_failure_notified.discard((guild.id, event.eos_id))
                for message_id, alert in list(self._alerts.get(guild.id, {}).items()):
                    if alert.eos_id == event.eos_id and alert.status in {"active", "watching"}:
                        updated = replace(alert, status="resolved", decision="Automatically kicked for a team kill while watched")
                        self._alerts[guild.id][message_id] = updated
                        changed_alerts.append(updated)
                self._open_alert_players.setdefault(guild.id, set()).discard(event.eos_id)
                await self._persist_runtime_state(guild)
        for alert in changed_alerts:
            async with self._alert_lock(alert.message_id):
                current = self._alerts.get(guild.id, {}).get(alert.message_id)
                if current is not None:
                    await self._edit_alert_message(guild, current)
        if notify:
            await self._send_watch_result(guild, event, settings.get("channel_id"), success=success)

    async def _send_watch_result(
        self,
        guild: discord.Guild,
        event: HLLTeamKillEvent,
        channel_id: object,
        *,
        success: bool,
    ) -> None:
        channel = (
            guild.get_channel_or_thread(channel_id)
            if isinstance(channel_id, int)
            else None
        )
        if channel is None or not hasattr(channel, "send"):
            return
        embed = discord.Embed(
            title=(
                "Automatic TK Watch Kick"
                if success
                else "Automatic TK Watch Kick Failed"
            ),
            description=(
                "The watched player committed another team kill and was kicked."
                if success
                else "The watched player committed another team kill, but RCON could not kick them. The watch remains active."
            ),
            color=discord.Color.red() if success else discord.Color.orange(),
            timestamp=event.occurred_at,
        )
        embed.add_field(name="Player", value=self._safe_embed_text(event.player_name, 1024), inline=False)
        embed.add_field(name="EOS ID", value=f"`{event.eos_id}`", inline=False)
        embed.add_field(name="Victim", value=self._safe_embed_text(event.victim_name, 1024), inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not send automatic TK-watch result in guild %s: %s",
                guild.id,
                exc,
            )

    async def _get_role(
        self,
        guild: discord.Guild,
        eos_id: str,
    ) -> tuple[bool, str] | None:
        try:
            player = await self.cog.kill_feed.execute_rcon(
                guild,
                "GetPlayer TK commander check",
                lambda client: client.get_player(eos_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - caller chooses fail-safe behavior
            log.warning(
                "Could not resolve player role for TK watch in guild %s: %s",
                guild.id,
                exc,
            )
            return None

        role = getattr(player, "role", None)
        role_id = self._optional_int(getattr(player, "role_id", None))
        role_name = str(
            getattr(role, "pretty_name", None)
            or getattr(role, "name", None)
            or "Unknown"
        )
        normalized = role_name.casefold().replace(" ", "")
        is_commander = role_id == 20 or normalized in {"commander", "armycommander"}
        return is_commander, role_name

    async def _cleanup_expired_state(self) -> None:
        now = int(discord.utils.utcnow().timestamp())
        for guild in self.cog.bot.guilds:
            changed = False
            async with self._guild_lock(guild.id):
                watches = self._watches.setdefault(guild.id, {})
                for eos_id, watch in list(watches.items()):
                    if watch.expires_at <= now:
                        watches.pop(eos_id, None)
                        self._watch_failure_notified.discard((guild.id, eos_id))
                        changed = True
                expired_alerts = [
                    alert
                    for alert in self._alerts.setdefault(guild.id, {}).values()
                    if alert.expires_at <= now
                ]
                if changed:
                    await self._persist_runtime_state(guild)
            for alert in expired_alerts:
                await self._expire_alert(guild, alert)

    async def _expire_alert(
        self,
        guild: discord.Guild,
        record: HLLTKAlertRecord,
    ) -> None:
        async with self._alert_lock(record.message_id):
            await self._expire_alert_while_locked(guild, record)

    async def _expire_alert_while_locked(
        self,
        guild: discord.Guild,
        record: HLLTKAlertRecord,
    ) -> None:
        """Expire an alert while its message-specific lock is already held."""
        current = self._alerts.get(guild.id, {}).get(record.message_id)
        if current is None:
            return
        if not await self._edit_alert_message(guild, current):
            return
        async with self._guild_lock(guild.id):
            current = self._alerts.setdefault(guild.id, {}).get(record.message_id)
            if current is None:
                return
            if current.status == "active" and self.should_poll(guild.id):
                # A failed default action still needs its durable retry record.
                return
            self._alerts[guild.id].pop(record.message_id, None)
            if not any(
                alert.eos_id == current.eos_id and alert.status in {"active", "watching"}
                for alert in self._alerts[guild.id].values()
            ):
                self._open_alert_players.setdefault(guild.id, set()).discard(current.eos_id)
            await self._persist_runtime_state(guild)
            self._timer_failures.pop(record.message_id, None)
            self._next_timer_at.pop(record.message_id, None)

    async def _edit_alert_message(
        self,
        guild: discord.Guild,
        record: HLLTKAlertRecord,
        message: discord.Message | None = None,
    ) -> bool:
        view = None
        try:
            if message is None:
                channel = guild.get_channel_or_thread(record.channel_id)
                if channel is None or not hasattr(channel, "get_partial_message"):
                    self._unregister_view(record.message_id)
                    return True
                message = await channel.get_partial_message(record.message_id).fetch()
            embed = message.embeds[0].copy() if message.embeds else discord.Embed()
            if record.status == "resolved":
                embed.color = discord.Color.green()
            elif record.status == "closed":
                embed.color = discord.Color.orange()
            fields = {
                "Automatic warning": "Sent to the player." if record.warning_sent else "Delivery pending or cancelled.",
                "Decision": record.decision or "Awaiting an administrator or the five-minute default.",
            }
            for name, value in fields.items():
                index = next((i for i, field in enumerate(embed.fields) if field.name == name), None)
                if index is None:
                    embed.add_field(name=name, value=value, inline=False)
                else:
                    embed.set_field_at(index, name=name, value=value, inline=False)
            expired = record.expires_at <= int(discord.utils.utcnow().timestamp())
            view = None if expired or record.status in {"resolved", "closed"} else HLLTKWatchActionView(
                self, watching=record.status == "watching"
            )
            embed.set_footer(text=(
                "Action window closed. This alert is kept as a record."
                if view is None else "Forgive is available for 15 minutes. This alert is kept as a record."
            ))
            # Stop the previous view before Discord registers its replacement.
            self._unregister_view(record.message_id)
            await message.edit(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            self._unregister_view(record.message_id)
        except discord.HTTPException:
            log.exception("Could not update HLL TK-watch alert %s", record.message_id)
            if view is not None:
                self.cog.bot.add_view(view, message_id=record.message_id)
                self._registered_views[record.message_id] = view
            return False
        else:
            if view is not None:
                self._registered_views[record.message_id] = view
        return True

    async def _load_state(self) -> None:
        now = int(discord.utils.utcnow().timestamp())
        all_guilds = await self.cog.config.all_guilds()
        for guild_id_raw, guild_data in all_guilds.items():
            guild_id = int(guild_id_raw)
            stored = (
                guild_data.get("hll_tk_watch")
                if isinstance(guild_data, Mapping)
                else None
            )
            settings = self._normalize_settings(stored)
            if settings["enabled"]:
                self._enabled_guilds.add(guild_id)
            watches = self._parse_watches(settings.get("active_watches"), now)
            alerts = self._parse_alerts(settings.get("active_alerts"))
            self._watches[guild_id] = watches
            self._alerts[guild_id] = alerts
            self._open_alert_players[guild_id] = {
                alert.eos_id
                for alert in alerts.values()
                if alert.status in {"active", "watching"}
            }
            settings["active_watches"] = {
                eos_id: watch.to_config() for eos_id, watch in watches.items()
            }
            settings["active_alerts"] = {
                str(message_id): alert.to_config()
                for message_id, alert in alerts.items()
            }
            await self.cog.config.guild_from_id(guild_id).hll_tk_watch.set(settings)

    async def _persist_runtime_state(self, guild: discord.Guild) -> None:
        settings = await self.get_settings(guild)
        self._write_runtime_state(guild.id, settings)
        await self.cog.config.guild(guild).hll_tk_watch.set(settings)

    def _write_runtime_state(self, guild_id: int, settings: dict[str, Any]) -> None:
        settings["active_watches"] = {
            eos_id: watch.to_config()
            for eos_id, watch in self._watches.get(guild_id, {}).items()
        }
        settings["active_alerts"] = {
            str(message_id): alert.to_config()
            for message_id, alert in self._alerts.get(guild_id, {}).items()
        }

    @classmethod
    def _normalize_settings(cls, stored: object) -> dict[str, Any]:
        value = stored if isinstance(stored, Mapping) else {}
        channel_id = cls._optional_id(value.get("channel_id"))
        threshold = cls._bounded_int(value.get("threshold_per_min"), 1, 100, 3)
        watch_duration = cls._bounded_int(
            value.get("watch_duration_minutes"),
            1,
            90,
            15,
        )
        return {
            "enabled": bool(value.get("enabled", False)),
            "channel_id": channel_id,
            "role_id": cls._optional_id(value.get("role_id")),
            "threshold_per_min": threshold,
            "watch_duration_minutes": watch_duration,
            "exclude_commander": bool(value.get("exclude_commander", True)),
            "active_watches": dict(value.get("active_watches", {}))
            if isinstance(value.get("active_watches"), Mapping)
            else {},
            "active_alerts": dict(value.get("active_alerts", {}))
            if isinstance(value.get("active_alerts"), Mapping)
            else {},
        }

    @classmethod
    def _parse_watches(
        cls,
        stored: object,
        now: int,
    ) -> dict[str, HLLTKWatchRecord]:
        if not isinstance(stored, Mapping):
            return {}
        parsed: dict[str, HLLTKWatchRecord] = {}
        for eos_id_raw, raw in stored.items():
            if not isinstance(raw, Mapping):
                continue
            eos_id = str(raw.get("eos_id") or eos_id_raw).strip()
            expires_at = cls._optional_int(raw.get("expires_at"))
            if not eos_id or expires_at is None or expires_at <= now:
                continue
            try:
                starts_at = float(raw.get("starts_at", 0))
            except (TypeError, ValueError, OverflowError):
                starts_at = 0.0
            parsed[eos_id] = HLLTKWatchRecord(
                eos_id=eos_id,
                player_name=cls._safe_plain_text(raw.get("player_name", "Unknown"), 100),
                expires_at=expires_at,
                starts_at=starts_at,
            )
        return parsed

    @classmethod
    def _parse_alerts(cls, stored: object) -> dict[int, HLLTKAlertRecord]:
        if not isinstance(stored, Mapping):
            return {}
        parsed: dict[int, HLLTKAlertRecord] = {}
        for message_id_raw, raw in stored.items():
            if not isinstance(raw, Mapping):
                continue
            message_id = cls._optional_id(raw.get("message_id") or message_id_raw)
            channel_id = cls._optional_id(raw.get("channel_id"))
            expires_at = cls._optional_int(raw.get("expires_at"))
            eos_id = str(raw.get("eos_id", "")).strip()
            if message_id is None or channel_id is None or expires_at is None or not eos_id:
                continue
            status = str(raw.get("status", "active"))
            if status == "processing":
                status = "active"
            if status not in {"active", "watching", "resolved", "closed"}:
                status = "active"
            parsed[message_id] = HLLTKAlertRecord(
                message_id=message_id,
                channel_id=channel_id,
                eos_id=eos_id,
                player_name=cls._safe_plain_text(raw.get("player_name", "Unknown"), 100),
                threshold_count=cls._bounded_int(raw.get("threshold_count"), 1, 100, 1),
                watch_duration_minutes=cls._bounded_int(
                    raw.get("watch_duration_minutes"),
                    1,
                    90,
                    15,
                ),
                expires_at=expires_at,
                status=status,
                default_action_at=cls._optional_int(raw.get("default_action_at"))
                or expires_at - cls.ACTION_WINDOW_SECONDS + cls.DEFAULT_ACTION_SECONDS,
                warning_sent=bool(raw.get("warning_sent", False)),
                decision=cls._safe_plain_text(raw.get("decision", ""), 1000),
            )
        return parsed

    def _active_watch(self, guild_id: int, eos_id: str) -> HLLTKWatchRecord | None:
        watch = self._watches.get(guild_id, {}).get(eos_id)
        if watch is None:
            return None
        if watch.expires_at <= int(discord.utils.utcnow().timestamp()):
            return None
        return watch

    def _record_team_kill(
        self,
        guild_id: int,
        eos_id: str,
        occurred_at: datetime,
    ) -> int:
        timestamp = occurred_at.timestamp()
        window = self._windows.setdefault(guild_id, {}).setdefault(eos_id, deque())
        cutoff = timestamp - self.WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        window.append(timestamp)
        return len(window)

    def _clear_window(self, guild_id: int, eos_id: str) -> None:
        guild_windows = self._windows.get(guild_id)
        if guild_windows is not None:
            guild_windows.pop(eos_id, None)

    def _mark_seen(self, guild_id: int, fingerprint: str) -> bool:
        seen = self._seen.setdefault(guild_id, OrderedDict())
        if fingerprint in seen:
            return False
        seen[fingerprint] = None
        while len(seen) > self.MAX_SEEN_EVENTS:
            seen.popitem(last=False)
        return True

    def _enqueue(self, guild_id: int, event: HLLTeamKillEvent) -> None:
        queue = self._queues.setdefault(guild_id, deque())
        if len(queue) >= self.MAX_QUEUE_SIZE:
            queue.popleft()
            log.warning("HLL TK-watch queue overflowed for guild %s", guild_id)
        queue.append(event)

    def _record_process_failure(self, guild_id: int, exc: Exception) -> None:
        failures = self._failure_counts.get(guild_id, 0) + 1
        self._failure_counts[guild_id] = failures
        delay = min(self.MAX_RETRY_SECONDS, 2 ** min(failures, 6))
        self._next_process_at[guild_id] = time.monotonic() + delay
        log.warning(
            "Could not deliver HLL TK-watch alert for guild %s; retrying in %s seconds: %s",
            guild_id,
            delay,
            exc,
        )

    def _reset_transient_guild(self, guild_id: int) -> None:
        self._queues.pop(guild_id, None)
        self._seen.pop(guild_id, None)
        self._windows.pop(guild_id, None)
        self._failure_counts.pop(guild_id, None)
        self._next_process_at.pop(guild_id, None)
        self._next_offline_check_at.pop(guild_id, None)

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _alert_lock(self, message_id: int) -> asyncio.Lock:
        return self._alert_locks.setdefault(message_id, asyncio.Lock())

    def _unregister_view(self, message_id: int) -> None:
        view = self._registered_views.pop(message_id, None)
        if view is not None:
            view.stop()

    @staticmethod
    def _safe_plain_text(value: object, limit: int) -> str:
        return str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()[:limit]

    @staticmethod
    def _safe_embed_text(value: object, limit: int) -> str:
        text = discord.utils.escape_mentions(str(value).replace("\x00", "").strip())
        text = discord.utils.escape_markdown(text, as_needed=False)
        return text[:limit] or "Unknown"

    @staticmethod
    def _safe_code_text(value: object, limit: int) -> str:
        return str(value).replace("`", "'").replace("\x00", "").strip()[:limit] or "Unknown"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _optional_id(cls, value: object) -> int | None:
        parsed = cls._optional_int(value)
        return parsed if parsed is not None and parsed > 0 else None

    @classmethod
    def _bounded_int(
        cls,
        value: object,
        minimum: int,
        maximum: int,
        default: int,
    ) -> int:
        parsed = cls._optional_int(value)
        if parsed is None:
            return default
        return max(minimum, min(maximum, parsed))


class HLLTKWatchCommandsMixin:
    """Authorized configuration command for automated team-kill alerts."""

    @HLLVN_COMMAND_GROUP.command(
        name="tkwatch",
        description="Configure threshold-based HLL team-kill alerts and staff actions.",
    )
    @app_commands.describe(
        toggle="Enable or disable team-kill watch.",
        channel="Text channel that should receive team-kill alerts.",
        threshold_per_min="Team kills in a rolling minute that trigger an alert.",
        watch_duration="Minutes to watch a player after Warn & Watch (1-90).",
        exclude_commander="Ignore players whose current in-game role is Commander.",
        role="Role to ping for each threshold alert; omit for no role ping.",
    )
    @app_commands.choices(
        toggle=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    @app_commands.guild_only()
    async def hllvn_tk_watch(
        self,
        interaction: discord.Interaction,
        toggle: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
        threshold_per_min: app_commands.Range[int, 1, 100] = 3,
        watch_duration: app_commands.Range[int, 1, 90] = 15,
        exclude_commander: bool = True,
        role: discord.Role | None = None,
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
            await interaction.followup.send("This command can only be used in a server.")
            return

        enabled = toggle.value == "enable"
        if not enabled:
            await self.tk_watch.disable(guild)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="HLL VN Team-Kill Watch",
                    description="Team-kill alerts are disabled. Pending alerts and active watches were cleared.",
                    color=discord.Color.orange(),
                    timestamp=discord.utils.utcnow(),
                )
            )
            return

        if channel is None:
            await interaction.followup.send("Choose a text channel when enabling TK watch.")
            return

        if role is not None and (role.guild.id != guild.id or role.is_default()):
            await interaction.followup.send("Choose a role in this server other than @everyone.")
            return

        bot_member = guild.me
        if bot_member is not None:
            permissions = channel.permissions_for(bot_member)
            if not permissions.send_messages or not permissions.embed_links:
                await interaction.followup.send(
                    "I need permission to send messages and embeds in that channel."
                )
                return
            if role is not None and not role.mentionable and not permissions.mention_everyone:
                await interaction.followup.send(
                    "Make that role mentionable or give me permission to mention roles in the alert channel."
                )
                return

        try:
            await self.kill_feed.test_connection(guild)
        except (ValueError, KillFeedConnectionTestError) as exc:
            await interaction.followup.send(str(exc))
            return
        except RuntimeError:
            log.exception("HLL team-kill watch is unavailable")
            await interaction.followup.send(
                "The `hllrcon` dependency is unavailable. Ask the bot owner to update "
                "the cog dependencies and restart Red."
            )
            return

        await self.tk_watch.configure(
            guild,
            channel_id=channel.id,
            threshold_per_min=int(threshold_per_min),
            watch_duration_minutes=int(watch_duration),
            exclude_commander=exclude_commander,
            role_id=role.id if role is not None else None,
        )
        embed = discord.Embed(
            title="HLL VN Team-Kill Watch",
            description=f"Team-kill alerts will be sent to {channel.mention}.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Role ping",
            value=role.mention if role is not None else "None",
            inline=True,
        )
        embed.add_field(
            name="Threshold",
            value=f"{threshold_per_min} team kill(s) in a rolling minute",
            inline=False,
        )
        embed.add_field(
            name="Warn & Watch duration",
            value=f"{watch_duration} minute(s)",
            inline=True,
        )
        embed.add_field(
            name="Commander handling",
            value="Ignored" if exclude_commander else "Included and identified in alerts",
            inline=True,
        )
        embed.set_footer(text="Automatic warning at threshold; Warn & Watch after 5 minutes; Forgive within 15 minutes.")
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
