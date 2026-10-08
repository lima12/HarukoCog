"""TK moderation regressions. Requires discord.py, but no Red or live RCON."""

import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

try:
    import discord
except ImportError:
    discord = None

tk = None
if discord is not None:
    root = Path(__file__).resolve().parents[1] / "BattleMetric/module"
    package = types.ModuleType("_tk_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    dependency = types.ModuleType("_tk_test.kill_feed")
    dependency.KillFeedConnectionTestError = type("KillFeedConnectionTestError", (Exception,), {})
    sys.modules[dependency.__name__] = dependency
    spec = importlib.util.spec_from_file_location("_tk_test.hll_tk_watch", root / "hll_tk_watch.py")
    tk = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tk
    spec.loader.exec_module(tk)


class Value:
    def __init__(self, value):
        self.value = value

    async def __call__(self):
        return copy.deepcopy(self.value)

    async def set(self, value):
        self.value = copy.deepcopy(value)


class Config:
    def __init__(self, settings):
        self.stored = Value(settings)

    def guild(self, guild):
        return NS(hll_tk_watch=self.stored)

    def guild_from_id(self, guild_id):
        return self.guild(None)

    async def all_guilds(self):
        return {1: {"hll_tk_watch": await self.stored()}}


class Message:
    def __init__(self, message_id, kwargs):
        self.id = message_id
        self.embeds = [kwargs["embed"]]
        self.view = kwargs.get("view")
        self.delete = AsyncMock()

    async def fetch(self):
        return self

    async def edit(self, **kwargs):
        self.embeds = [kwargs["embed"]]
        self.view = kwargs["view"]


class Channel:
    id = 2
    mention = "<#2>"

    def __init__(self):
        self.messages = {}
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = Message(len(self.messages) + 10, kwargs)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return self.messages[message_id]


@unittest.skipIf(tk is None, "discord.py is required for TK workflow tests")
class TKWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.seconds = 0
        self.utc_patch = patch.object(discord.utils, "utcnow", side_effect=lambda: self.base + timedelta(seconds=self.seconds))
        self.mono_patch = patch.object(tk, "time", NS(monotonic=lambda: 1000 + self.seconds))
        self.utc_patch.start()
        self.mono_patch.start()
        self.addCleanup(self.utc_patch.stop)
        self.addCleanup(self.mono_patch.stop)
        self.channel = Channel()
        self.role = NS(id=3, mention="<@&3>", is_default=lambda: False)
        self.guild = NS(id=1, get_channel_or_thread=lambda _: self.channel, get_role=lambda _: self.role)
        settings = dict(tk.HLLTKWatchModule._EMPTY_SETTINGS)
        settings.update(enabled=True, channel_id=2, role_id=3, exclude_commander=False)
        self.config = Config(settings)
        self.client = NS(
            message_player=AsyncMock(), kick_player=AsyncMock(),
            get_players=AsyncMock(return_value=NS(players=[NS(eos_id="eos")])),
        )

        async def execute(guild, stage, operation):
            return await operation(self.client)

        self.cog = NS(
            config=self.config,
            bot=NS(guilds=[self.guild], add_view=lambda *args, **kwargs: None),
            is_authorized=AsyncMock(return_value=True),
            kill_feed=NS(execute_rcon=execute),
        )
        self.module = tk.HLLTKWatchModule(self.cog)
        self.module._enabled_guilds.add(1)
        self.module._get_role = AsyncMock(return_value=(False, "Rifleman"))

    def event(self, seconds=None):
        return tk.HLLTeamKillEvent("Player @everyone", "eos", "Allies", "Victim", "victim", "rifle", self.base + timedelta(seconds=self.seconds if seconds is None else seconds))

    async def threshold(self):
        for _ in range(3):
            await self.module._process_event(self.guild, self.event())
        return list(self.module._alerts[1].values())[-1]

    def interaction(self, record):
        return NS(
            guild=self.guild, user=NS(mention="<@4>"),
            message=self.channel.messages[record.message_id],
            response=NS(send_message=AsyncMock(), defer=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )

    async def test_threshold_warns_once_and_only_pings_configured_role(self):
        record = await self.threshold()
        self.assertTrue(record.warning_sent)
        self.assertEqual(self.client.message_player.await_count, 1)
        self.assertFalse(self.module._watches.get(1))
        sent = self.channel.sent[0]
        self.assertEqual(sent["content"], "<@&3>")
        self.assertEqual(sent["allowed_mentions"].roles, [self.role])
        self.assertFalse(sent["allowed_mentions"].users)
        self.assertFalse(sent["allowed_mentions"].everyone)
        self.assertEqual([button.label for button in self.channel.messages[record.message_id].view.children], ["Forgive", "Warn & Watch", "Kick"])
        await self.module._process_event(self.guild, self.event())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.client.message_player.await_count, 1)

    async def test_five_minute_default_keeps_forgive_available(self):
        record = await self.threshold()
        self.seconds = 299
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))
        self.seconds = 300
        await self.module._process_alert_timers()
        current = self.module._alerts[1][record.message_id]
        self.assertEqual(current.status, "watching")
        self.assertEqual(self.client.message_player.await_count, 2)
        watch = self.module._watches[1]["eos"]
        self.assertEqual(watch.starts_at, int((self.base + timedelta(seconds=300)).timestamp()))
        self.assertEqual(watch.expires_at - watch.starts_at, 15 * 60)
        view = self.channel.messages[record.message_id].view
        self.assertFalse(view.forgive.disabled)
        self.assertTrue(view.warn_watch.disabled)
        await self.module._process_alert_timers()
        self.assertEqual(self.client.message_player.await_count, 2)

    async def test_forgive_before_default_cancels_it_and_preserves_embed(self):
        record = await self.threshold()
        self.seconds = 100
        await self.module.handle_action(self.interaction(record), "forgive")
        self.seconds = 301
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))
        self.assertEqual(self.client.message_player.await_count, 1)
        message = self.channel.messages[record.message_id]
        self.assertIsNone(message.view)
        message.delete.assert_not_awaited()
        self.assertIn("Latest team kill", [field.name for field in message.embeds[0].fields])

    async def test_forgive_after_default_cancels_watch_and_stale_kick(self):
        record = await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        watch = self.module._watches[1]["eos"]
        self.seconds = 600
        await self.module.handle_action(self.interaction(record), "forgive")
        await self.module._kick_watched_player(self.guild, self.event(), watch, await self.module.get_settings(self.guild))
        self.client.kick_player.assert_not_awaited()
        self.assertFalse(self.module._watches[1])

    async def test_manual_watch_allows_forgiveness(self):
        record = await self.threshold()
        self.seconds = 2
        await self.module.handle_action(self.interaction(record), "warn_watch")
        self.assertEqual(self.module._alerts[1][record.message_id].status, "watching")
        await self.module.handle_action(self.interaction(record), "forgive")
        self.assertFalse(self.module._watches[1])

    async def test_expiry_removes_buttons_but_keeps_message_and_watch(self):
        record = await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        self.seconds = 900
        await self.module.handle_action(self.interaction(record), "forgive")
        self.assertIn("eos", self.module._watches[1])
        self.assertIsNone(self.channel.messages[record.message_id].view)
        self.channel.messages[record.message_id].delete.assert_not_awaited()

    async def test_warning_and_watch_survive_reload(self):
        record = await self.threshold()
        reloaded = tk.HLLTKWatchModule(self.cog)
        reloaded._get_role = self.module._get_role
        await reloaded._load_state()
        await reloaded._process_alert_timers()
        self.assertEqual(self.client.message_player.await_count, 1)
        self.seconds = 300
        await reloaded._process_alert_timers()
        self.assertIn("eos", reloaded._watches[1])
        self.assertEqual(reloaded._alerts[1][record.message_id].status, "watching")
        second_reload = tk.HLLTKWatchModule(self.cog)
        await second_reload._load_state()
        self.seconds = 600
        await second_reload.handle_action(self.interaction(record), "forgive")
        self.assertFalse(second_reload._watches[1])

    async def test_failure_retries_without_creating_watch_or_repinging(self):
        record = await self.threshold()
        self.client.message_player.side_effect = RuntimeError("mock RCON failure")
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
        attempts = self.client.message_player.await_count
        await self.module._process_alert_timers()
        self.assertEqual(self.client.message_player.await_count, attempts)
        self.client.message_player.side_effect = None
        self.seconds = 303
        await self.module._process_alert_timers()
        self.assertIn("eos", self.module._watches[1])
        self.assertEqual(len(self.channel.sent), 1)

    async def test_initial_warning_failure_retries_without_duplicate_alert(self):
        self.client.message_player.side_effect = RuntimeError("mock warning failure")
        record = await self.threshold()
        self.assertFalse(record.warning_sent)
        self.assertFalse(self.module._watches.get(1))
        self.client.message_player.side_effect = None
        self.seconds = 3
        await self.module._process_alert_timers()
        self.assertTrue(self.module._alerts[1][record.message_id].warning_sent)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_offline_before_initial_warning_closes_without_watch_or_retries(self):
        self.client.message_player.side_effect = RuntimeError("player not found")
        self.client.get_players.return_value = NS(players=[])
        record = await self.threshold()
        self.assertEqual(record.status, "closed")
        self.assertFalse(record.warning_sent)
        self.assertIn("disconnected before action", record.decision)
        self.assertFalse(self.module._watches.get(1))
        self.assertFalse(self.module._timer_failures)
        self.assertFalse(self.module._next_timer_at)
        self.assertNotIn("eos", self.module._open_alert_players[1])
        message = self.channel.messages[record.message_id]
        self.assertIsNone(message.view)
        self.assertEqual(message.embeds[0].color, discord.Color.orange())
        message.delete.assert_not_awaited()
        self.seconds = 301
        await self.module._process_alert_timers()
        self.assertEqual(self.client.message_player.await_count, 1)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_offline_five_minute_default_closes_and_survives_reload(self):
        record = await self.threshold()
        self.client.message_player.side_effect = RuntimeError("player not found")
        self.client.get_players.return_value = NS(players=[NS(eos_id="another-player")])
        self.seconds = 300
        await self.module._process_alert_timers()
        closed = self.module._alerts[1][record.message_id]
        self.assertEqual(closed.status, "closed")
        self.assertTrue(closed.warning_sent)
        self.assertFalse(self.module._watches.get(1))
        reloaded = tk.HLLTKWatchModule(self.cog)
        await reloaded._load_state()
        self.assertEqual(reloaded._alerts[1][record.message_id].status, "closed")
        self.assertNotIn("eos", reloaded._open_alert_players[1])
        self.client.get_players.return_value = NS(players=[NS(eos_id="eos")])
        await reloaded._process_alert_timers()
        self.assertEqual(self.client.message_player.await_count, 2)
        self.seconds = 900
        await reloaded._cleanup_expired_state()
        self.assertNotIn(record.message_id, reloaded._alerts[1])
        self.channel.messages[record.message_id].delete.assert_not_awaited()

    async def test_offline_manual_warn_and_kick_close_without_false_success(self):
        for action in ("warn_watch", "kick"):
            with self.subTest(action=action):
                self.client.message_player.side_effect = None
                record = await self.threshold()
                self.client.message_player.side_effect = RuntimeError("player not found")
                self.client.kick_player.side_effect = RuntimeError("player not found")
                self.client.get_players.return_value = NS(players=[])
                interaction = self.interaction(record)
                await self.module.handle_action(interaction, action)
                closed = self.module._alerts[1][record.message_id]
                self.assertEqual(closed.status, "closed")
                self.assertIn("<@4>", closed.decision)
                self.assertTrue(interaction.followup.send.await_args.args[0].startswith("Case closed:"))
                self.assertFalse(self.module._watches.get(1))
                self.assertIsNone(self.channel.messages[record.message_id].view)
                self.seconds += 3

    async def test_offline_after_commander_lookup_failure_closes_case(self):
        record = await self.threshold()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = None
        self.client.get_players.return_value = NS(players=[])
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertEqual(self.module._alerts[1][record.message_id].status, "closed")
        self.assertEqual(self.client.message_player.await_count, 1)

    async def test_offline_manual_commander_lookup_failure_closes_case(self):
        record = await self.threshold()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = None
        self.client.get_players.return_value = NS(players=[])
        await self.module.handle_action(self.interaction(record), "kick")
        self.assertEqual(self.module._alerts[1][record.message_id].status, "closed")
        self.client.kick_player.assert_not_awaited()

    async def test_rcon_outage_does_not_close_case(self):
        record = await self.threshold()
        self.client.message_player.side_effect = RuntimeError("RCON timeout")
        self.client.get_players.side_effect = RuntimeError("RCON timeout")
        self.seconds = 300
        with self.assertLogs(tk.log, level="WARNING"):
            await self.module._process_alert_timers()
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
        self.assertIn(record.message_id, self.module._next_timer_at)
        self.assertIsNotNone(self.channel.messages[record.message_id].view)
        self.assertFalse(self.module._watches.get(1))

    async def test_malformed_roster_never_proves_absence(self):
        record = await self.threshold()
        for players in (None, "", {}, [NS()], [NS(eos_id="")], [NS(eos_id=None)]):
            with self.subTest(players=players):
                self.client.get_players.return_value = NS(players=players)
                result = await self.module._close_if_player_offline(self.guild, record, actor="test")
                self.assertIsNone(result)
                self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
                self.seconds += 3

    async def test_player_identity_comparison_is_exact_and_case_insensitive(self):
        record = await self.threshold()
        self.client.get_players.return_value = NS(players=[NS(eos_id=" EOS ")])
        self.assertIsNone(await self.module._close_if_player_offline(self.guild, record, actor="test"))
        self.seconds += 3
        self.client.get_players.return_value = NS(players=[NS(eos_id="eos-other")])
        result = await self.module._close_if_player_offline(self.guild, record, actor="test")
        self.assertEqual(result.status, "closed")

    async def test_failed_kick_for_offline_watched_player_keeps_watch_across_reload(self):
        record = await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        watch = self.module._watches[1]["eos"]
        self.client.kick_player.side_effect = RuntimeError("player not found")
        self.client.get_players.return_value = NS(players=[])
        await self.module.handle_action(self.interaction(record), "kick")
        closed = self.module._alerts[1][record.message_id]
        self.assertEqual(closed.status, "closed")
        self.assertIn("existing watch remains", closed.decision)
        self.assertEqual(self.module._watches[1]["eos"], watch)
        reloaded = tk.HLLTKWatchModule(self.cog)
        await reloaded._load_state()
        self.assertEqual(reloaded._watches[1]["eos"], watch)
        self.client.kick_player.side_effect = None
        self.seconds = 301
        await reloaded._process_event(self.guild, self.event())
        self.assertEqual(self.client.kick_player.await_count, 2)
        self.assertFalse(reloaded._watches[1])

    async def test_offline_checks_are_paced_including_manual_failures(self):
        record = await self.threshold()
        self.client.kick_player.side_effect = RuntimeError("action failed")
        for _ in range(3):
            with self.assertLogs(tk.log, level="ERROR"):
                await self.module.handle_action(self.interaction(record), "kick")
        self.assertEqual(self.client.get_players.await_count, 1)
        self.client.get_players.return_value = NS(players=[])
        self.seconds = 3
        await self.module.handle_action(self.interaction(record), "kick")
        self.assertEqual(self.client.get_players.await_count, 2)
        self.assertEqual(self.module._alerts[1][record.message_id].status, "closed")

    async def test_disable_during_offline_check_does_not_resurrect_alert(self):
        record = await self.threshold()
        async def roster():
            await self.module.disable(self.guild)
            return NS(players=[])
        self.client.get_players.side_effect = roster
        result = await self.module._close_if_player_offline(self.guild, record, actor="test")
        self.assertIsNone(result)
        self.assertFalse(self.module._alerts.get(1))
        self.assertFalse(self.module._next_offline_check_at)

    async def test_offline_close_does_not_override_forgiveness(self):
        record = await self.threshold()
        async def roster():
            await self.module._perform_action(self.guild, record, "forgive", actor="staff")
            return NS(players=[])
        self.client.get_players.side_effect = roster
        result = await self.module._close_if_player_offline(self.guild, record, actor="test")
        self.assertIsNone(result)
        self.assertEqual(self.module._alerts[1][record.message_id].status, "resolved")
        self.assertIn("Forgiven", self.module._alerts[1][record.message_id].decision)

    async def test_config_write_failure_keeps_case_pending(self):
        record = await self.threshold()
        self.client.get_players.return_value = NS(players=[])
        self.config.stored.set = AsyncMock(side_effect=RuntimeError("storage unavailable"))
        with self.assertLogs(tk.log, level="WARNING"):
            result = await self.module._close_if_player_offline(self.guild, record, actor="test")
        self.assertIsNone(result)
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
        self.assertIn("eos", self.module._open_alert_players[1])

    async def test_offline_check_cancellation_propagates_without_closing(self):
        record = await self.threshold()
        self.client.get_players.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.module._close_if_player_offline(self.guild, record, actor="test")
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")

    async def test_healthy_actions_and_forgiveness_do_not_fetch_roster(self):
        record = await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        await self.module.handle_action(self.interaction(record), "forgive")
        self.client.get_players.assert_not_awaited()

    async def test_closed_case_stale_buttons_do_not_issue_another_action(self):
        self.client.message_player.side_effect = RuntimeError("player not found")
        self.client.get_players.return_value = NS(players=[])
        record = await self.threshold()
        warnings = self.client.message_player.await_count
        rosters = self.client.get_players.await_count
        interaction = self.interaction(record)
        await self.module.handle_action(interaction, "kick")
        self.client.kick_player.assert_not_awaited()
        self.assertEqual(self.client.message_player.await_count, warnings)
        self.assertEqual(self.client.get_players.await_count, rosters)
        self.assertIn("disconnected before action", interaction.followup.send.await_args.args[0])

    async def test_overdue_default_retains_retry_record_after_controls_expire(self):
        record = await self.threshold()
        self.client.message_player.side_effect = RuntimeError("mock offline RCON")
        self.seconds = 901
        await self.module._process_alert_timers()
        await self.module._cleanup_expired_state()
        self.assertIn(record.message_id, self.module._alerts[1])
        self.assertIsNone(self.channel.messages[record.message_id].view)
        self.client.message_player.side_effect = None
        self.seconds = 904
        await self.module._process_alert_timers()
        self.assertIn("eos", self.module._watches[1])
        await self.module._cleanup_expired_state()
        self.assertNotIn(record.message_id, self.module._alerts[1])
        self.channel.messages[record.message_id].delete.assert_not_awaited()

    async def test_commander_exclusion_cancels_default(self):
        record = await self.threshold()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = (True, "Commander")
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))
        self.assertEqual(self.module._alerts[1][record.message_id].status, "resolved")
        self.assertEqual(self.client.message_player.await_count, 1)
        self.assertIsNone(self.channel.messages[record.message_id].view)

    async def test_excluded_commander_threshold_does_not_alert_or_warn(self):
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = (True, "Commander")
        for _ in range(10):
            await self.module._process_event(self.guild, self.event())
        self.assertFalse(self.channel.sent)
        self.assertFalse(self.module._alerts.get(1))
        self.assertFalse(self.module._watches.get(1))
        self.client.message_player.assert_not_awaited()
        self.client.kick_player.assert_not_awaited()

    async def test_commander_included_when_exclusion_is_false(self):
        self.module._get_role.return_value = (True, "Commander")
        record = await self.threshold()
        self.assertTrue(record.warning_sent)
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertIn("eos", self.module._watches[1])
        await self.module._process_event(self.guild, self.event(seconds=301))
        self.client.kick_player.assert_awaited_once()

    async def test_watched_player_becoming_commander_is_not_kicked(self):
        await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = (True, "Commander")
        await self.module._process_event(self.guild, self.event(seconds=301))
        self.client.kick_player.assert_not_awaited()

    async def test_staff_buttons_ignore_player_becoming_commander(self):
        for action in ("warn_watch", "kick"):
            with self.subTest(action=action):
                self.config.stored.value["exclude_commander"] = False
                self.module._get_role.return_value = (False, "Rifleman")
                record = await self.threshold()
                warnings = self.client.message_player.await_count
                self.config.stored.value["exclude_commander"] = True
                self.module._get_role.return_value = (True, "Commander")
                await self.module.handle_action(self.interaction(record), action)
                self.assertEqual(self.client.message_player.await_count, warnings)
                self.client.kick_player.assert_not_awaited()
                self.assertEqual(self.module._alerts[1][record.message_id].status, "resolved")
                self.assertFalse(self.module._watches.get(1))
                self.assertIsNone(self.channel.messages[record.message_id].view)

    async def test_staff_buttons_do_not_act_when_excluded_role_is_unknown(self):
        record = await self.threshold()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = None
        for action in ("warn_watch", "kick"):
            await self.module.handle_action(self.interaction(record), action)
        self.assertEqual(self.client.message_player.await_count, 1)
        self.client.kick_player.assert_not_awaited()
        self.assertFalse(self.module._watches.get(1))
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")

    async def test_unknown_role_does_not_start_automatic_watch(self):
        record = await self.threshold()
        self.config.stored.value["exclude_commander"] = True
        self.module._get_role.return_value = None
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
        self.assertEqual(self.client.message_player.await_count, 1)

    async def test_forgive_serializes_with_running_default(self):
        record = await self.threshold()
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_warning(*args):
            started.set()
            await finish.wait()

        self.client.message_player.side_effect = slow_warning
        self.seconds = 300
        automatic = asyncio.create_task(self.module._process_alert_timers())
        await asyncio.wait_for(started.wait(), timeout=1)
        forgive = asyncio.create_task(self.module.handle_action(self.interaction(record), "forgive"))
        await asyncio.sleep(0)
        self.assertFalse(forgive.done())
        finish.set()
        await asyncio.wait_for(asyncio.gather(automatic, forgive), timeout=1)
        self.assertFalse(self.module._watches[1])
        self.assertEqual(self.module._alerts[1][record.message_id].status, "resolved")
        self.assertIsNone(self.channel.messages[record.message_id].view)

    async def test_replacing_view_keeps_new_buttons_registered(self):
        from discord.ui.view import ViewStore

        store = ViewStore(NS())
        original_send = self.channel.send

        async def send(**kwargs):
            message = await original_send(**kwargs)
            store.add_view(message.view, message.id)
            original_edit = message.edit

            async def edit(**edit_kwargs):
                await original_edit(**edit_kwargs)
                if message.view is not None:
                    store.add_view(message.view, message.id)

            message.edit = edit
            return message

        self.channel.send = send
        record = await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        key = (discord.ComponentType.button.value, "hll_tk_watch:forgive")
        self.assertIn(key, store._views[record.message_id])
        await self.module.handle_action(self.interaction(record), "forgive")
        self.assertNotIn(record.message_id, store._views)

    async def test_backlogged_tk_before_watch_does_not_kick(self):
        await self.threshold()
        self.seconds = 300
        await self.module._process_alert_timers()
        await self.module._process_event(self.guild, self.event(seconds=299))
        self.client.kick_player.assert_not_awaited()
        await self.module._process_event(self.guild, self.event(seconds=301))
        self.client.kick_player.assert_awaited_once()
        self.assertFalse(self.module._watches[1])
        self.assertEqual(next(iter(self.module._alerts[1].values())).status, "resolved")

    async def test_watch_start_precision_survives_reload(self):
        await self.threshold()
        self.seconds = 300.75
        await self.module._process_alert_timers()
        reloaded = tk.HLLTKWatchModule(self.cog)
        await reloaded._load_state()
        await reloaded._process_event(self.guild, self.event(seconds=300.5))
        self.client.kick_player.assert_not_awaited()
        await reloaded._process_event(self.guild, self.event(seconds=300.9))
        self.client.kick_player.assert_awaited_once()

    async def test_old_resolved_alert_expiry_keeps_new_alert_deduplicated(self):
        record = await self.threshold()
        await self.module.handle_action(self.interaction(record), "forgive")
        self.seconds = 700
        new_record = await self.threshold()
        self.seconds = 900
        await self.module._cleanup_expired_state()
        self.assertIn(new_record.message_id, self.module._alerts[1])
        self.assertIn("eos", self.module._open_alert_players[1])
        for _ in range(3):
            await self.module._process_event(self.guild, self.event())
        self.assertEqual(len(self.channel.sent), 2)

    async def test_manual_kick_removes_buttons_without_deletion(self):
        record = await self.threshold()
        await self.module.handle_action(self.interaction(record), "kick")
        self.client.kick_player.assert_awaited_once()
        self.assertIsNone(self.channel.messages[record.message_id].view)
        self.channel.messages[record.message_id].delete.assert_not_awaited()
        self.seconds = 300
        await self.module._process_alert_timers()
        self.assertFalse(self.module._watches.get(1))

    async def test_disable_retains_alert_and_clears_state(self):
        record = await self.threshold()
        self.module._timer_failures[record.message_id] = 2
        self.module._next_timer_at[record.message_id] = 5000
        await self.module.disable(self.guild)
        self.assertFalse(self.module.should_poll(1))
        self.assertFalse(self.module._alerts.get(1))
        self.assertIsNone(self.channel.messages[record.message_id].view)
        self.channel.messages[record.message_id].delete.assert_not_awaited()
        self.assertFalse(self.module._timer_failures)
        self.assertFalse(self.module._next_timer_at)

    async def test_expiry_edit_failure_retains_record_for_retry(self):
        record = await self.threshold()
        await self.module.handle_action(self.interaction(record), "forgive")
        message = self.channel.messages[record.message_id]
        edit = message.edit
        message.edit = AsyncMock(side_effect=discord.HTTPException(NS(status=503, reason="mock unavailable"), "retry"))
        self.seconds = 900
        await self.module._cleanup_expired_state()
        self.assertIn(record.message_id, self.module._alerts[1])
        message.edit = edit
        await self.module._cleanup_expired_state()
        self.assertNotIn(record.message_id, self.module._alerts[1])
        message.delete.assert_not_awaited()

    async def test_user_deletion_removes_persisted_staff_mention(self):
        record = await self.threshold()
        await self.module.handle_action(self.interaction(record), "warn_watch")
        await self.module.delete_user_data(4)
        self.assertNotIn("<@4>", self.module._alerts[1][record.message_id].decision)
        stored = await self.config.stored()
        self.assertNotIn("<@4>", stored["active_alerts"][str(record.message_id)]["decision"])
        self.assertIn("eos", self.module._watches[1])

    async def test_reconfigure_without_role_clears_ping(self):
        await self.module.configure(self.guild, channel_id=2, threshold_per_min=3,
                                    watch_duration_minutes=15, exclude_commander=False)
        settings = await self.module.get_settings(self.guild)
        self.assertIsNone(settings["role_id"])
        await self.threshold()
        self.assertIsNone(self.channel.sent[0]["content"])
        self.assertFalse(self.channel.sent[0]["allowed_mentions"].roles)

    async def test_stop_removes_live_views_but_keeps_persisted_state(self):
        record = await self.threshold()
        view = self.module._registered_views[record.message_id]
        self.module.stop()
        self.assertTrue(view.is_finished())
        self.assertFalse(self.module._registered_views)
        self.assertFalse(self.module.should_poll(1))
        self.assertIn(str(record.message_id), (await self.config.stored())["active_alerts"])

    async def test_unauthorized_buttons_do_no_work(self):
        record = await self.threshold()
        self.cog.is_authorized.return_value = False
        interaction = self.interaction(record)
        await self.module.handle_action(interaction, "forgive")
        interaction.response.defer.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertEqual(self.module._alerts[1][record.message_id].status, "active")
        self.client.get_players.assert_not_awaited()

    async def test_unauthorized_configuration_does_no_work(self):
        self.cog.is_authorized.return_value = False
        self.cog.tk_watch = NS(configure=AsyncMock(), disable=AsyncMock())
        self.cog.kill_feed.test_connection = AsyncMock()
        interaction = NS(user=NS(), response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await tk.HLLTKWatchCommandsMixin.hllvn_tk_watch.callback(
            self.cog, interaction, discord.app_commands.Choice(name="Enable", value="enable")
        )
        interaction.response.defer.assert_not_awaited()
        self.cog.tk_watch.configure.assert_not_awaited()
        self.cog.kill_feed.test_connection.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_legacy_alert_deadline_and_new_command_role(self):
        stored = {"10": dict(message_id=10, channel_id=2, eos_id="eos", expires_at=1900, status="processing")}
        parsed = self.module._parse_alerts(stored)[10]
        self.assertEqual(parsed.default_action_at, 1300)
        self.assertEqual(parsed.status, "active")
        command = tk.HLLTKWatchCommandsMixin.hllvn_tk_watch
        role = next(parameter for parameter in command.parameters if parameter.name == "role")
        self.assertFalse(role.required)
        self.assertEqual(role.type, discord.AppCommandOptionType.role)


if __name__ == "__main__":
    unittest.main()
