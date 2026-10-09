# HarukoCog

Redbot cog repository containing BattleMetric, HaruAdmins, and
VoiceChannelHandling.

Replace `[p]` in every command below with your bot's command prefix. Repository
and cog installation commands must be run by a bot owner.

## Install From GitHub

Load Red's Downloader cog if it is not already loaded:

```text
[p]load downloader
```

Add this repository once:

```text
[p]repo add HarukoCog https://github.com/lima12/HarukoCog.git main
```

Install and load any cog:

```text
[p]cog install HarukoCog BattleMetric
[p]load BattleMetric
```

```text
[p]cog install HarukoCog VoiceChannelHandling
[p]load VoiceChannelHandling
```

```text
[p]cog install HarukoCog HaruAdmins
[p]load HaruAdmins
```

To install all three at once:

```text
[p]cog install HarukoCog BattleMetric HaruAdmins VoiceChannelHandling
[p]load BattleMetric
[p]load HaruAdmins
[p]load VoiceChannelHandling
```

Use `[p]cog list HarukoCog` to see the cogs available from this repository.

## BattleMetric

BattleMetric reads server data from the BattleMetrics API, maintains a Server
Info embed, publishes a pooled HLL: Vietnam kill feed over direct RCON, monitors
team-kill thresholds for staff action, and can store verified Discord/EOS links
and batched player statistics in PostgreSQL. It also reviews custom dog-tag
carvings from the sibling `tagweb` service and composites approved tags onto
`/vnstat` cards.

After loading, configure the API token as the bot owner in a private channel or
DM:

```text
[p]set api battlemetrics api_key,YOUR_TOKEN
```

Then a server manager grants access to the members who should use the cog:

```text
[p]bm auth add @member
```

An authorized member can configure a default BattleMetrics server and create a
Server Info panel:

```text
[p]bm setserver SERVER_ID
[p]serverinfo setup #server-status
```

The kill feed requires `hllrcon`, which requires Python 3.11 or newer. Use
Python 3.11 with the current stable Red 3.5 release. Downloader installs the
pinned requirement when the cog is installed or updated. For a local cog
checkout where that dependency is missing, install it into Red's environment
and restart the bot:

```text
[p]load downloader
[p]pipinstall "asyncpg>=0.30,<0.32" "Pillow>=11,<13" "pydantic>=2.11.5,<2.12" "hllrcon>=2.0.0.4,<2.0.1"
```

Configure the RCON password in a private channel or DM, then set the HLL:
Vietnam server's RCON endpoint and feed channel:

```text
[p]set api hllrcon password,YOUR_RCON_PASSWORD
[p]killfeed configure RCON_HOST RCON_PORT
[p]killfeed setup #kill-feed
```

The RCON port may differ from the public game/query port. Kill events are
queued and pooled into at most one Discord embed every three seconds.

Authorized members can enable two-stage seeding protection for Warfare and
Offensive. Choose whether offenders receive a configurable 5-30 second warning
before they are killed or are punished immediately:

```text
/hllvn seeding stage_one_players:60 stage_two_players:75 penalty_type:"Warning, then punish" toggle:Enable warning_seconds:15
```

The rule checks player count and match status every 60 seconds, then polls
positions every three seconds only while protection is active. It enforces only
on Warfare and Offensive. On Warfare, the fourth and fifth sectors are locked
before Stage 1, only the fifth remains locked after Stage 1, and Stage 2 fully
unlocks the map. On Offensive, attackers can reach the second objective before
Stage 1, the third after Stage 1, and all objectives after Stage 2. Defenders
are not restricted. Use the same command with `Disable` to turn the feature off.
Falling below either threshold re-locks the matching sectors on the next status
check, including for established players already standing there. While Stage 2
is fully unlocked and HQ protection is inactive, one roster snapshot every 60
seconds keeps player readiness current without enforcing penalties. This does
not undo objectives already captured during the status-check delay.
Omitting `warning_seconds` keeps the saved duration, which defaults to five
seconds. Reload `BattleMetric` after updating; slash sync is only needed for
command changes, not this protection fix.
Seeding and HQ enforcement silently wait for a ten-second settling period and
a new position after joins, team changes, and observed deaths, so retained
spawn-screen coordinates do not immediately trigger a warning.

Authorized members can also protect both teams' locked HQ sectors from enemy
spawn killing:

```text
/hllvn hqprotection penalty_type:"Warning for 5 seconds, then punish" toggle:Enable
```

HQ protection shares the seeding worker and RCON polling. It runs on Warfare,
handles mirrored maps, and releases a team's HQ sector when the enemy controls
four objectives so the final point remains playable. Because RCON does not
expose individual HQ spawn coordinates, the complete locked home sector is
protected.

Configure PostgreSQL credentials in a private channel or DM, then reload the
cog. The remaining connection values below match the module defaults and can
be omitted when unchanged:

