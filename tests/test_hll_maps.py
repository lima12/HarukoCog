"""Map command regressions with mocked Red/RCON; requires discord.py."""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
except ImportError:
    discord = None

try:
    from hllrcon import HLLVLayer, HLLVRcon
except Exception:
    HLLVLayer = HLLVRcon = None

maps = None
if discord is not None:
    root = Path(__file__).resolve().parents[1] / "BattleMetric/module"
    package = types.ModuleType("_map_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    dependency = types.ModuleType("_map_test.kill_feed")
    dependency.KillFeedConnectionTestError = type("KillFeedConnectionTestError", (RuntimeError,), {})
    sys.modules[dependency.__name__] = dependency
    spec = importlib.util.spec_from_file_location("_map_test.hll_maps", root / "hll_maps.py")
    maps = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = maps
    spec.loader.exec_module(maps)


class Catalog:
    layers = [
        NS(id="wdeve_warfare_day", pretty_name="Cam Ranh Port Warfare", map=NS(pretty_name="Cam Ranh Port")),
        NS(id="wdeve_offensiveus_day", pretty_name="Cam Ranh Port Off. US", map=NS(pretty_name="Cam Ranh Port")),
        NS(id="wdevd_warfare_day", pretty_name="Hue City Warfare", map=NS(pretty_name="Hue City")),
    ]

    @classmethod
    def all(cls):
        return cls.layers

    @classmethod
    def by_id(cls, map_id, *, strict):
        assert strict
        for layer in cls.layers:
            if layer.id.casefold() == map_id.casefold():
                return layer
        raise ValueError("Unknown test layer")


@unittest.skipIf(maps is None, "discord.py is required for map command tests")
class MapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.catalog_patch = patch.object(maps, "HLLVLayer", Catalog)
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.guild = NS(id=1)
        self.client = NS(
            get_available_maps=AsyncMock(return_value=[layer.id for layer in Catalog.layers]),
            change_map=AsyncMock(),
        )

        async def execute(guild, stage, operation):
            self.assertIs(guild, self.guild)
            return await operation(self.client)

        self.execute = AsyncMock(side_effect=execute)
        self.cog = NS(is_authorized=AsyncMock(return_value=True), kill_feed=NS(execute_rcon=self.execute))
        self.module = maps.HLLMapModule(self.cog)
        self.cog.hll_maps = self.module

    def interaction(self):
        return NS(
            guild=self.guild, user=NS(id=5),
            response=NS(send_message=AsyncMock(), defer=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )

    async def invoke(self, interaction, name):
        await maps.HLLMapCommandsMixin.hllvn_changemap.callback(self.cog, interaction, name)

    async def test_exact_id_uses_server_casing_and_shared_rcon(self):
        result = await self.module.change(self.guild, " WDEVE_WARFARE_DAY ")
        self.assertEqual(result, "wdeve_warfare_day")
        self.client.change_map.assert_awaited_once_with("wdeve_warfare_day")
        self.assertEqual([call.args[1] for call in self.execute.await_args_list],
                         ["GetAvailableMaps request", "ChangeMap request"])

    async def test_pretty_name_resolves_correct_mode(self):
        result = await self.module.change(self.guild, "cam  ranh port warfare")
        self.assertEqual(result, "wdeve_warfare_day")

    async def test_unique_base_map_name_is_accepted(self):
        self.assertEqual(await self.module.change(self.guild, "Hue City"), "wdevd_warfare_day")

    async def test_ambiguous_base_name_never_changes_map(self):
        with self.assertRaisesRegex(ValueError, "multiple modes"):
            await self.module.change(self.guild, "Cam Ranh Port")
        self.client.change_map.assert_not_awaited()

    async def test_unknown_or_unavailable_map_never_changes_map(self):
        self.client.get_available_maps.return_value = ["wdevd_warfare_day"]
        for name in ("Cam Ranh Port Warfare", "wdeve_warfare_day", "typo"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not available"):
                await self.module.change(self.guild, name)
        self.client.change_map.assert_not_awaited()

    async def test_new_server_map_does_not_require_catalog_update(self):
        self.client.get_available_maps.return_value = ["future_map_warfare_day"]
        result = await self.module.change(self.guild, "future_map_warfare_day")
        self.assertEqual(result, "future_map_warfare_day")
        self.assertEqual(self.module.display_name(result), result)

    async def test_missing_catalog_still_accepts_exact_server_id(self):
        with patch.object(maps, "HLLVLayer", None):
            self.assertEqual(self.module.choices("cam"), [])
            result = await self.module.change(self.guild, "wdeve_warfare_day")
        self.assertEqual(result, "wdeve_warfare_day")

    async def test_invalid_input_does_not_read_server_settings_or_rcon(self):
        for name in ("", "   ", "wdeve\nwarfare", "wdeve\x00warfare", "a" * 201):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.module.change(self.guild, name)
        self.execute.assert_not_awaited()

    async def test_malformed_or_empty_map_list_does_not_change_map(self):
        for response in ("wdeve_warfare_day", [None], [""], []):
            self.client.get_available_maps.return_value = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                await self.module.change(self.guild, "wdeve_warfare_day")
        self.client.change_map.assert_not_awaited()

    async def test_unauthorized_command_denies_before_defer_or_service(self):
        self.cog.is_authorized.return_value = False
        interaction = self.interaction()
        self.cog.hll_maps = NS(change=AsyncMock())
        await self.invoke(interaction, "wdeve_warfare_day")
        interaction.response.defer.assert_not_awaited()
        self.cog.hll_maps.change.assert_not_awaited()
        self.execute.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_success_is_private_and_reports_request_not_instant_completion(self):
        interaction = self.interaction()
        await self.invoke(interaction, "Cam Ranh Port Warfare")
        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertIn("Requested", kwargs["embed"].title)
        self.assertIn("60-second", kwargs["embed"].description)
        self.assertEqual(kwargs["embed"].fields[1].value, "wdeve_warfare_day")
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_non_guild_command_does_not_contact_rcon(self):
        interaction = self.interaction()
        interaction.guild = None
        await self.invoke(interaction, "wdeve_warfare_day")
        self.execute.assert_not_awaited()

    async def test_rcon_failure_never_reports_success_or_retries_change(self):
        self.client.change_map.side_effect = maps.KillFeedConnectionTestError("mock sanitized timeout")
        interaction = self.interaction()
        await self.invoke(interaction, "wdeve_warfare_day")
        self.client.change_map.assert_awaited_once()
        self.assertIn("could not be confirmed", interaction.followup.send.await_args.args[0])
        self.assertNotIn("embed", interaction.followup.send.await_args.kwargs)

    async def test_read_failure_never_submits_map_change(self):
        self.client.get_available_maps.side_effect = maps.KillFeedConnectionTestError("mock offline")
        await self.invoke(self.interaction(), "wdeve_warfare_day")
        self.client.change_map.assert_not_awaited()

    async def test_cancellation_propagates_without_success_reply(self):
        self.client.change_map.side_effect = asyncio.CancelledError
        interaction = self.interaction()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(interaction, "wdeve_warfare_day")
        interaction.followup.send.assert_not_awaited()

    async def test_autocomplete_authorizes_and_never_contacts_rcon(self):
        callback = maps.HLLMapCommandsMixin.hllvn_changemap_autocomplete
        result = await callback(self.cog, self.interaction(), "cam warfare")
        self.assertEqual([choice.value for choice in result], ["wdeve_warfare_day"])
        self.execute.assert_not_awaited()
        self.cog.is_authorized.return_value = False
        self.cog.hll_maps = NS(choices=Mock(side_effect=AssertionError("Must not access catalog")))
        self.assertEqual(await callback(self.cog, self.interaction(), "cam"), [])

    async def test_autocomplete_respects_discord_limits_and_command_registration(self):
        layers = [NS(id=f"map_{index}", pretty_name="N" * 120 + str(index)) for index in range(40)]
        with patch.object(Catalog, "layers", layers):
            result = self.module.choices("")
        self.assertEqual(len(result), 25)
        self.assertTrue(all(len(choice.name) <= 100 and len(choice.value) <= 100 for choice in result))
        command = maps.HLLMapCommandsMixin.hllvn_changemap
        self.assertIs(command.parent.get_command("changemap"), command)
        self.assertTrue(command.guild_only)
        self.assertEqual([parameter.name for parameter in command.parameters], ["map_name"])
        self.assertTrue(command.parameters[0].required)
        self.assertTrue(command.parameters[0].autocomplete)

    @unittest.skipIf(HLLVRcon is None, "hllrcon is required for the library contract check")
    async def test_real_library_catalog_and_change_map_payload(self):
        client = HLLVRcon(host="unused.invalid", port=1, password="not-used")
        available = [layer.id for layer in HLLVLayer.all()]
        client.get_command_details = AsyncMock(return_value=NS(
            dialogue_parameters=[NS(id="MapName", value_member=available)]
        ))
        client.execute = AsyncMock(return_value="")
        self.client = client
        with patch.object(maps, "HLLVLayer", HLLVLayer):
            result = await self.module.change(self.guild, "Cam Ranh Port Warfare")
            choices = self.module.choices("cam warfare")
        self.assertEqual(result, "wdeve_warfare_day")
        self.assertIn(result, [choice.value for choice in choices])
        client.get_command_details.assert_awaited_once_with("AddMapToRotation")
        client.execute.assert_awaited_once_with("ChangeMap", 2, {"MapName": result})


if __name__ == "__main__":
    unittest.main()
