"""Timed HLL: Vietnam VIP grants managed through the shared RCON client."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_database import HLLDatabaseError
from .kill_feed import KillFeedConnectionTestError

log = logging.getLogger("red.BattleMetric.hll_vip")


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


class HLLVIPModule:
    """Add VIPs immediately and remove them after a persisted expiration."""

    EXPIRY_INTERVAL_SECONDS = 60
    MIN_DURATION_SECONDS = 60
    MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
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

    def __init__(self, cog: Any):
        self.cog = cog
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(hll_vip_grants=[])

    async def start(self) -> None:
        if not self.expiry_worker.is_running():
            self.expiry_worker.start()

    def stop(self) -> None:
        self.expiry_worker.cancel()

    async def add_vip(
        self,
        guild: discord.Guild,
        *,
        eos_id: str,
        description: str,
        duration_seconds: int,
        granted_by: int,
        discord_id: int | None,
    ) -> HLLVIPGrant:
        normalized_eos_id = self.normalize_eos_id(eos_id)
        if normalized_eos_id is None:
            raise ValueError("Provide a valid 17-digit or 32-character EOS ID.")
        if not self.MIN_DURATION_SECONDS <= duration_seconds <= self.MAX_DURATION_SECONDS:
            raise ValueError("The VIP duration must be between 1 minute and 365 days.")

        eos_id = normalized_eos_id
        expires_at = int(discord.utils.utcnow().timestamp()) + duration_seconds
        grant = HLLVIPGrant(
            eos_id=eos_id,
            expires_at=expires_at,
            granted_by=granted_by,
            discord_id=discord_id,
        )

        async with self._guild_lock(guild.id):
            await self.cog.kill_feed.execute_rcon(
                guild,
                "AddVip request",
                lambda client: client.add_vip(eos_id, description),
            )
            grants = await self._get_grants(guild)
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
                except Exception as exc:
                    retained.append(grant)
                    log.warning(
                        "Will retry expired HLL VIP removal for guild %s and EOS %s: %s",
                        guild.id,
                        grant.eos_id,
                        exc,
                    )
                else:
                    changed = True
                    log.info(
                        "Removed expired HLL VIP for guild %s and EOS %s",
                        guild.id,
                        grant.eos_id,
                    )

            if changed:
                await self._set_grants(guild, retained)

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
    """Restricted slash commands for HLL: Vietnam server administration."""

    hllvn = app_commands.Group(
        name="hllvn",
        description="Manage the HLL: Vietnam server.",
    )

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
            await interaction.followup.send("This command can only be used in a server.")
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
