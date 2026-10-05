# Repository Guide for Coding Agents

This file applies to the entire `HarukoCog` repository. Read it before changing
code, then read the README and manifest for the cog you are touching. The
repository contains independent Redbot cogs, not one monolithic application.

## Repository identity

The Git root is this directory (`HarukoCog`), not its parent `redcog` directory.
The root `info.json` describes the installable Red cog repository. Each top-level
cog directory has its own loader, manifest, implementation, and README:

| Cog | Purpose | Main implementation |
| --- | --- | --- |
| `BattleMetric` | BattleMetrics, HLL: Vietnam RCON, PostgreSQL stats/linking, VIP, moderation, seeding, and dog tags | `BattleMetric/battlemetric.py` plus `BattleMetric/module/` |
| `HaruAdmins` | Discord moderation with persistent timeouts longer than Discord's 28-day limit | `HaruAdmins/haruadmins.py` |
| `VoiceChannelHandling` | Join-to-create temporary voice channels and persistent owner controls | `VoiceChannelHandling/voicechannelhandling.py` plus `VoiceChannelHandling/VCC/` |

Binary assets under `BattleMetric/res/` are runtime resources. The SQL under
`BattleMetric/sql/` is an operational schema/constraint migration. Do not treat
either directory as disposable generated output.

## Framework fundamentals

This is a Python 3.11+ Red-DiscordBot 3.5+ cog repository. Every cog loader must
continue to expose an asynchronous `setup(bot)` and await `bot.add_cog(...)`.
Manifests are part of the runtime contract: dependencies, minimum versions,
install messages, tags, and end-user-data statements must match the code.

Follow these rules across all cogs:

1. Use async Discord, HTTP, database, and RCON APIs. Do not block the event loop.
   CPU-bound rendering or blocking filesystem work should run outside the event
   loop when it can be significant.
2. Red `Config` is the durable source of truth for ordinary settings and state.
   Register defaults before reading them. Use per-guild state for guild-specific
   behavior and guard read-modify-write sequences with the appropriate lock.
3. Secrets belong in Red's shared API-token vault, never in `Config`, source,
   logs, embeds, or error messages. Existing services include `battlemetrics`,
   `hllrcon`, `battlemetric_db`, and `tagweb`.
4. Validate guild context, permissions, IDs, ranges, and text lengths before
   side effects. User-facing errors must be useful but must not expose secrets or
   raw connection details.
5. Start background workers in `cog_load` or a module `start()` method and stop
   them in `cog_unload` or `stop()`. Cancel tasks, close clients/pools, clear
   transient queues, and make repeated start/stop calls safe.
6. Bound queues and caches, deduplicate external events, pace outbound requests,
   and back off after repeated failures. One bad guild or endpoint must not stop
   a global worker.
7. Use `logging.getLogger("red.<Cog>[.<module>]")`. Log diagnostic context, but
   never credentials or private payloads.
8. Preserve Red's data-deletion behavior and update both the loader statement
   and manifest statement whenever stored or transmitted user data changes.
9. Preserve unrelated work in a dirty worktree. Inspect the diff before editing
   and do not overwrite or reformat files outside the requested scope.

## Discord interaction rules

- An interaction gets exactly one initial response. A modal must be sent as that
  initial response; do not defer before `send_modal()`.
- For slow non-modal work, defer once and use follow-up messages afterward.
- Administrative replies should normally be ephemeral. Public user commands may
  intentionally respond publicly; preserve their established contract.
- Restrict `AllowedMentions`. The admin-alert pipeline may mention only its
  configured role; other status/error output should not create arbitrary pings.
- Persistent views require stable `custom_id` values, `timeout=None`, and
  registration after cog construction/reload.
- Hybrid commands must work through both prefix and application-command paths.
  Keep their Red checks, Discord default permissions, bot permission checks, and
  guild-only constraints consistent.
- Adding or renaming application commands requires a cog reload and slash-command
  sync during deployment; document that operational step.

## BattleMetric architecture

`BattleMetric/battlemetric.py` is the composition root. The `BattleMetric` class
inherits command mixins, owns shared clients/configuration, creates feature
services, registers module config, connects shared log consumers, and coordinates
lifecycle and token updates. Keep it thin; substantial features belong under
`BattleMetric/module/`.