```text
[p]set api battlemetric_db user,DB_USER password,DB_PASSWORD host,162.120.6.39 port,5432 database,slhhll schema,slhhll
[p]reload BattleMetric
[p]slash enablecog BattleMetric
[p]slash sync
```

Members can then run `/link` and send the private token in the HLL server's Unit
or Team chat. `/vnstat` shows their linked statistics; its optional `member`
and `eos_id` fields support other-member and direct game-account lookups. See
the cog README for database constraints and ingestion behavior.

Authorized members can add a timed HLL VIP by linked Discord member or direct
EOS ID. The duration defaults to one day:

```text
/hllvn addvip member:@member duration:1d
/hllvn addvip eos_id:EOS_ID duration:1d
```

The cog persists the expiration and removes the VIP through the configured
RCON connection, retrying later if the server is unavailable.

Authorized members can also apply one moderation ban to both BattleMetrics and
the HLL: Vietnam server:

```text
/hllvn ban member:@member duration:2d reason:REASON
/hllvn ban eos_id:EOS_ID reason:REASON
```

The member form resolves the verified Discord/EOS link. The direct form accepts
a 17-digit or 32-character game account ID. Omitting `duration` creates a
permanent ban; temporary bans accept whole hours, days, or weeks from one hour
through 365 days. BattleMetrics and RCON are attempted independently, and the
private result identifies partial success so an administrator can repair only
the failed backend without silently undoing the successful ban.

The same authorized staff can remove the configured server's ban from both
backends by linked member or direct game account ID:

```text
/hllvn unban member:@member
/hllvn unban eos_id:EOS_ID
```

BattleMetrics removal is limited to exact identifier matches directly scoped to
the configured server, so shared organization or ban-list records are left
untouched. RCON clears either a temporary or permanent in-game ban. Each backend
is reported independently.

Authorized staff can also send an in-game popup to a linked mention, direct game
account ID, or every connected player:

```text
/hllvn mesg target:@member mesgs:MESSAGE
/hllvn mesg target:EOS_ID mesgs:MESSAGE
/hllvn mesg target:ALL mesgs:MESSAGE
```

The `target` option is text so it can accept all three forms. Linked mentions
must be members of the Discord server with a verified `/link`. These operations
reuse the shared serialized HLL RCON client.
The `/hllvn mesg` confirmation is public in the command channel and shows the
target and message for other administrators. Authorization denials stay private.

