"""Scheduled map votes using cached rotations and the shared RCON log stream."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from .hll_ban import HLLBanModule
from .hll_group import HLLVN_COMMAND_GROUP
from .hll_maps import HLLMapModule
from .kill_feed import KillFeedConnectionTestError
from .map_vote_rules import MapVoteError, map_id, next_map_plan, rotation_maps, sequence_maps, vote_counts, vote_winner

try:
    from hllrcon.admin_logs import HLLVMatchEndAdminLog, HLLVMatchStartAdminLog, HLLVPlayerSendMessageAdminLog
except Exception:
    HLLVMatchEndAdminLog = HLLVMatchStartAdminLog = HLLVPlayerSendMessageAdminLog = None

log = logging.getLogger("red.BattleMetric.map_vote")


class HLLMapVoteModule:
    STATUS_INTERVAL = 60
    LEADER_INTERVAL = 30
    MESSAGE_INTERVAL = 3
    MAX_EVENTS = 1000
    MAX_MESSAGES = 200
    MAX_SEEN = 2000
    MAX_VOTERS = 10000
    DEFAULTS = {"enabled": False, "vote_start_minutes": 20, "duration_minutes": None, "number_of_options": 5}

    def __init__(self, cog: Any):
        self.cog = cog
        self._enabled: set[int] = set()
        self._settings: dict[int, dict] = {}
        self._states: dict[int, dict] = {}
        self._rotations: dict[int, dict] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._events: dict[int, deque] = {}
        self._messages: dict[int, deque] = {}
        self._seen: dict[int, OrderedDict] = {}
        self._cooldowns: OrderedDict[tuple[int, str, str], float] = OrderedDict()
        self._next_status: dict[int, float] = {}
        self._next_sync: dict[int, float] = {}
        self._next_leader: dict[int, float] = {}
        self._next_message: dict[int, float] = {}
        self._next_event: dict[int, float] = {}
        self._failures: dict[tuple[int, str], int] = {}

    def register_config(self) -> None:
        self.cog.config.register_guild(
            hll_mapvote=dict(self.DEFAULTS), hll_mapvote_state={}, hll_mapvote_rotation={}
        )

    @classmethod
    def settings(cls, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raw = cls.DEFAULTS
        start = raw.get("vote_start_minutes", 20)
        duration = raw.get("duration_minutes")
        options = raw.get("number_of_options", 5)
        if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= 1440:
            raise MapVoteError("Vote start time must be 0-1440 minutes after match start.")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 1440):
            raise MapVoteError("Duration must be 1-1440 minutes, or omitted to vote until match end.")
        if isinstance(options, bool) or not isinstance(options, int) or not 1 <= options <= 5:
            raise MapVoteError("Number of options must be between 1 and 5.")
        if not isinstance(raw.get("enabled", False), bool):
            raise MapVoteError("Enable must be true or false.")
        return {"enabled": raw.get("enabled", False), "vote_start_minutes": start,
                "duration_minutes": duration, "number_of_options": options}

    async def start(self) -> None:
        if self.worker.is_running():
            return
        for guild in self.cog.bot.guilds:
            try:
                await self._load(guild)
            except Exception:
                log.exception("Could not restore map voting for guild %s", guild.id)
        if not self.worker.is_running():
            self.worker.start()

    def stop(self) -> None:
        self.worker.cancel()
        for values in (self._enabled, self._settings, self._states, self._rotations, self._locks,
                       self._events, self._messages, self._seen, self._cooldowns, self._next_status,
                       self._next_sync, self._next_leader, self._next_message, self._failures):
            values.clear()
        self._next_event.clear()

    def should_poll(self, guild_id: int) -> bool:
        return guild_id in self._enabled

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _endpoint(self, guild: discord.Guild) -> list:
        settings = await self.cog.kill_feed.get_settings(guild)
        return [settings.get("host"), settings.get("port")]

    async def _load(self, guild: discord.Guild) -> None:
        config = self.cog.config.guild(guild)
        self._settings[guild.id] = self.settings(await config.hll_mapvote())
        rotation = await config.hll_mapvote_rotation()
        self._rotations[guild.id] = rotation if self._valid_rotation(rotation) else {}
        state = await config.hll_mapvote_state()
        if not self._valid_state(state):
            state = {}
        self._states[guild.id] = state
        if self._settings[guild.id]["enabled"]:
            self._enabled.add(guild.id)

    @staticmethod
    def _valid_rotation(cache: Any) -> bool:
        try:
            if not isinstance(cache, dict) or not isinstance(cache["maps"], list) or len(cache["maps"]) > 1000:
                return False
            identifiers = [map_id(entry["id"]).casefold() for entry in cache["maps"]]
            return len(set(identifiers)) == len(identifiers) and all(
                isinstance(entry["name"], str) and len(entry["name"]) <= 200 for entry in cache["maps"]
            ) and isinstance(cache["endpoint"], list) and len(cache["endpoint"]) == 2
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _valid_state(state: Any) -> bool:
        if not isinstance(state, dict) or not state:
            return False
        try:
            map_id(state["map_id"])
            if state["phase"] not in {"waiting", "voting", "closed"}:
                return False
            if not isinstance(state["token"], str) or not isinstance(state["endpoint"], list):
                return False
            for field in ("started_at", "vote_started_at", "closes_at", "ended_at"):
                if state[field] is not None and (
                    isinstance(state[field], bool) or not isinstance(state[field], (int, float))
                    or not math.isfinite(state[field])
                ):
                    return False
            if not isinstance(state["choices"], list) or len(state["choices"]) > 5:
                return False
            for choice in state["choices"]:
                map_id(choice["id"])
                if not isinstance(choice["name"], str) or len(choice["name"]) > 200:
                    return False
            votes = state["votes"]
            if state["phase"] != "waiting" and not state["choices"]:
                return False
            if state.get("queued_winner") is not None and state["queued_winner"] not in {choice["id"] for choice in state["choices"]}:
                return False
            return isinstance(votes, dict) and len(votes) <= 10000 and all(
                HLLBanModule.normalize_eos_id(identifier) == identifier
                and not isinstance(option, bool) and isinstance(option, int) and 0 <= option < len(state["choices"])
                for identifier, option in votes.items()
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            return False

    async def _save(self, guild: discord.Guild) -> None:
        await self.cog.config.guild(guild).hll_mapvote_state.set(copy.deepcopy(self._states.get(guild.id, {})))

    async def configure(self, guild: discord.Guild, raw: dict) -> dict:
        settings = self.settings(raw)
        if settings["enabled"] and HLLVMatchStartAdminLog is None:
            raise RuntimeError("The required hllrcon match/chat models are unavailable.")
        async with self._lock(guild.id):
            if guild.id not in self._settings:
                await self._load(guild)
            if settings["enabled"]:
                await self._sync(guild)
            await self.cog.config.guild(guild).hll_mapvote.set(settings)
            self._settings[guild.id] = settings
            if settings["enabled"]:
                self._enabled.add(guild.id)
                self._next_status[guild.id] = 0
            else:
                self._enabled.discard(guild.id)
                self._states[guild.id] = {}
                self._events.pop(guild.id, None)
                self._messages.pop(guild.id, None)
                self._seen.pop(guild.id, None)
                for timers in (self._next_event, self._next_status, self._next_sync, self._next_leader, self._next_message):
                    timers.pop(guild.id, None)
                self._cooldowns = OrderedDict((key, value) for key, value in self._cooldowns.items() if key[0] != guild.id)
                await self._save(guild)
            return settings

    async def _sync(self, guild: discord.Guild) -> dict:
        response = await self.cog.kill_feed.execute_rcon(
            guild, "Map-vote rotation sync", lambda client: client.get_map_rotation()
        )
        maps = rotation_maps(response)
        for entry in maps:
            display = HLLMapModule.display_name(entry["id"])
            if display != entry["id"]:
                entry["name"] = display[:120]
        state = self._states.get(guild.id, {})
        cache = {"maps": maps, "endpoint": await self._endpoint(guild),
                 "synced_at": time.time(), "token": state.get("token")}
        await self.cog.config.guild(guild).hll_mapvote_rotation.set(cache)
        self._rotations[guild.id] = cache
        self._next_sync.pop(guild.id, None)
        return cache

    async def sync(self, guild: discord.Guild) -> dict:
        async with self._lock(guild.id):
            return await self._sync(guild)

    async def _session(self, guild: discord.Guild) -> Any:
        session = await self.cog.kill_feed.execute_rcon(
            guild, "Map-vote match status", lambda client: client.get_server_session()
        )
        map_id(getattr(session, "map_id", None))
        return session

    async def _new_match(self, guild: discord.Guild, session: Any, started_at: float | None) -> None:
        previous = self._states.get(guild.id, {})
        token = f"{session.map_id}:{started_at if started_at is not None else time.time()}"
        self._states[guild.id] = {
            "token": token, "endpoint": await self._endpoint(guild), "map_id": session.map_id,
            "map_name": str(getattr(session, "map_name", session.map_id)), "started_at": started_at,
            "vote_started_at": None, "closes_at": None, "ended_at": None, "phase": "waiting",
            "choices": [], "votes": {}, "queued_winner": None, "result_winner": None, "final_verified": False,
        }
        self._messages.pop(guild.id, None)
        self._next_leader.pop(guild.id, None)
        self._next_sync.pop(guild.id, None)
        if previous.get("token") != token:
            self._cooldowns = OrderedDict((key, value) for key, value in self._cooldowns.items() if key[0] != guild.id)
        await self._save(guild)
        # The new state is saved first; a failed sync cannot resurrect the last vote.
        try:
            await self._sync(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._next_sync[guild.id] = time.monotonic() + 60
            log.warning("Map rotation sync failed for new match in guild %s", guild.id, exc_info=True)

    async def start_vote(self, guild: discord.Guild) -> dict:
        async with self._lock(guild.id):
            if not self.should_poll(guild.id):
                raise MapVoteError("Enable map voting with /hllvn mapvote first.")
            session = await self._session(guild)
            state = self._states.get(guild.id, {})
            endpoint = await self._endpoint(guild)
            if state.get("map_id") != session.map_id or state.get("endpoint") != endpoint:
                await self._new_match(guild, session, None)
                state = self._states[guild.id]
            if state["phase"] != "waiting":
                raise MapVoteError("A vote has already started or finished for this match.")
            if state["ended_at"] is not None:
                raise MapVoteError("This match has ended. Wait for the next match before starting a vote.")
            await self._open_vote(guild)
            return copy.deepcopy(self._states[guild.id])

    async def _open_vote(self, guild: discord.Guild) -> None:
        state = self._states[guild.id]
        cache = self._rotations.get(guild.id, {})
        if cache.get("token") != state["token"] or cache.get("endpoint") != state["endpoint"]:
            raise MapVoteError("The rotation is not synced for this match. Use /hllvn mapchange sync.")
        candidates = [entry for entry in cache.get("maps", []) if entry["id"].casefold() != state["map_id"].casefold()]
        if not candidates:
            raise MapVoteError("The synced rotation has no alternative maps to vote for.")
        settings = self._settings[guild.id]
        choices = random.SystemRandom().sample(candidates, min(settings["number_of_options"], len(candidates)))
        now = time.time()
        duration = settings["duration_minutes"]
        state.update(phase="voting", choices=copy.deepcopy(choices), votes={}, vote_started_at=now,
                     closes_at=None if duration is None else now + duration * 60, queued_winner=None)
        await self._save(guild)
        self._enqueue(guild.id, None, self.popup(state), priority=True)

    @staticmethod
    def popup(state: dict) -> str:
        if not state.get("choices"):
            return "Map voting has not started. Please wait for the next vote."
        counts = vote_counts(state["choices"], state["votes"])
        lines = ["HLLVN NEXT MAP VOTE" if state["phase"] == "voting" else "HLLVN MAP VOTE - CLOSED"]
        for index, (choice, count) in enumerate(zip(state["choices"], counts), 1):
            lines.append(f"{index}. {choice['name'][:120]} ({count} votes)")
        if state["phase"] == "voting":
            lines.append(f"Type !vote 1-{len(counts)} in team/unit chat. You can change your vote.")
            lines.append("Type !maplist to see this list again.")
            if state["closes_at"] is None:
                lines.append("Voting stays open until match end.")
            else:
                lines.append(f"Voting closes in {max(0, int((state['closes_at'] - time.time() + 59) // 60))} minute(s).")
        return "\n".join(lines)[:1000]

    async def ingest_admin_logs(self, guild: discord.Guild, entries: Any) -> None:
        if not self.should_poll(guild.id) or HLLVPlayerSendMessageAdminLog is None:
            return
        seen = self._seen.setdefault(guild.id, OrderedDict())
        queue = self._events.setdefault(guild.id, deque())
        for entry in entries:
            if isinstance(entry, HLLVMatchStartAdminLog):
                kind = "start"
            elif isinstance(entry, HLLVMatchEndAdminLog):
                kind = "end"
            elif isinstance(entry, HLLVPlayerSendMessageAdminLog) and re.match(r"^!(?:vote|maplist)(?:\s|$)", str(entry.message).strip(), re.I):
                kind = "chat"
            else:
                continue
            fingerprint = f"{kind}:{entry.timestamp.isoformat()}:{entry.raw_message}"
            if fingerprint in seen:
                continue
            seen[fingerprint] = None
            while len(seen) > self.MAX_SEEN:
                seen.popitem(last=False)
            if len(queue) >= self.MAX_EVENTS:
                if kind == "chat":
                    log.warning("Dropped map-vote chat event at queue limit in guild %s", guild.id)
                    continue
                queue.popleft()
            queue.append((kind, entry))

    async def _event(self, guild: discord.Guild, kind: str, entry: Any) -> None:
        occurred = entry.timestamp.timestamp()
        state = self._states.get(guild.id, {})
        if state.get("started_at") is not None and occurred < state["started_at"]:
            return
        if kind == "start":
            if state.get("started_at") is not None and occurred <= state["started_at"]:
                return
            session = await self._session(guild)
            if str(entry.map_name).casefold() != str(session.map_name).casefold():
                return
            if (state.get("started_at") is None and state.get("phase") == "voting"
                    and state.get("map_id") == session.map_id and occurred <= state["vote_started_at"]):
                state["started_at"] = occurred
                await self._save(guild)
                return
            await self._new_match(guild, session, occurred)
            self._next_status[guild.id] = time.monotonic() + self.STATUS_INTERVAL
            return
        if not state:
            return
        if kind == "end":
            if str(entry.map_name).casefold() != str(state["map_name"]).casefold():
                return
            state["ended_at"] = occurred
            if state["phase"] == "voting":
                await self._close_vote(guild)
            return
        self._chat(guild.id, state, entry, occurred)

    def _chat(self, guild_id: int, state: dict, entry: Any, occurred: float) -> None:
        identifier = HLLBanModule.normalize_eos_id(str(entry.player_id))
        if identifier is None:
            return
        text = str(entry.message).strip().lower()
        command = text.split()[0]
        cooldown_key = (guild_id, identifier, command)
        now = time.monotonic()
        if self._cooldowns.get(cooldown_key, 0) > now:
            return
        self._cooldowns[cooldown_key] = now + (30 if command == "!maplist" else 5)
        self._cooldowns.move_to_end(cooldown_key)
        while len(self._cooldowns) > self.MAX_SEEN:
            self._cooldowns.popitem(last=False)
        if command == "!maplist":
            self._enqueue(guild_id, identifier, self.popup(state))
            return
        if state["phase"] != "voting" or occurred < state["vote_started_at"]:
            self._enqueue(guild_id, identifier, "Map voting is not open.")
            return
        if state["closes_at"] is not None and occurred >= state["closes_at"]:
            self._enqueue(guild_id, identifier, "Map voting has closed.")
            return
        match = re.fullmatch(r"!vote\s+([1-5])", text)
        if match is None or int(match[1]) > len(state["choices"]):
            self._enqueue(guild_id, identifier, f"Use !vote 1-{len(state['choices'])} to choose a map.")
            return
        if identifier not in state["votes"] and len(state["votes"]) >= self.MAX_VOTERS:
            return
        option = int(match[1]) - 1
        state["votes"][identifier] = option
        self._enqueue(guild_id, identifier, f"Vote recorded: {option + 1}. {state['choices'][option]['name']}")

    def _enqueue(self, guild_id: int, target: str | None, text: str, *, priority: bool = False) -> None:
        state = self._states.get(guild_id, {})
        queue = self._messages.setdefault(guild_id, deque())
        if len(queue) >= self.MAX_MESSAGES:
            queue.pop() if priority else queue.popleft()
        item = (state.get("token"), target, text[:1000])
        queue.appendleft(item) if priority else queue.append(item)

    async def _close_vote(self, guild: discord.Guild) -> None:
        state = self._states[guild.id]
        state["phase"] = "closed"
        state["result_winner"] = vote_winner(state["choices"], state["votes"])
        state["final_verified"] = False
        self._next_leader[guild.id] = 0
        await self._save(guild)
        winner = vote_winner(state["choices"], state["votes"])
        if winner is None:
            text = "Map voting closed with no votes. No further next-map update was submitted."
            self._enqueue(guild.id, None, text, priority=True)

    def _result_popup(self, guild_id: int, state: dict, winner: str) -> None:
        name = next(choice["name"] for choice in state["choices"] if choice["id"] == winner)
        self._enqueue(guild_id, None, f"Map voting closed. Next map: {name}", priority=True)

    async def _queue_leader(self, guild: discord.Guild) -> None:
        state = self._states[guild.id]
        closed = state["phase"] == "closed"
        winner = state.get("result_winner") if closed else vote_winner(state["choices"], state["votes"])
        if winner is None or (closed and state.get("final_verified")):
            return
        if not closed and winner == state.get("queued_winner"):
            return
        if state["ended_at"] is not None and time.time() > state["ended_at"] + 30:
            return
        if winner not in {entry["id"] for entry in self._rotations.get(guild.id, {}).get("maps", [])}:
            raise MapVoteError("The voted map was removed from the synced rotation.")

        async def operation(client: Any) -> bool:
            session = await client.get_server_session()
            if getattr(session, "map_id", None) != state["map_id"]:
                return False
            response = await client.get_map_sequence()
            action, old, new = next_map_plan(response, state["map_id"], winner)
            if action == "move":
                await client.move_map_in_sequence(old, new)
            elif action == "add":
                await client.add_map_to_sequence(winner, new)
            if action != "none":
                response = await client.get_map_sequence()
            maps, current = sequence_maps(response)
            return maps[current].casefold() == state["map_id"].casefold() and maps[(current + 1) % len(maps)].casefold() == winner.casefold()

        verified = await self.cog.kill_feed.execute_rcon(guild, "Map-vote next-map sequence update", operation)
        if not verified:
            raise MapVoteError("The next-map update was not verified or the match already advanced.")
        state["queued_winner"] = winner
        if closed:
            state["final_verified"] = True
        await self._save(guild)
        if state["phase"] == "closed":
            self._result_popup(guild.id, state, winner)

    def _backoff(self, guild_id: int, stage: str) -> float:
        key = (guild_id, stage)
        self._failures[key] = self._failures.get(key, 0) + 1
        return min(60, 3 * 2 ** min(5, self._failures[key] - 1))

    async def _tick(self, guild: discord.Guild) -> None:
        async with self._lock(guild.id):
            now = time.time()
            clock = time.monotonic()
            endpoint = await self._endpoint(guild)
            state = self._states.get(guild.id, {})
            if state and state.get("endpoint") != endpoint:
                self._states[guild.id] = {}
                self._events.pop(guild.id, None)
                self._messages.pop(guild.id, None)
                self._seen.pop(guild.id, None)
                self._rotations[guild.id] = {}
                await self._save(guild)
                self._next_status[guild.id] = 0
            queue = self._events.setdefault(guild.id, deque())
            batch = []
            if clock >= self._next_event.get(guild.id, 0):
                batch = [queue.popleft() for _ in range(min(100, len(queue)))]
            batch.sort(key=lambda item: item[1].timestamp)
            for index, (kind, entry) in enumerate(batch):
                try:
                    await self._event(guild, kind, entry)
                    if kind != "chat":
                        self._failures.pop((guild.id, "event"), None)
                        self._next_event.pop(guild.id, None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Map-vote event failed for guild %s", guild.id, exc_info=True)
                    if kind != "chat":
                        remaining = batch[index:]
                        while len(queue) + len(remaining) > self.MAX_EVENTS:
                            queue.pop()
                        queue.extendleft(reversed(remaining))
                        self._next_event[guild.id] = time.monotonic() + self._backoff(guild.id, "event")
                        break
            if batch:
                await self._save(guild)
            if clock >= self._next_status.get(guild.id, 0):
                self._next_status[guild.id] = clock + self.STATUS_INTERVAL
                session = await self._session(guild)
                if self._states.get(guild.id, {}).get("map_id") != session.map_id:
                    await self._new_match(guild, session, None)
            state = self._states.get(guild.id, {})
            if not state:
                return
            cache = self._rotations.get(guild.id, {})
            if cache.get("token") != state["token"] and clock >= self._next_sync.get(guild.id, 0):
                self._next_sync[guild.id] = clock + 60
                await self._sync(guild)
            if state["phase"] == "waiting" and state["started_at"] is not None and state["ended_at"] is None:
                if now >= state["started_at"] + self._settings[guild.id]["vote_start_minutes"] * 60:
                    if clock >= self._next_sync.get(guild.id, 0):
                        try:
                            await self._open_vote(guild)
                        except MapVoteError:
                            self._next_sync[guild.id] = clock + 60
                            log.warning("No map vote options for guild %s", guild.id, exc_info=True)
            # Allow the shared three-second log poll to deliver pre-deadline votes.
            if state["phase"] == "voting" and state["closes_at"] is not None and now >= state["closes_at"] + 6:
                await self._close_vote(guild)
            if state["phase"] in {"voting", "closed"} and clock >= self._next_leader.get(guild.id, 0):
                self._next_leader[guild.id] = clock + self.LEADER_INTERVAL
                try:
                    await self._queue_leader(guild)
                    self._failures.pop((guild.id, "leader"), None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._next_leader[guild.id] = time.monotonic() + self._backoff(guild.id, "leader")
                    log.warning("Could not queue map-vote winner in guild %s; will re-read before retry", guild.id, exc_info=True)
            messages = self._messages.setdefault(guild.id, deque())
            if messages and clock >= self._next_message.get(guild.id, 0):
                token, target, text = messages.popleft()
                self._next_message[guild.id] = clock + self.MESSAGE_INTERVAL
                if token == state["token"]:
                    try:
                        await self.cog.kill_feed.execute_rcon(
                            guild, "Map-vote popup delivery",
                            lambda client: client.message_all_players(text) if target is None else client.message_player(target, text),
                        )
                        self._failures.pop((guild.id, "message"), None)
                        self._next_message[guild.id] = time.monotonic() + self.MESSAGE_INTERVAL
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._next_message[guild.id] = time.monotonic() + self._backoff(guild.id, "message")
                        log.warning("Map-vote popup delivery was not confirmed in guild %s", guild.id)

    @tasks.loop(seconds=1)
    async def worker(self) -> None:
        for guild in self.cog.bot.guilds:
            if self.should_poll(guild.id):
                try:
                    await self._tick(guild)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Map-vote worker failed for guild %s", guild.id, exc_info=True)

    @worker.before_loop
    async def before_worker(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def delete_user_data(self, user_id: int) -> None:
        if not self.cog.hll_database.has_credentials():
            return
        stats = await self.cog.hll_database.get_stats_by_discord(user_id)
        if stats is None:
            return
        for guild_id, raw in (await self.cog.config.all_guilds()).items():
            async with self._lock(guild_id):
                state = self._states.get(guild_id, raw.get("hll_mapvote_state", {}))
                if not isinstance(state, dict):
                    continue
                if stats.eos_id in state.get("votes", {}):
                    del state["votes"][stats.eos_id]
                    await self.cog.config.guild_from_id(guild_id).hll_mapvote_state.set(copy.deepcopy(state))
                self._messages[guild_id] = deque(item for item in self._messages.get(guild_id, ()) if item[1] != stats.eos_id)
                self._events[guild_id] = deque(
                    item for item in self._events.get(guild_id, ()) if getattr(item[1], "player_id", None) != stats.eos_id
                )
                self._cooldowns = OrderedDict((key, value) for key, value in self._cooldowns.items() if key[:2] != (guild_id, stats.eos_id))


class HLLMapVoteCommandsMixin:
    hllvn = HLLVN_COMMAND_GROUP
    hllvn_mapchange = app_commands.Group(name="mapchange", description="Sync map voting or start a vote.", parent=hllvn)

    async def _mapvote_request(self, interaction: discord.Interaction, action: str, raw: dict | None = None) -> None:
        if not await self.is_authorized(interaction.user):
            await interaction.response.send_message("You are not authorized to use HLL: Vietnam administration commands.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            if action == "configure":
                settings = await self.map_vote.configure(interaction.guild, raw)
                text = (
                    f"Map voting {'enabled' if settings['enabled'] else 'disabled'}. "
                    f"Start: {settings['vote_start_minutes']} minute(s) after an observed match start. "
                    f"Options: {settings['number_of_options']}. "
                    + ("Duration: until match end." if settings['duration_minutes'] is None else f"Duration: {settings['duration_minutes']} minute(s).")
                )
            elif action == "sync":
                cache = await self.map_vote.sync(interaction.guild)
                text = f"Synced {len(cache['maps'])} unique rotation maps. Existing vote options were not changed."
            else:
                state = await self.map_vote.start_vote(interaction.guild)
                text = f"Map vote started with {len(state['choices'])} options. The in-game popup is queued."
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            text = str(exc)
        except KillFeedConnectionTestError:
            text = "RCON could not complete the map-vote request. Check the Red service log; do not assume a next-map change succeeded."
        except RuntimeError:
            text = "The required hllrcon dependency or match/chat models are unavailable."
        except Exception:
            log.exception("Map-vote administration failed for guild %s", interaction.guild.id)
            text = "The map-vote request could not be completed. Ask the bot owner to check the Red service log."
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @hllvn.command(name="mapvote", description="Configure automatic in-game next-map voting.")
    @app_commands.describe(
        vote_start_minutes="Minutes after an observed match start before voting opens.",
        duration_minutes="Vote duration in minutes. Omit to vote until match end.",
        number_of_options="Number of random rotation maps to offer (1-5).",
        enable="Enable or disable scheduled map voting and in-game vote commands.",
    )
    @app_commands.guild_only()
    async def hllvn_mapvote(
        self, interaction: discord.Interaction, vote_start_minutes: app_commands.Range[int, 0, 1440],
        number_of_options: app_commands.Range[int, 1, 5], enable: bool,
        duration_minutes: app_commands.Range[int, 1, 1440] | None = None,
    ) -> None:
        await self._mapvote_request(interaction, "configure", {
            "enabled": enable, "vote_start_minutes": vote_start_minutes,
            "duration_minutes": duration_minutes, "number_of_options": number_of_options,
        })

    @hllvn_mapchange.command(name="sync", description="Refresh the bot's cached server map rotation.")
    @app_commands.guild_only()
    async def hllvn_mapchange_sync(self, interaction: discord.Interaction) -> None:
        await self._mapvote_request(interaction, "sync")

    @hllvn_mapchange.command(name="startvote", description="Start this match's map vote immediately.")
    @app_commands.guild_only()
    async def hllvn_mapchange_startvote(self, interaction: discord.Interaction) -> None:
        await self._mapvote_request(interaction, "start")
