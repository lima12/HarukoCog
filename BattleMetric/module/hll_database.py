"""PostgreSQL account linking and batched HLL: Vietnam stat ingestion."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import discord
from discord import app_commands

log = logging.getLogger("red.BattleMetric.hll_database")

try:
    import asyncpg
except Exception as exc:  # noqa: BLE001 - keep the rest of the cog loadable
    asyncpg = None
    ASYNCPG_IMPORT_ERROR: Exception | None = exc
else:
    ASYNCPG_IMPORT_ERROR = None

try:
    from hllrcon.admin_logs import (
        HLLVPlayerKillAdminLog,
        HLLVPlayerSendMessageAdminLog,
        HLLVPlayerTeamKillAdminLog,
    )
except Exception as exc:  # noqa: BLE001 - reported separately from PostgreSQL
    HLLVPlayerKillAdminLog = None
    HLLVPlayerSendMessageAdminLog = None
    HLLVPlayerTeamKillAdminLog = None
    HLLRCON_MODEL_IMPORT_ERROR: Exception | None = exc
else:
    HLLRCON_MODEL_IMPORT_ERROR = None


class HLLDatabaseError(RuntimeError):
    """Base error for database-backed HLL features."""


class HLLDatabaseUnavailableError(HLLDatabaseError):
    """Raised when PostgreSQL is not configured or cannot be reached."""


class EOSAccountAlreadyLinkedError(HLLDatabaseError):
    """Raised when an EOS ID belongs to a different Discord account."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    schema: str


@dataclass(frozen=True)
class PendingLink:
    discord_id: int
    guild_id: int
    channel_id: int | None
    expires_at: float


@dataclass(frozen=True)
class StatDelta:
    eos_id: str
    kills: int
    deaths: int


