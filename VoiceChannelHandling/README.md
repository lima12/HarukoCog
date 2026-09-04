# VoiceChannelHandling

VoiceChannelHandling is a Redbot cog for temporary Discord voice channels.

Users join a configured creator voice channel. The cog creates a private-ish temporary voice room for that user, moves them into it, tracks ownership, posts a dashboard panel into the voice channel chat, and deletes the room after it becomes empty.

The cog is designed around two goals:

- Make temporary voice rooms self-service for normal users.
- Keep server staff in control without making every channel action require a moderator command.

## Requirements

This cog expects:

- Red-DiscordBot running on a Discord library version with `discord.ui` support.
- Bot permission to manage channels.
- Bot permission to move members.
- Bot permission to send messages in voice channel text chat, if you want the dashboard panel to appear inside the voice room.
- Slash or hybrid commands synced in your Redbot instance.

The dashboard panel uses Discord components:

- Buttons for actions like lock, unlock, hide, unhide, kick, and claim.
- Modals for actions that need user input, like channel name and user limit.
- Select menus for choosing a member to kick.

Because those features are Discord interaction features, older Discord.py builds that do not support `discord.ui.Modal`, persistent `View`s, or voice channel chat messages may not show all functionality.

## Basic Setup

Load the cog in Redbot, then run:

```text
/setupvch creator_room:<voice channel> delete_delay:<seconds> category:<optional category> name_template:<optional template>
```

Example:

```text
/setupvch creator_room:"Join to Create" delete_delay:10 category:"Temporary Voice" name_template:"{user}'s room"
```

After setup:

1. A user joins the creator room.
2. The cog creates a temporary voice channel.
3. The user is moved into that new channel.
4. The user is recorded as the owner.
5. A control dashboard is posted into the voice channel chat.
6. When the channel has no human members, the cog deletes it after the configured delay.

## Main Commands

### `/setupvch`

Configures the temporary voice system.

Parameters:

- `creator_room`: The voice channel users join to create a temporary room.
- `delete_delay`: How many seconds to wait before deleting an empty temporary room. The minimum is 3 seconds.
- `category`: Optional category where temporary rooms are created. If omitted, the creator room's category is used.
- `name_template`: Optional channel name template.

Supported name template placeholders:

- `{user}`: The member display name.
- `{id}`: The member ID.
- `{tag}`: The member's Discord string form.
- `{counter}`: A server-local incrementing counter.

Why this command requires admin/manage-guild access:

The setup command changes server-level behavior. It controls which voice channel creates rooms, where those rooms are created, and how long empty rooms stay alive. That is server configuration, not a normal user action.

### `/vch`

Shows current server settings.

Subcommands:

- `/vch setcreator`
- `/vch setname`
- `/vch setdelay`
- `/vch setcategory`

These are administrative configuration commands.

### `/voicechannelhandling`

Owner command group for users who own a temporary room.

Existing subcommands include:

- `/voicechannelhandling transfer <user>`
- `/voicechannelhandling limit <number>`
- `/voicechannelhandling invite [public]`
- `/voicechannelhandling rename <new name>`

These are kept for users who prefer commands or when the dashboard message is missing.

### `/voicedashboard`

Reposts the dashboard panel for the current voice room.

Rules:

- The command only works in a managed temporary voice channel.
- The user must be connected to that same voice channel.
- The command must be run from that voice channel's text chat.
- The user must be either the current room owner or a server administrator.

Why it works this way:

The dashboard controls a specific voice room. If the command could be run from any text channel, it would be easier to accidentally post a room control panel in the wrong place. Requiring the command to be run from the voice channel chat keeps the dashboard next to the room it controls.

Admins are allowed because they are server staff. Room owners are allowed because the room belongs to them. Other users are blocked because reposting management panels is a control action, not a general room action.

## Dashboard Panel

When a temporary room is created, the cog posts a dashboard embed with buttons.

The embed shows:

- Name: Current voice channel name.
- Owner: Current tracked room owner.
- Status: Lock/unlock state and hidden/visible state.
- Created: A Discord relative timestamp based on the channel creation time.
- People: Current human member count and the room user limit.

The created time uses Discord's timestamp formatting, such as:

```text
<t:1710000000:R>
```

Why relative timestamps are used:

Discord renders and updates relative timestamps on the client side. That means the bot does not need a background task just to keep text like "3 minutes ago" updated. This avoids unnecessary API calls and keeps the embed simple.

## Dashboard Controls

### Name

Opens a modal asking for the new voice channel name.

The name is trimmed, line breaks are removed, and the result is limited to Discord's 100-character channel name limit.

Why a modal is used:

A channel name is free-form text. Buttons and select menus are not good input methods for arbitrary text, so a modal is the correct Discord interaction pattern.

### Lock

Sets the temporary channel's `@everyone` overwrite so `connect` is denied.

The owner keeps explicit permission to view and connect.

Why lock is done with an overwrite:

Temporary rooms inherit permissions from their category or creator room. Changing the category would affect many channels. Changing only the temporary channel's overwrite keeps the lock local to that room and makes deletion clean.

### Unlock

Clears the temporary channel's `@everyone connect` overwrite.

Why unlock clears instead of setting `connect=True`:

Clearing the overwrite lets the channel go back to inherited permissions. Setting `connect=True` would force access even if the category or server permission model was supposed to deny it.

### Limit

Opens a modal asking for a user limit.

Rules:

- `0` means unlimited.
- Values below `0` are clamped to `0`.
- Values above `99` are clamped to `99`.

Why the limit is clamped:

Discord voice channels have practical limits, and allowing arbitrary values would only create API errors. Clamping makes the panel forgiving while still producing a valid channel edit.

### Kick

Opens an ephemeral select menu listing current non-bot members in the same room, excluding the owner.

After selecting a member:

1. The cog sets a member-specific overwrite with `connect=False`.
2. The cog disconnects the member from the voice channel.

Why the reconnect block is included:

If a kicked user can immediately rejoin, kick is not very useful. The member-specific overwrite prevents reconnecting to that same temporary room until the room is deleted or permissions are manually changed.

Why the select menu is ephemeral:

Kicking a member is a moderation-like room action. The selection UI only needs to be visible to the person performing the action, and keeping it ephemeral avoids cluttering the voice channel chat.

### Hide

Sets the temporary channel's `@everyone` overwrite so `view_channel` is denied.

The owner keeps explicit permission to view and connect.

Why hide does not delete or move the room:

Hide is meant to make the room invisible to general users, not end the room. The owner and current permission exceptions remain able to use it.

### Unhide

Clears the temporary channel's `@everyone view_channel` overwrite.

Why unhide clears instead of setting `view_channel=True`:

Clearing restores inherited permissions. This respects category permissions and avoids accidentally making a private category visible.

### Claim

Lets a member claim the room if the previous owner is no longer in that voice channel.

When successful:

1. The old owner mapping is removed.
2. The old owner's channel overwrite is removed when possible.
3. The claimant gets owner permissions on the room.
4. The config owner mapping is updated.
5. The dashboard embed is refreshed.

Why claim exists:

Temporary rooms can outlive the original owner if other users stay inside. Without claim, nobody in the room may be able to manage it after the owner leaves. Claim keeps the room usable without needing staff intervention.

Why claim is blocked while the owner is still present:

Ownership should not be stolen from someone actively using their room. Claim is only a recovery path for abandoned rooms.

## Permissions Model

The cog tracks ownership in config, not only through Discord permissions.

A room owner receives overwrites including:

- `view_channel=True`
- `connect=True`
- `speak=True`
- `manage_channels=True`
- `move_members=True`
- `mute_members=True`
- `deafen_members=True`

The dashboard checks the config owner mapping before allowing owner-only actions.

Why config ownership is used:

Discord permissions alone do not tell the bot why someone has access. A user could have `manage_channels` from a role, from an overwrite, or from server permissions. The config mapping gives the cog a clear answer to "who owns this temporary room?"

Why the owner also gets Discord permissions:

The owner should be able to manage their room naturally in Discord, and some Discord actions require actual channel permissions. The config mapping handles bot-side authorization; the Discord overwrites make the user experience match the ownership model.

Server administrators can use `/voicedashboard`, but dashboard button actions are owner-only except `Claim`.

Why button actions are owner-only:

The dashboard is primarily a user self-service tool. Admins still have normal Discord moderation powers and can use owner commands or direct channel management. Keeping the panel owner-focused reduces accidental staff changes to user rooms.

## Temporary Channel Lifecycle

### Creation

When a non-bot member joins the configured creator room:

1. The cog checks whether they already have a tracked temporary channel.
2. If the channel still exists, the user is moved back into it.
3. If not, the cog creates a new voice channel.
4. The cog copies overwrites from the creator room.
5. The cog adds owner overwrites for the user.
6. The cog moves the user into the room.
7. The cog posts the dashboard panel.

Why existing rooms are reused:

Without reuse, a user could repeatedly join the creator room and create many abandoned rooms. Reuse keeps one active room per owner and reduces clutter.

### Empty Channel Deletion

When a human member leaves a tracked temporary room, the cog checks whether any non-bot members remain.

If no human members remain:

1. A deletion task is scheduled.
2. The task waits for the configured delete delay.
3. The channel is checked again.
4. If humans are still absent, the channel is deleted.
5. Tracking data is cleaned up.

Why bots do not keep rooms alive:

Music bots and utility bots can remain in voice after users leave. If bots counted as real occupants, temporary rooms could stay forever. The cog intentionally checks for human members.

Why deletion is delayed:

Discord voice reconnects, accidental disconnects, and quick channel switches happen often. A small delay avoids deleting a room immediately when someone drops for a moment.

### Delete Task Cancellation

If a human member rejoins a tracked room before the delete delay ends, the pending deletion task is cancelled.

Why cancellation is needed:

