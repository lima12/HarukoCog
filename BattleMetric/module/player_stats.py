"""Public HLL: Vietnam player statistics command."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import discord
from discord import app_commands

from ..api import BattleMetricsAPIError
from .hll_database import HLLDatabaseError, HLLPlayerStats

log = logging.getLogger("red.BattleMetric.player_stats")

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # noqa: BLE001 - keep database stats available without Pillow
    Image = None
    ImageDraw = None
    ImageFont = None
    PILLOW_IMPORT_ERROR: Exception | None = exc
else:
    PILLOW_IMPORT_ERROR = None


@dataclass(frozen=True)
class BattleMetricsPlayerProfile:
    player_id: str
    name: str | None
    time_played_seconds: int | None


@dataclass(frozen=True)
class CardTextSlot:
    position: tuple[int, int]
    max_width: int
    font_size: int
    min_font_size: int = 14


class PlayerStatsModule:
    """Resolve local combat stats and optional BattleMetrics profile data."""

    EOS_PATTERN = re.compile(r"(?:\d{17}|[0-9a-fA-F]{32})")
    EOS_IDENTIFIER_TYPES: ClassVar[tuple[str, ...]] = (
        "eosID",
        "hllWindowsID",
    )
    STEAM_IDENTIFIER_TYPES: ClassVar[tuple[str, ...]] = ("steamID",)
    PROFILE_CACHE_SECONDS = 300
    MAX_PROFILE_CACHE_SIZE = 1000
    RESOURCE_DIRECTORY = Path(__file__).resolve().parent.parent / "res"
    TEMPLATE_PATH = RESOURCE_DIRECTORY / "template.png"
    FONT_PATH = RESOURCE_DIRECTORY / "Wallpoet-Regular.ttf"
    CARD_TEXT_COLOR: ClassVar[tuple[int, int, int, int]] = (29, 31, 27, 255)
    CARD_SLOTS: ClassVar[dict[str, CardTextSlot]] = {
        "name": CardTextSlot((200, 240), 370, 28),
        "date": CardTextSlot((856, 236), 175, 26),
        "discord_id": CardTextSlot((321, 335), 700, 28),
        "eos_id": CardTextSlot((321, 425), 700, 28),
        "kills": CardTextSlot((176, 982), 330, 32),
        "deaths": CardTextSlot((176, 1118), 330, 32),
        "time_played": CardTextSlot((176, 1266), 330, 30),
    }

    def __init__(self, cog: Any):
        self.cog = cog
        self._profile_cache: OrderedDict[
            tuple[str, str | None],
            tuple[float, BattleMetricsPlayerProfile | None],
        ] = OrderedDict()
        self._profile_lock = asyncio.Lock()

    async def get_battlemetrics_profile(
        self,
        eos_id: str,
        server_id: str | None,
    ) -> BattleMetricsPlayerProfile | None:
        """Return a cached BattleMetrics name and per-server playtime."""
        cache_key = (eos_id.casefold(), server_id)
        now = time.monotonic()
        cached = self._profile_cache.get(cache_key)
        if cached is not None and now - cached[0] < self.PROFILE_CACHE_SECONDS:
            self._profile_cache.move_to_end(cache_key)
            return cached[1]

        async with self._profile_lock:
            now = time.monotonic()
            cached = self._profile_cache.get(cache_key)
            if cached is not None and now - cached[0] < self.PROFILE_CACHE_SECONDS:
                self._profile_cache.move_to_end(cache_key)
                return cached[1]

            profile = await self._fetch_battlemetrics_profile(eos_id, server_id)
            self._profile_cache[cache_key] = (time.monotonic(), profile)
            self._profile_cache.move_to_end(cache_key)
            while len(self._profile_cache) > self.MAX_PROFILE_CACHE_SIZE:
                self._profile_cache.popitem(last=False)
            return profile

    async def _fetch_battlemetrics_profile(
        self,
        eos_id: str,
        server_id: str | None,
    ) -> BattleMetricsPlayerProfile | None:
        match_document = await self.cog.api.quick_match_player_identifiers(
            eos_id,
            self.STEAM_IDENTIFIER_TYPES
            if eos_id.isdigit()
            else self.EOS_IDENTIFIER_TYPES,
        )
        player_id = self._matched_player_id(match_document, eos_id)
        if player_id is None:
            return None

        player_document = await self.cog.api.get_player(
            player_id,
            auth=True,
        )
        player = player_document.get("data")
        if not isinstance(player, Mapping):
            return BattleMetricsPlayerProfile(player_id, None, None)

        attributes = player.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        name_value = attributes.get("name")
        name = str(name_value).strip() if name_value else None
        time_played_seconds = self._time_played(player, server_id)
        if server_id is not None:
            try:
                server_document = await self.cog.api.get_player_server_information(
                    player_id,
                    server_id,
                    auth=True,
                )
            except BattleMetricsAPIError as exc:
                log.warning(
                    "Could not fetch BattleMetrics server time for player %s on server %s: %s",
                    player_id,
                    server_id,
                    exc,
                )
            else:
                direct_time = self._player_server_time(server_document)
                if direct_time is not None:
                    time_played_seconds = direct_time

        return BattleMetricsPlayerProfile(
            player_id=player_id,
            name=name,
            time_played_seconds=time_played_seconds,
        )

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
            if (
                not isinstance(identifier, str)
                or identifier.casefold() != eos_id.casefold()
            ):
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

    @staticmethod
    def _time_played(player: Mapping[str, Any], server_id: str | None) -> int | None:
        if server_id is None:
            return None
        relationships = player.get("relationships")
        if not isinstance(relationships, Mapping):
            return None

        for relationship_name in ("servers", "server"):
            relationship = relationships.get(relationship_name)
            if not isinstance(relationship, Mapping):
                continue
            resources = relationship.get("data")
            if isinstance(resources, Mapping):
                resources = [resources]
            if not isinstance(resources, Sequence) or isinstance(
                resources, (str, bytes)
            ):
                continue
            for resource in resources:
                if (
                    not isinstance(resource, Mapping)
                    or str(resource.get("id")) != server_id
                ):
                    continue
                meta = resource.get("meta")
                if not isinstance(meta, Mapping):
                    return None
                try:
                    seconds = int(meta.get("timePlayed"))
                except (TypeError, ValueError):
                    return None
                return max(0, seconds)
        return None

    @staticmethod
    def _player_server_time(document: Mapping[str, Any]) -> int | None:
        resource = document.get("data")
        if not isinstance(resource, Mapping):
            return None
        attributes = resource.get("attributes")
        if not isinstance(attributes, Mapping):
            return None
        try:
            seconds = int(attributes.get("timePlayed"))
        except (TypeError, ValueError):
            return None
        return max(0, seconds)

    @classmethod
    def normalize_eos_id(cls, eos_id: str) -> str | None:
        value = eos_id.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if cls.EOS_PATTERN.fullmatch(value):
            return value.lower() if len(value) == 32 else value
        return None

    async def get_guild_member(
        self,
        guild: discord.Guild,
        member_id: int | None,
    ) -> discord.Member | None:
        if member_id is None:
            return None
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None

    @staticmethod
    def build_embed(
        stats: HLLPlayerStats,
        *,
        alias: str | None,
        member: discord.Member | None,
        time_played_seconds: int | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="HLL VN Stat",
            color=discord.Color.dark_green(),
            timestamp=discord.utils.utcnow(),
        )
        safe_alias = (
            discord.utils.escape_markdown(alias, as_needed=False)
            if alias
            else "Unavailable"
        )
        embed.add_field(name="Name", value=safe_alias[:1024], inline=False)
        embed.add_field(
            name="Enlisted Date",
            value=PlayerStatsModule._enlisted_date(member),
            inline=True,
        )
        embed.add_field(
            name="Enlisted ID",
            value=f"`{stats.discord_id}`"
            if stats.discord_id is not None
            else "Not linked",
            inline=True,
        )
        embed.add_field(name="File Number", value=f"`{stats.eos_id}`", inline=False)
        embed.add_field(name="Confirmed Kill", value=f"`{stats.kills:,}`", inline=True)
        embed.add_field(name="Wounded Times", value=f"`{stats.deaths:,}`", inline=True)
        embed.add_field(
            name="Time in Service",
            value=PlayerStatsModule._format_duration(time_played_seconds),
            inline=True,
        )
        embed.set_footer(text="Want to track your data? Use /link")
        return embed

    async def render_card(
        self,
        stats: HLLPlayerStats,
        *,
        alias: str | None,
        member: discord.Member | None,
        time_played_seconds: int | None,
    ) -> io.BytesIO:
        """Render the supplied statistics into the bundled passport template."""
        return await asyncio.to_thread(
            self._render_card_sync,
            stats,
            alias=alias,
            member=member,
            time_played_seconds=time_played_seconds,
        )

    def _render_card_sync(
        self,
        stats: HLLPlayerStats,
        *,
        alias: str | None,
        member: discord.Member | None,
        time_played_seconds: int | None,
    ) -> io.BytesIO:
        if PILLOW_IMPORT_ERROR is not None or None in (Image, ImageDraw, ImageFont):
            raise RuntimeError("Pillow is unavailable") from PILLOW_IMPORT_ERROR
        if not self.TEMPLATE_PATH.is_file() or not self.FONT_PATH.is_file():
            raise FileNotFoundError("Player-stat card template or font is missing")

        with Image.open(self.TEMPLATE_PATH) as source:
            image = source.convert("RGBA")
        draw = ImageDraw.Draw(image)
        values = {
            "name": self._single_line(alias or "Unavailable"),
            "date": self._enlisted_date_text(member),
            "discord_id": str(stats.discord_id)
            if stats.discord_id is not None
            else "Not linked",
            "eos_id": stats.eos_id,
            "kills": f"{stats.kills:,}",
            "deaths": f"{stats.deaths:,}",
            "time_played": self._format_duration_text(time_played_seconds),
        }
        font_cache: dict[int, Any] = {}
        for field_name, slot in self.CARD_SLOTS.items():
            text, font = self._fitted_text(
                draw,
                values[field_name],
                slot,
                font_cache,
            )
            draw.text(
                slot.position,
                text,
                font=font,
                fill=self.CARD_TEXT_COLOR,
            )

        output = io.BytesIO()
        try:
            image.save(output, format="PNG", compress_level=6)
        finally:
            image.close()
        output.seek(0)
        return output

    def _fitted_text(
        self,
        draw: Any,
        text: str,
        slot: CardTextSlot,
        font_cache: dict[int, Any],
    ) -> tuple[str, Any]:
        for size in range(slot.font_size, slot.min_font_size - 1, -1):
            font = font_cache.get(size)
            if font is None:
                font = ImageFont.truetype(str(self.FONT_PATH), size=size)
                font_cache[size] = font
            bounds = draw.textbbox((0, 0), text, font=font)
            if bounds[2] - bounds[0] <= slot.max_width:
                return text, font

        font = font_cache[slot.min_font_size]
        suffix = "..."
        shortened = text
        while shortened:
            candidate = shortened.rstrip() + suffix
            bounds = draw.textbbox((0, 0), candidate, font=font)
            if bounds[2] - bounds[0] <= slot.max_width:
                return candidate, font
            shortened = shortened[:-1]
        return suffix, font

    @staticmethod
    def build_card_embed() -> discord.Embed:
        embed = discord.Embed(
            title="HLL VN Stat",
            color=discord.Color.dark_green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_image(url="attachment://hll-vn-stat.png")
        embed.set_footer(text="Want to track your data? Use /link")
        return embed

    @staticmethod
    def _single_line(value: str) -> str:
        return " ".join(str(value).split()) or "Unavailable"

    @staticmethod
    def _enlisted_date_text(member: discord.Member | None) -> str:
        if member is None:
            return "Not linked"
        if member.joined_at is None:
            return "Unavailable"
        return member.joined_at.strftime("%m/%d/%Y")

    @staticmethod
    def _enlisted_date(member: discord.Member | None) -> str:
        if member is None:
            return "Not linked in this server"
        if member.joined_at is None:
            return "Unavailable"
        return discord.utils.format_dt(member.joined_at, style="D")

    @staticmethod
    def _format_duration(seconds: int | None) -> str:
        value = PlayerStatsModule._format_duration_text(seconds)
        return value if seconds is None else f"`{value}`"

    @staticmethod
    def _format_duration_text(seconds: int | None) -> str:
        if seconds is None:
            return "Unavailable"
        days, remainder = divmod(max(0, seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days:,}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)


class PlayerStatsCommandsMixin:
    """Public slash command for linked-member and direct EOS statistics."""

    @app_commands.command(
        name="vnstat", description="Show HLL: Vietnam player statistics."
    )
    @app_commands.describe(
        member="Optional linked Discord member.",
        eos_id="Optional 17-digit or 32-character game account ID.",
    )
    @app_commands.guild_only()
    async def vnstat(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        eos_id: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used in a server."
            )
            return

        if member is not None and eos_id is not None:
            await interaction.followup.send(
                "Choose either a Discord member or an EOS ID, not both."
            )
            return

        normalized_eos_id = None
        if eos_id is not None:
            normalized_eos_id = self.player_stats.normalize_eos_id(eos_id)
            if normalized_eos_id is None:
                await interaction.followup.send(
                    "Provide a valid 17-digit or 32-character EOS ID."
                )
                return

        if normalized_eos_id is not None:
            target_kind = "eos"
        elif member is not None:
            target_kind = "member"
        else:
            target_kind = "self"

        try:
            if normalized_eos_id is not None:
                stats = await self.hll_database.get_stats_by_eos(normalized_eos_id)
            else:
                discord_id = member.id if member is not None else interaction.user.id
                stats = await self.hll_database.get_stats_by_discord(discord_id)
        except HLLDatabaseError as exc:
            log.warning("Could not read HLL player statistics: %s", exc)
            await interaction.followup.send(
                "Player statistics are temporarily unavailable because the database could not be reached."
            )
            return
        except Exception:
            log.exception("Unexpected failure while reading HLL player statistics")
            await interaction.followup.send(
                "Player statistics are temporarily unavailable because the database query failed."
            )
            return

        if stats is None:
            if target_kind == "self":
                message = "Your Discord account is not linked. Use `/link` to link your account."
            elif target_kind == "member":
                message = "That Discord account is not linked. They can use `/link` to link it."
            else:
                message = "No recorded statistics were found for that EOS ID."
            await interaction.followup.send(message)
            return

        member = await self.player_stats.get_guild_member(guild, stats.discord_id)
        alias = self.hll_database.get_cached_alias(stats.eos_id)
        time_played_seconds = None
        battlemetrics_profile = None
        if await self.has_api_token():
            panel = await self.server_info.get_panel(guild)
            panel_server_id = panel.get("server_id")
            server_id = (
                str(panel_server_id)
                if panel_server_id
                else await self.get_default_server_id(guild)
            )
            try:
                battlemetrics_profile = (
                    await self.player_stats.get_battlemetrics_profile(
                        stats.eos_id,
                        server_id,
                    )
                )
            except BattleMetricsAPIError as exc:
                log.warning(
                    "Could not enrich EOS ID %s from BattleMetrics: %s",
                    stats.eos_id,
                    exc,
                )

        if battlemetrics_profile is not None:
            alias = battlemetrics_profile.name or alias
            time_played_seconds = battlemetrics_profile.time_played_seconds

        try:
            card = await self.player_stats.render_card(
                stats,
                alias=alias,
                member=member,
                time_played_seconds=time_played_seconds,
            )
        except Exception:
            log.exception("Could not render HLL VN stat card; using text embed")
            embed = self.player_stats.build_embed(
                stats,
                alias=alias,
                member=member,
                time_played_seconds=time_played_seconds,
            )
            await interaction.followup.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.followup.send(
            embed=self.player_stats.build_card_embed(),
            file=discord.File(card, filename="hll-vn-stat.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
