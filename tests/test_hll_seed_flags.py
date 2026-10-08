"""Seeder flag API contracts and VIP integration; no live network or Red."""

import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

try:
    import discord
except ImportError:
    discord = None

flags = vip = api = None
if discord is not None:
    root = Path(__file__).resolve().parents[1] / "BattleMetric"
    for name, path in (("_seed_test", root), ("_seed_test.module", root / "module")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    for name, exception in (("hll_database", "HLLDatabaseError"), ("kill_feed", "KillFeedConnectionTestError")):
        dependency = types.ModuleType(f"_seed_test.module.{name}")
        setattr(dependency, exception, type(exception, (Exception,), {}))
        sys.modules[dependency.__name__] = dependency
    for name in ("api", "module.player_flags", "module.hll_vip"):
        spec = importlib.util.spec_from_file_location(f"_seed_test.{name}", root / (name.replace(".", "/") + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    api = sys.modules["_seed_test.api"]
    flags = sys.modules["_seed_test.module.player_flags"]
    vip = sys.modules["_seed_test.module.hll_vip"]


def relation(kind, resource_id):
    return {"data": {"type": kind, "id": resource_id}}


def definition(flag_id="seed", organization="org", name="Seeder", shared=False):
    return {
        "type": "playerFlag", "id": flag_id, "attributes": {"name": name},
        "relationships": {"organization": relation("organization", organization)},
        "meta": {"shared": shared},
    }


def assignment(flag_id="seed", removed=None):
    return {
        "type": "flagPlayer", "attributes": {"removedAt": removed},
        "relationships": {"playerFlag": relation("playerFlag", flag_id)},
    }


class Value:
    def __init__(self):
        self.value = []

    async def __call__(self):
        return copy.deepcopy(self.value)

    async def set(self, value):
        self.value = copy.deepcopy(value)


@unittest.skipIf(flags is None, "discord.py and aiohttp are required for Seeder flag tests")
class SeederFlagTests(unittest.IsolatedAsyncioTestCase):
    EOS = "0002" + "a" * 28
    STEAM = "76561198000000001"

    async def asyncSetUp(self):
        self.now = 1000.0
        self.sleep_delays = []

        async def sleep(delay):
            self.sleep_delays.append(delay)
            self.now += delay

        self.time_patch = patch.object(flags, "time", NS(monotonic=lambda: self.now))
        self.sleep_patch = patch.object(flags, "asyncio", NS(Lock=asyncio.Lock, sleep=sleep, CancelledError=asyncio.CancelledError))
        self.time_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.addCleanup(self.sleep_patch.stop)
        self.guild = NS(id=1)

        async def match(identifiers):
            return {"data": [{
                "type": "identifier", "attributes": {"identifier": value, "type": kind},
                "relationships": {"player": relation("player", (
                    "42" if value == self.EOS else "43" if value == self.STEAM else str(100 + int(value, 16))
                ))},
            } for value, kind in identifiers]}

        self.api = NS(
            get_server=AsyncMock(return_value={"data": {
                "type": "server", "id": "server", "relationships": {"organization": relation("organization", "org")},
            }}),
            list_player_flags=AsyncMock(return_value={"data": [definition()]}),
            quick_match_player_batch=AsyncMock(side_effect=match),
            list_player_flag_assignments=AsyncMock(return_value={"data": []}),
            assign_player_flag=AsyncMock(return_value={"data": assignment()}),
            get=AsyncMock(),
        )
        self.grants = Value()
        self.cog = NS(
            api=self.api, get_api_token=AsyncMock(return_value="test-token"),
            get_default_server_id=AsyncMock(return_value="server"),
            config=NS(guild=lambda guild: NS(hll_vip_grants=self.grants)),
            is_authorized=AsyncMock(return_value=True),
            _format_purge_ids=vip.HLLVIPCommandsMixin._format_purge_ids,
        )
        self.module = flags.PlayerFlagsModule(self.cog)
        self.cog.player_flags = self.module

    async def test_successful_flags_use_exact_eos_and_steam_matches(self):
        result = await self.module.flag_seeders(self.guild, [self.EOS.upper(), self.STEAM, self.EOS])
        self.assertEqual(result.added, 2)
        self.assertFalse(result.failed)
        self.assertIsNone(result.error)
        self.assertEqual(self.api.assign_player_flag.await_args_list[0].args, ("42", "seed"))
        self.assertEqual(self.api.assign_player_flag.await_args_list[1].args, ("43", "seed"))
        self.assertEqual(self.api.quick_match_player_batch.await_args.args[0], [
            (self.EOS, "eosID"), (self.EOS, "hllWindowsID"), (self.STEAM, "steamID"),
        ])
        self.assertTrue(all(delay == 2 for delay in self.sleep_delays))

    async def test_existing_flag_skips_write_and_removed_assignment_does_not(self):
        self.api.list_player_flag_assignments.return_value = {"data": [assignment()]}
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertEqual(result.already_flagged, 1)
        self.api.assign_player_flag.assert_not_awaited()
        self.api.list_player_flag_assignments.return_value = {"data": [assignment(removed="yesterday")]}
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertEqual(result.added, 1)

    async def test_selects_only_own_nonshared_organization_flag(self):
        self.api.list_player_flags.return_value = {"data": [
            definition("wrong", "other"), definition("shared", shared=True), definition("right", name=" seeder "),
        ]}
        await self.module.flag_seeders(self.guild, [self.EOS])
        self.api.assign_player_flag.assert_awaited_once_with("42", "right")

    async def test_missing_or_ambiguous_flag_reports_setup_error_without_writing(self):
        for resources in ([], [definition("one"), definition("two")]):
            self.api.list_player_flags.return_value = {"data": resources}
            result = await self.module.flag_seeders(self.guild, [self.EOS])
            self.assertEqual(result.failed, (self.EOS,))
            self.assertIn("exactly one", result.error)
        self.api.quick_match_player_batch.assert_not_awaited()
        self.api.assign_player_flag.assert_not_awaited()

    async def test_setup_missing_token_server_or_organization_does_not_write(self):
        self.cog.get_api_token.return_value = None
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("token vault", result.error)
        self.api.get_server.assert_not_awaited()
        self.cog.get_api_token.return_value = "token"
        self.cog.get_default_server_id.return_value = None
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("serverinfo", result.error)
        self.cog.get_default_server_id.return_value = "server"
        self.api.get_server.return_value = {"data": {"type": "server", "id": "server"}}
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("organization", result.error)
        self.api.list_player_flags.assert_not_awaited()

    async def test_ambiguous_wrong_identifier_or_name_match_is_never_written(self):
        data = [
            {"type": "identifier", "attributes": {"identifier": self.EOS, "type": "eosID"},
             "relationships": {"player": relation("player", resource_id)}}
            for resource_id in ("42", "43")
        ]
        for document in ({"data": data}, {"data": [{
            "type": "identifier", "attributes": {"identifier": self.EOS, "type": "name"},
            "relationships": {"player": relation("player", "42")},
        }]}, {"data": []}):
            self.api.quick_match_player_batch.side_effect = None
            self.api.quick_match_player_batch.return_value = document
            result = await self.module.flag_seeders(self.guild, [self.EOS])
            self.assertEqual(result.failed, (self.EOS,))
        self.api.assign_player_flag.assert_not_awaited()

    async def test_pagination_checks_existing_assignments_and_definitions(self):
        self.api.list_player_flags.return_value = {"data": [], "links": {"next": "https://api.battlemetrics.com/player-flags?page[key]=x"}}
        self.api.list_player_flag_assignments.return_value = {"data": [], "links": {"next": {"href": "/players/42/relationships/flags?page[key]=x"}}}
        self.api.get.side_effect = [{"data": [definition()]}, {"data": [assignment()]}]
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertEqual(result.already_flagged, 1)
        self.api.assign_player_flag.assert_not_awaited()
        self.assertEqual(self.api.get.await_count, 2)

    async def test_untrusted_and_repeated_pagination_never_leaks_token_or_writes(self):
        for link in ("https://evil.example/player-flags", "//evil.example/player-flags", "http://api.battlemetrics.com/player-flags", "https:/player-flags", "/bans"):
            self.api.list_player_flags.return_value = {"data": [], "links": {"next": link}}
            result = await self.module.flag_seeders(self.guild, [self.EOS])
            self.assertIn("unsafe", result.error)
        self.api.get.assert_not_awaited()
        self.api.assign_player_flag.assert_not_awaited()
        self.api.list_player_flags.return_value = {"data": [], "links": {"next": "/player-flags?page[key]=x"}}
        self.api.get.return_value = self.api.list_player_flags.return_value
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("repeated", result.error)
        self.assertEqual(self.api.get.await_count, 1)

    async def test_pagination_is_bounded_and_malformed_lists_fail_closed(self):
        self.module.MAX_PAGES = 2
        self.api.list_player_flags.return_value = {"data": [definition()], "links": {"next": "/player-flags?page[key]=x"}}
        self.api.get.return_value = {"data": [], "links": {"next": "/player-flags?page[key]=y"}}
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("safety limit", result.error)
        self.assertEqual(self.api.get.await_count, 1)
        self.api.list_player_flags.return_value = {"data": "invalid"}
        result = await self.module.flag_seeders(self.guild, [self.EOS])
        self.assertIn("invalid", result.error)
        self.api.assign_player_flag.assert_not_awaited()

    async def test_rate_limit_or_permission_denial_stops_remaining_mutations(self):
        for status in (401, 403, 429):
            self.api.assign_player_flag.reset_mock()
            self.api.list_player_flag_assignments.reset_mock()
            self.api.assign_player_flag.side_effect = api.BattleMetricsAPIError("sensitive backend detail", status=status)
            result = await self.module.flag_seeders(self.guild, [self.EOS, self.STEAM])
            self.assertEqual(result.failed, (self.EOS, self.STEAM))
            self.assertIsNotNone(result.error)
            self.assertNotIn("sensitive", result.error)
            self.assertEqual(self.api.assign_player_flag.await_count, 1)
            self.assertEqual(self.api.list_player_flag_assignments.await_count, 1)

    async def test_independent_player_failure_does_not_stop_other_players(self):
        self.api.assign_player_flag.side_effect = [api.BattleMetricsAPIError("failed", status=500), {}]
        result = await self.module.flag_seeders(self.guild, [self.EOS, self.STEAM])
        self.assertEqual(result.failed, (self.EOS,))
        self.assertEqual(result.added, 1)

    async def test_empty_rewards_do_no_api_work_and_cancellation_propagates(self):
        self.assertEqual(await self.module.flag_seeders(self.guild, []), flags.HLLSeederFlagResult())
        self.cog.get_api_token.assert_not_awaited()
        self.api.get_server.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.module.flag_seeders(self.guild, [self.EOS])

    async def test_matches_large_batches_with_at_most_fifty_identifier_resources(self):
        targets = [f"{index:032x}" for index in range(1, 61)]
        result = await self.module.flag_seeders(self.guild, targets)
        self.assertEqual(result.added, 60)
        self.assertEqual(self.api.quick_match_player_batch.await_count, 3)
        self.assertEqual([len(call.args[0]) for call in self.api.quick_match_player_batch.await_args_list], [50, 50, 20])

    async def test_two_game_ids_for_same_profile_do_not_duplicate_assignment(self):
        async def match(identifiers):
            return {"data": [{
                "type": "identifier", "attributes": {"identifier": value, "type": kind},
                "relationships": {"player": relation("player", "42")},
            } for value, kind in identifiers]}
        self.api.quick_match_player_batch.side_effect = match
        result = await self.module.flag_seeders(self.guild, [self.EOS, self.STEAM])
        self.assertEqual((result.added, result.already_flagged), (1, 1))
        self.api.assign_player_flag.assert_awaited_once_with("42", "seed")

    async def test_uncertain_flag_write_is_not_repeated_for_an_alias(self):
        async def match(identifiers):
            return {"data": [{
                "type": "identifier", "attributes": {"identifier": value, "type": kind},
                "relationships": {"player": relation("player", "42")},
            } for value, kind in identifiers]}
        self.api.quick_match_player_batch.side_effect = match
        self.api.assign_player_flag.side_effect = api.BattleMetricsAPIError("timed out")
        result = await self.module.flag_seeders(self.guild, [self.EOS, self.STEAM])
        self.assertEqual(result.failed, (self.EOS, self.STEAM))
        self.api.assign_player_flag.assert_awaited_once_with("42", "seed")

    def make_vip(self, players, vips=()):
        self.client = NS(
            get_players=AsyncMock(return_value=NS(players=players)),
            get_vip_users=AsyncMock(return_value=NS(vips=[NS(id=value) for value in vips])),
            add_vip=AsyncMock(), message_player=AsyncMock(),
        )
        async def execute(guild, stage, operation):
            return await operation(self.client)
        self.cog.kill_feed = NS(execute_rcon=execute)
        self.cog.hll_vip = vip.HLLVIPModule(self.cog)
        return self.cog.hll_vip

    def player(self, eos):
        return NS(id=eos, eos_id=eos, name="Seeder")

    async def test_vip_flow_flags_successes_only_and_preserves_external_vips(self):
        module = self.make_vip([self.player(self.EOS), self.player(self.STEAM), self.player(self.EOS)], vips=[self.STEAM])
        with patch.object(vip, "asyncio", NS(Lock=asyncio.Lock, sleep=AsyncMock(), CancelledError=asyncio.CancelledError)):
            result = await module.give_seed_vip(self.guild, duration_seconds=86400, granted_by=4)
        self.assertEqual(result.rewarded, 2)
        self.assertEqual(result.protected_external_vip, 1)
        self.assertEqual(result.flags.added, 2)
        self.client.add_vip.assert_awaited_once()
        self.assertEqual(self.client.message_player.await_count, 2)
        grants = {grant.eos_id: grant for grant in await module._get_grants(self.guild)}
        self.assertFalse(grants[self.STEAM].remove_on_expiry)
        self.assertTrue(grants[self.EOS].remove_on_expiry)

    async def test_failed_vip_grant_is_not_flagged_and_popup_failure_is_independent(self):
        module = self.make_vip([self.player(self.EOS), self.player(self.STEAM)])
        self.client.add_vip.side_effect = [RuntimeError("grant failed"), None]
        self.client.message_player.side_effect = RuntimeError("popup failed")
        with patch.object(vip, "asyncio", NS(Lock=asyncio.Lock, sleep=AsyncMock(), CancelledError=asyncio.CancelledError)):
            result = await module.give_seed_vip(self.guild, duration_seconds=86400, granted_by=4)
        self.assertEqual(result.failed, (self.EOS,))
        self.assertEqual(result.message_failed, (self.STEAM,))
        self.assertEqual(result.flags.added, 1)
        self.api.assign_player_flag.assert_awaited_once_with("43", "seed")

    async def test_flag_setup_failure_never_rolls_back_or_hides_successful_vip(self):
        module = self.make_vip([self.player(self.EOS)])
        self.api.list_player_flags.return_value = {"data": []}
        with patch.object(vip, "asyncio", NS(Lock=asyncio.Lock, sleep=AsyncMock(), CancelledError=asyncio.CancelledError)):
            result = await module.give_seed_vip(self.guild, duration_seconds=86400, granted_by=4)
        self.assertEqual(result.rewarded, 1)
        self.assertFalse(result.failed)
        self.assertEqual(result.flags.failed, (self.EOS,))
        self.assertEqual(len(await module._get_grants(self.guild)), 1)

    async def test_same_guild_concurrent_reward_batch_is_blocked(self):
        module = self.make_vip([])
        async with module._seed_reward_locks.setdefault(self.guild.id, asyncio.Lock()):
            with self.assertRaisesRegex(ValueError, "already running"):
                await module.give_seed_vip(self.guild, duration_seconds=86400, granted_by=4)
        self.client.get_players.assert_not_awaited()

    async def test_seed_command_authorizes_before_defer_or_any_service_work(self):
        self.cog.is_authorized.return_value = False
        self.cog.hll_vip = NS(give_seed_vip=AsyncMock())
        interaction = NS(user=NS(), response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await vip.HLLVIPCommandsMixin.hllvn_giveseedvip.callback(self.cog, interaction, "2d")
        interaction.response.defer.assert_not_awaited()
        self.cog.hll_vip.give_seed_vip.assert_not_awaited()
        self.cog.get_api_token.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_command_summary_shows_independent_flag_results(self):
        module = self.make_vip([])
        module.give_seed_vip = AsyncMock(return_value=vip.HLLSeedVIPResult(
            2, 2, 0, 0, (), (), flags.HLLSeederFlagResult(1, 0, (self.EOS,), "Flagging unavailable"),
        ))
        interaction = NS(
            user=NS(id=4), guild=self.guild,
            response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()),
        )
        await vip.HLLVIPCommandsMixin.hllvn_giveseedvip.callback(self.cog, interaction, "2d")
        embed = interaction.followup.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["VIP Rewarded"], "2")
        self.assertEqual(fields["Flag Failures"], "1")
        self.assertEqual(fields["Seeder Flags Added"], "1")
        self.assertEqual(embed.color, discord.Color.orange())

    async def test_shared_api_payloads_are_additive_and_authenticated(self):
        client = api.BattleMetricsClient()
        client.request = AsyncMock(return_value={})
        await client.assign_player_flag("42", "flag-id")
        client.request.assert_awaited_once_with(
            "POST", "/players/42/relationships/flags",
            json={"data": {"type": "flagPlayer", "relationships": {
                "playerFlag": relation("playerFlag", "flag-id"),
            }}}, auth=True,
        )
        client.request.reset_mock()
        await client.quick_match_player_identifiers(self.EOS, ("eosID", "hllWindowsID"))
        self.assertEqual(client.request.await_args.args, ("POST", "/players/quick-match"))
        self.assertEqual(len(client.request.await_args.kwargs["json"]["data"]), 2)
        with self.assertRaises(ValueError):
            await client.quick_match_player_batch([])
        with self.assertRaises(ValueError):
            await client.quick_match_player_batch([(self.EOS, "eosID")] * 51)
        client.get = AsyncMock(return_value={})
        await client.list_player_flags()
        self.assertTrue(client.get.await_args.kwargs["auth"])
        await client.list_player_flag_assignments("42/unsafe")
        self.assertEqual(client.get.await_args.args[0], "/players/42%2Funsafe/relationships/flags")


if __name__ == "__main__":
    unittest.main()
