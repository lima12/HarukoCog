import logging
from typing import Optional

import discord
from redbot.core import Config, commands

from .api import BattleMetricsClient
from .commands_mixin import BattleMetricCommandsMixin


log = logging.getLogger("red.BattleMetric")


class BattleMetric(BattleMetricCommandsMixin, commands.Cog):
    """BattleMetrics API cog with a reusable async API layer."""

    __author__ = "Haruko"
    __version__ = "0.1.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x424D45545249435F01,
            force_registration=True,
        )
        self.config.register_global(api_token=None)
        self.config.register_guild(default_server_id=None, default_game=None)
        self.api = BattleMetricsClient()

    async def cog_load(self) -> None:
        self.api.set_token(await self.get_api_token())

    def cog_unload(self) -> None:
        self.bot.loop.create_task(self.api.close())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        return None

    async def get_api_token(self) -> Optional[str]:
        token = await self.config.api_token()
        token = token.strip() if isinstance(token, str) and token.strip() else None
        self.api.set_token(token)
        return token

    async def has_api_token(self) -> bool:
        return bool(await self.get_api_token())

    async def set_api_token(self, token: Optional[str]) -> None:
        token = token.strip() if token else None
        await self.config.api_token.set(token)
        self.api.set_token(token)

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
