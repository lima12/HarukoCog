"""Authorized BattleMetrics and in-game HLL: Vietnam bans."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import discord
from discord import app_commands

from ..api import BattleMetricsAPIError
from .hll_database import HLLDatabaseError
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError


log = logging.getLogger("red.BattleMetric.hll_ban")


@dataclass(frozen=True)
class HLLBanResult:
    """Outcome from the two independent ban backends."""

    battlemetrics_ban_id: str | None
    battlemetrics_error: Exception | None
    rcon_succeeded: bool
    rcon_error: Exception | None

    @property
    def complete(self) -> bool:
        return self.battlemetrics_ban_id is not None and self.rcon_succeeded


@dataclass(frozen=True)
class HLLUnbanResult:
    """Outcome from independently clearing the two ban backends."""

    battlemetrics_deleted_ids: tuple[str, ...]
    battlemetrics_succeeded: bool
    battlemetrics_error: Exception | None
    rcon_removed: bool
    rcon_succeeded: bool
    rcon_error: Exception | None

    @property
    def complete(self) -> bool:
        return self.battlemetrics_succeeded and self.rcon_succeeded


class HLLBanModule:
    """Apply the same moderation decision to BattleMetrics and HLL RCON."""

    EOS_PATTERN = re.compile(r"(?:\d{17}|[0-9a-fA-F]{32})")
    DURATION_PATTERN = re.compile(
        r"^\s*(\d+)\s*(h(?:(?:our|r)s?)?|d(?:ay)?s?|w(?:eek)?s?)?\s*$",
        re.IGNORECASE,
    )
    PERMANENT_ALIASES: ClassVar[set[str]] = {
        "permanent",
        "permanently",
        "perm",
        "forever",
    }
    UNIT_HOURS: ClassVar[dict[str, int]] = {
        "h": 1,
        "d": 24,
        "w": 7 * 24,
    }
    MAX_DURATION_HOURS = 365 * 24
    MAX_REASON_LENGTH = 255

    def __init__(self, cog: Any):
        self.cog = cog

    @classmethod
    def normalize_eos_id(cls, eos_id: str) -> str | None:
        value = eos_id.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if not cls.EOS_PATTERN.fullmatch(value):
            return None
        return value.lower() if len(value) == 32 else value

    @classmethod
    def parse_duration_hours(cls, duration: str | None) -> int | None:
        """Return whole RCON hours, or ``None`` for a permanent ban."""
        if duration is None or not duration.strip():
            return None
        normalized = duration.strip().lower()
        if normalized in cls.PERMANENT_ALIASES:
            return None

        match = cls.DURATION_PATTERN.fullmatch(normalized)
        if match is None:
            raise ValueError(
                "Use `permanent`, or a duration from 1 hour through 365 days, "
                "such as `6h`, `2d`, or `1w`."
            )
        amount = int(match.group(1))
        unit_text = (match.group(2) or "d").lower()
        hours = amount * cls.UNIT_HOURS[unit_text[0]]
        if not 1 <= hours <= cls.MAX_DURATION_HOURS:
            raise ValueError(
                "Use `permanent`, or a duration from 1 hour through 365 days, "
                "such as `6h`, `2d`, or `1w`."
            )
        return hours

    @staticmethod
    def format_duration(duration_hours: int | None) -> str:
        if duration_hours is None:
            return "Permanent"
        for unit_hours, label in ((7 * 24, "week"), (24, "day"), (1, "hour")):
            if duration_hours % unit_hours == 0:
                amount = duration_hours // unit_hours
                return f"{amount} {label}{'' if amount == 1 else 's'}"
        return f"{duration_hours} hours"

    @staticmethod
    def battlemetrics_identifier_type(eos_id: str) -> str:
        if eos_id.isdigit():
            return "steamID"
        if eos_id.casefold().startswith("0002"):
            return "eosID"
        return "hllWindowsID"

    async def ban_player(
        self,
        guild: discord.Guild,
        *,
        eos_id: str,
        server_id: str,
        reason: str,
        duration_hours: int | None,
        admin_id: int,
        admin_name: str,
    ) -> HLLBanResult:
        """Attempt the BattleMetrics and RCON bans without hiding partial success."""
        battlemetrics_ban_id = None
        battlemetrics_error = None
        try:
            battlemetrics_ban_id = await self._create_battlemetrics_ban(
                eos_id=eos_id,
                server_id=server_id,
                reason=reason,
                duration_hours=duration_hours,
                admin_id=admin_id,
                admin_name=admin_name,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the RCON ban must still be attempted
            battlemetrics_error = exc
            log.warning(
                "BattleMetrics ban failed for EOS %s in guild %s: %s",
                eos_id,
                guild.id,
                exc,
                exc_info=True,
            )

        rcon_succeeded = False
        rcon_error = None
        try:
            await self.cog.kill_feed.execute_rcon(
                guild,
                "BanPlayer request",
                lambda client: client.ban_player(
                    eos_id,
                    reason,
                    admin_name,
                    duration_hours,
                ),
            )
            rcon_succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - return both backend outcomes
            rcon_error = exc
            log.warning(
                "HLL RCON ban failed for EOS %s in guild %s: %s",
                eos_id,
                guild.id,
                exc,
                exc_info=True,
            )

        return HLLBanResult(
            battlemetrics_ban_id=battlemetrics_ban_id,
            battlemetrics_error=battlemetrics_error,
            rcon_succeeded=rcon_succeeded,
            rcon_error=rcon_error,
        )

    async def unban_player(
        self,
        guild: discord.Guild,
        *,
        eos_id: str,
        server_id: str,
    ) -> HLLUnbanResult:
        """Attempt both unban backends and retain every partial outcome."""
        battlemetrics_deleted_ids: list[str] = []
        battlemetrics_succeeded = False
        battlemetrics_error = None
        try:
            matching_ids = await self._find_battlemetrics_ban_ids(
                eos_id=eos_id,
                server_id=server_id,
            )
            deletion_errors: list[Exception] = []
            for ban_id in matching_ids:
                try:
                    await self.cog.api.delete_ban(ban_id)
                    battlemetrics_deleted_ids.append(ban_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - continue removing exact matches
                    deletion_errors.append(exc)
                    log.warning(
                        "Could not delete BattleMetrics ban %s for EOS %s: %s",
                        ban_id,
                        eos_id,
                        exc,
                        exc_info=True,
                    )
            if deletion_errors:
                battlemetrics_error = BattleMetricsAPIError(
                    f"Failed to delete {len(deletion_errors)} matching ban record(s)."
                )
            else:
                battlemetrics_succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the RCON unban must still be attempted
            battlemetrics_error = exc
            log.warning(
                "BattleMetrics unban failed for EOS %s in guild %s: %s",
                eos_id,
                guild.id,
                exc,
                exc_info=True,
            )

        rcon_removed = False
        rcon_succeeded = False
        rcon_error = None
        try:
            rcon_removed = bool(
                await self.cog.kill_feed.execute_rcon(
                    guild,
                    "UnbanPlayer request",
                    lambda client: client.unban_player(eos_id),
                )
            )
            rcon_succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - return both backend outcomes
            rcon_error = exc
            log.warning(
                "HLL RCON unban failed for EOS %s in guild %s: %s",
                eos_id,
                guild.id,
                exc,
                exc_info=True,
            )

        return HLLUnbanResult(
            battlemetrics_deleted_ids=tuple(battlemetrics_deleted_ids),
            battlemetrics_succeeded=battlemetrics_succeeded,
            battlemetrics_error=battlemetrics_error,
            rcon_removed=rcon_removed,
            rcon_succeeded=rcon_succeeded,
            rcon_error=rcon_error,
        )

    async def _find_battlemetrics_ban_ids(
        self,
        *,
        eos_id: str,
        server_id: str,
    ) -> list[str]:
        """Find exact, directly server-scoped bans without touching shared bans."""
        document = await self.cog.api.list_bans(search=eos_id, page_size=100)
        matched_ids: list[str] = []
        pages_read = 0

        while True:
            pages_read += 1
            resources = document.get("data")
            if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes)):
                raise BattleMetricsAPIError(
                    "BattleMetrics returned an invalid ban-list response."
                )
            for resource in resources:
                if not isinstance(resource, Mapping) or resource.get("id") is None:
                    continue
                if not self._ban_has_identifier(resource, eos_id):
                    continue
                if not self._ban_is_directly_scoped_to_server(resource, server_id):
                    continue
                matched_ids.append(str(resource["id"]))

            links = document.get("links")
            next_link = links.get("next") if isinstance(links, Mapping) else None
            if not isinstance(next_link, str) or not next_link.strip():
                break
            if pages_read >= 20:
                raise BattleMetricsAPIError(
                    "BattleMetrics returned too many ban-search pages to verify safely."
                )
            document = await self.cog.api.get(next_link, auth=True)

        return list(dict.fromkeys(matched_ids))

    @staticmethod
    def _ban_has_identifier(resource: Mapping[str, Any], eos_id: str) -> bool:
        attributes = resource.get("attributes")
        if not isinstance(attributes, Mapping):
            return False
        identifiers = attributes.get("identifiers")
        if not isinstance(identifiers, Sequence) or isinstance(identifiers, (str, bytes)):
            return False
        expected = eos_id.casefold()
        return any(
            isinstance(identifier, Mapping)
            and isinstance(identifier.get("identifier"), str)
            and identifier["identifier"].casefold() == expected
            for identifier in identifiers
        )

    @staticmethod
    def _ban_is_directly_scoped_to_server(
        resource: Mapping[str, Any],
        server_id: str,
    ) -> bool:
        relationships = resource.get("relationships")
        if not isinstance(relationships, Mapping):
            return False
        expected = str(server_id)
        for relationship_name in ("server", "servers"):
            relationship = relationships.get(relationship_name)
            if not isinstance(relationship, Mapping):
                continue
            data = relationship.get("data")
            if isinstance(data, Mapping) and str(data.get("id")) == expected:
                return True
            if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
                if any(
                    isinstance(item, Mapping) and str(item.get("id")) == expected
                    for item in data
                ):
                    return True
        return False

    async def _create_battlemetrics_ban(
        self,
        *,
        eos_id: str,
        server_id: str,
        reason: str,
        duration_hours: int | None,
        admin_id: int,
        admin_name: str,
    ) -> str:
        identifier_type = self.battlemetrics_identifier_type(eos_id)
        relationships: dict[str, Any] = {
            "server": {"data": {"type": "server", "id": server_id}},
        }

        try:
            match_document = await self.cog.api.quick_match_player_identifiers(
                eos_id,
                (identifier_type,),
            )
        except BattleMetricsAPIError as exc:
            # A manual identifier ban is still valid when profile matching is
            # temporarily unavailable.
            log.warning(
                "Could not pre-link BattleMetrics player profile for EOS %s: %s",
                eos_id,
                exc,
            )
        else:
            player_id = self._matched_player_id(match_document, eos_id)
            if player_id is not None:
                relationships["player"] = {
                    "data": {"type": "player", "id": player_id}
                }

        expires = None
        if duration_hours is not None:
            expires = (
                datetime.now(UTC) + timedelta(hours=duration_hours)
            ).isoformat()

        document = {
            "data": {
                "type": "ban",
                "attributes": {
                    "autoAddEnabled": True,
                    "expires": expires,
                    "identifiers": [
                        {
                            "type": identifier_type,
                            "identifier": eos_id,
                            "manual": True,
                        }
                    ],
                    "nativeEnabled": None,
                    "reason": reason,
                    "note": (
                        f"Issued through Discord by {admin_name} "
                        f"(Discord ID {admin_id})."
                    ),
                },
                "relationships": relationships,
            }
        }
        response = await self.cog.api.create_ban(document)
        resource = response.get("data")
        if not isinstance(resource, Mapping) or resource.get("id") is None:
            raise BattleMetricsAPIError(
                "BattleMetrics created the ban but did not return its ban ID."
            )
        return str(resource["id"])

    @staticmethod
    def _matched_player_id(document: Mapping[str, Any], eos_id: str) -> str | None:
        resources = document.get("data")
        if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes)):
            return None
        for resource in resources:
            if not isinstance(resource, Mapping):
                continue
            attributes = resource.get("attributes")
            if not isinstance(attributes, Mapping):
                continue
            identifier = attributes.get("identifier")
            if not isinstance(identifier, str) or identifier.casefold() != eos_id.casefold():
                continue
            relationships = resource.get("relationships")
            if not isinstance(relationships, Mapping):
                continue
            player_relationship = relationships.get("player")
            if not isinstance(player_relationship, Mapping):
                continue
            player = player_relationship.get("data")
            if isinstance(player, Mapping) and player.get("id") is not None:
                return str(player["id"])
        return None


class HLLBanCommandsMixin:
    """Restricted slash commands for dual BattleMetrics/RCON bans."""

    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(
        name="ban",
        description="Ban a linked member or EOS ID on BattleMetrics and HLL RCON.",
    )
    @app_commands.describe(
        member="Linked Discord member to ban.",
        eos_id="Direct 17-digit or 32-character game account ID.",
        duration="Optional length such as 6h, 2d, or 1w. Omit for permanent.",
        reason="Required ban reason, up to 255 characters.",
    )
    @app_commands.guild_only()
    async def hllvn_ban(
        self,
        interaction: discord.Interaction,
        reason: str,
        member: discord.Member | None = None,
        eos_id: str | None = None,
        duration: str | None = None,
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

        try:
            duration_hours = self.hll_ban.parse_duration_hours(duration)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        safe_reason = " ".join(reason.split())
        if not safe_reason:
            await interaction.followup.send("Provide a ban reason.")
            return
        if len(safe_reason) > self.hll_ban.MAX_REASON_LENGTH:
            await interaction.followup.send(
                f"Keep the reason at or below {self.hll_ban.MAX_REASON_LENGTH} characters."
            )
            return

        if member is not None:
            try:
                stats = await self.hll_database.get_stats_by_discord(member.id)
            except HLLDatabaseError as exc:
                log.warning("Could not resolve linked member for an HLL ban: %s", exc)
                await interaction.followup.send(
                    "The account-link database is temporarily unavailable."
                )
                return
            except Exception:
                log.exception("Unexpected database failure while resolving an HLL ban target")
                await interaction.followup.send(
                    "The linked account could not be read because the database query failed."
                )
                return
            if stats is None:
                await interaction.followup.send(
                    "That Discord account is not linked. They must use `/link` "
                    "before being banned by mention."
                )
                return
            normalized_eos_id = stats.eos_id
            target_display = member.mention
        else:
            normalized_eos_id = self.hll_ban.normalize_eos_id(eos_id or "")
            if normalized_eos_id is None:
                await interaction.followup.send(
                    "Provide a valid 17-digit or 32-character EOS ID."
                )
                return
            target_display = f"`{normalized_eos_id}`"

        panel = await self.server_info.get_panel(guild)
        panel_server_id = panel.get("server_id")
        server_id = (
            str(panel_server_id)
            if panel_server_id
            else await self.get_default_server_id(guild)
        )
        if server_id is None:
            await interaction.followup.send(
                "Set a default BattleMetrics server with the "
                "`bm setserver <server_id>` command first."
            )
            return
        if not await self.has_api_token():
            await interaction.followup.send(
                "The BattleMetrics API token is missing. Configure it in Red's "
                "shared API-token vault first."
            )
            return
        if not self.kill_feed.is_available():
            await interaction.followup.send(
                "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                "update the cog dependencies and restart Red."
            )
            return

        await self.kill_feed.refresh_password()
        rcon_settings = await self.kill_feed.get_settings(guild)
        if not rcon_settings.get("host") or not rcon_settings.get("port"):
            await interaction.followup.send(
                "Configure the HLL RCON host and port before using linked bans."
            )
            return
        if not self.kill_feed.has_password():
            await interaction.followup.send(
                "The HLL RCON password is missing from Red's shared API-token vault."
            )
            return

        display_name = getattr(interaction.user, "display_name", str(interaction.user))
        admin_name = " ".join(str(display_name).split())[:80] or str(interaction.user.id)
        result = await self.hll_ban.ban_player(
            guild,
            eos_id=normalized_eos_id,
            server_id=server_id,
            reason=safe_reason,
            duration_hours=duration_hours,
            admin_id=interaction.user.id,
            admin_name=admin_name,
        )

        if result.complete:
            color = discord.Color.green()
            title = "HLL VN Ban Applied"
        elif result.battlemetrics_ban_id is not None or result.rcon_succeeded:
            color = discord.Color.orange()
            title = "HLL VN Ban Partially Applied"
        else:
            color = discord.Color.red()
            title = "HLL VN Ban Failed"

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Player", value=target_display, inline=False)
        embed.add_field(name="EOS ID", value=f"`{normalized_eos_id}`", inline=False)
        embed.add_field(
            name="Duration",
            value=self.hll_ban.format_duration(duration_hours),
            inline=True,
        )
        embed.add_field(name="Reason", value=safe_reason, inline=False)
        embed.add_field(
            name="BattleMetrics",
            value=(
                f"Ban created (`{result.battlemetrics_ban_id}`)."
                if result.battlemetrics_ban_id is not None
                else self._hll_ban_failure_text(result.battlemetrics_error)
            ),
            inline=False,
        )
        embed.add_field(
            name="In-game RCON",
            value=(
                "Ban applied."
                if result.rcon_succeeded
                else self._hll_ban_failure_text(result.rcon_error)
            ),
            inline=False,
        )
        if not result.complete:
            embed.set_footer(
                text=(
                    "Review the failed backend before retrying; the successful "
                    "ban was not rolled back."
                )
            )
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @hllvn.command(
        name="unban",
        description="Unban a linked member or EOS ID on BattleMetrics and HLL RCON.",
    )
    @app_commands.describe(
        member="Linked Discord member to unban.",
        eos_id="Direct 17-digit or 32-character game account ID.",
    )
    @app_commands.guild_only()
    async def hllvn_unban(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        eos_id: str | None = None,
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

        if member is not None:
            try:
                stats = await self.hll_database.get_stats_by_discord(member.id)
            except HLLDatabaseError as exc:
                log.warning("Could not resolve linked member for an HLL unban: %s", exc)
                await interaction.followup.send(
                    "The account-link database is temporarily unavailable."
                )
                return
            except Exception:
                log.exception(
                    "Unexpected database failure while resolving an HLL unban target"
                )
                await interaction.followup.send(
                    "The linked account could not be read because the database query failed."
                )
                return
            if stats is None:
                await interaction.followup.send(
                    "That Discord account is not linked. Use a direct EOS ID instead."
                )
                return
            normalized_eos_id = stats.eos_id
            target_display = member.mention
        else:
            normalized_eos_id = self.hll_ban.normalize_eos_id(eos_id or "")
            if normalized_eos_id is None:
                await interaction.followup.send(
                    "Provide a valid 17-digit or 32-character EOS ID."
                )
                return
            target_display = f"`{normalized_eos_id}`"

        panel = await self.server_info.get_panel(guild)
        panel_server_id = panel.get("server_id")
        server_id = (
            str(panel_server_id)
            if panel_server_id
            else await self.get_default_server_id(guild)
        )
        if server_id is None:
            await interaction.followup.send(
                "Set a default BattleMetrics server with the "
                "`bm setserver <server_id>` command first."
            )
            return
        if not await self.has_api_token():
            await interaction.followup.send(
                "The BattleMetrics API token is missing. Configure it in Red's "
                "shared API-token vault first."
            )
            return
        if not self.kill_feed.is_available():
            await interaction.followup.send(
                "The `hllrcon` dependency is unavailable. Ask the bot owner to "
                "update the cog dependencies and restart Red."
            )
            return

        await self.kill_feed.refresh_password()
        rcon_settings = await self.kill_feed.get_settings(guild)
        if not rcon_settings.get("host") or not rcon_settings.get("port"):
            await interaction.followup.send(
                "Configure the HLL RCON host and port before using linked unbans."
            )
            return
        if not self.kill_feed.has_password():
            await interaction.followup.send(
                "The HLL RCON password is missing from Red's shared API-token vault."
            )
            return

        result = await self.hll_ban.unban_player(
            guild,
            eos_id=normalized_eos_id,
            server_id=server_id,
        )
        if result.complete:
            color = discord.Color.green()
            title = "HLL VN Unban Completed"
        elif result.battlemetrics_succeeded or result.rcon_succeeded:
            color = discord.Color.orange()
            title = "HLL VN Unban Partially Completed"
        else:
            color = discord.Color.red()
            title = "HLL VN Unban Failed"

        if result.battlemetrics_succeeded:
            if result.battlemetrics_deleted_ids:
                visible_ids = result.battlemetrics_deleted_ids[:10]
                deleted = ", ".join(f"`{ban_id[:64]}`" for ban_id in visible_ids)
                remaining = len(result.battlemetrics_deleted_ids) - len(visible_ids)
                suffix = f" and {remaining} more" if remaining else ""
                battlemetrics_text = f"Deleted ban record(s): {deleted}{suffix}."
            else:
                battlemetrics_text = "No matching server ban was found."
        else:
            battlemetrics_text = self._hll_ban_failure_text(
                result.battlemetrics_error
            )
            if result.battlemetrics_deleted_ids:
                battlemetrics_text += (
                    f" Deleted {len(result.battlemetrics_deleted_ids)} other "
                    "matching record(s) before the failure."
                )

        if result.rcon_succeeded:
            rcon_text = (
                "Ban removed."
                if result.rcon_removed
                else "No temporary or permanent in-game ban was found."
            )
        else:
            rcon_text = self._hll_ban_failure_text(result.rcon_error)

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Player", value=target_display, inline=False)
        embed.add_field(name="EOS ID", value=f"`{normalized_eos_id}`", inline=False)
        embed.add_field(
            name="BattleMetrics",
            value=battlemetrics_text,
            inline=False,
        )
        embed.add_field(name="In-game RCON", value=rcon_text, inline=False)
        if not result.complete:
            embed.set_footer(
                text="Review the failed backend; successful removals were kept."
            )
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _hll_ban_failure_text(error: Exception | None) -> str:
        if isinstance(error, (BattleMetricsAPIError, KillFeedConnectionTestError, ValueError)):
            return f"Failed: {error}"
        if isinstance(error, RuntimeError):
            return "Failed: the required RCON dependency is unavailable."
        return "Failed. Ask the bot owner to check the Red service log."
