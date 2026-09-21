"""Automatic HLL: Vietnam fourth-point and HQ territory protection."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError
from .seeding_rules import (
    SeedingOffender,
    SeedingRuleError,
    find_fourth_point_offenders,
    find_hq_offenders,
    get_game_mode_id,
)

log = logging.getLogger("red.BattleMetric.seeding")


@dataclass(frozen=True)
class HLLSeedingSettings:
    enabled: bool
    min_players: int
    penalty_type: str

    def to_config(self) -> dict[str, bool | int | str]:
        return {
            "enabled": self.enabled,
            "min_players": self.min_players,
            "penalty_type": self.penalty_type,
        }


@dataclass(frozen=True)
class HLLHQProtectionSettings:
    enabled: bool
    penalty_type: str

    def to_config(self) -> dict[str, bool | str]:
        return {
            "enabled": self.enabled,
            "penalty_type": self.penalty_type,
        }


@dataclass(frozen=True)
class HLLSeedingInspection:
    player_count: int
    game_mode: str
    map_name: str
    offenders: tuple[SeedingOffender, ...]


@dataclass
class _ViolationState:
    offender: SeedingOffender
    first_seen: float
    last_seen: float
    last_warning: float | None = None
    punished: bool = False


class HLLSeedingModule:
    """Poll player positions and enforce the configured fourth-point rule."""

    PENALTY_WARNING = "warning_to_punish"
    PENALTY_PUNISH = "punish"
    VALID_PENALTIES = frozenset({PENALTY_WARNING, PENALTY_PUNISH})

    DEFAULT_MIN_PLAYERS = 40
    STATUS_SCAN_INTERVAL_SECONDS = 60
    POSITION_SCAN_INTERVAL_SECONDS = 3
    WORKER_INTERVAL_SECONDS = 1
    WARNING_INTERVAL_SECONDS = 1
    WARNING_GRACE_SECONDS = 15
    MAX_ACTIONS_PER_TICK = 20
    MAX_RETRY_SECONDS = 60

    _EMPTY_SETTINGS: ClassVar[dict[str, bool | int | str]] = {
        "enabled": False,
        "min_players": DEFAULT_MIN_PLAYERS,
        "penalty_type": PENALTY_WARNING,
    }
    _EMPTY_HQ_SETTINGS: ClassVar[dict[str, bool | str]] = {
        "enabled": False,
        "penalty_type": PENALTY_WARNING,
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._violations: dict[int, dict[str, _ViolationState]] = {}
        self._hq_violations: dict[int, dict[str, _ViolationState]] = {}
        self._sessions: dict[int, Any] = {}
        self._active_guilds: set[int] = set()
        self._hq_active_guilds: set[int] = set()
        self._next_status_scan_at: dict[int, float] = {}
        self._next_position_scan_at: dict[int, float] = {}
        self._position_failure_counts: dict[int, int] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(
            hll_seeding=dict(self._EMPTY_SETTINGS),
            hll_hq_protection=dict(self._EMPTY_HQ_SETTINGS),
        )

    async def start(self) -> None:
        if not self.enforcement_worker.is_running():
            self.enforcement_worker.start()

    def stop(self) -> None:
        self.enforcement_worker.cancel()
        self._violations.clear()
        self._hq_violations.clear()
        self._sessions.clear()
        self._active_guilds.clear()
        self._hq_active_guilds.clear()
        self._next_status_scan_at.clear()
        self._next_position_scan_at.clear()
        self._position_failure_counts.clear()

    async def get_settings(self, guild: discord.Guild) -> HLLSeedingSettings:
        raw = await self.cog.config.guild(guild).hll_seeding()
        if not isinstance(raw, Mapping):
            raw = self._EMPTY_SETTINGS
        min_players = self._bounded_int(
            raw.get("min_players"),
            default=self.DEFAULT_MIN_PLAYERS,
            minimum=1,
            maximum=100,
        )
        penalty_type = str(raw.get("penalty_type", self.PENALTY_WARNING))
        if penalty_type not in self.VALID_PENALTIES:
            penalty_type = self.PENALTY_WARNING
        return HLLSeedingSettings(
            enabled=bool(raw.get("enabled", False)),
            min_players=min_players,
            penalty_type=penalty_type,
        )

    async def get_hq_settings(self, guild: discord.Guild) -> HLLHQProtectionSettings:
        raw = await self.cog.config.guild(guild).hll_hq_protection()
        if not isinstance(raw, Mapping):
            raw = self._EMPTY_HQ_SETTINGS
        penalty_type = str(raw.get("penalty_type", self.PENALTY_WARNING))
        if penalty_type not in self.VALID_PENALTIES:
            penalty_type = self.PENALTY_WARNING
        return HLLHQProtectionSettings(
            enabled=bool(raw.get("enabled", False)),
            penalty_type=penalty_type,
        )

    async def configure(
        self,
        guild: discord.Guild,
        *,
        enabled: bool,
        min_players: int,
        penalty_type: str,
    ) -> HLLSeedingSettings:
        if not 1 <= min_players <= 100:
            raise ValueError("Minimum players must be between 1 and 100.")
        if penalty_type not in self.VALID_PENALTIES:
            raise ValueError("Choose warning-to-punish or immediate punishment.")
        settings = HLLSeedingSettings(enabled, min_players, penalty_type)
        await self.cog.config.guild(guild).hll_seeding.set(settings.to_config())
        self.reset_guild(guild.id)
        return settings

    async def configure_hq(
        self,
        guild: discord.Guild,
        *,
        enabled: bool,
        penalty_type: str,
    ) -> HLLHQProtectionSettings:
        if penalty_type not in self.VALID_PENALTIES:
            raise ValueError("Choose warning-to-punish or immediate punishment.")
        settings = HLLHQProtectionSettings(enabled, penalty_type)
        await self.cog.config.guild(guild).hll_hq_protection.set(settings.to_config())
        self.reset_guild(guild.id)
        return settings

    def reset_guild(self, guild_id: int) -> None:
        self._violations.pop(guild_id, None)
        self._hq_violations.pop(guild_id, None)
        self._sessions.pop(guild_id, None)
        self._active_guilds.discard(guild_id)
        self._hq_active_guilds.discard(guild_id)
        self._next_status_scan_at.pop(guild_id, None)
        self._next_position_scan_at.pop(guild_id, None)
        self._position_failure_counts.pop(guild_id, None)

    async def inspect_server(self, guild: discord.Guild) -> HLLSeedingInspection:
        return await self._inspect_server(guild, find_fourth_point_offenders)

    async def inspect_hq_server(self, guild: discord.Guild) -> HLLSeedingInspection:
        return await self._inspect_server(guild, find_hq_offenders)

    async def _inspect_server(
        self,
        guild: discord.Guild,
        rule: Any,
    ) -> HLLSeedingInspection:
        async def operation(client: Any) -> tuple[Any, Any]:
            session = await client.get_server_session()
            players = await client.get_players()
            return session, players

        session, response = await self.cog.kill_feed.execute_rcon(
            guild,
            "seeding state requests",
            operation,
        )
        players = tuple(getattr(response, "players", ()))
        game_mode = get_game_mode_id(session)
        offenders = rule(session, players)
        reported_count = self._bounded_int(
            getattr(session, "player_count", 0),
            default=0,
            minimum=0,
            maximum=1000,
        )
        return HLLSeedingInspection(
            player_count=max(reported_count, len(players)),
            game_mode=game_mode,
            map_name=str(getattr(session, "map_name", "Unknown map")),
            offenders=offenders,
        )

    async def _inspect_session(self, guild: discord.Guild) -> Any:
        return await self.cog.kill_feed.execute_rcon(
            guild,
            "seeding status request",
            lambda client: client.get_server_session(),
        )

    async def _inspect_players(self, guild: discord.Guild) -> tuple[Any, ...]:
        response = await self.cog.kill_feed.execute_rcon(
            guild,
            "seeding player-position request",
            lambda client: client.get_players(),
        )
        return tuple(getattr(response, "players", ()))

    @tasks.loop(seconds=WORKER_INTERVAL_SECONDS)
    async def enforcement_worker(self) -> None:
        try:
            await self._enforce_all_guilds()
        except Exception:
            log.exception("Unhandled error in the HLL: Vietnam seeding worker")

    @enforcement_worker.before_loop
    async def before_enforcement_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _enforce_all_guilds(self) -> None:
        coroutines = []
        for guild in self.cog.bot.guilds:
            settings = await self.get_settings(guild)
            hq_settings = await self.get_hq_settings(guild)
            if settings.enabled or hq_settings.enabled:
                coroutines.append(
                    self._enforce_guild(guild, settings, hq_settings)
                )
            else:
                self.reset_guild(guild.id)
        if coroutines:
            await asyncio.gather(*coroutines)

    async def _enforce_guild(
        self,
        guild: discord.Guild,
        settings: HLLSeedingSettings,
        hq_settings: HLLHQProtectionSettings,
    ) -> None:
        now = time.monotonic()
        if now >= self._next_status_scan_at.get(guild.id, 0):
            try:
                session = await self._inspect_session(guild)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one guild's RCON endpoint
                self._record_status_failure(guild.id, exc)
                return

            now = time.monotonic()
            self._sessions[guild.id] = session
            self._violations.pop(guild.id, None)
            self._hq_violations.pop(guild.id, None)
            self._next_status_scan_at[guild.id] = (
                now + self.STATUS_SCAN_INTERVAL_SECONDS
            )
            player_count = self._bounded_int(
                getattr(session, "player_count", 0),
                default=0,
                minimum=0,
                maximum=1000,
            )
            warfare = get_game_mode_id(session) == "warfare"
            seeding_active = (
                settings.enabled
                and warfare
                and player_count <= settings.min_players
            )
            hq_active = hq_settings.enabled and warfare

            if not seeding_active:
                self._active_guilds.discard(guild.id)
            if not hq_active:
                self._hq_active_guilds.discard(guild.id)

            if seeding_active or hq_active:
                # Resolve geometry before enforcement starts. An unknown map
                # must fail closed instead of applying old or guessed sectors.
                try:
                    if seeding_active:
                        find_fourth_point_offenders(session, ())
                    if hq_active:
                        find_hq_offenders(session, ())
                except SeedingRuleError as exc:
                    self._record_status_failure(guild.id, exc)
                    return
                if seeding_active:
                    self._active_guilds.add(guild.id)
                if hq_active:
                    self._hq_active_guilds.add(guild.id)
                self._next_position_scan_at.setdefault(guild.id, 0)
            else:
                self._next_position_scan_at.pop(guild.id, None)

        if (
            guild.id not in self._active_guilds
            and guild.id not in self._hq_active_guilds
        ):
            return
        session = self._sessions.get(guild.id)
        if session is None:
            return

        fresh_scan = False
        if now >= self._next_position_scan_at.get(guild.id, 0):
            fresh_scan = True
            try:
                players = await self._inspect_players(guild)
                offenders = (
                    find_fourth_point_offenders(session, players)
                    if guild.id in self._active_guilds
                    else ()
                )
                hq_offenders = (
                    find_hq_offenders(session, players)
                    if guild.id in self._hq_active_guilds
                    else ()
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one guild's RCON endpoint
                self._record_position_failure(guild.id, exc)
                return

            now = time.monotonic()
            self._position_failure_counts.pop(guild.id, None)
            self._next_position_scan_at[guild.id] = (
                now + self.POSITION_SCAN_INTERVAL_SECONDS
            )
            self._update_violations(guild.id, offenders, now)
            self._update_hq_violations(guild.id, hq_offenders, now)

        if guild.id in self._active_guilds:
            await self._apply_actions(guild, settings, now, fresh_scan=fresh_scan)
        if guild.id in self._hq_active_guilds:
            await self._apply_hq_actions(
                guild,
                hq_settings,
                now,
                fresh_scan=fresh_scan,
            )

    def _update_violations(
        self,
        guild_id: int,
        offenders: tuple[SeedingOffender, ...],
        now: float,
    ) -> None:
        self._update_rule_violations(self._violations, guild_id, offenders, now)

    def _update_hq_violations(
        self,
        guild_id: int,
        offenders: tuple[SeedingOffender, ...],
        now: float,
    ) -> None:
        self._update_rule_violations(self._hq_violations, guild_id, offenders, now)

    @staticmethod
    def _update_rule_violations(
        state_store: dict[int, dict[str, _ViolationState]],
        guild_id: int,
        offenders: tuple[SeedingOffender, ...],
        now: float,
    ) -> None:
        states = state_store.setdefault(guild_id, {})
        current_ids = {offender.player_id for offender in offenders}
        for player_id in tuple(states):
            if player_id not in current_ids:
                del states[player_id]
        for offender in offenders:
            state = states.get(offender.player_id)
            if state is None:
                states[offender.player_id] = _ViolationState(offender, now, now)
            else:
                state.offender = offender
                state.last_seen = now

    async def _apply_actions(
        self,
        guild: discord.Guild,
        settings: HLLSeedingSettings,
        now: float,
        *,
        fresh_scan: bool,
    ) -> None:
        await self._apply_rule_actions(
            guild,
            penalty_type=settings.penalty_type,
            states=self._violations.get(guild.id, {}),
            now=now,
            fresh_scan=fresh_scan,
            stage="seeding enforcement requests",
            warning_message=self._warning_message,
            punishment_message=self._punishment_message(),
        )

    async def _apply_hq_actions(
        self,
        guild: discord.Guild,
        settings: HLLHQProtectionSettings,
        now: float,
        *,
        fresh_scan: bool,
    ) -> None:
        await self._apply_rule_actions(
            guild,
            penalty_type=settings.penalty_type,
            states=self._hq_violations.get(guild.id, {}),
            now=now,
            fresh_scan=fresh_scan,
            stage="HQ-protection enforcement requests",
            warning_message=self._hq_warning_message,
            punishment_message=self._hq_punishment_message(),
        )

    async def _apply_rule_actions(
        self,
        guild: discord.Guild,
        *,
        penalty_type: str,
        states: dict[str, _ViolationState],
        now: float,
        fresh_scan: bool,
        stage: str,
        warning_message: Any,
        punishment_message: str,
    ) -> None:
        actions: list[tuple[str, _ViolationState, str]] = []
        for state in sorted(states.values(), key=lambda item: item.first_seen):
            if state.punished:
                continue
            elapsed = now - state.first_seen
            should_punish = penalty_type == self.PENALTY_PUNISH or (
                fresh_scan and elapsed >= self.WARNING_GRACE_SECONDS
            )
            if should_punish:
                actions.append(("punish", state, punishment_message))
            elif (
                penalty_type == self.PENALTY_WARNING
                and elapsed < self.WARNING_GRACE_SECONDS
                and (
                    state.last_warning is None
                    or now - state.last_warning >= self.WARNING_INTERVAL_SECONDS
                )
            ):
                remaining = max(1, math.ceil(self.WARNING_GRACE_SECONDS - elapsed))
                actions.append(("warning", state, warning_message(remaining)))
            if len(actions) >= self.MAX_ACTIONS_PER_TICK:
                break

        if not actions:
            return

        async def operation(client: Any) -> list[bool]:
            results = []
            for action, state, message in actions:
                if action == "warning":
                    await client.message_player(state.offender.player_id, message)
                    results.append(True)
                else:
                    result = await client.kill_player(state.offender.player_id, message)
                    results.append(bool(result))
            return results

        try:
            await self.cog.kill_feed.execute_rcon(
                guild,
                stage,
                operation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry eligible actions on the next tick
            log.warning(
                "Could not apply HLL protection actions for guild %s during %s: %s",
                guild.id,
                stage,
                exc,
            )
            return

        for action, state, _ in actions:
            if action == "warning":
                state.last_warning = now
            else:
                # A false result means the player was already dead or left; do not
                # hammer RCON until a later scan observes them leave the sector.
                state.punished = True

    def _record_status_failure(self, guild_id: int, exc: Exception) -> None:
        self._violations.pop(guild_id, None)
        self._hq_violations.pop(guild_id, None)
        self._sessions.pop(guild_id, None)
        self._active_guilds.discard(guild_id)
        self._hq_active_guilds.discard(guild_id)
        self._next_position_scan_at.pop(guild_id, None)
        self._position_failure_counts.pop(guild_id, None)
        self._next_status_scan_at[guild_id] = (
            time.monotonic() + self.STATUS_SCAN_INTERVAL_SECONDS
        )
        log.warning(
            "Could not inspect HLL seeding status for guild %s; retrying in %s seconds: %s",
            guild_id,
            self.STATUS_SCAN_INTERVAL_SECONDS,
            exc,
        )

    def _record_position_failure(self, guild_id: int, exc: Exception) -> None:
        self._violations.pop(guild_id, None)
        self._hq_violations.pop(guild_id, None)
        failure_count = self._position_failure_counts.get(guild_id, 0) + 1
        self._position_failure_counts[guild_id] = failure_count
        retry_delay = min(
            self.MAX_RETRY_SECONDS,
            self.POSITION_SCAN_INTERVAL_SECONDS * (2 ** min(failure_count - 1, 5)),
        )
        self._next_position_scan_at[guild_id] = time.monotonic() + retry_delay
        log.warning(
            "Could not inspect HLL seeding positions for guild %s; retrying in %s seconds: %s",
            guild_id,
            retry_delay,
            exc,
        )

    @staticmethod
    def _warning_message(remaining: int) -> str:
        return (
            "SEEDING RULE: Leave the enemy fourth capture sector. "
            f"You will be punished in {remaining} second{'s' if remaining != 1 else ''}."
        )

    @staticmethod
    def _punishment_message() -> str:
        return "SEEDING RULE: Enemy fourth-point capture is locked during seeding."

    @staticmethod
    def _hq_warning_message(remaining: int) -> str:
        return (
            "HQ PROTECTION: Leave the enemy HQ sector. "
            f"You will be punished in {remaining} second{'s' if remaining != 1 else ''}."
        )

    @staticmethod
    def _hq_punishment_message() -> str:
        return "HQ PROTECTION: Enemy entry into this locked HQ sector is prohibited."

    @staticmethod
    def _bounded_int(
        value: object,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return min(maximum, max(minimum, parsed))


class HLLSeedingCommandsMixin:
    """Restricted configuration commands for HLL territory protection."""

    @HLLVN_COMMAND_GROUP.command(
        name="seeding",
        description="Configure automatic fourth-point protection while seeding.",
    )
    @app_commands.describe(
        min_players="Enforce while player count is at or below this number.",
        penalty_type="Warn for five seconds first, or punish immediately.",
        toggle="Enable or disable seeding protection.",
    )
    @app_commands.choices(
        penalty_type=[
            app_commands.Choice(
                name="Warning for 5 seconds, then punish",
                value=HLLSeedingModule.PENALTY_WARNING,
            ),
            app_commands.Choice(
                name="Punish immediately",
                value=HLLSeedingModule.PENALTY_PUNISH,
            ),
        ],
        toggle=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ],
    )
    @app_commands.guild_only()
    async def hllvn_seeding(
        self,
        interaction: discord.Interaction,
        min_players: app_commands.Range[int, 1, 100],
        penalty_type: app_commands.Choice[str],
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
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        enabled = toggle.value == "enable"
        inspection: HLLSeedingInspection | None = None
        if enabled:
            try:
                inspection = await self.seeding.inspect_server(guild)
            except (KillFeedConnectionTestError, SeedingRuleError, ValueError) as exc:
                await interaction.followup.send(
                    f"Seeding protection was not enabled: {exc}"
                )
                return
            except RuntimeError:
                log.exception("HLL seeding protection is unavailable")
                await interaction.followup.send(
                    "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                    "update the cog dependencies and restart Red."
                )
                return
            except Exception:
                log.exception("Unexpected HLL seeding setup failure for guild %s", guild.id)
                await interaction.followup.send(
                    "Seeding protection could not inspect the server. Ask the bot owner "
                    "to check the Red service log."
                )
                return

        settings = await self.seeding.configure(
            guild,
            enabled=enabled,
            min_players=min_players,
            penalty_type=penalty_type.value,
        )
        embed = discord.Embed(
            title="HLL VN Seeding Protection",
            color=discord.Color.green() if enabled else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Status",
            value="Enabled" if enabled else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Player threshold",
            value=f"At or below `{settings.min_players}`",
            inline=True,
        )
        embed.add_field(
            name="Penalty",
            value=(
                "Warn for 5 seconds, then punish"
                if settings.penalty_type == HLLSeedingModule.PENALTY_WARNING
                else "Punish immediately"
            ),
            inline=False,
        )
        if enabled and inspection is not None:
            runtime_status = (
                "Active now"
                if inspection.game_mode == "warfare"
                and inspection.player_count <= settings.min_players
                else "Standing by"
            )
            embed.add_field(
                name="Current server",
                value=(
                    f"{runtime_status} - `{inspection.player_count}` players - "
                    f"{inspection.map_name} ({inspection.game_mode.title()})"
                ),
                inline=False,
            )
        embed.set_footer(
            text="Enforcement automatically suspends above the threshold and outside Warfare."
        )
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @HLLVN_COMMAND_GROUP.command(
        name="hqprotection",
        description="Protect each team's locked HQ sector from enemy spawn killing.",
    )
    @app_commands.describe(
        penalty_type="Warn for five seconds first, or punish immediately.",
        toggle="Enable or disable HQ protection.",
    )
    @app_commands.choices(
        penalty_type=[
            app_commands.Choice(
                name="Warning for 5 seconds, then punish",
                value=HLLSeedingModule.PENALTY_WARNING,
            ),
            app_commands.Choice(
                name="Punish immediately",
                value=HLLSeedingModule.PENALTY_PUNISH,
            ),
        ],
        toggle=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ],
    )
    @app_commands.guild_only()
    async def hllvn_hqprotection(
        self,
        interaction: discord.Interaction,
        penalty_type: app_commands.Choice[str],
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
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        enabled = toggle.value == "enable"
        inspection: HLLSeedingInspection | None = None
        if enabled:
            try:
                inspection = await self.seeding.inspect_hq_server(guild)
            except (KillFeedConnectionTestError, SeedingRuleError, ValueError) as exc:
                await interaction.followup.send(
                    f"HQ protection was not enabled: {exc}"
                )
                return
            except RuntimeError:
                log.exception("HLL HQ protection is unavailable")
                await interaction.followup.send(
                    "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                    "update the cog dependencies and restart Red."
                )
                return
            except Exception:
                log.exception("Unexpected HLL HQ setup failure for guild %s", guild.id)
                await interaction.followup.send(
                    "HQ protection could not inspect the server. Ask the bot owner "
                    "to check the Red service log."
                )
                return

        settings = await self.seeding.configure_hq(
            guild,
            enabled=enabled,
            penalty_type=penalty_type.value,
        )
        embed = discord.Embed(
            title="HLL VN HQ Protection",
            color=discord.Color.green() if enabled else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Status",
            value="Enabled" if enabled else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Penalty",
            value=(
                "Warn for 5 seconds, then punish"
                if settings.penalty_type == HLLSeedingModule.PENALTY_WARNING
                else "Punish immediately"
            ),
            inline=False,
        )
        if enabled and inspection is not None:
            runtime_status = (
                "Active now" if inspection.game_mode == "warfare" else "Standing by"
            )
            embed.add_field(
                name="Current server",
                value=(
                    f"{runtime_status} - {inspection.map_name} "
                    f"({inspection.game_mode.title()})"
                ),
                inline=False,
            )
        embed.set_footer(
            text="Each side unlocks automatically when its enemy controls four objectives."
        )
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
