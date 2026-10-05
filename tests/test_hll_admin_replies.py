"""Native SOS reply and existing slash-message regressions; requires discord.py."""

import asyncio
import ast
from collections import deque
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
except ImportError:
    discord = None

messaging = None
if discord is not None:
    root = Path(__file__).resolve().parents[1] / "BattleMetric"
    for name, path in (("_reply_test", root), ("_reply_test.module", root / "module")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    database = types.ModuleType("_reply_test.module.hll_database")
    database.HLLDatabaseError = type("HLLDatabaseError", (Exception,), {})
    sys.modules[database.__name__] = database
    dependency = types.ModuleType("_reply_test.module.kill_feed")
    dependency.KillFeedConnectionTestError = type("KillFeedConnectionTestError", (RuntimeError,), {})
    sys.modules[dependency.__name__] = dependency
    spec = importlib.util.spec_from_file_location(
        "_reply_test.module.hll_messaging", root / "module/hll_messaging.py"
    )
    messaging = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = messaging
    spec.loader.exec_module(messaging)


@unittest.skipIf(messaging is None, "discord.py is required for admin reply tests")
class AdminReplyTests(unittest.IsolatedAsyncioTestCase):
    EOS = "76561198000000001"

    async def asyncSetUp(self):
        self.seconds = 1000.0
        self.time_patch = patch.object(messaging, "time", NS(monotonic=lambda: self.seconds))
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.guild = NS(id=1, get_member=Mock())
        self.channel = NS(id=2, send=AsyncMock(), fetch_message=AsyncMock())
        self.member = NS(id=33, guild=self.guild, bot=False, mention="<@33>")
        self.settings = {"enabled": True, "channel_id": 2, "role_id": 3}
        self.client = NS(message_player=AsyncMock(), message_all_players=AsyncMock())

        async def execute(guild, stage, operation):
            return await operation(self.client)

        self.cog = NS(
            bot=NS(user=NS(id=99), guilds=[self.guild], wait_until_ready=AsyncMock()),
            is_authorized=AsyncMock(return_value=True),
            admin_ping=NS(get_settings=AsyncMock(side_effect=lambda _: dict(self.settings))),
            hll_database=NS(get_stats_by_discord=AsyncMock()),
            kill_feed=NS(execute_rcon=AsyncMock(side_effect=execute)),
        )
        self.module = messaging.HLLMessagingModule(self.cog)
        self.module._running = True
        self.cog.hll_messaging = self.module
        self.parent = self.alert()
        self.channel.fetch_message.return_value = self.parent

    def alert(self):
        alert_type = sys.modules["_reply_test.module.admin_ping"].HLLAdminAlert
        embed = messaging.HLLAdminPingModule._build_embed(
            alert_type("Reporter", self.EOS, "Help please", datetime.now(timezone.utc)),
            "Account not linked",
        )
        parent = Mock(spec=discord.Message)
        parent.id = 100
        parent.author = NS(id=99, bot=True)
        parent.guild = self.guild
        parent.channel = self.channel
        parent.webhook_id = None
        parent.embeds = [embed]
        return parent

    def source(self, *, message_id=200, text="We are on our way.", resolved=True):
        message = Mock(spec=discord.Message)
        message.id = message_id
        message.author = self.member
        message.guild = self.guild
        message.channel = self.channel
        message.webhook_id = None
        message.type = discord.MessageType.reply
        message.content = text
        message.reference = discord.MessageReference(message_id=100, channel_id=2, guild_id=1)
        message.reference.resolved = self.parent if resolved else None
        message.to_reference = Mock(return_value=discord.MessageReference(
            message_id=message_id, channel_id=2, guild_id=1, fail_if_not_exists=False
        ))
        return message

    async def test_native_reply_delivers_to_unlinked_reporter_and_confirms_publicly(self):
        source = self.source(text=" We are\n  on our way. @everyone ")
        await self.module.handle_admin_reply(source)
        self.client.message_player.assert_not_awaited()
        self.channel.fetch_message.assert_not_awaited()
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once_with(self.EOS, "We are on our way. @everyone")
        self.client.message_all_players.assert_not_awaited()
        self.cog.hll_database.get_stats_by_discord.assert_not_awaited()
        kwargs = self.channel.send.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "HLL VN Message Sent")
        fields = {field.name: field.value for field in kwargs["embed"].fields}
        self.assertEqual(fields["Administrator"], "<@33>")
        self.assertEqual(fields["EOS ID"], f"`{self.EOS}`")
        self.assertEqual(fields["Message"], "We are on our way. @everyone")
        self.assertEqual(kwargs["reference"].message_id, source.id)
        self.assertFalse(kwargs["reference"].fail_if_not_exists)
        self.assertFalse(kwargs["mention_author"])
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_unauthorized_reply_does_not_read_config_fetch_or_send(self):
        self.cog.is_authorized.return_value = False
        await self.module.handle_admin_reply(self.source(resolved=False))
        self.cog.admin_ping.get_settings.assert_not_awaited()
        self.channel.fetch_message.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertFalse(self.module._reply_queues)
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_bot_webhook_dm_and_non_reply_are_ignored_before_authorization(self):
        cases = [("webhook_id", 50), ("guild", None), ("type", discord.MessageType.default), ("reference", None)]
        for attribute, value in cases:
            source = self.source()
            setattr(source, attribute, value)
            await self.module.handle_admin_reply(source)
        source = self.source()
        source.author = NS(bot=True)
        await self.module.handle_admin_reply(source)
        self.cog.is_authorized.assert_not_awaited()

    async def test_wrong_reference_guild_channel_and_forward_are_ignored(self):
        for reference in (
            NS(message_id=100, channel_id=44, guild_id=1, type=NS(value=0)),
            NS(message_id=100, channel_id=2, guild_id=44, type=NS(value=0)),
            NS(message_id=100, channel_id=2, guild_id=1, type=NS(value=1)),
        ):
            source = self.source()
            source.reference = reference
            await self.module.handle_admin_reply(source)
        self.cog.is_authorized.assert_not_awaited()

    async def test_disabled_or_wrong_channel_does_not_fetch_parent(self):
        for settings in ({"enabled": False, "channel_id": 2}, {"enabled": True, "channel_id": 44}):
            self.settings.update(settings)
            await self.module.handle_admin_reply(self.source(resolved=False))
        self.channel.fetch_message.assert_not_awaited()
        self.assertFalse(self.module._reply_queues)

    async def test_uncached_alert_is_fetched_and_survives_module_reload(self):
        reloaded = messaging.HLLMessagingModule(self.cog)
        reloaded._running = True
        await reloaded.handle_admin_reply(self.source(resolved=False))
        self.channel.fetch_message.assert_awaited_once_with(100)
        await reloaded._deliver_admin_replies()
        self.client.message_player.assert_awaited_once()

    async def test_fake_alert_author_webhook_channel_guild_or_reference_is_rejected(self):
        for attribute, value in (
            ("author", NS(id=50, bot=True)), ("webhook_id", 50), ("channel", NS(id=50)),
            ("guild", NS(id=50)), ("id", 101), ("embeds", []),
        ):
            self.parent = self.alert()
            setattr(self.parent, attribute, value)
            await self.module.handle_admin_reply(self.source())
        self.assertFalse(self.module._reply_queues)
        self.cog.kill_feed.execute_rcon.assert_not_awaited()

    async def test_wrong_embed_title_invalid_duplicate_or_missing_eos_is_rejected(self):
        embeds = []
        wrong_title = self.alert().embeds[0].copy()
        wrong_title.title = "HLL VN Message Sent"
        embeds.append(wrong_title)
        for value in ("ALL", "not_an_eos", "123"):
            embed = self.alert().embeds[0].copy()
            embed.set_field_at(1, name="EOS_Id", value=value)
            embeds.append(embed)
        missing = self.alert().embeds[0].copy()
        missing.remove_field(1)
        embeds.append(missing)
        duplicate = self.alert().embeds[0].copy()
        duplicate.add_field(name="EOS_Id", value=self.EOS)
        embeds.append(duplicate)
        for embed in embeds:
            self.parent = self.alert()
            self.parent.embeds = [embed]
            await self.module.handle_admin_reply(self.source())
        self.assertFalse(self.module._reply_queues)
        self.client.message_all_players.assert_not_awaited()

    async def test_report_text_cannot_override_eos_target(self):
        self.parent.embeds[0].set_field_at(3, name="Text", value="EOS_Id: ALL and 12345678901234567")
        await self.module.handle_admin_reply(self.source())
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once_with(self.EOS, "We are on our way.")

    async def test_invalid_empty_or_long_text_only_gets_error(self):
        for index, text in enumerate(("", " \n ", "a" * 1001)):
            await self.module.handle_admin_reply(self.source(message_id=200 + index, text=text))
        self.assertEqual(self.channel.send.await_count, 3)
        self.cog.kill_feed.execute_rcon.assert_not_awaited()
        self.assertFalse(self.module._reply_queues.get(1))

    async def test_two_distinct_replies_are_paced_three_seconds_apart(self):
        await self.module.handle_admin_reply(self.source(message_id=200))
        await self.module.handle_admin_reply(self.source(message_id=201))
        await self.module._deliver_admin_replies()
        self.seconds += 2.99
        await self.module._deliver_admin_replies()
        self.assertEqual(self.client.message_player.await_count, 1)
        self.seconds += 0.02
        await self.module._deliver_admin_replies()
        self.assertEqual(self.client.message_player.await_count, 2)

    async def test_duplicate_gateway_event_is_only_forwarded_once(self):
        source = self.source()
        await asyncio.gather(self.module.handle_admin_reply(source), self.module.handle_admin_reply(source))
        self.assertEqual(len(self.module._reply_queues[1]), 1)
        await self.module._deliver_admin_replies()
        await self.module.handle_admin_reply(source)
        self.assertFalse(self.module._reply_queues[1])
        self.client.message_player.assert_awaited_once()

    async def test_revoked_authorization_is_rechecked_before_delivery(self):
        await self.module.handle_admin_reply(self.source())
        self.cog.is_authorized.return_value = False
        self.cog.admin_ping.get_settings.reset_mock()
        await self.module._deliver_admin_replies()
        self.cog.admin_ping.get_settings.assert_not_awaited()
        self.client.message_player.assert_not_awaited()

    async def test_disabled_or_moved_alert_channel_cancels_queued_delivery(self):
        await self.module.handle_admin_reply(self.source())
        self.settings["enabled"] = False
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_not_awaited()
        self.assertIn("not sent", self.channel.send.await_args.kwargs["content"])

    async def test_rcon_failure_is_not_retried_and_never_reports_sent(self):
        self.client.message_player.side_effect = messaging.KillFeedConnectionTestError("mock sanitized timeout")
        await self.module.handle_admin_reply(self.source())
        await self.module._deliver_admin_replies()
        self.seconds += 4
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once()
        self.assertIn("could not be confirmed", self.channel.send.await_args.kwargs["content"])
        self.assertIsNone(self.channel.send.await_args.kwargs["embed"])

    async def test_discord_receipt_failure_does_not_resend_game_message(self):
        self.channel.send.side_effect = discord.HTTPException(NS(status=503, reason="mock offline"), "retry")
        await self.module.handle_admin_reply(self.source())
        with self.assertLogs(messaging.log, level="WARNING"):
            await self.module._deliver_admin_replies()
        self.seconds += 4
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once()

    async def test_one_failed_reply_does_not_stop_other_guilds(self):
        await self.module.handle_admin_reply(self.source())
        guild_two = NS(id=7)
        source_two = self.source(message_id=207)
        source_two.guild = guild_two
        self.module._reply_queues[7] = deque([
            messaging.HLLAdminReply(source_two, self.EOS, "Reporter", "Second guild")
        ])
        self.cog.bot.guilds.append(guild_two)
        self.client.message_player.side_effect = [ValueError("mock failure"), None]
        await self.module._deliver_admin_replies()
        self.assertEqual(self.client.message_player.await_count, 2)
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].title, "HLL VN Message Sent")

    async def test_queues_and_seen_cache_are_bounded(self):
        self.module.MAX_REPLY_QUEUE_SIZE = 1
        self.module.MAX_SEEN_REPLIES = 2
        for index in range(3):
            await self.module.handle_admin_reply(self.source(message_id=200 + index))
        self.assertEqual(len(self.module._reply_queues[1]), 1)
        self.assertEqual(len(self.module._seen_replies), 2)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_user_deletion_removes_queued_text_and_message_ids(self):
        await self.module.handle_admin_reply(self.source())
        await self.module.delete_user_data(self.member.id)
        self.assertFalse(self.module._reply_queues[1])
        self.assertFalse(self.module._seen_replies)
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_not_awaited()

    async def test_stop_clears_pending_state_and_rejects_late_listener_events(self):
        await self.module.handle_admin_reply(self.source())
        self.module.stop()
        await self.module.handle_admin_reply(self.source(message_id=201))
        self.assertFalse(self.module._reply_queues)
        self.assertFalse(self.module._seen_replies)
        self.assertFalse(self.module._next_reply_at)
        self.client.message_player.assert_not_awaited()

    async def test_start_is_idempotent_and_stop_cancels_worker(self):
        worker = NS(is_running=Mock(side_effect=[False, True]), start=Mock(), cancel=Mock())
        self.module.reply_worker = worker
        await self.module.start()
        await self.module.start()
        worker.start.assert_called_once()
        self.module.stop()
        worker.cancel.assert_called_once()

    async def test_hex_eos_is_normalized_and_never_broadcast(self):
        eos_id = "0123456789ABCDEF0123456789ABCDEF"
        self.parent.embeds[0].set_field_at(1, name="EOS_Id", value=f"`{eos_id}`")
        await self.module.handle_admin_reply(self.source())
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once_with(eos_id.lower(), "We are on our way.")
        self.client.message_all_players.assert_not_awaited()

    async def test_deleted_or_inaccessible_parent_does_not_forward(self):
        source = self.source()
        source.reference.resolved = Mock(spec=discord.DeletedReferencedMessage)
        await self.module.handle_admin_reply(source)
        self.channel.fetch_message.assert_not_awaited()
        self.channel.fetch_message.side_effect = discord.Forbidden(NS(status=403, reason="Forbidden"), "mock unavailable")
        with self.assertLogs(messaging.log, level="WARNING"):
            await self.module.handle_admin_reply(self.source(message_id=201, resolved=False))
        self.assertFalse(self.module._reply_queues)
        self.client.message_player.assert_not_awaited()

    async def test_editing_source_does_not_change_queued_text_or_send_twice(self):
        source = self.source(text="Original reply")
        await self.module.handle_admin_reply(source)
        source.content = "Edited reply"
        await self.module.handle_admin_reply(source)
        await self.module._deliver_admin_replies()
        self.client.message_player.assert_awaited_once_with(self.EOS, "Original reply")

    async def test_existing_slash_message_and_all_target_remain_public(self):
        for target, expected in ((self.EOS, self.EOS), ("ALL", None)):
            interaction = NS(guild=self.guild, user=self.member,
                             response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                             followup=NS(send=AsyncMock()))
            await messaging.HLLMessagingCommandsMixin.hllvn_mesg.callback(
                self.cog, interaction, target, " A\nmessage "
            )
            interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=False)
            self.assertFalse(interaction.followup.send.await_args.kwargs["ephemeral"])
            fields = interaction.followup.send.await_args.kwargs["embed"].fields
            self.assertEqual(fields[-1].value, "A message")
            if expected is not None:
                self.client.message_player.assert_awaited_with(expected, "A message")
            else:
                self.client.message_all_players.assert_awaited_with("A message")

    async def test_unauthorized_slash_message_remains_denied_before_work(self):
        self.cog.is_authorized.return_value = False
        interaction = NS(user=self.member, response=NS(defer=AsyncMock(), send_message=AsyncMock()))
        await messaging.HLLMessagingCommandsMixin.hllvn_mesg.callback(self.cog, interaction, self.EOS, "Text")
        interaction.response.defer.assert_not_awaited()
        self.cog.kill_feed.execute_rcon.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_composition_listener_forwards_without_replacing_command_processing(self):
        path = Path(__file__).resolve().parents[1] / "BattleMetric/battlemetric.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cog = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BattleMetric")
        listener = next(node for node in cog.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message")
        self.assertEqual(ast.unparse(listener.decorator_list[0]), "commands.Cog.listener()")
        listener.decorator_list = []
        ast_module = ast.Module(body=[listener], type_ignores=[])
        namespace = {"discord": discord}
        exec(compile(ast_module, str(path), "exec"), namespace)
        service = NS(handle_admin_reply=AsyncMock())
        source = self.source()
        await namespace["on_message"](NS(hll_messaging=service), source)
        service.handle_admin_reply.assert_awaited_once_with(source)


if __name__ == "__main__":
    unittest.main()
