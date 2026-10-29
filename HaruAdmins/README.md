# HaruAdmins

HaruAdmins provides hybrid Discord moderation commands for Redbot. It supports
normal and extended timeouts plus automatic removal of unapproved bots.

## Install and load

```text
[p]cog install HarukoCog HaruAdmins
[p]load HaruAdmins
[p]slash enablecog HaruAdmins
[p]slash sync
```

The bot needs the **Moderate Members** permission and its highest role must be
above the member being timed out. The command is available to Red moderators
and members with **Moderate Members**. Discord does not allow bots, the server
owner, or members with **Administrator** to be timed out.

## Timeout command

Use the slash command:

```text
/timeout member:@member duration:30d reason:Repeated rule violations
```

Or use the prefix version:

```text
[p]timeout @member 30d Repeated rule violations
```

The reason is optional. Durations accept `s`, `m`, `h`, `d`, and `w`, which can
be combined (`30m`, `2d`, `6w`, or `30d12h`). The minimum is one second and the
maximum is ten years.

Discord accepts a timeout ending no more than 28 days in the future. For a
longer duration, HaruAdmins applies the first 28-day segment and stores the real
expiration. A background worker renews the timeout shortly before each segment
ends. The stored schedule survives cog reloads and bot restarts.

If a moderator manually removes or changes a managed timeout in Discord,
HaruAdmins treats that edit as intentional and stops renewing it. Leaving the
server or reaching the final expiration also removes the stored schedule.

## Bot protection

Configure the role to ping and the text channel where alerts should be sent:

```text
/botprotection notify_role:@Server Admin channel:#admin-alerts toggle:true
```

`toggle` defaults to `true`. The bot needs **Kick Members**, **View Channel**,
and **Send Messages**. The notification role must be mentionable unless the bot
can mention all roles in the alert channel. **View Audit Log** is optional, but
without it the alert says `Someone` instead of identifying who added the bot.
The bot's **Server Members Intent** must also be enabled in Discord's Developer
Portal so Red receives member-join events.

While enabled, human joins are ignored. A joining bot is allowed only when its
Discord ID is in the persistent whitelist. All other bots are kicked
immediately, then the configured role is notified with the bot's name, ID,
join timestamp, and the inviter when Discord's audit log makes that available.

Whitelist a kicked bot, then invite it again:

```text
/botprotection allow:123456789012345678
```

Only members who currently have the configured `notify_role` can use the
`allow` option. This restriction also applies to server administrators: an
administrator without that role cannot whitelist a bot. Configuration changes
still require the **Manage Server** permission.

Disable protection without deleting its configuration or whitelist:

```text
/botprotection toggle:false
```

The bot-protection command and listeners are kept in
`modules/bot_protection.py` instead of being added to the main cog file.

## Honeypot

The Discord server owner or a configured Red bot owner can turn a text channel
into a honeypot. Choose whether a member who posts there is kicked immediately
or timed out for seven days and then kicked:

```text
/honeypot trap channel:#do-not-use action:Kick notify_role:@Security
/honeypot trap channel:#do-not-use action:timeout notify_role:@Security notify_channel:#security-alerts
```

`notify_channel` is optional and defaults to the trap channel. Members with the
configured `notify_role` are exempt. Bot messages are also ignored so an alert
sent into the trap channel cannot trigger the trap again.

For the timeout action, the final kick deadline is stored in Red Config and
survives cog reloads and bot restarts. If the timeout was a mistake, the server
owner or Red bot owner can cancel both the timeout and scheduled kick:

```text
/honeypot release suspect:@member
```

The bot needs **View Channel** in the trap channel, **View Channel** and **Send
Messages** in the notification channel, and **Kick Members**. The timeout action
also requires **Moderate Members**. The notification role must be mentionable
unless the bot can mention all roles in the notification channel.

The honeypot command, listener, and expiration worker are isolated in
`modules/honeypot.py`.