While `/hllvn adminping` is enabled, members authorized through `[p]bm auth` and
bot owners can also use Discord's **Reply** on this bot's original `HLLVN SOS`
alert. Their text is forwarded to the report's EOS ID as a private in-game popup,
without requiring the player to link Discord. Replies are limited to 1,000
characters and delivered at most once every three seconds per guild. The bot
posts a public delivery confirmation for other admins, without pinging anyone.
Only direct replies in the configured alert channel/thread are accepted; role
membership alone does not authorize forwarding. The bot needs Message Content
intent and Read Message History, alongside its normal send/embed permissions.
See the [Discord intent guide](https://discordpy.readthedocs.io/en/stable/intents.html#message-content).
Reload BattleMetric to enable the reply listener; no slash sync is needed.

Authorized members can request a map change using a map/mode from autocomplete
or an exact server map ID:

```text
/hllvn changemap map_name:Cam Ranh Port Warfare
```

The command validates against the live server map list and immediately submits
one shared RCON request. The game server's 60-second countdown still applies;
this does not guarantee a zero-delay transition. Autocomplete makes no RCON
calls, and the confirmation is private. Reload BattleMetric and run
`[p]slash sync` after updating to register the command.

Authorized members can reward everyone currently on the game server with timed
VIP and a private in-game thank-you popup:

```text
/hllvn giveseedvip duration:2d
```

The command extends existing bot-managed VIP time. External VIPs receive the
popup and temporary purge protection without changing their original VIP.
RCON requests are spaced two seconds apart, so a full server can take several
minutes.

Successfully rewarded players also receive the existing BattleMetrics `Seeder`
flag from the configured server's organization. Create that organization-owned
flag once in [BattleMetrics](https://learn.battlemetrics.com/article/51-how-can-i-create-or-edit-player-flags)
and give the existing vaulted API key identifier/flag access. Exact game IDs
are matched in batches, existing flags are preserved, and flag requests are
paced two seconds apart. The private result separates grant, popup, and flag
failures; BattleMetrics failures never undo VIP rewards. Seeder flags remain
after VIP expiry. See [Seeder flag setup](BattleMetric/README.md#battlemetrics-seeder-flag).
Reload BattleMetric to activate this addition; no slash sync is needed.

All linked members can also exchange their confirmed kills through the private
VIP purchase modal:

```text
/hllvn buyvip
```

Packages range from 100 kills for one day to 36,500 kills for 365 days. The
database deduction is atomic and only commits after the RCON VIP grant is
accepted.

Authorized members can preview or purge server VIPs that are not tracked by the
cog. VIPs created by `/hllvn addvip`, `/hllvn buyvip`, or
`/hllvn giveseedvip` remain protected, and removals are sent through the shared
RCON connection at one request every two seconds:

```text
/hllvn purgevip confirm:false
/hllvn purgevip confirm:true
```

Authorized members can also enable the VIP-only in-game team switch:

```text
/hllvn allowvipteamswap toggle:Enable
```

Players on the server then use `!changeteam` in Team or Unit chat. Current RCON
VIPs switch immediately, which kills a living soldier; non-VIPs receive a
private rejection. Repeated requests from the same player are silently ignored
for 90 seconds.

Authorized members can route in-game admin requests to a Discord role. Run the
enable command in the channel that should receive alerts:

```text
/hllvn adminping role:@HLL-Admin toggle:Enable
```

Players use `!admin` or `!admin message text` in Team or Unit chat. The bot pings
the configured role with an `HLLVN SOS` embed containing the player's name, EOS
ID, linked Discord account when available, and report text. Alerts are queued
and delivered at most once every three seconds per server.

Authorized members can also configure rolling team-kill alerts with staff action
buttons:

```text
/hllvn tkwatch toggle:Enable channel:#admin-alerts threshold_per_min:3 watch_duration:15 exclude_commander:true role:@Admins
```

Reaching the threshold within one rolling minute posts a 15-minute action card.
The player is warned automatically; staff can **Forgive**, **Warn & Watch** for
the configured 1-90 minutes, or **Kick**. With no staff decision after five
minutes, Warn & Watch starts automatically. Forgive can cancel the watch until
15 minutes after alert creation. A watched player's next team kill triggers an
automatic kick. Decisions and timeouts remove buttons rather than deleting the
alert embed; an active watch retains Forgive and Kick until the deadline. The
optional role is pinged once per new alert. Commander exclusion uses the live
in-game role; when disabled, Commander alerts are explicitly labeled. The
configuration command and every button are limited to users authorized through
`[p]bm auth` and bot owners. Reload the cog and run `[p]slash sync` after updating.

Failed TK warnings/defaults and staff Warn & Watch/Kick actions close the case
only if a fresh, successful RCON player list confirms the target is offline.
The embed is kept as `Closed - player disconnected before action`, with no
buttons or further retries for that case. RCON outages keep the case pending;
already-active watches retain their original expiry even if the player leaves.
Offline checks use the shared RCON connection at most once per guild every
three seconds. See the [TK disconnect handling](BattleMetric/README.md#players-who-disconnect-before-an-action)
for failure behavior. This change needs a cog reload, not a slash sync.

For the dog-tag carving site, configure the shared IPC secret and staff review
channel after deploying `tagweb`:

```text
[p]set api tagweb ipc_secret,SAME_RANDOM_SECRET_USED_BY_TAGWEB
[p]dogtag setup #dog-tag-review
[p]dogtag status
```

See the cog README and `tagweb/README.md` deployment bundle for storage,
Discord OAuth2, systemd, and Nginx configuration.

See [BattleMetric/README.md](BattleMetric/README.md) for all commands and
authorization details.

## HaruAdmins

HaruAdmins adds a hybrid timeout command for moderators. Durations longer than
Discord's 28-day maximum are saved and renewed automatically in 28-day
segments, including after a bot restart:

```text
/timeout member:@member duration:30d reason:Repeated rule violations
```

See [HaruAdmins/README.md](HaruAdmins/README.md) for duration formats,
permissions, and renewal behavior.

## VoiceChannelHandling

VoiceChannelHandling creates a temporary voice room when a member joins a
configured creator channel. Before setup, ensure the bot can manage channels,
move members, and send messages in voice-channel chat when dashboard panels are
wanted.

After loading, configure it with:

```text
/setupvch creator_room:<voice channel> delete_delay:<seconds> category:<optional category> name_template:<optional template>
```

Example:

```text
/setupvch creator_room:"Join to Create" delete_delay:10 category:"Temporary Voice" name_template:"{user}'s room"
```

See [VoiceChannelHandling/README.md](VoiceChannelHandling/README.md) for its
permissions model, dashboard controls, and troubleshooting.

## Local Development Install

When testing this checkout directly rather than installing from GitHub, add the
folder that contains the cog packages, then load the cog:

```text
[p]addpath "C:\\Users\\igiha\\Desktop\\discord bot\\slh\\redcog\\HarukoCog"
[p]load BattleMetric
```

Replace the path with the location of your local `HarukoCog` folder. Load
`HaruAdmins` or `VoiceChannelHandling` instead when testing those cogs.

## Updating

Update the repository and installed cogs with:

```text
[p]repo update HarukoCog
[p]cog update HarukoCog
```
