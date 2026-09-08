import logging
from typing import Mapping, Optional, Set

import discord
from redbot.core import Config, commands

from .api import BattleMetricsClient
from .authorization import BattleMetricAuthorizationError
from .commands_mixin import BattleMetricCommandsMixin
from .module import (
    KillFeedCommandsMixin,
    KillFeedModule,
    ServerInfoCommandsMixin,
    ServerInfoModule,
)


log = logging.getLogger("red.BattleMetric")


class BattleMetric(
    BattleMetricCommandsMixin,
    ServerInfoCommandsMixin,
    KillFeedCommandsMixin,
    commands.Cog,
):
    """BattleMetrics API cog with a reusable async API layer."""

    __author__ = "Haruko"
    __version__ = "0.4.0"

    API_SERVICE_NAME = "battlemetrics"
    API_TOKEN_NAME = "api_key"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x424D45545249435F01,
            force_registration=True,
        )
        # api_token is retained only to migrate installations from version 0.2.0.
        self.config.register_global(api_token=None)
        self.config.register_guild(
            default_server_id=None,
            default_game=None,
            authorized_user_ids=[],
        )
        self.api = BattleMetricsClient()
        self.server_info = ServerInfoModule(self)
        self.kill_feed = KillFeedModule(self)
        self.server_info.register_config()
        self.kill_feed.register_config()

    async def cog_load(self) -> None:
        await self._migrate_legacy_api_token()
        self.api.set_token(await self.get_api_token())
        await self.server_info.start()
        await self.kill_feed.start()

    def cog_unload(self) -> None:
        self.server_info.stop()
        self.kill_feed.stop()
        self.bot.loop.create_task(self.api.close())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        for guild_id, guild_data in (await self.config.all_guilds()).items():
            authorized_user_ids = guild_data.get("authorized_user_ids", [])
            if user_id not in authorized_user_ids:
                continue
            await self.config.guild_from_id(guild_id).authorized_user_ids.set(
                [member_id for member_id in authorized_user_ids if member_id != user_id]
            )

    async def get_api_token(self) -> Optional[str]:
        tokens = await self.bot.get_shared_api_tokens(self.API_SERVICE_NAME)
        token = tokens.get(self.API_TOKEN_NAME)
        token = token.strip() if isinstance(token, str) and token.strip() else None
        self.api.set_token(token)
        return token

    async def has_api_token(self) -> bool:
        return bool(await self.get_api_token())

    async def on_red_api_tokens_update(
        self,
        service_name: str,
        api_tokens: Mapping[str, str],
    ) -> None:
        if service_name == self.kill_feed.RCON_SERVICE_NAME:
            self.kill_feed.set_password(api_tokens.get(self.kill_feed.RCON_PASSWORD_NAME))
            return
        if service_name != self.API_SERVICE_NAME:
            return
        token = api_tokens.get(self.API_TOKEN_NAME)
        self.api.set_token(token.strip() if isinstance(token, str) and token.strip() else None)

    async def _migrate_legacy_api_token(self) -> None:
        """Move a token saved by the first cog version into Red's shared vault."""
        legacy_token = await self.config.api_token()
        legacy_token = legacy_token.strip() if isinstance(legacy_token, str) and legacy_token.strip() else None
        if not legacy_token:
            return

        vault_tokens = await self.bot.get_shared_api_tokens(self.API_SERVICE_NAME)
        if not vault_tokens.get(self.API_TOKEN_NAME):
            await self.bot.set_shared_api_tokens(
                self.API_SERVICE_NAME,
                **{self.API_TOKEN_NAME: legacy_token},
            )
            log.info("Migrated BattleMetrics API token into Red shared API-token storage")
        await self.config.api_token.set(None)

    async def get_authorized_user_ids(self, guild: discord.Guild) -> Set[int]:
        stored_ids = await self.config.guild(guild).authorized_user_ids()
        return {int(member_id) for member_id in stored_ids if str(member_id).isdigit()}

    async def add_authorized_user(self, guild: discord.Guild, user_id: int) -> bool:
        authorized_user_ids = await self.get_authorized_user_ids(guild)
        if user_id in authorized_user_ids:
            return False
        authorized_user_ids.add(user_id)
        await self.config.guild(guild).authorized_user_ids.set(sorted(authorized_user_ids))
        return True

    async def remove_authorized_user(self, guild: discord.Guild, user_id: int) -> bool:
        authorized_user_ids = await self.get_authorized_user_ids(guild)
        if user_id not in authorized_user_ids:
            return False
        authorized_user_ids.remove(user_id)
        await self.config.guild(guild).authorized_user_ids.set(sorted(authorized_user_ids))
        return True

    async def is_authorized(self, user: discord.abc.User) -> bool:
        if await self.bot.is_owner(user):
            return True
        if not isinstance(user, discord.Member):
            return False
        return user.id in await self.get_authorized_user_ids(user.guild)

    async def can_manage_authorization(self, user: discord.abc.User) -> bool:
        if await self.bot.is_owner(user):
            return True
        return isinstance(user, discord.Member) and (
            user.guild_permissions.administrator or user.guild_permissions.manage_guild
        )

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, BattleMetricAuthorizationError):
            await ctx.send("You are not authorized to use BattleMetric commands in this server.")
            return
        raise error

    async def get_default_server_id(self, guild: discord.Guild) -> Optional[str]:
        server_id = await self.config.guild(guild).default_server_id()
        return str(server_id).strip() if server_id else None

    async def set_default_server_id(self, guild: discord.Guild, server_id: Optional[str]) -> None:
        server_id = server_id.strip() if server_id else None
        await self.config.guild(guild).default_server_id.set(server_id)

    async def get_default_game(self, guild: discord.Guild) -> Optional[str]:
        game = await self.config.guild(guild).default_game()
        return str(game).strip().lower() if game else None

    async def set_default_game(self, guild: discord.Guild, game: Optional[str]) -> None:
        game = game.strip().lower() if game else None
        await self.config.guild(guild).default_game.set(game)
