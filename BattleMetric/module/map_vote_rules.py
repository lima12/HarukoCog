"""Dependency-free validation and tally policy for in-game map voting."""

from __future__ import annotations

import re
from typing import Any


class MapVoteError(ValueError):
    """Invalid vote configuration or an unsafe server map response."""


def map_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", value):
        raise MapVoteError("The server returned an invalid map ID.")
    return value


def rotation_maps(response: Any) -> list[dict[str, str]]:
    entries = getattr(response, "maps", None)
    if not isinstance(entries, (list, tuple)) or len(entries) > 1000:
        raise MapVoteError("The server returned an invalid map rotation.")
    result = []
    seen = set()
    for entry in entries:
        identifier = map_id(getattr(entry, "id", None))
        if identifier.casefold() in seen:
            continue
        seen.add(identifier.casefold())
        name = str(getattr(entry, "name", identifier))
        mode = str(getattr(entry, "game_mode_name", ""))
        label = " ".join(f"{name} {mode}".split())[:120] or identifier
        result.append({"id": identifier, "name": label})
    return result


def sequence_maps(response: Any) -> tuple[list[str], int]:
    entries = getattr(response, "maps", None)
    current = getattr(response, "current_index", None)
    if not isinstance(entries, (list, tuple)) or not entries or len(entries) > 1000:
        raise MapVoteError("The server returned an invalid map sequence.")
    positioned = {}
    for entry in entries:
        position = getattr(entry, "position", None)
        if isinstance(position, bool) or not isinstance(position, int) or position in positioned:
            raise MapVoteError("The server returned ambiguous map-sequence positions.")
        positioned[position] = map_id(getattr(entry, "id", None))
    if set(positioned) != set(range(len(entries))):
        raise MapVoteError("Map-sequence positions are not contiguous zero-based indices.")
    if isinstance(current, bool) or not isinstance(current, int) or current not in positioned:
        raise MapVoteError("The server returned an invalid current map index.")
    return [positioned[index] for index in range(len(entries))], current


def next_map_plan(response: Any, current_map: str, winner: str) -> tuple[str, int | None, int]:
    maps, current = sequence_maps(response)
    if maps[current].casefold() != current_map.casefold():
        raise MapVoteError(
            f"Map-sequence mismatch: recorded_map={current_map}, "
            f"sequence_current_index={current}, sequence_current_map={maps[current]}. "
            "No map change was submitted; indices will not be guessed."
        )
    next_index = (current + 1) % len(maps)
    if len(maps) > 1 and maps[next_index].casefold() == winner.casefold():
        return "none", None, next_index
    candidates = [index for index, name in enumerate(maps) if index != current and name.casefold() == winner.casefold()]
    if not candidates:
        return "add", None, current + 1
    old = min(candidates, key=lambda index: (index <= current, index))
    # Removing an entry before the current map shifts the current index left.
    return "move", old, current + 1 if old > current else current


def vote_counts(choices: list[dict[str, str]], votes: dict[str, int]) -> list[int]:
    counts = [0] * len(choices)
    for option in votes.values():
        if not isinstance(option, bool) and isinstance(option, int) and 0 <= option < len(counts):
            counts[option] += 1
    return counts


def vote_winner(choices: list[dict[str, str]], votes: dict[str, int]) -> str | None:
    counts = vote_counts(choices, votes)
    if not counts or not any(counts):
        return None
    return choices[counts.index(max(counts))]["id"]
