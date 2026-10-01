"""Dependency-free HLL territory-protection rule evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class SeedingRuleError(ValueError):
    """Raised when the current game state cannot be evaluated safely."""


@dataclass(frozen=True)
class SeedingOffender:
    player_id: str
    player_name: str
    team_id: int


def get_game_mode_id(session: Any) -> str:
    game_mode = getattr(session, "game_mode", None)
    value = getattr(game_mode, "id", game_mode)
    return str(value or "unknown").strip().lower()


def get_seeding_stage(
    player_count: int,
    stage_one_players: int,
    stage_two_players: int,
) -> int:
    """Return 0 before Stage 1, 1 before Stage 2, or 2 when fully unlocked."""
    if not 1 <= stage_one_players < stage_two_players <= 100:
        raise SeedingRuleError(
            "Stage 1 must be lower than Stage 2, and both must be between 1 and 100."
        )
    if player_count >= stage_two_players:
        return 2
    if player_count >= stage_one_players:
        return 1
    return 0


def find_seeding_offenders(
    session: Any,
    players: Iterable[Any],
    stage: int,
) -> tuple[SeedingOffender, ...]:
    """Return players inside sectors locked by the current seeding stage."""
    game_mode = get_game_mode_id(session)
    if game_mode not in {"warfare", "offensive"} or stage >= 2:
        return ()
    if stage not in {0, 1}:
        raise SeedingRuleError("The current seeding stage is invalid.")

    layer, sectors, mirrored = _get_layer_geometry(session, game_mode)
    if game_mode == "warfare":
        # Before Stage 1, lock objectives 4 and 5. At Stage 1, only objective
        # 5 remains locked. Each team's attack direction is handled separately.
        first_locked_index = 3 if stage == 0 else 4
        allies_order = list(reversed(sectors)) if mirrored else list(sectors)
        target_by_team = {
            1: tuple(allies_order[first_locked_index:]),
            2: tuple(reversed(allies_order))[first_locked_index:],
        }
    else:
        # Offensive begins with the first objective already controlled. Before
        # Stage 1 attackers may attack objective 2; at Stage 1 objective 3 also
        # opens. Defenders are never restricted by this seeding rule.
        try:
            attacking_team = int(layer.attacking_team.id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SeedingRuleError(
                "The current Offensive layer does not identify its attacking team."
            ) from exc
        first_locked_index = 2 if stage == 0 else 3
        allies_order = list(reversed(sectors)) if mirrored else list(sectors)
        attack_order = (
            allies_order if attacking_team == 1 else list(reversed(allies_order))
        )
        if attacking_team not in {1, 2}:
            raise SeedingRuleError(
                "The current Offensive layer has an unsupported attacking team."
            )
        target_by_team = {attacking_team: tuple(attack_order[first_locked_index:])}

    return _find_players_in_sectors(players, target_by_team)


def _find_players_in_sectors(
    players: Iterable[Any],
    target_by_team: dict[int, tuple[Any, ...]],
) -> tuple[SeedingOffender, ...]:
    offenders: list[SeedingOffender] = []
    for player in players:
        position = getattr(player, "world_position", None)
        if position is None:
            continue
        try:
            world_position = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            continue
        if len(world_position) < 3 or world_position[:3] == (0.0, 0.0, 0.0):
            continue

        try:
            faction = player.faction
            team_id = int(faction.team.id) if faction is not None else 0
        except (AttributeError, TypeError, ValueError):
            continue
        targets = target_by_team.get(team_id, ())
        if not any(target.is_inside(world_position[:2]) for target in targets):
            continue

        player_id = str(getattr(player, "id", "")).strip()
        if not player_id:
            continue
        offenders.append(
            SeedingOffender(
                player_id=player_id,
                player_name=str(getattr(player, "name", player_id)).strip() or player_id,
                team_id=team_id,
            )
        )

    return tuple(offenders)


def _get_layer_geometry(session: Any, game_mode: str) -> tuple[Any, list[Any], bool]:
    label = game_mode.title()
    try:
        layer = session.find_layer()
        sectors = layer.sectors
        mirrored = bool(layer.map.is_mirrored)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SeedingRuleError(
            f"The current {label} layer is not recognized by the installed "
            "hllrcon version."
        ) from exc

    if len(sectors) < 5:
        raise SeedingRuleError(
            f"The current {label} layer does not expose five capture sectors."
        )
    return layer, sectors, mirrored


def find_hq_offenders(
    session: Any,
    players: Iterable[Any],
) -> tuple[SeedingOffender, ...]:
    """Return enemies inside a locked home-HQ sector on a Warfare layer."""
    if get_game_mode_id(session) != "warfare":
        return ()

    _, sectors, mirrored = _get_layer_geometry(session, "warfare")

    allies_home = sectors[4] if mirrored else sectors[0]
    axis_home = sectors[0] if mirrored else sectors[4]
    target_by_attacking_team = {
        1: axis_home,
        2: allies_home,
    }
    score_by_attacking_team = {
        1: _nonnegative_int(getattr(session, "allied_score", 0)),
        2: _nonnegative_int(getattr(session, "axis_score", 0)),
    }

    offenders: list[SeedingOffender] = []
    for player in players:
        position = getattr(player, "world_position", None)
        if position is None:
            continue
        try:
            world_position = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            continue
        if len(world_position) < 3 or world_position[:3] == (0.0, 0.0, 0.0):
            continue

        try:
            faction = player.faction
            team_id = int(faction.team.id) if faction is not None else 0
        except (AttributeError, TypeError, ValueError):
            continue
        target = target_by_attacking_team.get(team_id)
        if (
            target is None
            or score_by_attacking_team.get(team_id, 0) >= 4
            or not target.is_inside(world_position[:2])
        ):
            continue

        player_id = str(getattr(player, "id", "")).strip()
        if not player_id:
            continue
        offenders.append(
            SeedingOffender(
                player_id=player_id,
                player_name=str(getattr(player, "name", player_id)).strip() or player_id,
                team_id=team_id,
            )
        )

    return tuple(offenders)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
