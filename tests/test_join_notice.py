"""Join-notice delivery and authorization regressions with fake shared services."""

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
except ImportError:
    discord = None

notice = None
if discord is not None:
    root = Path(__file__).resolve().parents[1] / "BattleMetric"
    for name, path in (("_joinnotice_test", root), ("_joinnotice_test.module", root / "module")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    for name, exception in (("hll_database", "HLLDatabaseError"), ("kill_feed", "KillFeedConnectionTestError")):
        dependency = types.ModuleType(f"_joinnotice_test.module.{name}")
        setattr(dependency, exception, type(exception, (RuntimeError,), {}))
        sys.modules[dependency.__name__] = dependency
    spec = importlib.util.spec_from_file_location(
        "_joinnotice_test.module.join_notice", root / "module/join_notice.py",
    )
    notice = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = notice
    spec.loader.exec_module(notice)


class Connect(NS):
    pass


@unittest.skipIf(notice is None, "discord.py is required for join-notice tests")
class JoinNoticeTests(unittest.IsolatedAsyncioTestCase):
    EOS = "76561198000000001"

    async def asyncSetUp(self):
        self.seconds = 100.0
        self.guild = NS(id=1)
        self.saved = {}
        self.value = NS(set=AsyncMock(side_effect=self.save))
        self.client = NS(message_player=AsyncMock(return_value=True))

        async def execute(guild, stage, operation):
            return await operation(self.client)

        self.cog = NS(
            bot=NS(guilds=[self.guild], wait_until_ready=AsyncMock()),
            config=NS(
                guild=Mock(return_value=NS(hll_join_notice_enabled=self.value)),
                all_guilds=AsyncMock(side_effect=lambda: dict(self.saved)),
            ),
            is_authorized=AsyncMock(return_value=True),
            hll_database=NS(
                should_poll=Mock(return_value=True),
                get_stats_by_eos=AsyncMock(return_value=NS(kills=1234, deaths=56)),
            ),
            kill_feed=NS(
                get_settings=AsyncMock(return_value={"host": "test", "port": 123}),
                execute_rcon=AsyncMock(side_effect=execute),
            ),
        )
        self.module = notice.HLLJoinNoticeModule(self.cog)
        self.module._running = True
        self.cog.join_notice = self.module
        self.time_patch = patch.object(notice, "time", NS(monotonic=lambda: self.seconds))
        self.model_patch = patch.object(notice, "HLLVPlayerConnectAdminLog", Connect)
        self.time_patch.start()
        self.model_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.addCleanup(self.model_patch.stop)

    async def save(self, value):
        self.saved[1] = {"hll_join_notice_enabled": value}

    def entry(self, eos_id=None, offset=1):
        return Connect(
            player_id=eos_id or self.EOS,
            timestamp=self.module._started_at + timedelta(seconds=offset),
            raw_message=f"CONNECTED {eos_id or self.EOS} {offset}",
        )

    def interaction(self):
        return NS(
            user=NS(id=5), guild=self.guild,
            response=NS(send_message=AsyncMock(), defer=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )

    async def command(self, interaction, **kwargs):
        await notice.HLLJoinNoticeCommandsMixin.hllvn_joinnotice.callback(self.cog, interaction, **kwargs)

    async def test_delays_deduplicates_and_sends_database_counts_privately(self):
        entry = self.entry()
        await self.module.ingest_admin_logs(self.guild, [entry, entry, NS()])
        await self.module.ingest_admin_logs(self.guild, [entry])
        await self.module._deliver_notices()
        self.cog.hll_database.get_stats_by_eos.assert_not_awaited()
        self.seconds += 5
        await self.module._deliver_notices()
        self.cog.hll_database.get_stats_by_eos.assert_awaited_once_with(self.EOS)
        self.client.message_player.assert_awaited_once()
        eos_id, message = self.client.message_player.await_args.args
        self.assertEqual(eos_id, self.EOS)
        self.assertIn("Kill: 1,234 Confirmed\nWounded: 56 Times", message)
        self.assertIn("discord.gg/slh4theppl", message)
        self.assertIn("dogtag", message)
        self.assertLessEqual(len(message), 1000)
        await self.module._deliver_notices()
        self.assertEqual(self.client.message_player.await_count, 1)

    async def test_duplicate_endpoint_guild_does_not_send_twice(self):
        entry = self.entry()
        other = NS(id=2)
        self.cog.bot.guilds.append(other)
        await self.module.ingest_admin_logs(self.guild, [entry])
        await self.module.ingest_admin_logs(other, [entry])
        self.seconds += 5
        await self.module._deliver_notices()
        self.client.message_player.assert_awaited_once()

    async def test_distinct_reconnects_each_receive_a_paced_notice(self):
        await self.module.ingest_admin_logs(self.guild, [self.entry(), self.entry(offset=2)])
        self.seconds += 5
        await self.module._deliver_notices()
        await self.module._deliver_notices()
        self.assertEqual(self.client.message_player.await_count, 1)
        self.seconds += 2
        await self.module._deliver_notices()
        self.assertEqual(self.client.message_player.await_count, 2)

    async def test_ignores_pre_start_invalid_and_expired_notices(self):
        await self.module.ingest_admin_logs(self.guild, [self.entry(offset=-1), self.entry("bad")])
        self.assertFalse(self.module._queues)
        await self.module.ingest_admin_logs(self.guild, [self.entry()])
        self.seconds += self.module.MAX_NOTICE_AGE_SECONDS
        await self.module._deliver_notices()
        self.client.message_player.assert_not_awaited()

    async def test_queue_and_fingerprint_cache_are_bounded(self):
        self.module.MAX_QUEUE_SIZE = 2
        self.module.MAX_SEEN_EVENTS = 3
        with self.assertLogs(notice.log, level="WARNING"):
            await self.module.ingest_admin_logs(self.guild, [self.entry(offset=i) for i in range(1, 8)])
        self.assertEqual(len(self.module._queues[1]), 2)
        self.assertEqual(len(self.module._seen), 3)

    async def test_new_player_has_zero_stats_without_a_discord_link(self):
        self.cog.hll_database.get_stats_by_eos.return_value = None
        message = await self.module.test(self.guild, self.EOS)
        self.assertIn("Kill: 0 Confirmed\nWounded: 0 Times", message)

    async def test_database_failure_never_reports_false_zero_counts(self):
        self.cog.hll_database.get_stats_by_eos.side_effect = RuntimeError("private connection data")
        with self.assertLogs(notice.log, level="WARNING") as output:
            message = await self.module.test(self.guild, self.EOS)
        self.assertIn("Kill: Unavailable", message)
        self.assertNotIn("private connection data", message + str(output.output))

    async def test_invalid_test_target_does_no_database_or_rcon_work(self):
        with self.assertRaises(ValueError):
            await self.module.test(self.guild, "ALL")
        self.cog.hll_database.get_stats_by_eos.assert_not_awaited()
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_endpoint_change_does_not_send_an_old_join_to_the_new_server(self):
        await self.module.ingest_admin_logs(self.guild, [self.entry()])
        self.cog.kill_feed.get_settings.return_value = {"host": "replacement", "port": 123}
        self.seconds += 5
        await self.module._deliver_notices()
        self.client.message_player.assert_not_awaited()

    async def test_disable_discards_pending_notices_but_allows_manual_test(self):
        await self.module.ingest_admin_logs(self.guild, [self.entry()])
        await self.module.configure(self.guild, False)
        self.assertEqual(self.saved[1], {"hll_join_notice_enabled": False})
        self.assertFalse(self.module.should_poll(1))
        self.assertNotIn(1, self.module._queues)
        await self.module.test(self.guild, self.EOS)
        self.client.message_player.assert_awaited_once()

    async def test_enable_does_not_replay_old_connections(self):
        await self.module.configure(self.guild, False)
        old = self.entry(offset=-1)
        await self.module.configure(self.guild, True)
        await self.module.ingest_admin_logs(self.guild, [old])
        self.assertFalse(self.module._queues)

    async def test_start_loads_saved_disable_and_stop_clears_state(self):
        self.module.stop()
        self.saved[1] = {"hll_join_notice_enabled": False}
        worker = NS(is_running=Mock(return_value=False), start=Mock(), cancel=Mock())
        with patch.object(self.module, "delivery_worker", worker):
            await self.module.start()
            await self.module.start()
            worker.start.assert_called_once()
            self.assertFalse(self.module.enabled(1))
            self.assertTrue(self.module.enabled(2))
            self.module.stop()
            self.module.stop()
            self.assertFalse(self.module._running)
            self.assertFalse(self.module._queues)
            self.assertFalse(self.module._seen)
            self.assertFalse(self.module._locks)

    async def test_rcon_failure_is_not_retried_and_other_guild_still_delivers(self):
        other = NS(id=2)
        self.cog.bot.guilds.append(other)
        await self.module.ingest_admin_logs(self.guild, [self.entry()])
        self.cog.kill_feed.get_settings.side_effect = lambda g: {
            "host": "other" if g.id == 2 else "test", "port": 123,
        }
        await self.module.ingest_admin_logs(other, [self.entry()])
        self.client.message_player.side_effect = [RuntimeError("lost acknowledgement"), True]
        self.seconds += 5
        with self.assertLogs(notice.log, level="WARNING"):
            await self.module._deliver_notices()
        self.assertEqual(self.client.message_player.await_count, 2)
        self.assertGreater(self.module._next_delivery_at[1], self.seconds)
        self.seconds += 100
        await self.module._deliver_notices()
        self.assertEqual(self.client.message_player.await_count, 2)

    async def test_unauthorized_command_does_no_acknowledgement_or_service_work(self):
        self.cog.is_authorized.return_value = False
        interaction = self.interaction()
        await self.command(interaction, test=self.EOS)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        interaction.response.defer.assert_not_awaited()
        self.cog.config.guild.assert_not_called()
        self.cog.hll_database.get_stats_by_eos.assert_not_awaited()
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_command_test_is_ephemeral_and_does_not_enable_automatic_notices(self):
        await self.module.configure(self.guild, False)
        interaction = self.interaction()
        await self.command(interaction, test="A" * 32)
        self.assertFalse(self.module.enabled(1))
        self.client.message_player.assert_awaited_once()
        self.assertEqual(self.client.message_player.await_args.args[0], "a" * 32)
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(interaction.followup.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_command_rejects_combined_test_and_toggle(self):
        interaction = self.interaction()
        await self.command(interaction, test=self.EOS, toggle=False)
        self.value.set.assert_not_awaited()
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_boolean_toggle_persists_both_values_and_omission_only_reads_status(self):
        for enabled in (False, True):
            with self.subTest(toggle=enabled):
                interaction = self.interaction()
                await self.command(interaction, toggle=enabled)
                self.assertEqual(self.module.enabled(1), enabled)
                self.assertEqual(self.saved[1]["hll_join_notice_enabled"], enabled)
                self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.value.set.reset_mock()
        await self.command(self.interaction())
        self.value.set.assert_not_awaited()
        self.cog.hll_database.get_stats_by_eos.assert_not_awaited()
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_unavailable_database_or_rcon_models_do_not_request_polling(self):
        self.cog.hll_database.should_poll.return_value = False
        self.assertFalse(self.module.should_poll(1))
        self.cog.hll_database.should_poll.return_value = True
        with patch.object(notice, "HLLVPlayerConnectAdminLog", None):
            self.assertFalse(self.module.should_poll(1))


if __name__ == "__main__":
    unittest.main()