class HLLDatabaseModule:
    """Link Discord users to EOS IDs and aggregate RCON combat statistics."""

    DB_SERVICE_NAME = "battlemetric_db"
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 5432
    DEFAULT_DATABASE = "slhhll"
    DEFAULT_SCHEMA = "slhhll"

    TOKEN_TTL_SECONDS = 300
    TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])VN-\d{4}(?![A-Z0-9])", re.IGNORECASE)
    FLUSH_INTERVAL_SECONDS = 3
    MAX_BATCH_RECORDS = 50
    MAX_QUEUE_SIZE = 5000
    MAX_SEEN_EVENTS = 20000
    DATABASE_TIMEOUT_SECONDS = 15

    LINK_UPSERT_SQL: ClassVar[str] = (
        'INSERT INTO slhhll."Discord" ("Discord_Id", "EOS_Id") '
        "VALUES ($1, $2) "
        'ON CONFLICT ("Discord_Id") DO UPDATE SET "EOS_Id" = EXCLUDED."EOS_Id"'
    )
    STATS_UPSERT_SQL: ClassVar[str] = (
        'INSERT INTO slhhll."RCON_DATA" ("EOS_Id", "Kill", "Dead") '
        "VALUES ($1, $2, $3) "
        'ON CONFLICT ("EOS_Id") DO UPDATE SET '
        '"Kill" = slhhll."RCON_DATA"."Kill" + EXCLUDED."Kill", '
        '"Dead" = slhhll."RCON_DATA"."Dead" + EXCLUDED."Dead"'
    )

    def __init__(self, cog: Any):
        self.cog = cog
        self._settings: DatabaseSettings | None = None
        self._pool: Any | None = None
        self._pool_lock = asyncio.Lock()
        self._tokens: dict[str, PendingLink] = {}
        self._claimed_tokens: set[str] = set()
        self._token_lock = asyncio.Lock()
        self._stat_queue: asyncio.Queue[StatDelta] = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._stats_task: asyncio.Task[None] | None = None
        self._stats_seen: OrderedDict[str, None] = OrderedDict()
        self._dropped_stats = 0
        self._last_drop_log_at = 0.0
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        await self.refresh_settings()
        if not self.is_available():
            log.error("HLL database module is unavailable: %s", self.dependency_error())
            return
        if self._stats_task is None or self._stats_task.done():
            self._stats_task = asyncio.create_task(
                self._stats_worker(),
                name="BattleMetric HLL database writer",
            )
        if self.has_credentials():
            try:
                await self.ensure_ready()
            except HLLDatabaseUnavailableError as exc:
                log.warning("Initial HLL database connection failed; the writer will retry: %s", exc)

    def stop(self) -> None:
        self._stopping = True
        if self._stats_task is not None:
            self._stats_task.cancel()
            self._stats_task = None
        self._tokens.clear()
        self._claimed_tokens.clear()
        self.cog.bot.loop.create_task(self.close())

    async def close(self) -> None:
        async with self._pool_lock:
            pool = self._pool
            self._pool = None
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=self.DATABASE_TIMEOUT_SECONDS)
            except TimeoutError:
                pool.terminate()

    @staticmethod
    def is_available() -> bool:
        return ASYNCPG_IMPORT_ERROR is None and HLLRCON_MODEL_IMPORT_ERROR is None

    @staticmethod
    def dependency_error() -> str | None:
        error = ASYNCPG_IMPORT_ERROR or HLLRCON_MODEL_IMPORT_ERROR
        return str(error) if error is not None else None

    def has_credentials(self) -> bool:
        return self._settings is not None

    def is_ready(self) -> bool:
        return self._pool is not None

    def should_poll(self, guild_id: int) -> bool:
        del guild_id
        return not self._stopping and self.is_available() and self.has_credentials()

    def queue_size(self) -> int:
        return self._stat_queue.qsize()

    async def refresh_settings(self) -> None:
        tokens = await self.cog.bot.get_shared_api_tokens(self.DB_SERVICE_NAME)
        await self.set_api_tokens(tokens)

    async def set_api_tokens(self, tokens: Mapping[str, str]) -> None:
        settings = self._parse_settings(tokens)
        if settings == self._settings:
            return
        self._settings = settings
        await self.close()

    @classmethod
    def _parse_settings(cls, tokens: Mapping[str, str]) -> DatabaseSettings | None:
        user_value = tokens.get("user")
        password_value = tokens.get("password")
        user = user_value.strip() if isinstance(user_value, str) else ""
        password = password_value if isinstance(password_value, str) else ""
        if not user or not password:
            return None

        host_value = tokens.get("host")
        database_value = tokens.get("database")
        schema_value = tokens.get("schema")
        host = host_value.strip() if isinstance(host_value, str) else cls.DEFAULT_HOST
        database = (
            database_value.strip()
            if isinstance(database_value, str)
            else cls.DEFAULT_DATABASE
        )
        schema = schema_value.strip() if isinstance(schema_value, str) else cls.DEFAULT_SCHEMA
        host = host or cls.DEFAULT_HOST
        database = database or cls.DEFAULT_DATABASE
        schema = schema or cls.DEFAULT_SCHEMA
        try:
            port = int(tokens.get("port", cls.DEFAULT_PORT))
        except (TypeError, ValueError):
            log.error("Ignoring HLL database configuration because its port is invalid")
            return None
        if not 1 <= port <= 65535:
            log.error("Ignoring HLL database configuration because its port is outside 1-65535")
            return None
        if schema != cls.DEFAULT_SCHEMA:
            log.error("Ignoring HLL database configuration because only schema %s is supported", cls.DEFAULT_SCHEMA)
            return None
        return DatabaseSettings(host, port, database, user, password, schema)

    async def ensure_ready(self) -> Any:
        if not self.is_available() or asyncpg is None:
            raise HLLDatabaseUnavailableError(
                "The asyncpg or hllrcon dependency is unavailable."
            )
        settings = self._settings
        if settings is None:
            raise HLLDatabaseUnavailableError("PostgreSQL credentials are not configured.")

        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            try:
                pool = await asyncio.wait_for(
                    asyncpg.create_pool(
                        host=settings.host,
                        port=settings.port,
                        database=settings.database,
                        user=settings.user,
                        password=settings.password,
                        min_size=1,
                        max_size=3,
                        command_timeout=self.DATABASE_TIMEOUT_SECONDS,
                        server_settings={"search_path": f"{settings.schema},public"},
                    ),
                    timeout=self.DATABASE_TIMEOUT_SECONDS,
                )
                await self._validate_schema(pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if "pool" in locals():
                    await pool.close()
                raise HLLDatabaseUnavailableError(
                    "PostgreSQL could not be reached or its HLL schema is unavailable."
                ) from exc
            self._pool = pool
            log.info(
                "Connected to HLL PostgreSQL database %s:%s/%s",
                settings.host,
                settings.port,
                settings.database,
            )
            return pool

    async def _validate_schema(self, pool: Any) -> None:
        async with pool.acquire() as connection:
            await connection.fetch(
                'SELECT "Discord_Id", "EOS_Id" FROM slhhll."Discord" LIMIT 0'
            )
            await connection.fetch(
                'SELECT "EOS_Id", "Kill", "Dead" FROM slhhll."RCON_DATA" LIMIT 0'
            )

    async def create_link_token(
        self,
        discord_id: int,
        guild_id: int,
        channel_id: int | None,
    ) -> tuple[str, float]:
        now = time.monotonic()
        expires_at = now + self.TOKEN_TTL_SECONDS
        async with self._token_lock:
            self._purge_expired_tokens(now)
            for token, pending in list(self._tokens.items()):
                if pending.discord_id == discord_id and pending.guild_id == guild_id:
                    self._tokens.pop(token, None)
                    self._claimed_tokens.discard(token)

            for _ in range(100):
                token = f"VN-{secrets.randbelow(10000):04d}"
                if token not in self._tokens:
                    self._tokens[token] = PendingLink(
                        discord_id=discord_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        expires_at=expires_at,
                    )
                    return token, expires_at
        raise HLLDatabaseError("Could not allocate a unique verification token.")

    async def ingest_admin_logs(self, guild: discord.Guild, entries: Sequence[Any]) -> None:
        if not self.should_poll(guild.id):
            return

        endpoint = await self.cog.kill_feed.get_settings(guild)
        endpoint_key = f"{endpoint.get('host')}:{endpoint.get('port')}"
        for entry in entries:
            if (
                HLLVPlayerSendMessageAdminLog is not None
                and isinstance(entry, HLLVPlayerSendMessageAdminLog)
            ):
                await self._handle_chat_entry(guild, entry)

            is_kill = HLLVPlayerKillAdminLog is not None and isinstance(
                entry,
                HLLVPlayerKillAdminLog,
            )
            is_team_kill = HLLVPlayerTeamKillAdminLog is not None and isinstance(
                entry,
                HLLVPlayerTeamKillAdminLog,
            )
            if not is_kill and not is_team_kill:
                continue

            fingerprint = f"{endpoint_key}\0{entry.timestamp.isoformat()}\0{entry.raw_message}"
            if not self._mark_stat_seen(fingerprint):
                continue
            self._enqueue_stat(StatDelta(str(entry.instigator_id), 1, 0))
            self._enqueue_stat(StatDelta(str(entry.victim_id), 0, 1))

    async def _handle_chat_entry(self, guild: discord.Guild, entry: Any) -> None:
        tokens = {match.group(0).upper() for match in self.TOKEN_PATTERN.finditer(entry.message)}
        for token in tokens:
            pending = await self._claim_token(token, guild.id)
            if pending is None:
                continue
            burn_token = False
            try:
                await self._upsert_link(pending.discord_id, str(entry.player_id))
                burn_token = True
            except EOSAccountAlreadyLinkedError:
                burn_token = True
                await self._notify_link_result(
                    guild,
                    pending,
                    success=False,
                    message="That HLL account is already linked to another Discord account.",
                )
            except Exception as exc:
                log.warning(
                    "Could not store HLL account link for Discord user %s: %s",
                    pending.discord_id,
                    exc,
                    exc_info=True,
                )
            else:
                await self._notify_link_result(
                    guild,
                    pending,
                    success=True,
                    message="Your Discord and HLL accounts are now linked.",
                )
            finally:
                await self._release_token(token, burn=burn_token)

    async def _claim_token(self, token: str, guild_id: int) -> PendingLink | None:
        now = time.monotonic()
        async with self._token_lock:
            self._purge_expired_tokens(now)
            pending = self._tokens.get(token)
            if pending is None or pending.guild_id != guild_id or token in self._claimed_tokens:
                return None
            self._claimed_tokens.add(token)
            return pending

    async def _release_token(self, token: str, *, burn: bool) -> None:
        async with self._token_lock:
            self._claimed_tokens.discard(token)
            if burn:
                self._tokens.pop(token, None)

    def _purge_expired_tokens(self, now: float) -> None:
        for token, pending in list(self._tokens.items()):
            if pending.expires_at <= now:
                self._tokens.pop(token, None)
                self._claimed_tokens.discard(token)

    async def _upsert_link(self, discord_id: int, eos_id: str) -> None:
        pool = await self.ensure_ready()
        discord_id_text = str(discord_id)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", eos_id)
            existing_discord_id = await connection.fetchval(
                'SELECT "Discord_Id" FROM slhhll."Discord" WHERE "EOS_Id" = $1 LIMIT 1',
                eos_id,
            )
            if existing_discord_id is not None and str(existing_discord_id) != discord_id_text:
                raise EOSAccountAlreadyLinkedError
            await connection.execute(self.LINK_UPSERT_SQL, discord_id_text, eos_id)

    async def _notify_link_result(
        self,
        guild: discord.Guild,
        pending: PendingLink,
        *,
        success: bool,
        message: str,
    ) -> None:
        color = discord.Color.green() if success else discord.Color.red()
        embed = discord.Embed(
            title="HLL Account Linked" if success else "HLL Account Link Failed",
            description=message,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        user = guild.get_member(pending.discord_id) or self.cog.bot.get_user(pending.discord_id)
        if user is None:
            try:
                user = await self.cog.bot.fetch_user(pending.discord_id)
            except (discord.HTTPException, discord.NotFound):
                user = None
        if user is not None:
            try:
                await user.send(embed=embed)
                return
            except discord.HTTPException:
                pass

        channel = (
            guild.get_channel_or_thread(pending.channel_id)
            if pending.channel_id is not None
            else None
        )
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    content=f"<@{pending.discord_id}>",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        roles=False,
                        users=True,
                    ),
                )
            except discord.HTTPException:
                log.warning("Could not deliver account-link result for user %s", pending.discord_id)

    def _mark_stat_seen(self, fingerprint: str) -> bool:
        if fingerprint in self._stats_seen:
            return False
        self._stats_seen[fingerprint] = None
        while len(self._stats_seen) > self.MAX_SEEN_EVENTS:
            self._stats_seen.popitem(last=False)
        return True

    def _enqueue_stat(self, delta: StatDelta) -> None:
        try:
            self._stat_queue.put_nowait(delta)
        except asyncio.QueueFull:
            self._dropped_stats += 1
            now = time.monotonic()
            if now - self._last_drop_log_at >= 60:
                log.warning(
                    "HLL stat queue is full; %s records have been dropped",
                    self._dropped_stats,
                )
                self._last_drop_log_at = now

    async def _stats_worker(self) -> None:
        retry_batch: list[StatDelta] | None = None
        retry_delay = self.FLUSH_INTERVAL_SECONDS
        try:
            while True:
                batch = retry_batch or await self._collect_stat_batch()
                try:
                    await self._flush_stat_batch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve batch and retry database failures
                    retry_batch = batch
                    log.warning(
                        "Could not flush %s HLL stat records; retrying in %s seconds: %s",
                        len(batch),
                        retry_delay,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(30, retry_delay * 2)
                else:
                    for _ in batch:
                        self._stat_queue.task_done()
                    if self._dropped_stats:
                        log.warning(
                            "HLL database writing recovered after %s queued records were dropped",
                            self._dropped_stats,
                        )
                        self._dropped_stats = 0
                    retry_batch = None
                    retry_delay = self.FLUSH_INTERVAL_SECONDS
        except asyncio.CancelledError:
            return

    async def _collect_stat_batch(self) -> list[StatDelta]:
        batch = [await self._stat_queue.get()]
        deadline = asyncio.get_running_loop().time() + self.FLUSH_INTERVAL_SECONDS
        while len(batch) < self.MAX_BATCH_RECORDS:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._stat_queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _flush_stat_batch(self, batch: Sequence[StatDelta]) -> None:
        aggregated: dict[str, list[int]] = {}
        for delta in batch:
            totals = aggregated.setdefault(delta.eos_id, [0, 0])
            totals[0] += delta.kills
            totals[1] += delta.deaths

        pool = await self.ensure_ready()
        async with pool.acquire() as connection, connection.transaction():
            linked_rows = await connection.fetch(
                'SELECT "EOS_Id" FROM slhhll."Discord" WHERE "EOS_Id" = ANY($1::text[])',
                list(aggregated),
            )
            linked_ids = {str(row["EOS_Id"]) for row in linked_rows}
            updates = [
                (eos_id, totals[0], totals[1])
                for eos_id, totals in aggregated.items()
                if eos_id in linked_ids
            ]
            if updates:
                await connection.executemany(self.STATS_UPSERT_SQL, updates)

    async def delete_user_data(self, user_id: int) -> None:
        async with self._token_lock:
            for token, pending in list(self._tokens.items()):
                if pending.discord_id == user_id:
                    self._tokens.pop(token, None)
                    self._claimed_tokens.discard(token)

        if not self.has_credentials():
            return
        pool = await self.ensure_ready()
        discord_id = str(user_id)
        async with pool.acquire() as connection, connection.transaction():
            eos_id = await connection.fetchval(
                'SELECT "EOS_Id" FROM slhhll."Discord" WHERE "Discord_Id" = $1',
                discord_id,
            )
            if eos_id is not None:
                await connection.execute(
                    'DELETE FROM slhhll."RCON_DATA" WHERE "EOS_Id" = $1',
                    str(eos_id),
                )
            await connection.execute(
                'DELETE FROM slhhll."Discord" WHERE "Discord_Id" = $1',
                discord_id,
            )


class HLLDatabaseCommandsMixin:
    """Public account-link command backed by the shared HLL RCON stream."""

    @app_commands.command(name="link", description="Link your Discord account to your HLL account.")
    @app_commands.guild_only()
    async def link_hll_account(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        if not self.hll_database.is_available():
            await interaction.followup.send(
                "Account linking is unavailable because a required dependency did not load.",
                ephemeral=True,
            )
            return

        await self.hll_database.refresh_settings()
        await self.kill_feed.refresh_password()
        rcon_settings = await self.kill_feed.get_settings(guild)
        if not rcon_settings.get("host") or not rcon_settings.get("port"):
            await interaction.followup.send(
                "Account linking is not configured for this server.",
                ephemeral=True,
            )
            return
        if not self.kill_feed.has_password():
            await interaction.followup.send(
                "Account linking is unavailable because RCON credentials are missing.",
                ephemeral=True,
            )
            return
        try:
            await self.hll_database.ensure_ready()
            await self.kill_feed.test_connection(guild)
            token, expires_at = await self.hll_database.create_link_token(
                interaction.user.id,
                guild.id,
                interaction.channel_id,
            )
        except HLLDatabaseError as exc:
            log.warning("Could not start account linking for user %s: %s", interaction.user.id, exc)
            await interaction.followup.send(
                "Account linking is temporarily unavailable because the database could not be reached.",
                ephemeral=True,
            )
            return
        except (RuntimeError, ValueError) as exc:  # RCON details are logged by kill-feed
            log.warning("Could not start account linking because RCON is unavailable: %s", exc)
            await interaction.followup.send(
                "Account linking is temporarily unavailable because the HLL RCON server "
                "could not be reached.",
                ephemeral=True,
            )
            return

        expires_unix = int(discord.utils.utcnow().timestamp() + (expires_at - time.monotonic()))
        embed = discord.Embed(
            title="Link Your HLL Account",
            description=(
                f"Send `{token}` in this server's in-game **Unit** or **Team** chat.\n"
                f"This token expires <t:{expires_unix}:R> and can be used once."
            ),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