The scheduled delete task is based on the room being empty at the time it was scheduled. If people come back, the original reason for deletion is no longer true.

## Stored Data

The cog uses Red Config per guild.

Stored fields:

- `creation_channel_id`: The join-to-create voice channel.
- `name_template`: The template for new room names.
- `delete_delay`: Empty-room deletion delay.
- `temp_category_id`: Category for new temporary rooms.
- `temp_channels`: IDs of tracked temporary rooms.
- `counter`: Incrementing counter used by `{counter}` in names.
- `owner_channels`: Mapping of owner user ID to temporary channel ID.
- `control_panels`: Mapping of temporary channel ID to dashboard message ID.

The cog also writes a per-guild JSON snapshot in the cog data folder.

Why Red Config is the source of truth:

Red Config is the normal persistence layer for Redbot cogs. It survives restarts and avoids inventing a separate database system.

Why a JSON snapshot also exists:

The JSON file is useful for inspection, debugging, or external tooling. It mirrors the important guild state in a human-readable format.

Why panel message IDs are stored:

The cog can refresh the same dashboard message after status changes instead of posting a new panel every time a member joins, leaves, renames, locks, or changes the limit.

Why `/voicedashboard` can post a new panel:

Users may delete the original message, Discord may fail to render old components after a restart, or the panel may simply be buried in chat. `/voicedashboard` gives the owner or an admin a clean way to bring it back.

## File Layout

```text
VoiceChannelHandling/
  __init__.py
  voicechannelhandling.py
  VCC/
    __init__.py
    commands_mixin.py
    VCOwnerCommand.py
    panel.py
```

### `voicechannelhandling.py`

Main cog file.

Contains:

- Red Config registration.
- JSON snapshot helpers.
- Setup command.
- `/voicedashboard`.
- Voice state listener.
- Temporary room creation and deletion.
- Dashboard action handlers.
- Permission overwrite helpers.

### `VCC/commands_mixin.py`

Administrative configuration commands under `/vch`.

This is separated from the main cog so configuration commands do not make the main lifecycle file harder to read.

### `VCC/VCOwnerCommand.py`

Owner command group under `/voicechannelhandling`.

These commands provide command-based access to common room-owner actions.

### `VCC/panel.py`

Discord UI definitions for the dashboard.

Contains:

- Name modal.
- Limit modal.
- Kick select menu.
- Persistent dashboard button view.

Why UI classes live separately:

Discord UI code is mostly component definitions and callbacks. Keeping it separate makes the main cog focus on behavior and state changes, while `panel.py` focuses on Discord interaction layout.

## Notes and Limitations

### Voice channel chat support

The dashboard is sent into the voice channel chat by calling `send` on the voice channel object.

If the installed Discord library does not expose voice channel chat as messageable, the cog logs a warning and cannot post the panel.

### Component persistence

The dashboard view is registered as a persistent view when the cog loads.

Why persistent views are used:

Discord button interactions can arrive after the original Python object is gone, such as after a bot restart. Persistent views let the bot route button clicks by stable `custom_id` values.

### Select menu limit

Discord select menus can show at most 25 options.

The kick menu lists up to 25 eligible non-bot users currently in the room.

### Existing rooms after updates

Rooms created before the dashboard feature may not already have a panel message.

The cog handles this by posting or refreshing a panel when:

- A new room is created.
- A user is moved back into an existing room through the creator channel.
- An authorized user runs `/voicedashboard`.

## Troubleshooting

### The room is created, but no dashboard appears

Check:

- The bot can send messages in voice channel chat.
- The Discord library supports voice channel chat messages.
- The bot can view the temporary voice channel.
- The bot has not been denied send-message permissions by the category or channel.

### Lock or hide does not behave as expected

Check category and role permissions.

Lock and hide work by changing `@everyone` overwrites on the temporary channel. Other roles or member-specific overwrites may still grant access.

This is intentional because the cog should not erase the server's permission design.

### A kicked user can still join

Check whether the user has a role or permission path that overrides the member-specific deny.

Discord deny overwrites usually take priority over allows at the same channel level, but administrators can bypass normal channel restrictions.

### `/voicedashboard` says it must be used in the voice channel chat

Run the command from inside the temporary voice channel's chat, not from a normal text channel.

This avoids posting a control panel in the wrong place.

### A room does not delete

Check:

- Whether a human member is still in the room.
- Whether the delete delay has elapsed.
- Whether the bot has permission to manage/delete the channel.
- Whether the room is listed in `temp_channels`.

Bots alone should not keep the room alive.

## Design Summary

The cog uses a conservative Discord permission model:

- It edits only the temporary room, not global roles or categories.
- It clears overwrites when unlocking or unhiding, instead of forcing allows.
- It stores ownership separately from permissions so bot authorization is deterministic.
- It uses relative Discord timestamps so time display is handled by Discord clients.
- It uses modals and select menus where Discord's UI model fits the action.
- It keeps commands available as a fallback for dashboard actions.

That design keeps temporary rooms flexible while minimizing permanent server changes.
