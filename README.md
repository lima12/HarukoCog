# HarukoCog

Redbot cog repository containing BattleMetric and VoiceChannelHandling.

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

Install and load either cog:

```text
[p]cog install HarukoCog BattleMetric
[p]load BattleMetric
```

```text
[p]cog install HarukoCog VoiceChannelHandling
[p]load VoiceChannelHandling
```

To install both at once:

```text
[p]cog install HarukoCog BattleMetric VoiceChannelHandling
[p]load BattleMetric
[p]load VoiceChannelHandling
```

Use `[p]cog list HarukoCog` to see the cogs available from this repository.

## BattleMetric

BattleMetric reads server data from the BattleMetrics API, maintains a Server
Info embed, publishes a pooled HLL: Vietnam kill feed over direct RCON, and can
store verified Discord/EOS links and batched player statistics in PostgreSQL.

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
[p]pipinstall "asyncpg>=0.30,<0.32" "pydantic>=2.11.5,<2.12" "hllrcon>=2.0.0.4,<2.0.1"
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
or Team chat. See the cog README for database constraints and ingestion
behavior.

See [BattleMetric/README.md](BattleMetric/README.md) for all commands and
authorization details.

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
`VoiceChannelHandling` instead when testing that cog.

## Updating

Update the repository and installed cogs with:

```text
[p]repo update HarukoCog
[p]cog update HarukoCog
```