A BattleMetric feature normally has:

- a service/module class that owns its config, state, locks, and workers;
- a command mixin when the feature exposes commands;
- explicit `register_config()`, `start()`, `stop()`, token-refresh, and
  user-deletion integration where applicable;
- exports in `BattleMetric/module/__init__.py` and composition in the main cog;
- matching README, manifest, privacy, and version updates.

Do not create a second top-level cog for a BattleMetric feature unless the user
explicitly requests one. Preserve the existing module boundary.

### Authorization is a hard boundary

Administrative BattleMetric commands are limited to guild-scoped users managed
by `bm auth` and Red bot owners. Authorization managers are bot owners or members
allowed to manage the guild. Reuse `BattleMetric/authorization.py` for hybrid
commands and `BattleMetric.is_authorized(...)` for direct app-command handlers.

For every administrative application command, authorization must happen before:

- `defer()` or any other interaction acknowledgement beyond the private denial;
- reading configuration or resolving linked accounts;
- API, database, filesystem, or RCON work;
- any mutation or externally visible side effect.

On denial, send one ephemeral response and return. Do not replace this model with
role-name checks or Discord Administrator alone. Public commands such as account
linking, player statistics, and player-initiated VIP purchase are deliberately
separate; do not accidentally put them behind the admin gate.

### Shared integrations

- `BattleMetric/api.py` is the only BattleMetrics HTTP boundary. Reuse its
  session, URL/parameter construction, authentication, JSON:API parsing, and
  sanitized errors rather than opening ad hoc sessions in feature modules.
- `KillFeedModule` owns the shared HLL RCON client, per-guild serialization, and
  admin-log polling. Execute commands through its RCON helper and register a log
  consumer instead of starting another client or poller. Database ingestion, VIP
  tracking, and admin alerts already consume this stream.
- Keep RCON network operations serialized per guild. Snapshot player state once
  for a batch, deduplicate by EOS ID, and retain request pacing.
- The shared `/hllvn` group is declared in `module/hll_group.py`. Import and add
  commands to that group; do not construct competing groups with the same name.
- Optional integrations (`hllrcon`, PostgreSQL, Pillow) must fail locally and
  clearly without preventing unrelated BattleMetric features from loading.
- The BattleMetrics/RCON ban path intentionally targets both systems and reports
  each result independently. Preserve partial-success reporting: failure at one
  destination must not falsely report that neither or both succeeded.

### Durable data and ownership invariants

- PostgreSQL account links and stats use the schema assumptions documented in
  `BattleMetric/sql/hll_constraints.sql`: EOS is the player identity, one Discord
  account maps to one EOS ID, and the foreign key points from the Discord mapping
  to the RCON stats row. Do not silently merge conflicting identities.
- Link tokens are short-lived and verified from the shared RCON stream. Stats are
  queued and batch-written. Preserve queue bounds, retry behavior, and idempotent
  event deduplication.
- VIP records distinguish access created by this bot from externally managed VIP
  access. Expiry and purge code must never remove an external VIP. Destructive
  bulk cleanup remains preview/dry-run by default and requires explicit confirm.
- A VIP purchase must deduct kills only as part of a transaction that succeeds
  with the RCON grant; do not charge a player for a failed grant.
- Server-info refreshes fetch each unique server once per cycle and retain the
  last good embed when BattleMetrics is temporarily unavailable.
- Admin alerts use the shared log stream, bounded queues, deduplication, rate
  limiting, and tightly scoped mentions.
- Dog-tag review uses per-user locking, stale-review detection, hash validation,
  path validation, and atomic moves. IPC should remain local by default, and its
  secret stays in the shared-token vault.
- `seeding_rules.py` is deliberately dependency-free policy logic. Keep pure
  decisions there and orchestration/Discord/RCON effects in `seeding.py`.

## HaruAdmins invariants

Discord accepts a timeout of at most 28 days, while this cog accepts validated
durations up to ten years. Long timeouts are represented as a true final
`expires_at` plus the currently expected `segment_until`; the worker renews only
the Discord-sized segment.

