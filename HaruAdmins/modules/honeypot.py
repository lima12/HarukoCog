import asyncio
import logging
from datetime import timedelta
from typing import Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import tasks
from redbot.core import commands


log = logging.getLogger("red.HaruAdmins.honeypot")

HONEYPOT_TIMEOUT_DURATION = timedelta(days=7)
HONEYPOT_EXPIRY_INTERVAL_SECONDS = 30


class HoneypotMixin:
    """Channel honeypot configuration, enforcement, release, and expiry handling."""

    def register_honeypot_config(self) -> None:
        self.config.register_guild(
            honeypot_trap=None,
            honeypot_pending_kicks={},
        )
        self._honeypot_inflight: Set[Tuple[int, int]] = set()

    def start_honeypot(self) -> None:
        self.honeypot_expiry_worker.start()

    def stop_honeypot(self) -> None:
        self.honeypot_expiry_worker.cancel()
        self._honeypot_inflight.clear()

    async def delete_honeypot_user_data(
        self,
        guild_id: int,
        user_id: int,
    ) -> None:
        async with self._guild_lock(guild_id):
            conf = self.config.guild_from_id(guild_id)
            pending = await conf.honeypot_pending_kicks()
            if pending.pop(str(user_id), None) is not None:
                await conf.honeypot_pending_kicks.set(pending)

    @commands.hybrid_group(name="honeypot", invoke_without_command=True)
    @app_commands.guild_only()
    @commands.guild_only()
    async def honeypot(self, ctx: commands.Context) -> None:
        """Configure or release members from the channel honeypot."""
        if not await self._require_honeypot_owner(ctx):
            return
        await ctx.send_help()

    @honeypot.command(name="trap")
    @app_commands.describe(
        channel="Channel that should act as the honeypot.",
        action="Action applied when a non-exempt member sends a message.",
        notify_role="Role exempt from the trap and notified when it is triggered.",
        notify_channel="Optional alert channel. Defaults to the trap channel.",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Timeout for 7 days, then kick", value="timeout"),
        ]
    )
    @commands.guild_only()
    async def honeypot_trap(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        action: str,
        notify_role: discord.Role,
        notify_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Configure the channel monitored by the honeypot."""
        if not await self._require_honeypot_owner(ctx):
            return
        guild = ctx.guild
        if guild is None:
            return

        action = action.lower()
        if action not in {"kick", "timeout"}:
            await self._send(ctx, "Action must be either `kick` or `timeout`.")
            return

        alert_channel = notify_channel or channel
        validation_error = self._honeypot_setup_error(
            guild,
            channel,
            action,
            notify_role,
            alert_channel,
        )
        if validation_error is not None:
            await self._send(ctx, validation_error)
            return

        trap = {
            "channel_id": channel.id,
            "action": action,
            "notify_role_id": notify_role.id,
            "notify_channel_id": alert_channel.id,
        }
        async with self._guild_lock(guild.id):
            await self.config.guild(guild).honeypot_trap.set(trap)

        await self._send(
            ctx,
            f"Honeypot enabled in {channel.mention}. Members with "
            f"{notify_role.mention} are exempt. The action is **{action}**, and "
            f"alerts will be sent to {alert_channel.mention}.",
        )

    @honeypot.command(name="release")
    @app_commands.describe(suspect="Timed-out member to release from the honeypot action.")
    @commands.guild_only()
    async def honeypot_release(
        self,
        ctx: commands.Context,
        suspect: discord.Member,
    ) -> None:
        """Cancel a pending honeypot kick and remove its seven-day timeout."""
        if not await self._require_honeypot_owner(ctx):
            return
        guild = ctx.guild
        if guild is None:
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        async with self._guild_lock(guild.id):
            pending = await self.config.guild(guild).honeypot_pending_kicks()
            record = pending.get(str(suspect.id))
            if record is None:
                await self._send(
                    ctx,
                    f"{suspect.mention} has no pending honeypot timeout action.",
                )
                return

            try:
                if suspect.timed_out_until is not None:
                    await suspect.timeout(
                        None,
                        reason=f"Honeypot release by {ctx.author} ({ctx.author.id}).",
                    )
            except discord.Forbidden:
                await self._send(
                    ctx,
                    "Discord refused to remove that timeout. Check my Moderate "
                    "Members permission and role position.",
                )
                return
            except discord.HTTPException:
                log.exception(
                    "Could not release honeypot suspect %s in guild %s",
                    suspect.id,
                    guild.id,
                )
                await self._send(ctx, "Discord could not release that member right now.")
                return

            async with self.config.guild(guild).honeypot_pending_kicks() as mutable:
                mutable.pop(str(suspect.id), None)

        await self._send(
            ctx,
            f"Released {suspect.mention}. Their honeypot timeout and scheduled kick "
            "have been removed.",
        )

    async def _require_honeypot_owner(self, ctx: commands.Context) -> bool:
        guild = ctx.guild
        if guild is None or not isinstance(ctx.author, discord.Member):
            return False
        if ctx.author.id == guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        await self._send(
            ctx,
            "Only the Discord server owner or a configured Red bot owner can use "
            "honeypot commands.",
        )
        return False

    def _honeypot_setup_error(
        self,
        guild: discord.Guild,
        trap_channel: discord.TextChannel,
        action: str,
        notify_role: discord.Role,
        notify_channel: discord.TextChannel,
    ) -> Optional[str]:
        if notify_role.is_default():
            return "The `@everyone` role cannot be the exempt notification role."

        bot_member = guild.me
        if bot_member is None:
            return "I could not resolve my server member record."

        trap_permissions = trap_channel.permissions_for(bot_member)
        if not trap_permissions.view_channel:
            return f"I need View Channel in the trap channel {trap_channel.mention}."

        alert_permissions = notify_channel.permissions_for(bot_member)
        if not alert_permissions.view_channel or not alert_permissions.send_messages:
            return f"I need View Channel and Send Messages in {notify_channel.mention}."
        if not notify_role.mentionable and not alert_permissions.mention_everyone:
            return (
                f"{notify_role.mention} must be mentionable, or I need Mention "
                "@everyone, @here, and All Roles in the notification channel."
            )

        guild_permissions = bot_member.guild_permissions
        if not guild_permissions.kick_members:
            return "I need the Kick Members permission for honeypot enforcement."
        if action == "timeout" and not guild_permissions.moderate_members:
            return "I need the Moderate Members permission for the timeout action."
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        trap = await self.config.guild(guild).honeypot_trap()
        if not trap or message.channel.id != trap.get("channel_id"):
            return

        role_id = trap.get("notify_role_id")
        notify_role = guild.get_role(role_id) if role_id is not None else None
        if notify_role is None:
            log.error(
                "Guild %s honeypot is paused because its notification role is missing",
                guild.id,
            )
            return
        if notify_role in message.author.roles:
            return

        action = trap.get("action")
        if action not in {"kick", "timeout"}:
            log.error("Guild %s has an invalid honeypot action: %r", guild.id, action)
            return

        inflight_key = (guild.id, message.author.id)
        if inflight_key in self._honeypot_inflight:
            return
        self._honeypot_inflight.add(inflight_key)
        try:
            applied, failure = await self._apply_honeypot_action(message.author, action)
            await self._send_honeypot_alert(
                message.author,
                trap,
                action,
                applied=applied,
                failure=failure,
            )
        finally:
            self._honeypot_inflight.discard(inflight_key)

    async def _apply_honeypot_action(
        self,
        suspect: discord.Member,
        action: str,
    ) -> tuple:
        guild = suspect.guild
        reason = f"Honeypot triggered by {suspect} ({suspect.id})."
        try:
            if action == "kick":
                await suspect.kick(reason=reason)
                return True, None

            kick_at = discord.utils.utcnow() + HONEYPOT_TIMEOUT_DURATION
            await suspect.timeout(kick_at, reason=reason)
            record = {
                "kick_at": int(kick_at.timestamp()),
            }
            try:
                async with self._guild_lock(guild.id):
                    async with self.config.guild(guild).honeypot_pending_kicks() as pending:
                        pending[str(suspect.id)] = record
            except Exception:
                log.exception(
                    "Could not persist honeypot kick for member %s in guild %s",
                    suspect.id,
                    guild.id,
                )
                try:
                    await suspect.timeout(
                        None,
                        reason="Honeypot timeout rolled back after persistence failure.",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Could not roll back honeypot timeout for member %s in guild %s",
                        suspect.id,
                        guild.id,
                    )
                return False, "The timeout schedule could not be saved safely."
            return True, None
        except discord.Forbidden:
            return False, "Discord refused the action due to permissions or role hierarchy."
        except discord.HTTPException:
            log.exception(
                "Could not apply honeypot %s action to member %s in guild %s",
                action,
                suspect.id,
                guild.id,
            )
            return False, "Discord could not apply the action."

    async def _send_honeypot_alert(
        self,
        suspect: discord.Member,
        trap: dict,
        action: str,
        *,
        applied: bool,
        failure: Optional[str],
    ) -> None:
        guild = suspect.guild
        role_id = trap.get("notify_role_id")
        channel_id = trap.get("notify_channel_id") or trap.get("channel_id")
        role = guild.get_role(role_id) if role_id is not None else None
        channel = guild.get_channel(channel_id) if channel_id is not None else None
        if role is None or not isinstance(channel, discord.TextChannel):
            log.error(
                "Could not send honeypot alert in guild %s because its role or "
                "notification channel is missing",
                guild.id,
            )
            return

        display_action = action.capitalize()
        if applied:
            result = f"The **{display_action}** action has been applied!"
        else:
            result = f"The **{display_action}** action could not be applied. {failure}"
        content = (
            f"{role.mention}, a suspect {suspect.mention} has hit the honeypot. "
            f"{result}"
        )
        if action == "timeout" and applied:
            content += (
                "\nIf there is reason beyond doubt that this was a mistake, use "
                f"`/honeypot release suspect:{suspect.mention}`."
            )

        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=[role],
                    users=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not send honeypot alert for member %s in guild %s",
                suspect.id,
                guild.id,
            )

    @tasks.loop(seconds=HONEYPOT_EXPIRY_INTERVAL_SECONDS)
    async def honeypot_expiry_worker(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._process_honeypot_expirations(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not process honeypot expirations for guild %s", guild.id)

    @honeypot_expiry_worker.before_loop
    async def before_honeypot_expiry_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_honeypot_expirations(self, guild: discord.Guild) -> None:
        async with self._guild_lock(guild.id):
            pending = await self.config.guild(guild).honeypot_pending_kicks()
            if not pending:
                return

            now = int(discord.utils.utcnow().timestamp())
            retained = dict(pending)
            changed = False
            for member_id_text, record in pending.items():
                try:
                    member_id = int(member_id_text)
                    kick_at = int(record["kick_at"])
                except (KeyError, TypeError, ValueError):
                    retained.pop(member_id_text, None)
                    changed = True
                    continue

                if kick_at > now:
                    continue

                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        retained.pop(member_id_text, None)
                        changed = True
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning(
                            "Could not fetch honeypot suspect %s in guild %s",
                            member_id,
                            guild.id,
                        )
                        continue

                try:
                    await member.kick(reason="Seven-day honeypot timeout expired.")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    log.warning(
                        "Could not kick expired honeypot suspect %s in guild %s",
                        member_id,
                        guild.id,
                    )
                    continue
                except discord.HTTPException:
                    log.exception(
                        "Discord failed to kick expired honeypot suspect %s in guild %s",
                        member_id,
                        guild.id,
                    )
                    continue

                retained.pop(member_id_text, None)
                changed = True

            if changed:
                await self.config.guild(guild).honeypot_pending_kicks.set(retained)
