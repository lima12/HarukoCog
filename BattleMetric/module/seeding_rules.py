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


def find_fourth_point_offenders(
    session: Any,
    players: Iterable[Any],
) -> tuple[SeedingOffender, ...]:
    """Return attackers inside their fourth sector on a Warfare layer."""
    if get_game_mode_id(session) != "warfare":
        return ()

    try:
        layer = session.find_layer()
        sectors = layer.sectors
        mirrored = bool(layer.map.is_mirrored)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SeedingRuleError(
            "The current Warfare layer is not recognized by the installed hllrcon version."
        ) from exc

    if len(sectors) < 5:
        raise SeedingRuleError(
            "The current Warfare layer does not expose five capture sectors."
        )

    # Sector order follows map coordinates. Allies/South normally attack from
    # sector 1 toward sector 5; mirrored maps reverse that direction.
    target_by_team = {
        1: sectors[1] if mirrored else sectors[3],
        2: sectors[3] if mirrored else sectors[1],
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
        target = target_by_team.get(team_id)
        if target is None or not target.is_inside(world_position[:2]):
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


def find_hq_offenders(
    session: Any,
    players: Iterable[Any],
) -> tuple[SeedingOffender, ...]:
    """Return enemies inside a locked home-HQ sector on a Warfare layer."""
    if get_game_mode_id(session) != "warfare":
        return ()

    try:
        layer = session.find_layer()
        sectors = layer.sectors
        mirrored = bool(layer.map.is_mirrored)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SeedingRuleError(
            "The current Warfare layer is not recognized by the installed hllrcon version."
        ) from exc

    if len(sectors) < 5:
        raise SeedingRuleError(
            "The current Warfare layer does not expose five capture sectors."
        )

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
