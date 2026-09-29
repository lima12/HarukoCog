"""Authorized in-game messaging for HLL: Vietnam."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands

from .hll_ban import HLLBanModule
from .hll_database import HLLDatabaseError
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError


log = logging.getLogger("red.BattleMetric.hll_messaging")


class HLLMessagingModule:
    """Resolve safe message targets and use the shared RCON connection."""

    MEMBER_MENTION_PATTERN = re.compile(r"<@!?(\d{15,22})>")
    MAX_MESSAGE_LENGTH = 1000

    def __init__(self, cog: Any):
        self.cog = cog

    @classmethod
    def parse_member_mention(cls, target: str) -> int | None:
        match = cls.MEMBER_MENTION_PATTERN.fullmatch(target.strip())
        return int(match.group(1)) if match is not None else None

    async def send(
        self,
        guild: discord.Guild,
        *,
        eos_id: str | None,
        message: str,
    ) -> None:
        if eos_id is None:
            await self.cog.kill_feed.execute_rcon(
                guild,
                "MessageAllPlayers request",
                lambda client: client.message_all_players(message),
            )
            return
        await self.cog.kill_feed.execute_rcon(
            guild,
            "MessagePlayer request",
            lambda client: client.message_player(eos_id, message),
        )


class HLLMessagingCommandsMixin:
    """Restricted slash command for direct and server-wide RCON messages."""

    hllvn = HLLVN_COMMAND_GROUP

    @hllvn.command(
        name="mesg",
        description="Send an in-game message to an EOS ID, linked member, or everyone.",
    )
    @app_commands.describe(
        target="EOS ID, linked @mention, or ALL.",
        mesgs="Message to show in game, up to 1000 characters.",
    )
    @app_commands.guild_only()
    async def hllvn_mesg(
        self,
        interaction: discord.Interaction,
        target: str,
        mesgs: str,
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

        message = " ".join(mesgs.split())
        if not message:
            await interaction.followup.send("Provide a message to send.")
            return
        if len(message) > self.hll_messaging.MAX_MESSAGE_LENGTH:
            await interaction.followup.send(
                "Keep the message at or below "
                f"{self.hll_messaging.MAX_MESSAGE_LENGTH} characters."
            )
            return

        raw_target = target.strip()
        normalized_eos_id: str | None
        if raw_target.casefold() == "all":
            normalized_eos_id = None
            target_display = "All players"
        else:
            member_id = self.hll_messaging.parse_member_mention(raw_target)
            if member_id is not None:
                member = guild.get_member(member_id)
                if member is None:
                    await interaction.followup.send(
                        "Mention a member who is currently in this Discord server."
                    )
                    return
                try:
                    stats = await self.hll_database.get_stats_by_discord(member.id)
                except HLLDatabaseError as exc:
                    log.warning(
                        "Could not resolve linked member for an HLL message: %s",
                        exc,
                    )
                    await interaction.followup.send(
                        "The account-link database is temporarily unavailable."
                    )
                    return
                except Exception:
                    log.exception(
                        "Unexpected database failure while resolving an HLL message target"
                    )
                    await interaction.followup.send(
                        "The linked account could not be read because the database query failed."
                    )
                    return
                if stats is None:
                    await interaction.followup.send(
                        "That Discord account is not linked. They must use `/link` first."
                    )
                    return
                normalized_eos_id = stats.eos_id
                target_display = member.mention
            else:
                normalized_eos_id = HLLBanModule.normalize_eos_id(raw_target)
                if normalized_eos_id is None:
                    await interaction.followup.send(
                        "Target must be a valid linked @mention, a 17-digit or "
                        "32-character EOS ID, or `ALL`."
                    )
                    return
                target_display = f"`{normalized_eos_id}`"

        try:
            await self.hll_messaging.send(
                guild,
                eos_id=normalized_eos_id,
                message=message,
            )
        except asyncio.CancelledError:
            raise
        except (KillFeedConnectionTestError, ValueError) as exc:
            await interaction.followup.send(f"The in-game message failed: {exc}")
            return
        except RuntimeError:
            await interaction.followup.send(
                "The in-game message failed because the required RCON dependency "
                "is unavailable."
            )
            return
        except Exception:
            log.exception("Unexpected failure while sending an HLL in-game message")
            await interaction.followup.send(
                "The in-game message failed. Ask the bot owner to check the Red service log."
            )
            return

        embed = discord.Embed(
            title="HLL VN Message Sent",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Target", value=target_display, inline=False)
        if normalized_eos_id is not None:
            embed.add_field(
                name="EOS ID",
                value=f"`{normalized_eos_id}`",
                inline=False,
            )
        embed.add_field(name="Message", value=message, inline=False)
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
