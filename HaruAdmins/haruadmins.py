import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import tasks
from redbot.core import Config, commands


log = logging.getLogger("red.HaruAdmins")

DISCORD_MAX_TIMEOUT = timedelta(days=28)
RENEWAL_WINDOW = timedelta(minutes=10)
MAX_MANAGED_TIMEOUT = timedelta(days=3650)
WORKER_INTERVAL_SECONDS = 60

DURATION_PATTERN = re.compile(r"(?:\d+\s*[smhdw]\s*)+", re.IGNORECASE)
DURATION_PART_PATTERN = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


class HaruAdmins(commands.Cog):
    """Moderation commands, including persistent long Discord timeouts."""

    __author__ = "Haruko"
    __version__ = "1.0.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x4841525541444D49_01,
            force_registration=True,
        )
        self.config.register_guild(managed_timeouts={})
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        # Expected gateway updates prevent the listener from treating this cog's
        # own timeout changes as manual moderator overrides.
        self._expected_updates: Dict[
            Tuple[int, int], Tuple[Optional[int], float]
        ] = {}

    async def cog_load(self) -> None:
        self.renewal_worker.start()

    def cog_unload(self) -> None:
        self.renewal_worker.cancel()
        self._guild_locks.clear()
        self._expected_updates.clear()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    @commands.hybrid_command(
        name="timeout",
        description="Timeout a member, automatically renewing durations over 28 days.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        member="Member to timeout.",
        duration="Duration such as 30m, 2d, 6w, or 30d12h.",
        reason="Optional moderation reason.",
    )
    @commands.guild_only()
    @commands.mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout_command(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Timeout a member for up to ten years.

        Durations longer than Discord's 28-day limit are persisted and renewed
        shortly before each Discord timeout segment expires.
        """
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return

        duration_seconds = self.parse_duration(duration)
        if duration_seconds is None:
            await self._send(
                ctx,
                "Invalid duration. Use combinations like `30m`, `2d`, `6w`, or "
                "`30d12h` (maximum 10 years).",
            )
            return

        hierarchy_error = self._hierarchy_error(ctx.author, member)
        if hierarchy_error is not None:
            await self._send(ctx, hierarchy_error)
            return

        cleaned_reason = (reason or "No reason provided.").strip()
        if not cleaned_reason:
            cleaned_reason = "No reason provided."
        if len(cleaned_reason) > 400:
            await self._send(ctx, "The reason must be 400 characters or fewer.")
            return

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        now = discord.utils.utcnow()
        final_until = now + timedelta(seconds=duration_seconds)
        first_segment_until = min(final_until, now + DISCORD_MAX_TIMEOUT)
        is_managed = final_until > first_segment_until
        record = {
            "expires_at": int(final_until.timestamp()),
            "segment_until": int(first_segment_until.timestamp()),
            "moderator_id": ctx.author.id,
            "reason": cleaned_reason,
        }

        async with self._guild_lock(ctx.guild.id):
            try:
                await self._set_member_timeout(
                    member,
                    first_segment_until,
                    self._audit_reason(ctx.author.id, cleaned_reason, renewal=False),
                )
            except discord.Forbidden:
                await self._send(
                    ctx,
                    "Discord refused the timeout. Check the bot's role position and "
                    "Moderate Members permission.",
                )
                return
            except discord.HTTPException:
                log.exception(
                    "Failed to timeout member %s in guild %s", member.id, ctx.guild.id
                )
                await self._send(ctx, "Discord could not apply that timeout. Please try again.")
                return

            async with self.config.guild(ctx.guild).managed_timeouts() as records:
                if is_managed:
                    records[str(member.id)] = record
                else:
                    records.pop(str(member.id), None)

        duration_text = self.format_duration(duration_seconds)
        if is_managed:
            status = (
                " I will renew it in 28-day segments until "
                f"<t:{record['expires_at']}:F>."
            )
        else:
            status = f" It ends <t:{record['expires_at']}:R>."
        await self._send(
            ctx,
            f"Timed out {member.mention} for **{duration_text}**.{status}\n"
            f"Reason: {cleaned_reason}",
        )

    def _hierarchy_error(
        self,
        moderator: discord.Member,
        target: discord.Member,
    ) -> Optional[str]:
        guild = moderator.guild
        if target.id == moderator.id:
            return "You cannot timeout yourself."
        if target.id == guild.owner_id:
            return "The server owner cannot be timed out."
        if target.bot:
            return "Bots cannot be timed out."
        if target.guild_permissions.administrator:
            return "Members with the Administrator permission cannot be timed out."
        if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
            return "You cannot timeout a member whose top role is equal to or above yours."

        bot_member = guild.me
        if bot_member is None:
            return "I could not resolve my server member record."
        if target.top_role >= bot_member.top_role:
            return "I cannot timeout a member whose top role is equal to or above mine."
        return None

    async def _set_member_timeout(
        self,
        member: discord.Member,
        until,
        audit_reason: str,
    ) -> None:
        expected_timestamp = int(until.timestamp()) if until is not None else None
        key = (member.guild.id, member.id)
        self._expected_updates[key] = (expected_timestamp, time.monotonic() + 30)
        try:
            await member.timeout(until, reason=audit_reason)
        except Exception:
            self._expected_updates.pop(key, None)
            raise

    @tasks.loop(seconds=WORKER_INTERVAL_SECONDS)
    async def renewal_worker(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._renew_guild_timeouts(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not process managed timeouts for guild %s", guild.id)
        self._prune_expected_updates()

    @renewal_worker.before_loop
    async def before_renewal_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _renew_guild_timeouts(self, guild: discord.Guild) -> None:
        async with self._guild_lock(guild.id):
            records = await self.config.guild(guild).managed_timeouts()
            if not records:
                return

            now = discord.utils.utcnow()
            retained = dict(records)
            changed = False
            for member_id_text, record in records.items():
                try:
                    member_id = int(member_id_text)
                    expires_at = int(record["expires_at"])
                    segment_until = int(record["segment_until"])
                    moderator_id = int(record.get("moderator_id") or 0)
                    reason = str(record.get("reason") or "No reason provided.")
                    final_until = datetime.fromtimestamp(expires_at, tz=timezone.utc)
                except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
                    retained.pop(member_id_text, None)
                    changed = True
                    continue

                if final_until <= now:
                    retained.pop(member_id_text, None)
                    changed = True
                    continue

                member = guild.get_member(member_id)
                member_was_fetched = False
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                        member_was_fetched = True
                    except discord.NotFound:
                        retained.pop(member_id_text, None)
                        changed = True
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning(
                            "Could not fetch member %s while renewing a timeout in guild %s",
                            member_id,
                            guild.id,
                        )
                        continue

                current_until = member.timed_out_until
                current_timestamp = (
                    int(current_until.timestamp()) if current_until is not None else None
                )
                segment_matches = self._timestamps_match(
                    segment_until, current_timestamp
                )
                segment_elapsed = segment_until <= int(now.timestamp()) + 5
                if not segment_matches and not (
                    current_until is None and segment_elapsed
                ):
                    # The saved segment should still be active, so a different
                    # value means a moderator changed it while this cog was off.
                    retained.pop(member_id_text, None)
                    changed = True
                    log.info(
                        "Stopped managed timeout for member %s in guild %s after "
                        "detecting an offline moderation change",
                        member_id,
                        guild.id,
                    )
                    continue
                if current_until is not None and current_until > now + RENEWAL_WINDOW:
                    continue

                # Refresh from Discord immediately before extending the timeout.
                # This also respects manual removals when the member-update
                # gateway intent is unavailable and the cache is stale.
                if not member_was_fetched:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        retained.pop(member_id_text, None)
                        changed = True
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning(
                            "Could not refresh member %s before timeout renewal in guild %s",
                            member_id,
                            guild.id,
                        )
                        continue

                    current_until = member.timed_out_until
                    current_timestamp = (
                        int(current_until.timestamp())
                        if current_until is not None
                        else None
                    )
                    segment_matches = self._timestamps_match(
                        segment_until, current_timestamp
                    )
                    segment_elapsed = segment_until <= int(now.timestamp()) + 5
                    if not segment_matches and not (
                        current_until is None and segment_elapsed
                    ):
                        retained.pop(member_id_text, None)
                        changed = True
                        log.info(
                            "Stopped managed timeout for member %s in guild %s after "
                            "confirming a moderation change",
                            member_id,
                            guild.id,
                        )
                        continue

                next_until = min(final_until, now + DISCORD_MAX_TIMEOUT)
                try:
                    await self._set_member_timeout(
                        member,
                        next_until,
                        self._audit_reason(moderator_id, reason, renewal=True),
                    )
                    retained[member_id_text]["segment_until"] = int(
                        next_until.timestamp()
                    )
                    changed = True
                except discord.Forbidden:
                    log.warning(
                        "Discord refused timeout renewal for member %s in guild %s",
                        member_id,
                        guild.id,
                    )
                except discord.HTTPException:
                    log.exception(
                        "Discord failed timeout renewal for member %s in guild %s",
                        member_id,
                        guild.id,
                    )

            if changed:
                await self.config.guild(guild).managed_timeouts.set(retained)

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if before.timed_out_until == after.timed_out_until:
            return

        key = (after.guild.id, after.id)
        expected = self._expected_updates.get(key)
        after_timestamp = (
            int(after.timed_out_until.timestamp())
            if after.timed_out_until is not None
            else None
        )
        if expected is not None:
            expected_timestamp, deadline = expected
            if time.monotonic() <= deadline and self._timestamps_match(
                expected_timestamp, after_timestamp
            ):
                self._expected_updates.pop(key, None)
                return
            if time.monotonic() > deadline:
                self._expected_updates.pop(key, None)

        records = await self.config.guild(after.guild).managed_timeouts()
        if str(after.id) not in records:
            return

        # Any unexpected timeout edit is treated as a moderator override. This
        # ensures manually removing or shortening a timeout stops auto-renewal.
        async with self._guild_lock(after.guild.id):
            async with self.config.guild(after.guild).managed_timeouts() as mutable:
                if mutable.pop(str(after.id), None) is not None:
                    log.info(
                        "Stopped managed timeout for member %s in guild %s after a manual edit",
                        after.id,
                        after.guild.id,
                    )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self._expected_updates.pop((member.guild.id, member.id), None)
        async with self._guild_lock(member.guild.id):
            async with self.config.guild(member.guild).managed_timeouts() as records:
                records.pop(str(member.id), None)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        for guild_id, guild_data in (await self.config.all_guilds()).items():
            records = guild_data.get("managed_timeouts", {})
            changed = False
            if records.pop(str(user_id), None) is not None:
                changed = True
            for record in records.values():
                if record.get("moderator_id") == user_id:
                    record["moderator_id"] = 0
                    changed = True
            if changed:
                await self.config.guild_from_id(guild_id).managed_timeouts.set(records)

    @staticmethod
    def parse_duration(value: str) -> Optional[int]:
        normalized = value.strip()
        if DURATION_PATTERN.fullmatch(normalized) is None:
            return None
        seconds = sum(
            int(amount) * DURATION_UNITS[unit.lower()]
            for amount, unit in DURATION_PART_PATTERN.findall(normalized)
        )
        maximum = int(MAX_MANAGED_TIMEOUT.total_seconds())
        return seconds if 1 <= seconds <= maximum else None

    @staticmethod
    def format_duration(seconds: int) -> str:
        parts = []
        remaining = seconds
        for unit_seconds, label in (
            (7 * 24 * 60 * 60, "week"),
            (24 * 60 * 60, "day"),
            (60 * 60, "hour"),
            (60, "minute"),
            (1, "second"),
        ):
            amount, remaining = divmod(remaining, unit_seconds)
            if amount:
                parts.append(f"{amount} {label}{'' if amount == 1 else 's'}")
        return ", ".join(parts)

    @staticmethod
    def _audit_reason(moderator_id: int, reason: str, *, renewal: bool) -> str:
        action = "renewed" if renewal else "set"
        return f"HaruAdmins timeout {action} by moderator {moderator_id}: {reason}"[:512]

    @staticmethod
    def _timestamps_match(first: Optional[int], second: Optional[int]) -> bool:
        if first is None or second is None:
            return first is second
        return abs(first - second) <= 2

    def _prune_expected_updates(self) -> None:
        now = time.monotonic()
        self._expected_updates = {
            key: value for key, value in self._expected_updates.items() if value[1] > now
        }

    @staticmethod
    async def _send(ctx: commands.Context, content: str) -> None:
        kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
        if ctx.interaction is not None:
            kwargs["ephemeral"] = True
        await ctx.send(content, **kwargs)
