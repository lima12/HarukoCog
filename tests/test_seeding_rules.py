"""Territory protection regressions; run with unittest without Red installed."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


RULES_PATH = Path(__file__).resolve().parents[1] / "BattleMetric/module/seeding_rules.py"
spec = importlib.util.spec_from_file_location("territory_rules_under_test", RULES_PATH)
rules = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rules
spec.loader.exec_module(rules)


def player(team=1, position=(350, 10, 20), deaths=0):
    return SimpleNamespace(
        id="player-eos",
        name="Player",
        faction=SimpleNamespace(team=SimpleNamespace(id=team)),
        world_position=position,
        stats=SimpleNamespace(deaths=deaths),
    )


class Sector:
    def __init__(self, index):
        self.index = index

    def is_inside(self, position):
        return 100 * self.index <= position[0] < 100 * (self.index + 1)


def session(mode="warfare", attacker=1):
    layer = SimpleNamespace(
        sectors=[Sector(index) for index in range(5)],
        map=SimpleNamespace(is_mirrored=False),
        attacking_team=SimpleNamespace(id=attacker),
    )
    return SimpleNamespace(
        game_mode=SimpleNamespace(id=mode),
        allied_score=2,
        axis_score=2,
        find_layer=lambda: layer,
    )


class TerritoryPlayerTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = rules.TerritoryPlayerTracker()

    def test_join_requires_settling_and_new_position(self):
        joined = player()
        self.assertEqual(self.tracker.eligible_players([joined], 0), ())
        self.assertEqual(self.tracker.eligible_players([joined], 12), ())
        moved = player(position=(351, 10, 20))
        self.assertEqual(self.tracker.eligible_players([moved], 15), (moved,))

    def test_movement_during_settling_does_not_start_warning(self):
        self.tracker.eligible_players([player()], 0)
        moved = player(position=(351, 10, 20))
        self.assertEqual(self.tracker.eligible_players([moved], 3), ())
        self.assertEqual(self.tracker.eligible_players([moved], 9), ())
        self.assertEqual(self.tracker.eligible_players([moved], 12), (moved,))

    def test_team_switch_does_not_reinterpret_old_spawn_as_enemy_hq(self):
        own_spawn = player(position=(50, 10, 20))
        self.tracker.eligible_players([own_spawn], 0)
        self.tracker.eligible_players([player(position=(51, 10, 20))], 12)
        switched = player(team=2, position=(51, 10, 20))
        self.assertEqual(self.tracker.eligible_players([switched], 15), ())
        self.assertEqual(self.tracker.eligible_players([switched], 60), ())
        spawn = player(team=2, position=(450, 10, 20))
        eligible = self.tracker.eligible_players([spawn], 63)
        self.assertEqual(eligible, (spawn,))
        self.assertEqual(rules.find_seeding_offenders(session(), eligible, 0), ())
        self.assertEqual(rules.find_hq_offenders(session(), eligible), ())

    def test_death_with_retained_coordinates_stays_silent(self):
        self.tracker.eligible_players([player()], 0)
        self.tracker.eligible_players([player(position=(351, 10, 20))], 12)
        dead = player(position=(351, 10, 20), deaths=1)
        self.assertEqual(self.tracker.eligible_players([dead], 15), ())
        self.assertEqual(self.tracker.eligible_players([dead], 90), ())
        respawn = player(position=(50, 10, 20), deaths=1)
        self.assertEqual(self.tracker.eligible_players([respawn], 93), (respawn,))

    def test_unassigned_player_must_settle_after_choosing_team(self):
        self.assertEqual(self.tracker.eligible_players([player(team=0)], 0), ())
        self.assertEqual(self.tracker.eligible_players([player()], 60), ())
        moved = player(position=(351, 10, 20))
        self.assertEqual(self.tracker.eligible_players([moved], 63), ())
        self.assertEqual(self.tracker.eligible_players([moved], 72), (moved,))

    def test_disconnect_removes_readiness(self):
        self.tracker.eligible_players([player()], 0)
        self.tracker.eligible_players([player(position=(351, 10, 20))], 12)
        self.tracker.eligible_players([], 15)
        returning = player(position=(351, 10, 20))
        self.assertEqual(self.tracker.eligible_players([returning], 18), ())
        self.assertEqual(self.tracker.eligible_players([returning], 60), ())

    def test_invalid_position_removes_readiness(self):
        for position in ((0, 0, 0), (float("nan"), 10, 20), (float("inf"), 10, 20), None):
            with self.subTest(position=position):
                tracker = rules.TerritoryPlayerTracker()
                tracker.eligible_players([player()], 0)
                tracker.eligible_players([player(position=(351, 10, 20))], 12)
                self.assertEqual(tracker.eligible_players([player(position=position)], 15), ())
                self.assertEqual(tracker.eligible_players([player()], 18), ())

    def test_real_violation_is_enforced_after_join_settles(self):
        self.tracker.eligible_players([player(position=(50, 10, 20))], 0)
        crossing = player(position=(350, 10, 20))
        eligible = self.tracker.eligible_players([crossing], 12)
        self.assertEqual(len(rules.find_seeding_offenders(session(), eligible, 0)), 1)
        self.assertEqual(rules.find_seeding_offenders(session(), eligible, 1), ())
        self.assertEqual(len(rules.find_seeding_offenders(session("offensive"), eligible, 1)), 1)
        self.assertEqual(rules.find_seeding_offenders(session("offensive", 2), eligible, 1), ())

    def test_deaths_not_reported_does_not_block_active_players(self):
        joined = player()
        joined.stats = None
        self.tracker.eligible_players([joined], 0)
        moved = player(position=(351, 10, 20))
        moved.stats = None
        self.assertEqual(self.tracker.eligible_players([moved], 12), (moved,))


if __name__ == "__main__":
    unittest.main()
