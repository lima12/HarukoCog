# HaruAdmins

HaruAdmins provides a hybrid Discord timeout command for Redbot. It supports
normal Discord timeouts and durations longer than Discord's 28-day maximum.

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
