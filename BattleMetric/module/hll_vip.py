"""Timed HLL: Vietnam VIP grants managed through the shared RCON client."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_database import HLLDatabaseError
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError

log = logging.getLogger("red.BattleMetric.hll_vip")

try:
    from hllrcon.admin_logs import HLLVPlayerSendMessageAdminLog
    from hllrcon.responses import ForceMode
except Exception as exc:  # noqa: BLE001 - keep the rest of the cog loadable
    HLLVPlayerSendMessageAdminLog = None
    ForceMode = None
    HLLRCON_MODEL_IMPORT_ERROR: Exception | None = exc
else:
    HLLRCON_MODEL_IMPORT_ERROR = None


@dataclass(frozen=True)
class HLLVIPGrant:
    eos_id: str
    expires_at: int
    granted_by: int | None
    discord_id: int | None

    def to_config(self) -> dict[str, int | str | None]:
        return {
            "eos_id": self.eos_id,
            "expires_at": self.expires_at,
            "granted_by": self.granted_by,
            "discord_id": self.discord_id,
        }


@dataclass(frozen=True)
class HLLVIPPackage:
    number: str
    kill_cost: int
    duration_days: int


@dataclass(frozen=True)
class HLLVIPPurgeResult:
    server_total: int
    protected_count: int
    candidates: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]
    skipped_invalid: int


class HLLVIPPurchaseModal(discord.ui.Modal):
    """Text-entry package picker compatible with Red's discord.py version."""

    def __init__(self, cog: Any):
        super().__init__(title="HLLVN VIP EXCHANGE - NO REFUND!!!", timeout=300)
        self.cog = cog
        self.package = discord.ui.TextInput(
            label="Package number (1, 2, 3, or 4)",
            placeholder="1: 100/1d | 2: 1,500/15d | 3: 3,000/30d | 4: 36,500/1yr",
            min_length=1,
            max_length=12,
            required=True,
        )
        self.add_item(self.package)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._complete_hll_vip_purchase(
            interaction,
            str(self.package.value),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        log.error(
            "Unexpected HLL VIP purchase modal failure",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "The VIP purchase could not be processed. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class HLLVIPModule:
    """Add VIPs immediately and remove them after a persisted expiration."""

    EXPIRY_INTERVAL_SECONDS = 60
    MIN_DURATION_SECONDS = 60
    MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
    PURGE_REQUEST_INTERVAL_SECONDS = 2
    TEAM_SWAP_COOLDOWN_SECONDS = 90
    VIP_CACHE_SECONDS = 30
    TEAM_SWAP_COMMAND = "!changeteam"
    EOS_PATTERN = re.compile(r"(?:\d{17}|[0-9a-fA-F]{32})")
    DURATION_PATTERN = re.compile(
        r"^\s*(\d+)\s*(m(?:in(?:ute)?)?s?|h(?:(?:our|r)s?)?|d(?:ay)?s?|w(?:eek)?s?)?\s*$",
        re.IGNORECASE,
    )
    UNIT_SECONDS: ClassVar[dict[str, int]] = {
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }
    VIP_PACKAGES: ClassVar[dict[str, HLLVIPPackage]] = {
        "1": HLLVIPPackage(number="1", kill_cost=100, duration_days=1),
        "2": HLLVIPPackage(number="2", kill_cost=1500, duration_days=15),
        "3": HLLVIPPackage(number="3", kill_cost=3000, duration_days=30),
        "4": HLLVIPPackage(number="4", kill_cost=36500, duration_days=365),
    }
    PACKAGE_ALIASES: ClassVar[dict[str, str]] = {
        "1": "1",
        "100": "1",
        "1d": "1",
        "day": "1",
        "2": "2",
        "1500": "2",
        "15d": "2",
        "3": "3",
        "3000": "3",
        "30d": "3",
        "4": "4",
        "36500": "4",
        "365d": "4",
        "1y": "4",
        "1yr": "4",
        "1year": "4",
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._team_swap_enabled: set[int] = set()
        self._team_swap_cooldowns: dict[tuple[int, str], float] = {}
        self._vip_cache: dict[int, tuple[float, frozenset[str]]] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(
            hll_vip_grants=[],
            hll_vip_team_swap_enabled=False,
        )

    async def start(self) -> None:
        all_guilds = await self.cog.config.all_guilds()
        self._team_swap_enabled = {
            int(guild_id)
            for guild_id, settings in all_guilds.items()
            if isinstance(settings, Mapping)
            and bool(settings.get("hll_vip_team_swap_enabled", False))
        }
        if not self.expiry_worker.is_running():
            self.expiry_worker.start()

    def stop(self) -> None:
        self.expiry_worker.cancel()
        self._team_swap_enabled.clear()
        self._team_swap_cooldowns.clear()
        self._vip_cache.clear()

    def should_poll(self, guild_id: int) -> bool:
        """Return whether the shared admin-log poller is needed for team swaps."""
        return guild_id in self._team_swap_enabled

    async def configure_team_swap(self, guild: discord.Guild, *, enabled: bool) -> None:
        await self.cog.config.guild(guild).hll_vip_team_swap_enabled.set(enabled)
        if enabled:
            self._team_swap_enabled.add(guild.id)
        else:
            self._team_swap_enabled.discard(guild.id)
        self._clear_team_swap_state(guild.id)

    async def ingest_admin_logs(
        self,
        guild: discord.Guild,
        entries: Sequence[Any],
    ) -> None:
        """Handle VIP team-switch requests from the shared RCON admin log."""
        if not self.should_poll(guild.id) or HLLVPlayerSendMessageAdminLog is None:
            return

        now = time.monotonic()
        self._purge_team_swap_cooldowns(now)
        for entry in entries:
            if not isinstance(entry, HLLVPlayerSendMessageAdminLog):
                continue
            if str(entry.message).strip().casefold() != self.TEAM_SWAP_COMMAND:
                continue

            player_id = str(entry.player_id).strip()
            cooldown_key = (guild.id, player_id)
            if self._team_swap_cooldowns.get(cooldown_key, 0) > now:
                continue

            # Reserve the cooldown before any I/O so repeated log entries stay silent.
            self._team_swap_cooldowns[cooldown_key] = (
                now + self.TEAM_SWAP_COOLDOWN_SECONDS
            )
            await self._handle_team_swap_request(guild, player_id)

    async def _handle_team_swap_request(
        self,
        guild: discord.Guild,
        player_id: str,
    ) -> None:
        normalized_id = self.normalize_eos_id(player_id)
        if normalized_id is None:
            log.warning(
                "Ignored HLL VIP team-swap request with invalid player ID %r in guild %s",
                player_id,
                guild.id,
            )
            return

        try:
            vip_ids = await self._get_server_vip_ids(guild)
            if normalized_id not in vip_ids:
                await self.cog.kill_feed.execute_rcon(
                    guild,
                    "MessagePlayer non-VIP team-swap response",
                    lambda client: client.message_player(
                        player_id,
                        "This feature is for VIPs only.",
                    ),
                )
                return

            if ForceMode is None:
                raise RuntimeError(
                    f"hllrcon models are unavailable: {HLLRCON_MODEL_IMPORT_ERROR}"
                )
            await self.cog.kill_feed.execute_rcon(
                guild,
                "ForceTeamSwitch request",
                lambda client: client.force_team_switch(
                    player_id,
                    ForceMode.IMMEDIATE,
                ),
            )
            log.info(
                "Applied VIP team switch for player %s in guild %s",
                player_id,
                guild.id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate player requests from the poller
            log.warning(
                "Could not process VIP team switch for player %s in guild %s: %s",
                player_id,
                guild.id,
                exc,
            )

    async def _get_server_vip_ids(self, guild: discord.Guild) -> frozenset[str]:
        now = time.monotonic()
        cached = self._vip_cache.get(guild.id)
        if cached is not None and cached[0] > now:
            return cached[1]

        response = await self.cog.kill_feed.execute_rcon(
            guild,
            "GetVips request",
            lambda client: client.get_vip_users(),
        )
        entries = getattr(response, "vips", None)
        if not isinstance(entries, (list, tuple)):
            raise ValueError(  # noqa: TRY004 - invalid remote response, not caller input
                "HLL RCON returned an invalid VIP list."
            )

        vip_ids = frozenset(
            eos_id
            for entry in entries
            if (eos_id := self.normalize_eos_id(str(getattr(entry, "id", ""))))
            is not None
        )
        self._vip_cache[guild.id] = (now + self.VIP_CACHE_SECONDS, vip_ids)
        return vip_ids

    def _invalidate_vip_cache(self, guild_id: int) -> None:
        self._vip_cache.pop(guild_id, None)

    def _clear_team_swap_state(self, guild_id: int) -> None:
        self._invalidate_vip_cache(guild_id)
        for key in [key for key in self._team_swap_cooldowns if key[0] == guild_id]:
            self._team_swap_cooldowns.pop(key, None)

    def _purge_team_swap_cooldowns(self, now: float) -> None:
        for key, expires_at in list(self._team_swap_cooldowns.items()):
            if expires_at <= now:
                self._team_swap_cooldowns.pop(key, None)

    async def add_vip(
        self,
        guild: discord.Guild,
        *,
        eos_id: str,
        description: str,
        duration_seconds: int,
        granted_by: int,
        discord_id: int | None,
        extend_existing: bool = False,
    ) -> HLLVIPGrant:
        normalized_eos_id = self.normalize_eos_id(eos_id)
        if normalized_eos_id is None:
            raise ValueError("Provide a valid 17-digit or 32-character EOS ID.")
        if not self.MIN_DURATION_SECONDS <= duration_seconds <= self.MAX_DURATION_SECONDS:
            raise ValueError("The VIP duration must be between 1 minute and 365 days.")

        eos_id = normalized_eos_id
        async with self._guild_lock(guild.id):
            grants = await self._get_grants(guild)
            now = int(discord.utils.utcnow().timestamp())
            current = next(
                (stored for stored in grants if stored.eos_id == eos_id),
                None,
            )
            starts_at = (
                max(now, current.expires_at)
                if extend_existing and current is not None
                else now
            )
            grant = HLLVIPGrant(
                eos_id=eos_id,
                expires_at=starts_at + duration_seconds,
                granted_by=granted_by,
                discord_id=discord_id,
            )
            await self.cog.kill_feed.execute_rcon(
                guild,
                "AddVip request",
                lambda client: client.add_vip(eos_id, description),
            )
            self._invalidate_vip_cache(guild.id)
            grants = [stored for stored in grants if stored.eos_id != eos_id]
            grants.append(grant)
            await self._set_grants(guild, grants)

        return grant

    async def delete_user_data(self, user_id: int) -> None:
        for guild_id, guild_data in (await self.cog.config.all_guilds()).items():
            raw_grants = guild_data.get("hll_vip_grants", [])
            grants = self._parse_grants(raw_grants)
            changed = False
            redacted = []
            for grant in grants:
                granted_by = grant.granted_by
                discord_id = grant.discord_id
                if granted_by == user_id:
                    granted_by = None
                    changed = True
                if discord_id == user_id:
                    discord_id = None
                    changed = True
                redacted.append(
                    HLLVIPGrant(
                        eos_id=grant.eos_id,
                        expires_at=grant.expires_at,
                        granted_by=granted_by,
                        discord_id=discord_id,
                    )
                )
            if changed:
                await self.cog.config.guild_from_id(guild_id).hll_vip_grants.set(
                    [grant.to_config() for grant in redacted]
                )

    @classmethod
    def normalize_eos_id(cls, eos_id: str) -> str | None:
        value = eos_id.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if not cls.EOS_PATTERN.fullmatch(value):
            return None
        return value.lower() if len(value) == 32 else value

    @classmethod
    def parse_duration(cls, duration: str) -> int | None:
        match = cls.DURATION_PATTERN.fullmatch(duration)
        if match is None:
            return None
        amount = int(match.group(1))
        unit_text = (match.group(2) or "d").lower()
        unit = unit_text[0]
        seconds = amount * cls.UNIT_SECONDS[unit]
        if not cls.MIN_DURATION_SECONDS <= seconds <= cls.MAX_DURATION_SECONDS:
            return None
        return seconds

    @classmethod
    def get_vip_package(cls, value: str) -> HLLVIPPackage | None:
        normalized = value.strip().lower().replace(",", "").replace(" ", "")
        package_number = cls.PACKAGE_ALIASES.get(normalized)
        return cls.VIP_PACKAGES.get(package_number) if package_number else None

    @staticmethod
    def format_duration(seconds: int) -> str:
        for unit_seconds, suffix in (
            (7 * 24 * 60 * 60, "week"),
            (24 * 60 * 60, "day"),
            (60 * 60, "hour"),
            (60, "minute"),
        ):
            if seconds % unit_seconds == 0:
                amount = seconds // unit_seconds
                return f"{amount} {suffix}{'' if amount == 1 else 's'}"
        return f"{seconds:,} seconds"

    @tasks.loop(seconds=EXPIRY_INTERVAL_SECONDS)
    async def expiry_worker(self) -> None:
        for guild in self.cog.bot.guilds:
            try:
                await self._expire_guild(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not process HLL VIP expirations for guild %s", guild.id)

    @expiry_worker.before_loop
    async def before_expiry_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _expire_guild(self, guild: discord.Guild) -> None:
        async with self._guild_lock(guild.id):
            grants = await self._get_grants(guild)
            now = int(discord.utils.utcnow().timestamp())
            if not any(grant.expires_at <= now for grant in grants):
                return

            retained: list[HLLVIPGrant] = []
            changed = False
            for grant in grants:
                if grant.expires_at > now:
                    retained.append(grant)
                    continue
                try:
                    await self.cog.kill_feed.execute_rcon(
                        guild,
                        "RemoveVip request",
                        lambda client, eos_id=grant.eos_id: client.remove_vip(eos_id),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry independent grants later
                    retained.append(grant)
                    log.warning(
                        "Will retry expired HLL VIP removal for guild %s and EOS %s: %s",
                        guild.id,
                        grant.eos_id,
                        exc,
                    )
                else:
                    changed = True
                    self._invalidate_vip_cache(guild.id)
                    log.info(
                        "Removed expired HLL VIP for guild %s and EOS %s",
                        guild.id,
                        grant.eos_id,
                    )

            if changed:
                await self._set_grants(guild, retained)

    async def purge_unmanaged_vips(
        self,
        guild: discord.Guild,
        *,
        execute: bool,
    ) -> HLLVIPPurgeResult:
        """List or remove server VIPs that are not tracked by this cog."""
        async with self._guild_lock(guild.id):
            grants = await self._get_grants(guild)
            managed_ids = {grant.eos_id for grant in grants}
            response = await self.cog.kill_feed.execute_rcon(
                guild,
                "GetVips request",
                lambda client: client.get_vip_users(),
            )
            entries = getattr(response, "vips", None)
            if not isinstance(entries, (list, tuple)):
                raise ValueError(  # noqa: TRY004 - invalid remote response, not caller input
                    "HLL RCON returned an invalid VIP list."
                )

            server_total = len(entries)
            protected_count = 0
            skipped_invalid = 0
            candidates = []
            seen_ids = set()
            for entry in entries:
                eos_id = self.normalize_eos_id(str(getattr(entry, "id", "")))
                if eos_id is None:
                    skipped_invalid += 1
                    continue
                if eos_id in seen_ids:
                    continue
                seen_ids.add(eos_id)
                if eos_id in managed_ids:
                    protected_count += 1
                else:
                    candidates.append(eos_id)

            removed = []
            failed = []
            if execute:
                for index, eos_id in enumerate(candidates):
                    try:
                        await self.cog.kill_feed.execute_rcon(
                            guild,
                            "RemoveVip request",
                            lambda client, target=eos_id: client.remove_vip(target),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - continue independent removals
                        failed.append(eos_id)
                        log.warning(
                            "Could not purge unmanaged HLL VIP %s for guild %s: %s",
                            eos_id,
                            guild.id,
                            exc,
                        )
                    else:
                        removed.append(eos_id)
                        self._invalidate_vip_cache(guild.id)

                    if index + 1 < len(candidates):
                        await asyncio.sleep(self.PURGE_REQUEST_INTERVAL_SECONDS)

            return HLLVIPPurgeResult(
                server_total=server_total,
                protected_count=protected_count,
                candidates=tuple(candidates),
                removed=tuple(removed),
                failed=tuple(failed),
                skipped_invalid=skipped_invalid,
            )

    async def _get_grants(self, guild: discord.Guild) -> list[HLLVIPGrant]:
        return self._parse_grants(await self.cog.config.guild(guild).hll_vip_grants())

    async def _set_grants(
        self,
        guild: discord.Guild,
        grants: list[HLLVIPGrant],
    ) -> None:
        await self.cog.config.guild(guild).hll_vip_grants.set(
            [grant.to_config() for grant in grants]
        )

    @classmethod
    def _parse_grants(cls, raw_grants: object) -> list[HLLVIPGrant]:
        if not isinstance(raw_grants, list):
            return []
        grants = []
        for raw in raw_grants:
            if not isinstance(raw, Mapping):
                continue
            eos_id = cls.normalize_eos_id(str(raw.get("eos_id", "")))
            try:
                expires_at = int(raw["expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if eos_id is None or expires_at <= 0:
                continue
            grants.append(
                HLLVIPGrant(
                    eos_id=eos_id,
                    expires_at=expires_at,
                    granted_by=cls._optional_id(raw.get("granted_by")),
                    discord_id=cls._optional_id(raw.get("discord_id")),
                )
            )
        return grants

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _optional_id(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return parsed if parsed is not None and parsed > 0 else None


class HLLVIPCommandsMixin:
    """Public purchase and restricted administration commands for HLL VIPs."""

    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(
        name="allowvipteamswap",
        description="Enable or disable the VIP-only in-game !changeteam command.",
    )
    @app_commands.describe(toggle="Enable or disable VIP team switching.")
    @app_commands.choices(
        toggle=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    @app_commands.guild_only()
    async def hllvn_allowvipteamswap(
        self,
        interaction: discord.Interaction,
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
        if enabled:
            try:
                await self.kill_feed.test_connection(guild)
            except (ValueError, KillFeedConnectionTestError) as exc:
                await interaction.followup.send(str(exc))
                return
            except RuntimeError:
                log.exception("HLL VIP team switching is unavailable")
                await interaction.followup.send(
                    "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                    "update the cog dependencies and restart Red."
                )
                return

        await self.hll_vip.configure_team_swap(guild, enabled=enabled)
        embed = discord.Embed(
            title="HLL VN VIP Team Swap",
            description=(
                "VIP players can now use `!changeteam` in Team or Unit chat. "
                "The switch is immediate and kills a living soldier."
                if enabled
                else "The in-game `!changeteam` command is disabled."
            ),
            color=discord.Color.green() if enabled else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        if enabled:
            embed.set_footer(
                text="Each player has a silent 90-second request cooldown."
            )
        await interaction.followup.send(embed=embed)

    @hllvn.command(name="addvip", description="Add a timed VIP through HLL RCON.")
    @app_commands.describe(
        member="Linked Discord member to add as VIP.",
        eos_id="Direct 17-digit or 32-character game account ID.",
        duration="Grant length such as 30m, 12h, 1d, or 2w. Defaults to 1d.",
    )
    @app_commands.guild_only()
    async def hllvn_addvip(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        eos_id: str | None = None,
        duration: str = "1d",
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
            await interaction.followup.send(
                "This command can only be used in a server."
            )
            return
        if (member is None) == (eos_id is None):
            await interaction.followup.send(
                "Choose exactly one target: a linked Discord member or an EOS ID."
            )
            return

        duration_seconds = self.hll_vip.parse_duration(duration)
        if duration_seconds is None:
            await interaction.followup.send(
                "Use a duration from 1 minute through 365 days, such as `30m`, `12h`, `1d`, or `2w`."
            )
            return

        linked_discord_id = None
        if member is not None:
            try:
                stats = await self.hll_database.get_stats_by_discord(member.id)
            except HLLDatabaseError as exc:
                log.warning("Could not resolve a linked member for HLL VIP: %s", exc)
                await interaction.followup.send(
                    "The account-link database is temporarily unavailable."
                )
                return
            except Exception:
                log.exception("Unexpected database failure while resolving an HLL VIP target")
                await interaction.followup.send(
                    "The linked account could not be read because the database query failed."
                )
                return
            if stats is None:
                await interaction.followup.send(
                    "That Discord account is not linked. They must use `/link` before being added by mention."
                )
                return
            normalized_eos_id = stats.eos_id
            linked_discord_id = member.id
            target_name = member.display_name
            target_display = member.mention
        else:
            normalized_eos_id = self.hll_vip.normalize_eos_id(eos_id or "")
            if normalized_eos_id is None:
                await interaction.followup.send(
                    "Provide a valid 17-digit or 32-character EOS ID."
                )
                return
            target_name = self.hll_database.get_cached_alias(normalized_eos_id) or normalized_eos_id
            target_display = f"`{normalized_eos_id}`"

        safe_name = re.sub(r"[\x00-\x1f\x7f]+", " ", target_name).strip()[:80]
        description = f"{safe_name or normalized_eos_id} | Discord timed VIP"
        try:
            grant = await self.hll_vip.add_vip(
                guild,
                eos_id=normalized_eos_id,
                description=description,
                duration_seconds=duration_seconds,
                granted_by=interaction.user.id,
                discord_id=linked_discord_id,
            )
        except (ValueError, KillFeedConnectionTestError) as exc:
            await interaction.followup.send(str(exc))
            return
        except RuntimeError:
            log.exception("HLL VIP command is unavailable")
            await interaction.followup.send(
                "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                "update the cog dependencies and restart Red."
            )
            return
        except Exception:
            log.exception("Unexpected HLL VIP grant failure for guild %s", guild.id)
            await interaction.followup.send(
                "The VIP could not be added. Ask the bot owner to check the Red service log."
            )
            return

        embed = discord.Embed(
            title="HLL VN VIP Added",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Player", value=target_display, inline=False)
        embed.add_field(name="EOS ID", value=f"`{grant.eos_id}`", inline=False)
        embed.add_field(
            name="Duration",
            value=self.hll_vip.format_duration(duration_seconds),
            inline=True,
        )
        embed.add_field(
            name="Expires",
            value=f"<t:{grant.expires_at}:F>\n<t:{grant.expires_at}:R>",
            inline=True,
        )
        embed.set_footer(text="Expiration is managed by the bot and retried if RCON is unavailable.")
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @hllvn.command(
        name="purgevip",
        description="Remove server VIPs that are not tracked by the bot.",
    )
    @app_commands.describe(
        confirm="Set true to remove unmanaged VIPs; false performs a dry run.",
    )
    @app_commands.guild_only()
    async def hllvn_purgevip(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
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
            await interaction.followup.send(
                "This command can only be used in a server."
            )
            return

        try:
            result = await self.hll_vip.purge_unmanaged_vips(
                guild,
                execute=confirm,
            )
        except (ValueError, KillFeedConnectionTestError) as exc:
            await interaction.followup.send(str(exc))
            return
        except RuntimeError:
            log.exception("HLL VIP purge is unavailable")
            await interaction.followup.send(
                "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                "update the cog dependencies and restart Red."
            )
            return
        except Exception:
            log.exception("Unexpected HLL VIP purge failure for guild %s", guild.id)
            await interaction.followup.send(
                "The VIP purge failed. Ask the bot owner to check the Red service log."
            )
            return

        if confirm:
            embed = discord.Embed(
                title="HLL VN VIP Purge Complete",
                description="All bot-managed VIPs were preserved.",
                color=(
                    discord.Color.orange()
                    if result.failed
                    else discord.Color.green()
                ),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(
                name="Server VIPs",
                value=f"`{result.server_total}`",
                inline=True,
            )
            embed.add_field(
                name="Protected",
                value=f"`{result.protected_count}`",
                inline=True,
            )
            embed.add_field(
                name="Removed",
                value=f"`{len(result.removed)}`",
                inline=True,
            )
            embed.add_field(
                name="Failed",
                value=f"`{len(result.failed)}`",
                inline=True,
            )
            if result.failed:
                embed.add_field(
                    name="Failed EOS IDs",
                    value=self._format_purge_ids(result.failed),
                    inline=False,
                )
        else:
            embed = discord.Embed(
                title="HLL VN VIP Purge Preview",
                description="Dry run only. No VIPs were removed.",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(
                name="Server VIPs",
                value=f"`{result.server_total}`",
                inline=True,
            )
            embed.add_field(
                name="Protected",
                value=f"`{result.protected_count}`",
                inline=True,
            )
            embed.add_field(
                name="Would Remove",
                value=f"`{len(result.candidates)}`",
                inline=True,
            )
            if result.candidates:
                embed.add_field(
                    name="Unmanaged EOS IDs",
                    value=self._format_purge_ids(result.candidates),
                    inline=False,
                )
            embed.set_footer(text="Run /hllvn purgevip with confirm:true to execute.")

        if result.skipped_invalid:
            embed.add_field(
                name="Skipped Invalid Entries",
                value=f"`{result.skipped_invalid}`",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @staticmethod
    def _format_purge_ids(eos_ids: tuple[str, ...]) -> str:
        visible = eos_ids[:20]
        lines = [f"`{eos_id}`" for eos_id in visible]
        if len(eos_ids) > len(visible):
            lines.append(f"...and {len(eos_ids) - len(visible)} more")
        return "\n".join(lines)

    @hllvn.command(
        name="buyvip",
        description="Exchange confirmed kills for timed HLL VIP access.",
    )
    @app_commands.guild_only()
    async def hllvn_buyvip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        try:
            stats = await asyncio.wait_for(
                self.hll_database.get_stats_by_discord(interaction.user.id),
                timeout=2,
            )
        except TimeoutError:
            await interaction.response.send_message(
                "The account-link database took too long to respond. Please try again.",
                ephemeral=True,
            )
            return
        except HLLDatabaseError as exc:
            log.warning("Could not check an HLL VIP buyer's account link: %s", exc)
            await interaction.response.send_message(
                "The account-link database is temporarily unavailable.",
                ephemeral=True,
            )
            return
        except Exception:
            log.exception("Unexpected database failure before opening the HLL VIP modal")
            await interaction.response.send_message(
                "Your linked account could not be checked. Please try again later.",
                ephemeral=True,
            )
            return

        if stats is None:
            await interaction.response.send_message(
                "Please use `/link` to link your Discord with your HLL account first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(HLLVIPPurchaseModal(self))

    async def _complete_hll_vip_purchase(
        self,
        interaction: discord.Interaction,
        package_value: str,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This purchase must be completed in a server.")
            return

        package = self.hll_vip.get_vip_package(package_value)
        if package is None:
            await interaction.followup.send(
                "Enter package `1`, `2`, `3`, or `4`. Run `/hllvn buyvip` to try again."
            )
            return

        try:
            stats = await self.hll_database.get_stats_by_discord(interaction.user.id)
        except HLLDatabaseError as exc:
            log.warning("Could not recheck an HLL VIP buyer's account link: %s", exc)
            await interaction.followup.send(
                "The account-link database is temporarily unavailable. No kills were deducted."
            )
            return
        except Exception:
            log.exception("Unexpected database failure while rechecking an HLL VIP buyer")
            await interaction.followup.send(
                "Your linked account could not be checked. No kills were deducted."
            )
            return

        if stats is None:
            await interaction.followup.send(
                "Please use `/link` to link your Discord with your HLL account first."
            )
            return

        duration_seconds = package.duration_days * 24 * 60 * 60
        display_name = getattr(interaction.user, "display_name", str(interaction.user))
        safe_name = re.sub(r"[\x00-\x1f\x7f]+", " ", display_name).strip()[:80]
        grant = None
        remaining_kills = None
        try:
            async with self.hll_database.spend_kills(
                stats.eos_id,
                package.kill_cost,
            ) as remaining_kills:
                if remaining_kills is not None:
                    grant = await self.hll_vip.add_vip(
                        guild,
                        eos_id=stats.eos_id,
                        description=f"{safe_name or stats.eos_id} | Kill exchange VIP",
                        duration_seconds=duration_seconds,
                        granted_by=interaction.user.id,
                        discord_id=interaction.user.id,
                        extend_existing=True,
                    )
        except (ValueError, KillFeedConnectionTestError) as exc:
            await interaction.followup.send(f"{exc}\nNo kills were deducted.")
            return
        except HLLDatabaseError as exc:
            log.warning("Could not complete an HLL VIP kill exchange: %s", exc)
            await interaction.followup.send(
                "The kill exchange database update failed. No purchase was completed."
            )
            return
        except Exception:
            log.exception("Unexpected HLL VIP purchase failure for guild %s", guild.id)
            await interaction.followup.send(
                "The VIP purchase could not be completed. Ask an administrator to check the bot log."
            )
            return

        if remaining_kills is None:
            await interaction.followup.send(
                "You do not have enough confirmed kills for that transaction. "
                "Go have more fun and come back later."
            )
            return
        if grant is None:
            log.error("HLL VIP purchase completed without producing a grant for guild %s", guild.id)
            await interaction.followup.send(
                "The VIP purchase did not complete correctly. Ask an administrator to check the bot log."
            )
            return

        embed = discord.Embed(
            title="HLLVN VIP Purchase Complete",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Package",
            value=f"{package.kill_cost:,} kills for {package.duration_days} days",
            inline=False,
        )
        embed.add_field(name="Remaining Kills", value=f"{remaining_kills:,}", inline=True)
        embed.add_field(
            name="VIP Expires",
            value=f"<t:{grant.expires_at}:F>\n<t:{grant.expires_at}:R>",
            inline=True,
        )
        embed.set_footer(text="VIP exchanges are final and non-refundable.")
        await interaction.followup.send(embed=embed)
