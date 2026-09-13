"""Review and render custom HLL: Vietnam dog-tag overlays."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import discord
from aiohttp import web
from discord import app_commands
from redbot.core import commands

from ..authorization import requires_authorized_user

log = logging.getLogger("red.BattleMetric.dog_tags")

try:
    from PIL import Image
except Exception as exc:  # noqa: BLE001 - keep the rest of BattleMetric loadable
    Image = None
    PILLOW_IMPORT_ERROR: Exception | None = exc
else:
    PILLOW_IMPORT_ERROR = None


def _environment_port(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if 1 <= value <= 65535 else default


class StaleDogTagSubmissionError(RuntimeError):
    """Raised when a staged file does not match its review submission."""


class DogTagConfigurationError(RuntimeError):
    """Raised when the local dog-tag service is not ready to process files."""


class DogTagReviewView(discord.ui.View):
    """Persistent staff controls for one staged overlay."""

    def __init__(
        self,
        module: DogTagModule,
        user_id: str,
        username: str,
        submission_id: str,
    ):
        super().__init__(timeout=None)
        self.module = module
        self.user_id = user_id
        self.username = username
        self.submission_id = submission_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"dogtag:approve:{user_id}",
        )
        deny = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"dogtag:deny:{user_id}",
        )
        approve.callback = self._approve
        deny.callback = self._deny
        self.add_item(approve)
        self.add_item(deny)

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self.module.handle_review_action(
            interaction,
            self,
            approve=True,
        )

    async def _deny(self, interaction: discord.Interaction) -> None:
        await self.module.handle_review_action(
            interaction,
            self,
            approve=False,
        )

    def disable(self) -> None:
        for child in self.children:
            child.disabled = True


class DogTagModule:
    """Receive staged overlays, coordinate staff review, and composite tags."""

    IPC_SERVICE_NAME = "tagweb"
    IPC_SECRET_NAME = "ipc_secret"
    DEFAULT_STORAGE_PATH = Path("/home/phuled/SLHTAG")
    IPC_HOST = os.getenv("HLLVN_TAG_IPC_HOST", "127.0.0.1")
    IPC_PORT = _environment_port("HLLVN_TAG_IPC_PORT", 8765)
    MAX_IPC_BODY_BYTES = 32 * 1024
    TARGET_BOX: ClassVar[tuple[int, int, int, int]] = (612, 900, 1024, 1100)

    def __init__(self, cog: Any):
        self.cog = cog
        self.storage_path = Path(
            os.getenv("HLLVN_TAG_STORAGE", str(self.DEFAULT_STORAGE_PATH))
        ).expanduser()
        self.staging_path = self.storage_path / "staging"
        self.base_tag_path = self.storage_path / "dogtag.png"
        self._ipc_secret: str | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._pending_lock = asyncio.Lock()
        self._review_locks: dict[str, asyncio.Lock] = {}
        self._missing_base_logged = False

    def register_config(self) -> None:
        self.cog.config.register_global(
            dogtag_review_channel_id=None,
            dogtag_pending={},
        )

    async def start(self) -> None:
        await self.refresh_ipc_secret()
        await self._restore_persistent_views()
        self._ensure_storage()
        await self._start_ipc_server()

    async def stop(self) -> None:
        site = self._site
        runner = self._runner
        self._site = None
        self._runner = None
        if site is not None:
            await site.stop()
        if runner is not None:
            await runner.cleanup()

    async def refresh_ipc_secret(self) -> None:
        tokens = await self.cog.bot.get_shared_api_tokens(self.IPC_SERVICE_NAME)
        value = tokens.get(self.IPC_SECRET_NAME)
        self._ipc_secret = value.strip() if isinstance(value, str) and value.strip() else None

    async def restart_ipc(self, secret: str | None) -> None:
        normalized = secret.strip() if isinstance(secret, str) and secret.strip() else None
        if normalized == self._ipc_secret and self._runner is not None:
            return
        await self.stop()
        self._ipc_secret = normalized
        await self._start_ipc_server()

    async def set_review_channel(self, channel: discord.TextChannel) -> None:
        await self.cog.config.dogtag_review_channel_id.set(channel.id)

    async def get_review_channel(self) -> discord.TextChannel | None:
        channel_id = await self.cog.config.dogtag_review_channel_id()
        try:
            parsed_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = self.cog.bot.get_channel(parsed_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    def ipc_is_running(self) -> bool:
        return self._runner is not None and self._site is not None

    def has_ipc_secret(self) -> bool:
        return bool(self._ipc_secret)

    async def delete_user_data(self, user_id: int) -> None:
        user_id_text = str(user_id)
        async with self._pending_lock:
            pending = await self._get_pending()
            if pending.pop(user_id_text, None) is not None:
                await self.cog.config.dogtag_pending.set(pending)
        await asyncio.to_thread(self._delete_user_files, user_id_text)

    async def handle_review_action(
        self,
        interaction: discord.Interaction,
        view: DogTagReviewView,
        *,
        approve: bool,
    ) -> None:
        if not await self.cog.is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to review dog-tag submissions.",
                ephemeral=True,
            )
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "The review message is unavailable.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._review_lock(view.user_id):
            record = (await self._get_pending()).get(view.user_id)
            if not isinstance(record, Mapping) or self._record_id(record, "message_id") != message.id:
                await interaction.followup.send(
                    "This review is no longer current. A newer submission may exist.",
                    ephemeral=True,
                )
                return

            try:
                if approve:
                    await asyncio.to_thread(
                        self._approve_file,
                        view.user_id,
                        view.submission_id,
                    )
                else:
                    await asyncio.to_thread(
                        self._deny_file,
                        view.user_id,
                        view.submission_id,
                    )
            except StaleDogTagSubmissionError:
                await self._remove_pending(view.user_id, message.id)
                view.disable()
                await self._edit_review_message(
                    message,
                    view,
                    decision="Superseded by another upload",
                    reviewer=interaction.user,
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(
                    "This staged file no longer matches the reviewed submission.",
                    ephemeral=True,
                )
                return
            except FileNotFoundError:
                await self._remove_pending(view.user_id, message.id)
                view.disable()
                await self._edit_review_message(
                    message,
                    view,
                    decision="Staged file missing",
                    reviewer=interaction.user,
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(
                    "The staged file was not found. This review has been closed.",
                    ephemeral=True,
                )
                return
            except Exception:
                log.exception("Could not process dog-tag review for user %s", view.user_id)
                await interaction.followup.send(
                    "The dog-tag file could not be processed. Check the bot log.",
                    ephemeral=True,
                )
                return

            await self._remove_pending(view.user_id, message.id)
            view.disable()
            decision = "Approved" if approve else "Denied and deleted"
            color = discord.Color.green() if approve else discord.Color.red()
            await self._edit_review_message(
                message,
                view,
                decision=decision,
                reviewer=interaction.user,
                color=color,
            )
            await interaction.followup.send(
                f"Dog tag {decision.lower()} for <@{view.user_id}>.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    def compose_onto_stats(self, stats_image: Any, discord_id: int | None) -> Any:
        """Place the base tag and an optional approved overlay on a stat card."""
        if Image is None or discord_id is None or not self.base_tag_path.is_file():
            if Image is not None and discord_id is not None and not self._missing_base_logged:
                log.warning("Dog-tag base image is missing: %s", self.base_tag_path)
                self._missing_base_logged = True
            return stats_image

        x1, y1, x2, y2 = self.TARGET_BOX
        if stats_image.width < x2 or stats_image.height < y2:
            log.warning(
                "Player-stat template is too small for dog-tag target box %s",
                self.TARGET_BOX,
            )
            return stats_image

        with Image.open(self.base_tag_path) as source:
            tag = source.convert("RGBA")
        try:
            overlay_path = self.storage_path / f"{int(discord_id)}.png"
            if overlay_path.is_file():
                try:
                    overlay = self._load_overlay(overlay_path, tag.size)
                except Exception:
                    log.exception("Ignoring invalid approved dog-tag overlay %s", overlay_path)
                else:
                    try:
                        composite = Image.alpha_composite(tag, overlay)
                    finally:
                        overlay.close()
                    tag.close()
                    tag = composite

            resized = tag.resize((x2 - x1, y2 - y1), Image.Resampling.LANCZOS)
            try:
                stats_image.paste(resized, (x1, y1), resized)
            finally:
                resized.close()
        finally:
            tag.close()
        return stats_image

    async def _start_ipc_server(self) -> None:
        if not self._ipc_secret:
            log.warning(
                "Dog-tag IPC is disabled until the tagweb ipc_secret is configured in Red's vault"
            )
            return
        if self._runner is not None:
            return

        app = web.Application(client_max_size=self.MAX_IPC_BODY_BYTES)
        app.router.add_post("/tag_submission", self._handle_submission)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.IPC_HOST, self.IPC_PORT)
            await site.start()
        except Exception:
            await runner.cleanup()
            log.exception(
                "Could not start dog-tag IPC server on %s:%s",
                self.IPC_HOST,
                self.IPC_PORT,
            )
            return

        self._runner = runner
        self._site = site
        log.info("Dog-tag IPC server listening on %s:%s", self.IPC_HOST, self.IPC_PORT)

    async def _handle_submission(self, request: web.Request) -> web.Response:
        supplied_secret = request.headers.get("X-Tagweb-Secret", "")
        expected_secret = self._ipc_secret or ""
        if not supplied_secret or not secrets.compare_digest(supplied_secret, expected_secret):
            return web.json_response({"error": "forbidden"}, status=403)

        try:
            data = await request.json()
        except (ValueError, web.HTTPException):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(data, Mapping):
            return web.json_response({"error": "invalid payload"}, status=400)

        user_id = str(data.get("user_id", "")).strip()
        username = " ".join(str(data.get("username", "Unknown user")).split())[:100]
        submission_id = str(data.get("submission_id", "")).strip().lower()
        if not self._valid_user_id(user_id) or not self._valid_submission_id(submission_id):
            return web.json_response({"error": "invalid submission identifiers"}, status=400)

        async with self._review_lock(user_id):
            try:
                preview = await asyncio.to_thread(
                    self._render_preview,
                    user_id,
                    submission_id,
                )
            except FileNotFoundError:
                return web.json_response({"error": "staged file not found"}, status=404)
            except StaleDogTagSubmissionError:
                return web.json_response({"error": "staged file mismatch"}, status=409)
            except DogTagConfigurationError:
                log.exception("Dog-tag service is not ready to process a submission")
                return web.json_response({"error": "dog-tag service unavailable"}, status=503)
            except Exception:
                log.exception("Rejected invalid staged dog-tag overlay for user %s", user_id)
                return web.json_response({"error": "invalid staged image"}, status=422)

            channel = await self.get_review_channel()
            if channel is None:
                return web.json_response({"error": "review channel unavailable"}, status=503)

            view = DogTagReviewView(self, user_id, username, submission_id)
            embed = self._build_review_embed(user_id, username)
            embed.set_image(url="attachment://dog-tag-preview.png")
            try:
                message = await channel.send(
                    embed=embed,
                    file=discord.File(preview, filename="dog-tag-preview.png"),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.exception("Could not send dog-tag review for user %s", user_id)
                return web.json_response({"error": "review dispatch failed"}, status=503)

            previous = await self._set_pending(
                user_id,
                {
                    "username": username,
                    "channel_id": channel.id,
                    "message_id": message.id,
                    "submission_id": submission_id,
                    "submitted_at": int(time.time()),
                },
            )
            if previous is not None:
                await self._disable_previous_review(previous, user_id)
                previous_submission_id = str(previous.get("submission_id", "")).lower()
                if (
                    previous_submission_id != submission_id
                    and self._valid_submission_id(previous_submission_id)
                ):
                    await asyncio.to_thread(
                        self._delete_staging_submission,
                        user_id,
                        previous_submission_id,
                    )

        return web.json_response({"status": "queued"}, status=201)

    def _render_preview(self, user_id: str, submission_id: str) -> io.BytesIO:
        if Image is None:
            raise DogTagConfigurationError("Pillow is unavailable") from PILLOW_IMPORT_ERROR
        if not self.base_tag_path.is_file():
            raise DogTagConfigurationError(f"Base dog-tag image is missing: {self.base_tag_path}")
        staging_file = self._staging_file(user_id, submission_id)
        if not staging_file.is_file():
            raise FileNotFoundError(staging_file)
        self._verify_submission(staging_file, submission_id)

        with Image.open(self.base_tag_path) as source:
            base = source.convert("RGBA")
        try:
            overlay = self._load_overlay(staging_file, base.size)
            try:
                composite = Image.alpha_composite(base, overlay)
            finally:
                overlay.close()
            output = io.BytesIO()
            try:
                composite.save(output, format="PNG", compress_level=6)
            finally:
                composite.close()
        finally:
            base.close()
        output.seek(0)
        return output

    def _approve_file(self, user_id: str, submission_id: str) -> None:
        if Image is None:
            raise DogTagConfigurationError("Pillow is unavailable") from PILLOW_IMPORT_ERROR
        staging_file = self._staging_file(user_id, submission_id)
        target_file = self.storage_path / f"{user_id}.png"
        if not staging_file.is_file():
            raise FileNotFoundError(staging_file)
        if not self.base_tag_path.is_file():
            raise DogTagConfigurationError(f"Base dog-tag image is missing: {self.base_tag_path}")
        self._verify_submission(staging_file, submission_id)
        with Image.open(self.base_tag_path) as source:
            expected_size = source.size
        overlay = self._load_overlay(staging_file, expected_size)
        overlay.close()
        os.replace(staging_file, target_file)

    def _deny_file(self, user_id: str, submission_id: str) -> None:
        staging_file = self._staging_file(user_id, submission_id)
        if not staging_file.is_file():
            raise FileNotFoundError(staging_file)
        self._verify_submission(staging_file, submission_id)
        staging_file.unlink()

    def _delete_user_files(self, user_id: str) -> None:
        if not self._valid_user_id(user_id):
            return
        (self.storage_path / f"{user_id}.png").unlink(missing_ok=True)
        for path in self.staging_path.glob(f"{user_id}-*.png"):
            path.unlink(missing_ok=True)

    def _delete_staging_submission(self, user_id: str, submission_id: str) -> None:
        self._staging_file(user_id, submission_id).unlink(missing_ok=True)

    @staticmethod
    def _load_overlay(path: Path, expected_size: tuple[int, int]) -> Any:
        if Image is None:
            raise RuntimeError("Pillow is unavailable") from PILLOW_IMPORT_ERROR
        with Image.open(path) as source:
            if source.format != "PNG" or source.size != expected_size:
                raise ValueError("Overlay must be a PNG matching the base tag dimensions")
            source.load()
            return source.convert("RGBA")

    def _ensure_storage(self) -> bool:
        try:
            self.staging_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Could not create dog-tag staging directory %s", self.staging_path)
            return False
        return True

    async def _restore_persistent_views(self) -> None:
        pending = await self._get_pending()
        for user_id, record in pending.items():
            if not self._valid_user_id(user_id) or not isinstance(record, Mapping):
                continue
            message_id = self._record_id(record, "message_id")
            submission_id = str(record.get("submission_id", "")).lower()
            if message_id is None or not self._valid_submission_id(submission_id):
                continue
            username = str(record.get("username", "Unknown user"))[:100]
            self.cog.bot.add_view(
                DogTagReviewView(self, user_id, username, submission_id),
                message_id=message_id,
            )

    async def _get_pending(self) -> dict[str, dict[str, Any]]:
        value = await self.cog.config.dogtag_pending()
        if not isinstance(value, Mapping):
            return {}
        return {
            str(user_id): dict(record)
            for user_id, record in value.items()
            if isinstance(record, Mapping)
        }

    async def pending_count(self) -> int:
        return len(await self._get_pending())

    async def _set_pending(
        self,
        user_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self._pending_lock:
            pending = await self._get_pending()
            previous = pending.get(user_id)
            pending[user_id] = record
            await self.cog.config.dogtag_pending.set(pending)
            return previous

    async def _remove_pending(self, user_id: str, message_id: int) -> None:
        async with self._pending_lock:
            pending = await self._get_pending()
            record = pending.get(user_id)
            if isinstance(record, Mapping) and self._record_id(record, "message_id") == message_id:
                pending.pop(user_id, None)
                await self.cog.config.dogtag_pending.set(pending)

    async def _disable_previous_review(
        self,
        record: Mapping[str, Any],
        user_id: str,
    ) -> None:
        channel_id = self._record_id(record, "channel_id")
        message_id = self._record_id(record, "message_id")
        channel = self.cog.bot.get_channel(channel_id) if channel_id is not None else None
        if not isinstance(channel, discord.TextChannel) or message_id is None:
            return
        try:
            message = await channel.fetch_message(message_id)
            submission_id = str(record.get("submission_id", "")).lower()
            if not self._valid_submission_id(submission_id):
                return
            username = str(record.get("username", "Unknown user"))[:100]
            old_view = DogTagReviewView(self, user_id, username, submission_id)
            old_view.disable()
            await message.edit(view=old_view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Could not disable superseded dog-tag review message %s", message_id)

    @staticmethod
    async def _edit_review_message(
        message: discord.Message,
        view: DogTagReviewView,
        *,
        decision: str,
        reviewer: discord.abc.User,
        color: discord.Color,
    ) -> None:
        embed = message.embeds[0].copy() if message.embeds else discord.Embed()
        embed.color = color
        embed.add_field(
            name="Decision",
            value=f"{decision} by {reviewer.mention}",
            inline=False,
        )
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Could not update dog-tag review message %s", message.id)

    @staticmethod
    def _build_review_embed(user_id: str, username: str) -> discord.Embed:
        embed = discord.Embed(
            title="Dog Tag Carving Submission",
            description=f"User: <@{user_id}> ({username})\nDiscord ID: `{user_id}`",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Approve to publish this carving on the player's VN stat card.")
        return embed

    def _review_lock(self, user_id: str) -> asyncio.Lock:
        return self._review_locks.setdefault(user_id, asyncio.Lock())

    def _staging_file(self, user_id: str, submission_id: str) -> Path:
        if not self._valid_user_id(user_id) or not self._valid_submission_id(submission_id):
            raise ValueError("Invalid dog-tag staging identifier")
        return self.staging_path / f"{user_id}-{submission_id}.png"

    @staticmethod
    def _verify_submission(path: Path, submission_id: str) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not secrets.compare_digest(digest, submission_id):
            raise StaleDogTagSubmissionError

    @staticmethod
    def _record_id(record: Mapping[str, Any], key: str) -> int | None:
        value = record.get(key)
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _valid_user_id(user_id: str) -> bool:
        return user_id.isdigit() and 15 <= len(user_id) <= 22

    @staticmethod
    def _valid_submission_id(submission_id: str) -> bool:
        return len(submission_id) == 64 and all(
            character in "0123456789abcdef" for character in submission_id
        )


class DogTagCommandsMixin:
    """Authorized configuration commands for the dog-tag review pipeline."""

    @commands.hybrid_group(name="dogtag", invoke_without_command=True)
    @commands.guild_only()
    @requires_authorized_user()
    async def dogtag(self, ctx: commands.Context) -> None:
        """Configure the custom dog-tag review pipeline."""
        await ctx.send_help()

    @dogtag.command(name="setup")
    @app_commands.describe(channel="Staff channel that should receive dog-tag reviews.")
    @commands.guild_only()
    async def dogtag_setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        if ctx.guild is None:
            return
        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("The bot member is unavailable in this server.")
            return
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.attach_files
        ):
            await ctx.send(
                "I need View Channel, Send Messages, Embed Links, and Attach Files there."
            )
            return

        await self.dog_tags.set_review_channel(channel)
        warnings = []
        if not self.dog_tags.has_ipc_secret():
            warnings.append("the `tagweb` IPC secret is not configured")
        if not self.dog_tags.base_tag_path.is_file():
            warnings.append(f"the base image is missing at `{self.dog_tags.base_tag_path}`")
        suffix = f" Warning: {'; '.join(warnings)}." if warnings else ""
        await ctx.send(f"Dog-tag reviews will be sent to {channel.mention}.{suffix}")

    @dogtag.command(name="status")
    @commands.guild_only()
    async def dogtag_status(self, ctx: commands.Context) -> None:
        channel = await self.dog_tags.get_review_channel()
        pending_count = await self.dog_tags.pending_count()
        lines = [
            f"Review channel: {channel.mention if channel else 'not configured'}",
            f"Storage: `{self.dog_tags.storage_path}`",
            f"Base image: `{'ready' if self.dog_tags.base_tag_path.is_file() else 'missing'}`",
            f"IPC secret: `{'configured' if self.dog_tags.has_ipc_secret() else 'missing'}`",
            f"IPC listener: `{'running' if self.dog_tags.ipc_is_running() else 'stopped'}`",
            f"Pending reviews: `{pending_count}`",
        ]
        await ctx.send("Dog-tag pipeline status:\n" + "\n".join(f"- {line}" for line in lines))
