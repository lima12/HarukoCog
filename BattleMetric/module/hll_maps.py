"""Authorized map changes through the shared HLL: Vietnam RCON client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import discord
from discord import app_commands

from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError

try:
    from hllrcon import HLLVLayer
except Exception:  # Keep unrelated features loadable without the optional library.
    HLLVLayer = None


log = logging.getLogger("red.BattleMetric.hll_maps")


class HLLMapModule:
    """Validate map selections against the game server before submitting a change."""

    MAX_MAP_NAME_LENGTH = 200

    def __init__(self, cog: Any):
        self.cog = cog

    @classmethod
    def validate_name(cls, name: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ValueError("Enter a map name or ID on one line without control characters.")
        name = " ".join(name.split())
        if not name:
            raise ValueError("Provide a map name or ID.")
        if len(name) > cls.MAX_MAP_NAME_LENGTH:
            raise ValueError(f"Keep the map name at or below {cls.MAX_MAP_NAME_LENGTH} characters.")
        return name

    @staticmethod
    def _layer(map_id: str) -> Any:
        if HLLVLayer is not None:
            try:
                return HLLVLayer.by_id(map_id, strict=True)
            except ValueError:
                pass
        return None

    @classmethod
    def display_name(cls, map_id: str) -> str:
        layer = cls._layer(map_id)
        return layer.pretty_name if layer is not None else map_id

    @classmethod
    def resolve_map(cls, name: str, available: Sequence[str]) -> str:
        requested = cls.validate_name(name).casefold()
        exact = [map_id for map_id in available if map_id.casefold() == requested]
        if exact:
            return exact[0]
        matches = []
        for map_id in available:
            layer = cls._layer(map_id)
            if layer is not None and requested in {
                layer.pretty_name.casefold(), layer.map.pretty_name.casefold()
            }:
                matches.append(map_id)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(
                "That map has multiple modes or variants. Select a specific autocomplete "
                "option or enter its exact map ID."
            )
        raise ValueError(
            "That map is not available on this server. Select an autocomplete option "
            "or enter an exact server map ID."
        )

    @classmethod
    def choices(cls, current: str) -> list[app_commands.Choice[str]]:
        if HLLVLayer is None:
            return []
        terms = current.casefold().split()
        layers = sorted(HLLVLayer.all(), key=lambda layer: (layer.pretty_name, layer.id))
        return [
            app_commands.Choice(name=layer.pretty_name[:100], value=layer.id)
            for layer in layers
            if len(layer.id) <= 100
            and all(term in f"{layer.pretty_name} {layer.id}".casefold() for term in terms)
        ][:25]

    async def change(self, guild: discord.Guild, map_name: str) -> str:
        name = self.validate_name(map_name)
        available = await self.cog.kill_feed.execute_rcon(
            guild, "GetAvailableMaps request", lambda client: client.get_available_maps()
        )
        if not isinstance(available, (list, tuple)) or not all(
            isinstance(map_id, str) and map_id.strip() for map_id in available
        ):
            raise ValueError("The server returned an invalid map list. No map change was submitted.")
        map_id = self.resolve_map(name, available)
        await self.cog.kill_feed.execute_rcon(
            guild, "ChangeMap request", lambda client: client.change_map(map_id)
        )
        return map_id


class HLLMapCommandsMixin:
    """Restricted map administration in the shared /hllvn command group."""

    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(name="changemap", description="Request a map change on the HLL: Vietnam server.")
    @app_commands.describe(map_name="Map and mode from autocomplete, or an exact server map ID.")
    @app_commands.guild_only()
    async def hllvn_changemap(self, interaction: discord.Interaction, map_name: str) -> None:
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
        try:
            map_id = await self.hll_maps.change(guild, map_name)
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            await interaction.followup.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        except KillFeedConnectionTestError as exc:
            await interaction.followup.send(
                f"The map-change request could not be confirmed: {exc} "
                "Check the current or pending map before retrying.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except RuntimeError:
            await interaction.followup.send("The required `hllrcon` dependency is unavailable.")
            return
        except Exception:
            log.exception("Unexpected HLL map-change failure for guild %s", guild.id)
            await interaction.followup.send(
                "The map-change request could not be confirmed. Check the current or pending "
                "map before retrying, and ask the bot owner to check the Red service log."
            )
            return

        embed = discord.Embed(
            title="HLL VN Map Change Requested",
            description=(
                "The server accepted the map-change request. RCON ChangeMap normally "
                "starts a 60-second server countdown; this does not bypass it."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Map", value=self.hll_maps.display_name(map_id), inline=False)
        embed.add_field(name="Map ID", value=map_id, inline=False)
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @hllvn_changemap.autocomplete("map_name")
    async def hllvn_changemap_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not await self.is_authorized(interaction.user):
            return []
        if interaction.guild is None:
            return []
        return self.hll_maps.choices(current)
