"""Feature modules for the BattleMetric cog."""

from .admin_ping import HLLAdminPingCommandsMixin, HLLAdminPingModule
from .dog_tags import DogTagCommandsMixin, DogTagModule
from .hll_admin import HLLAdminCommandsMixin, HLLAdminModule
from .hll_ban import HLLBanCommandsMixin, HLLBanModule
from .hll_database import HLLDatabaseCommandsMixin, HLLDatabaseModule
from .hll_maps import HLLMapCommandsMixin, HLLMapModule
from .hll_messaging import HLLMessagingCommandsMixin, HLLMessagingModule
from .hll_tk_watch import HLLTKWatchCommandsMixin, HLLTKWatchModule
from .hll_vip import HLLVIPCommandsMixin, HLLVIPModule
from .join_notice import HLLJoinNoticeCommandsMixin, HLLJoinNoticeModule
from .kill_feed import KillFeedCommandsMixin, KillFeedModule
from .map_vote import HLLMapVoteCommandsMixin, HLLMapVoteModule
from .player_stats import PlayerStatsCommandsMixin, PlayerStatsModule
from .player_flags import PlayerFlagsModule
from .seeding import HLLSeedingCommandsMixin, HLLSeedingModule
from .server_info import ServerInfoCommandsMixin, ServerInfoModule

__all__ = (
    "DogTagCommandsMixin",
    "DogTagModule",
    "HLLAdminCommandsMixin",
    "HLLAdminModule",
    "HLLAdminPingCommandsMixin",
    "HLLAdminPingModule",
    "HLLBanCommandsMixin",
    "HLLBanModule",
    "HLLDatabaseCommandsMixin",
    "HLLDatabaseModule",
    "HLLMapCommandsMixin",
    "HLLMapModule",
    "HLLMessagingCommandsMixin",
    "HLLMessagingModule",
    "HLLJoinNoticeCommandsMixin",
    "HLLJoinNoticeModule",
    "HLLSeedingCommandsMixin",
    "HLLSeedingModule",
    "HLLTKWatchCommandsMixin",
    "HLLTKWatchModule",
    "HLLVIPCommandsMixin",
    "HLLVIPModule",
    "KillFeedCommandsMixin",
    "KillFeedModule",
    "HLLMapVoteCommandsMixin",
    "HLLMapVoteModule",
    "PlayerStatsCommandsMixin",
    "PlayerStatsModule",
    "PlayerFlagsModule",
    "ServerInfoCommandsMixin",
    "ServerInfoModule",
)