Do not simplify this into one timestamp. Before renewal, fetch fresh member state
and compare it with the expected segment. A moderator's manual timeout change or
removal is authoritative and must cancel bot management. The `_expected_updates`
map prevents the cog's own gateway events from being mistaken for overrides.

Keep the hierarchy safeguards: no self-timeout, bots, guild owner, administrators,
or targets at/above the moderator or bot role. Continue using guild locks, audit
reasons, permission checks for both actor and bot, worker cleanup, and the
`red_delete_data_for_user` implementation.

## VoiceChannelHandling invariants

Red `Config` is authoritative. Per-guild JSON files in the cog data directory are
atomic snapshots for diagnostics/external inspection, not a second database and
not recovery input unless a future migration explicitly defines that behavior.

Channel creation, reuse, ownership, deletion, and panel state are concurrent.
Keep guild locks around state transitions. Empty-channel deletion must be delayed,
cancelled on rejoin, and rechecked immediately before deletion; bots do not keep a
room alive. Joining the creator room should reuse a user's tracked existing room
before creating another one, and creation cooldowns prevent churn.

The owner mapping in Config is the authority for owner-only controls; Discord
overwrites are the enforcement mechanism. A room may be claimed only when the
tracked owner is absent. Lock/hide edits should affect `@everyone`; unlock/unhide
must clear the overwrite to restore category inheritance rather than force an
allow. Kick excludes the owner, caps select options at Discord's limit, denies
reconnect, and disconnects the selected member.

Passive voice events mark panels dirty. The refresh worker coalesces edits; do not
edit panel messages directly for every event. Dashboard operations require the
interaction to be in the appropriate temporary channel context and must degrade
cleanly if voice-channel chat is unavailable.

## Safe change workflow

1. Run `git status --short` and inspect relevant diffs. Existing modifications
   belong to the user unless proven otherwise.
2. Read the cog README, `info.json`, loader, main class, and all modules involved
   in the requested behavior. Trace lifecycle, configuration, permissions, data
   deletion, and error paths, not only the command callback.
3. Preserve the user's exact command contract: command/group name, argument
   names, optional/default semantics, visibility, and authorization model.
4. Reuse shared services and pure helpers. Add a lock when a read-modify-write or
   external command can race; do not hold a lock across unrelated slow work.
5. Handle partial external failure explicitly. State what succeeded and failed,
   make retries safe, and avoid rollback claims when an API cannot provide them.
6. Update the cog README with setup, commands, permissions, storage, failure
   behavior, and the reason for non-obvious design decisions. Update the root
   README when the repository-level feature summary changes.
7. Update versions/manifests/privacy statements when behavior, dependencies, or
   stored/transmitted data changes.

## Validation and handoff

Run regressions with `python -m unittest discover -s tests -v`. Territory-protection
tests are dependency-free; TK-watch, map-command, and admin-reply tests need `discord.py` and
mock Red and RCON (they are skipped if Discord is unavailable). The map library
contract check also needs `hllrcon`. There is no CI
configuration, so validation must also be explicit and proportional to the change.

- Parse every Python file with `ast.parse` for a dependency-free syntax check.
  Prefer this over `py_compile` when you do not want to create `__pycache__`.
- Parse every `info.json` as JSON and run `git diff --check`.
- Exercise pure policy helpers, especially `seeding_rules.py`, with focused input
  cases. For network code, mock Discord, BattleMetrics, PostgreSQL, and RCON at
  their shared boundaries rather than adding parallel clients.
- Inspect the final diff and confirm that no token, password, connection string,
  generated data, or local environment file was added.
- Local Python may not have Redbot/Discord dependencies. Import and runtime tests
  must use the same Python environment as the running Red instance. Report clearly
  when validation stopped at static checks.
- After application-command changes, run `[p]reload <CogName>`,
  `[p]slash enablecog <CogName>` when needed, and `[p]slash sync` in the
  deployment environment. Then test both authorized and unauthorized paths,
  reload persistence, worker startup/shutdown, and a representative failure path.

When handing work back, summarize the behavior changed, files changed, checks
run, checks that require the live Red environment, and any operational reload,
sync, migration, or credential step the operator still needs to perform.
