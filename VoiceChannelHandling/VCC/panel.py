import discord


class VCHNameModal(discord.ui.Modal, title="Change voice channel name"):
    def __init__(self, cog, channel_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel_id = channel_id
        self.new_name = discord.ui.TextInput(
            label="New channel name",
            placeholder="Enter a new voice channel name",
            min_length=1,
            max_length=100,
            required=True,
        )
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_change_name(interaction, self.channel_id, str(self.new_name.value))


class VCHLimitModal(discord.ui.Modal, title="Change user limit"):
    def __init__(self, cog, channel_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel_id = channel_id
        self.limit = discord.ui.TextInput(
            label="User limit",
            placeholder="0 for unlimited, 1-99 for a limit",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_change_limit(interaction, self.channel_id, str(self.limit.value))


class VCHKickSelect(discord.ui.Select):
    def __init__(self, cog, channel: discord.VoiceChannel, owner: discord.Member):
        self.cog = cog
        self.channel_id = channel.id

        options = []
        for member in channel.members:
            if member.bot or member.id == owner.id:
                continue

            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=str(member)[:100],
                )
            )

            if len(options) >= 25:
                break

        super().__init__(
            placeholder="Select a member to kick",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="vch_panel:kick_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member_id = int(self.values[0])
        await self.cog.panel_kick_member(interaction, self.channel_id, member_id)


class VCHKickSelectView(discord.ui.View):
    def __init__(self, cog, channel: discord.VoiceChannel, owner: discord.Member):
        super().__init__(timeout=60)
        self.add_item(VCHKickSelect(cog, channel, owner))


class VCHControlPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Name",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:name",
        row=0,
    )
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCHNameModal(self.cog, interaction.channel_id or 0))

    @discord.ui.button(
        label="Lock",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:lock",
        row=0,
    )
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_lock(interaction)

    @discord.ui.button(
        label="Unlock",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:unlock",
        row=0,
    )
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_unlock(interaction)

    @discord.ui.button(
        label="Limit",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:limit",
        row=0,
    )
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCHLimitModal(self.cog, interaction.channel_id or 0))

    @discord.ui.button(
        label="Kick",
        style=discord.ButtonStyle.danger,
        custom_id="vch_panel:kick",
        row=1,
    )
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_show_kick_select(interaction)

    @discord.ui.button(
        label="Hide",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:hide",
        row=1,
    )
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_hide(interaction)

    @discord.ui.button(
        label="Unhide",
        style=discord.ButtonStyle.secondary,
        custom_id="vch_panel:unhide",
        row=1,
    )
    async def unhide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_unhide(interaction)

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.primary,
        custom_id="vch_panel:claim",
        row=1,
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.panel_claim(interaction)
