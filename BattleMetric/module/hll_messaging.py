"""Authorized in-game messaging for HLL: Vietnam."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from .admin_ping import HLLAdminPingModule
from .hll_ban import HLLBanModule
from .hll_database import HLLDatabaseError
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError


log = logging.getLogger("red.BattleMetric.hll_messaging")


@dataclass(frozen=True)
class HLLAdminReply:
    source: discord.Message
    eos_id: str
    player_name: str
    text: str


class HLLMessagingModule:
    """Resolve safe message targets and use the shared RCON connection."""

    MEMBER_MENTION_PATTERN = re.compile(r"<@!?(\d{15,22})>")
    MAX_MESSAGE_LENGTH = 1000
    REPLY_INTERVAL_SECONDS = 3
    MAX_REPLY_QUEUE_SIZE = 100
    MAX_SEEN_REPLIES = 2000

    def __init__(self, cog: Any):
        self.cog = cog
        self._reply_queues: dict[int, deque[HLLAdminReply]] = {}
        self._seen_replies: OrderedDict[int, int] = OrderedDict()
        self._next_reply_at: dict[int, float] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        if not self.reply_worker.is_running():
            self.reply_worker.start()

    def stop(self) -> None:
        self._running = False
        self.reply_worker.cancel()
        self._reply_queues.clear()
        self._seen_replies.clear()
        self._next_reply_at.clear()

    async def delete_user_data(self, user_id: int) -> None:
        for guild_id, queue in list(self._reply_queues.items()):
            self._reply_queues[guild_id] = deque(
                reply for reply in queue if reply.source.author.id != user_id
            )
        for message_id, author_id in list(self._seen_replies.items()):
            if author_id == user_id:
                self._seen_replies.pop(message_id, None)

    @classmethod
    def normalize_message(cls, content: str) -> str:
        message = " ".join(content.split())
        if not message:
            raise ValueError("Provide a message to send. Attachments alone cannot be forwarded.")
        if len(message) > cls.MAX_MESSAGE_LENGTH:
            raise ValueError(f"Keep the message at or below {cls.MAX_MESSAGE_LENGTH} characters.")
        return message

    @staticmethod
    def confirmation_embed(
        *, target: str, eos_id: str | None, message: str, administrator: str | None = None
    ) -> discord.Embed:
        embed = discord.Embed(
            title="HLL VN Message Sent", color=discord.Color.green(), timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Target", value=target, inline=False)
        if eos_id is not None:
            embed.add_field(name="EOS ID", value=f"`{eos_id}`", inline=False)
        if administrator is not None:
            embed.add_field(name="Administrator", value=administrator, inline=False)
        embed.add_field(name="Message", value=message, inline=False)
        return embed

    def _reply_target(self, parent: discord.Message, source: discord.Message) -> tuple[str, str] | None:
        bot_user = self.cog.bot.user
        if (
            bot_user is None or parent.author.id != bot_user.id or parent.webhook_id is not None
            or parent.guild is None or parent.guild.id != source.guild.id
            or parent.channel.id != source.channel.id or parent.id != source.reference.message_id
            or len(parent.embeds) != 1
        ):
            return None
        embed = parent.embeds[0]
        if embed.title != HLLAdminPingModule.ALERT_TITLE:
            return None
        eos_fields = [field.value for field in embed.fields if field.name == HLLAdminPingModule.EOS_FIELD_NAME]
        players = [field.value for field in embed.fields if field.name == "Player report"]
        if len(eos_fields) != 1 or len(players) != 1:
            return None
        value = eos_fields[0].strip()
        if value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
        eos_id = HLLBanModule.normalize_eos_id(value)
        return (eos_id, players[0][:1024]) if eos_id is not None else None

    def _remember_reply(self, source: discord.Message) -> None:
        self._seen_replies[source.id] = source.author.id
        while len(self._seen_replies) > self.MAX_SEEN_REPLIES:
            self._seen_replies.popitem(last=False)

    async def handle_admin_reply(self, source: discord.Message) -> None:
        reference = source.reference
        if (
            not self._running or source.guild is None or source.author.bot or source.webhook_id is not None
            or source.type != discord.MessageType.reply or reference is None
            or getattr(getattr(reference, "type", None), "value", 0) != 0
            or reference.message_id is None or reference.channel_id != source.channel.id
            or (reference.guild_id is not None and reference.guild_id != source.guild.id)
        ):
            return
        if not await self.cog.is_authorized(source.author):
            return
        if source.id in self._seen_replies:
            return

        try:
            settings = await self.cog.admin_ping.get_settings(source.guild)
            if not settings["enabled"] or settings["channel_id"] != source.channel.id:
                return
            parent = reference.resolved
            if isinstance(parent, discord.DeletedReferencedMessage):
                return
            if not isinstance(parent, discord.Message):
                parent = await source.channel.fetch_message(reference.message_id)
            target = self._reply_target(parent, source)
            if target is None:
                return
            # Claim before yielding so simultaneous duplicate events cannot enqueue twice.
            if not self._running or source.id in self._seen_replies:
                return
            self._remember_reply(source)
            try:
                text = self.normalize_message(source.content)
            except ValueError as exc:
                await self._reply_status(source, content=str(exc))
                return
            queue = self._reply_queues.setdefault(source.guild.id, deque())
            if len(queue) >= self.MAX_REPLY_QUEUE_SIZE:
                await self._reply_status(source, content="The in-game reply queue is full. Try a new reply later.")
                return
            queue.append(HLLAdminReply(source, target[0], target[1], text))
        except asyncio.CancelledError:
            raise
        except discord.HTTPException:
            log.warning("Could not read the referenced admin alert in guild %s", source.guild.id, exc_info=True)
        except Exception:
            log.exception("Could not queue an HLL admin reply in guild %s", source.guild.id)

    @tasks.loop(seconds=1)
    async def reply_worker(self) -> None:
        try:
            await self._deliver_admin_replies()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error in the HLL admin-reply worker")

    @reply_worker.before_loop
    async def before_reply_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _deliver_admin_replies(self) -> None:
        for guild in self.cog.bot.guilds:
            if not self._running:
                return
            queue = self._reply_queues.get(guild.id)
            if not queue or time.monotonic() < self._next_reply_at.get(guild.id, 0):
                continue
            reply = queue.popleft()
            try:
                await self._deliver_admin_reply(reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected HLL admin-reply delivery failure in guild %s", guild.id)
                await self._reply_status(
                    reply.source,
                    content="The in-game reply could not be confirmed. Check the game before retrying with a new reply.",
                )
            finally:
                if self._running:
                    self._next_reply_at[guild.id] = time.monotonic() + self.REPLY_INTERVAL_SECONDS

    async def _deliver_admin_reply(self, reply: HLLAdminReply) -> None:
        source = reply.source
        if not self._running or not await self.cog.is_authorized(source.author):
            return
        settings = await self.cog.admin_ping.get_settings(source.guild)
        if not settings["enabled"] or settings["channel_id"] != source.channel.id:
            await self._reply_status(source, content="Admin alerts were disabled or moved. This reply was not sent in game.")
            return
        try:
            await self.send(source.guild, eos_id=reply.eos_id, message=reply.text)
        except asyncio.CancelledError:
            raise
        except (KillFeedConnectionTestError, ValueError) as exc:
            await self._reply_status(source, content=f"The in-game reply could not be confirmed: {exc} Check the game before retrying.")
            return
        except RuntimeError:
            await self._reply_status(source, content="The in-game reply failed because the RCON dependency is unavailable.")
            return
        embed = self.confirmation_embed(
            target=reply.player_name, eos_id=reply.eos_id, message=reply.text, administrator=source.author.mention
        )
        await self._reply_status(source, embed=embed)

    @staticmethod
    async def _reply_status(
        source: discord.Message, *, content: str | None = None, embed: discord.Embed | None = None
    ) -> None:
        try:
            await source.channel.send(
                content=content, embed=embed,
                reference=source.to_reference(fail_if_not_exists=False),
                mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            # A failed Discord receipt must never resend an already delivered RCON message.
            log.warning("Could not post HLL admin-reply status in guild %s", source.guild.id, exc_info=True)

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

        await interaction.response.defer(thinking=True, ephemeral=False)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        try:
            message = self.hll_messaging.normalize_message(mesgs)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
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

        embed = self.hll_messaging.confirmation_embed(
            target=target_display, eos_id=normalized_eos_id, message=message
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
