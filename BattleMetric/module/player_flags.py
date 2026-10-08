"""Organization-scoped BattleMetrics Seeder flags for successful VIP rewards."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from ..api import BattleMetricsAPIError

log = logging.getLogger("red.BattleMetric.player_flags")


@dataclass(frozen=True)
class HLLSeederFlagResult:
    added: int = 0
    already_flagged: int = 0
    failed: tuple[str, ...] = ()
    error: str | None = None


class PlayerFlagsModule:
    """Use the shared HTTP client with paced requests and exact player matching."""

    FLAG_NAME = "Seeder"
    REQUEST_INTERVAL_SECONDS = 2
    MATCH_BATCH_SIZE = 25
    MAX_PAGES = 20

    def __init__(self, cog: Any):
        self.cog = cog
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def _request(self, operation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        async with self._request_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await operation()
            finally:
                self._next_request_at = time.monotonic() + self.REQUEST_INTERVAL_SECONDS

    @staticmethod
    def _relationship(resource: Mapping[str, Any], name: str, resource_type: str) -> str | None:
        relationships = resource.get("relationships")
        relationship = relationships.get(name) if isinstance(relationships, Mapping) else None
        data = relationship.get("data") if isinstance(relationship, Mapping) else None
        if isinstance(data, Mapping) and data.get("type") == resource_type:
            value = data.get("id")
            return str(value) if value is not None else None
        return None

    async def _resources(self, document: Mapping[str, Any], path: str) -> list[Mapping[str, Any]]:
        resources: list[Mapping[str, Any]] = []
        visited: set[str] = set()
        for page in range(self.MAX_PAGES):
            data = document.get("data")
            if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
                raise ValueError("BattleMetrics returned an invalid flag list.")
            resources.extend(data)
            links = document.get("links")
            next_link = links.get("next") if isinstance(links, Mapping) else None
            if isinstance(next_link, Mapping):
                next_link = next_link.get("href")
            if next_link is None:
                return resources
            if not isinstance(next_link, str) or not next_link:
                raise ValueError("BattleMetrics returned an invalid flag pagination link.")
            parsed = urlparse(next_link)
            # Never forward the shared bearer token to a pagination-supplied host.
            if (
                parsed.fragment or parsed.path != path
                or (parsed.scheme and parsed.scheme != "https")
                or (parsed.netloc and parsed.netloc != "api.battlemetrics.com")
                or bool(parsed.scheme) != bool(parsed.netloc)
                or next_link in visited
            ):
                raise ValueError("BattleMetrics returned an unsafe or repeated flag pagination link.")
            visited.add(next_link)
            if page + 1 == self.MAX_PAGES:
                break
            document = await self._request(lambda url=next_link: self.cog.api.get(url, auth=True))
        raise ValueError("BattleMetrics flag pagination exceeded the safety limit.")

    async def _find_seeder_flag(self, guild: Any) -> str:
        if not await self.cog.get_api_token():
            raise ValueError("Configure the BattleMetrics API key in Red's shared token vault.")
        server_id = await self.cog.get_default_server_id(guild)
        if not server_id:
            raise ValueError("Set the BattleMetrics server with serverinfo setup or bm setserver.")
        document = await self._request(lambda: self.cog.api.get_server(server_id, auth=True))
        server = document.get("data")
        if (
            not isinstance(server, Mapping) or server.get("type") != "server"
            or str(server.get("id")) != str(server_id)
        ):
            raise ValueError("BattleMetrics returned an invalid server resource.")
        organization_id = self._relationship(server, "organization", "organization")
        if not organization_id:
            raise ValueError("The configured BattleMetrics server has no accessible organization.")
        document = await self._request(self.cog.api.list_player_flags)
        flags = await self._resources(document, "/player-flags")
        matches: set[str] = set()
        for flag in flags:
            attributes = flag.get("attributes")
            meta = flag.get("meta")
            if (
                flag.get("type") != "playerFlag" or not isinstance(attributes, Mapping)
                or str(attributes.get("name", "")).strip().casefold() != self.FLAG_NAME.casefold()
                or self._relationship(flag, "organization", "organization") != organization_id
                or (isinstance(meta, Mapping) and meta.get("shared"))
            ):
                continue
            if flag.get("id") is not None:
                matches.add(str(flag["id"]))
        if len(matches) != 1:
            raise ValueError("Create exactly one organization-owned player flag named Seeder for this server.")
        return matches.pop()

    @staticmethod
    def _identifier_types(eos_id: str) -> tuple[str, ...]:
        return ("steamID",) if len(eos_id) == 17 and eos_id.isdigit() else ("eosID", "hllWindowsID")

    @classmethod
    def _matched_players(cls, document: Mapping[str, Any], targets: Sequence[str]) -> dict[str, str]:
        data = document.get("data")
        if not isinstance(data, list):
            raise ValueError("BattleMetrics returned an invalid player match list.")
        matches: dict[str, set[str]] = {target: set() for target in targets}
        for identifier in data:
            if not isinstance(identifier, Mapping) or identifier.get("type") != "identifier":
                continue
            attributes = identifier.get("attributes")
            if not isinstance(attributes, Mapping) or not isinstance(attributes.get("identifier"), str):
                continue
            value = attributes["identifier"].casefold()
            if value not in matches or attributes.get("type") not in cls._identifier_types(value):
                continue
            player_id = cls._relationship(identifier, "player", "player")
            if player_id and player_id.isdigit():
                matches[value].add(player_id)
        return {target: next(iter(ids)) for target, ids in matches.items() if len(ids) == 1}

    async def _already_flagged(self, player_id: str, flag_id: str) -> bool:
        document = await self._request(lambda: self.cog.api.list_player_flag_assignments(player_id))
        path = f"/players/{quote(player_id, safe='')}/relationships/flags"
        for assignment in await self._resources(document, path):
            attributes = assignment.get("attributes")
            if isinstance(attributes, Mapping) and attributes.get("removedAt") is not None:
                continue
            if (
                assignment.get("type") == "flagPlayer"
                and self._relationship(assignment, "playerFlag", "playerFlag") == flag_id
            ):
                return True
        return False

    @staticmethod
    def _stop_reason(exc: Exception) -> str | None:
        if isinstance(exc, BattleMetricsAPIError):
            if exc.status in {401, 403}:
                return "BattleMetrics denied access. Check token permissions for identifiers and player flags."
            if exc.status == 429:
                return "BattleMetrics rate-limited flagging. Remaining flag requests were stopped; VIP rewards remain valid."
        return None

    async def flag_seeders(self, guild: Any, eos_ids: Sequence[str]) -> HLLSeederFlagResult:
        targets = tuple(dict.fromkeys(eos_id.casefold() for eos_id in eos_ids))
        if not targets:
            return HLLSeederFlagResult()
        try:
            flag_id = await self._find_seeder_flag(guild)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Seeder flag setup failed in guild %s (%s)", guild.id, type(exc).__name__)
            error = self._stop_reason(exc) or (
                str(exc) if isinstance(exc, ValueError) else "BattleMetrics flag setup is unavailable. VIP rewards remain valid."
            )
            return HLLSeederFlagResult(failed=targets, error=error)

        added = already = 0
        failed: list[str] = []
        stopped: str | None = None
        confirmed_players: set[str] = set()
        failed_players: set[str] = set()
        for offset in range(0, len(targets), self.MATCH_BATCH_SIZE):
            chunk = targets[offset:offset + self.MATCH_BATCH_SIZE]
            try:
                identifiers = [(eos_id, kind) for eos_id in chunk for kind in self._identifier_types(eos_id)]
                document = await self._request(lambda: self.cog.api.quick_match_player_batch(identifiers))
                matched = self._matched_players(document, chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stopped = self._stop_reason(exc)
                failed.extend(targets[offset:] if stopped else chunk)
                log.warning("Seeder player matching failed in guild %s (%s)", guild.id, type(exc).__name__)
                if stopped:
                    break
                continue

            for index, eos_id in enumerate(chunk):
                player_id = matched.get(eos_id)
                if player_id is None:
                    failed.append(eos_id)
                    continue
                if player_id in confirmed_players:
                    already += 1
                    continue
                if player_id in failed_players:
                    failed.append(eos_id)
                    continue
                try:
                    if await self._already_flagged(player_id, flag_id):
                        already += 1
                    else:
                        await self._request(lambda: self.cog.api.assign_player_flag(player_id, flag_id))
                        added += 1
                    confirmed_players.add(player_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_players.add(player_id)
                    stopped = self._stop_reason(exc)
                    failed.extend(targets[offset + index:] if stopped else (eos_id,))
                    log.warning("Seeder flagging failed in guild %s (%s)", guild.id, type(exc).__name__)
                    if stopped:
                        break
            if stopped:
                break
        return HLLSeederFlagResult(added, already, tuple(failed), stopped)
