"""Discord-native AI administration dashboard.

All AI requests are routed through the bot's existing authorization, rate-limit,
provider, typed-tool, audit and approval pipeline. Configuration controls are
restricted to server administrators.
"""
from __future__ import annotations

import os
from typing import Optional

import aiosqlite
import discord
from discord import app_commands


def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return bool(
        interaction.guild
        and (
            getattr(user, "id", None) == interaction.guild.owner_id
            or (isinstance(user, discord.Member) and user.guild_permissions.administrator)
        )
    )


async def _is_controller(bot, interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    context = bot.ai_context_factory(interaction.guild, interaction.user, channel=interaction.channel)
    return bool(await bot.ai_is_controller(context))


def _dashboard_embed(guild: discord.Guild, page: str = "overview") -> discord.Embed:
    page = page if page in {"overview", "ai", "security", "operations", "logs"} else "overview"
    common = (
        f"**Server:** {guild.name}\n"
        f"**Members:** {guild.member_count or len(guild.members)}\n"
        f"**Channels:** {len(guild.channels)} · **Roles:** {max(0, len(guild.roles) - 1)}"
    )
    pages = {
        "overview": (
            "DragonSlayerBot · Control Center",
            common + "\n\nChoose a section below. AI actions use the existing permission and approval checks.",
        ),
        "ai": (
            "AI Control",
            "• Ask AI to inspect or manage the server\n"
            "• AI requests are rate-limited\n"
            "• Destructive actions still require private approval\n"
            "• Controller and approval roles are configured below",
        ),
        "security": (
            "Security",
            "• Run read-only server diagnostics\n"
            "• Inspect anti-raid settings and recent incidents\n"
            "• Review privileged roles and bot permissions\n"
            "No automatic punishment is triggered from this dashboard.",
        ),
        "operations": (
            "Operations",
            "• Inspect open tickets and tracked temporary voice rooms\n"
            "• Review Discord scheduled events\n"
            "• Use the AI assistant for supported operations",
        ),
        "logs": (
            "Logs & Audit",
            "• AI action history is stored in the bot database\n"
            "• Discord Audit Log is separate from Railway runtime logs\n"
            "• Configure the AI audit destination with /set-ai-log-channel",
        ),
    }
    title, description = pages[page]
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.set_footer(text=f"DragonSlayerBot · {page.title()} · Admin controls require Administrator")
    return embed


class AIPromptModal(discord.ui.Modal, title="DragonSlayerBot AI"):
    prompt = discord.ui.TextInput(
        label="What should the AI do?",
        placeholder="Example: run server diagnostics and report missing permissions",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=2000,
        required=True,
    )

    def __init__(self, bot, channel):
        super().__init__(timeout=300)
        self.bot = bot
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _is_controller(self.bot, interaction):
            return await interaction.response.send_message(
                "You are not allowed to use the AI control panel.", ephemeral=True
            )
        allowed, retry_after = await self.bot.ai_consume_rate_limit(
            interaction.guild.id, interaction.user.id
        )
        if not allowed:
            return await interaction.response.send_message(
                f"AI rate limit reached. Try again in {retry_after} seconds.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        context = self.bot.ai_context_factory(
            interaction.guild,
            interaction.user,
            can_use_tools=True,
            channel=self.channel or interaction.channel,
            allow_memory_write=self.bot.ai_memory_write_intent(str(self.prompt.value)),
        )
        try:
            result = await self.bot.ai_run(context, str(self.prompt.value))
        except Exception as exc:
            result = f"AI request failed safely: {type(exc).__name__}."
        result = str(result or "The AI returned no response.")
        for offset in range(0, len(result), 1900):
            await interaction.followup.send(
                result[offset:offset + 1900],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class DashboardPageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Overview", value="overview", emoji="🏠", default=True),
            discord.SelectOption(label="AI Control", value="ai", emoji="🤖"),
            discord.SelectOption(label="Security", value="security", emoji="🛡️"),
            discord.SelectOption(label="Operations", value="operations", emoji="⚙️"),
            discord.SelectOption(label="Logs & Audit", value="logs", emoji="📜"),
        ]
        super().__init__(
            placeholder="Choose dashboard section",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dragon_dashboard:page",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_dashboard_embed(interaction.guild, self.values[0]),
            view=self.view,
        )


class ControllerRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="Set AI controller role (Admin only)",
            min_values=1,
            max_values=1,
            custom_id="dragon_dashboard:controller_role",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        role = self.values[0]
        if role.is_default() or role.managed:
            return await interaction.response.send_message("Choose a normal, unmanaged role.", ephemeral=True)
        async with aiosqlite.connect(self.view.bot.ai_database_path) as db:
            await db.execute(
                "INSERT INTO ai_controller_roles(guild_id,role_id) VALUES(?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id",
                (interaction.guild.id, role.id),
            )
            await db.commit()
        await interaction.response.send_message(
            f"AI controller role set to {role.mention}.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ApprovalRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="Set AI approval role (Admin only)",
            min_values=1,
            max_values=1,
            custom_id="dragon_dashboard:approval_role",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        role = self.values[0]
        if role.is_default() or role.managed:
            return await interaction.response.send_message("Choose a normal, unmanaged role.", ephemeral=True)
        async with aiosqlite.connect(self.view.bot.ai_database_path) as db:
            await db.execute(
                "INSERT INTO ai_approval_roles(guild_id,role_id) VALUES(?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id",
                (interaction.guild.id, role.id),
            )
            await db.commit()
        await interaction.response.send_message(
            f"AI approval role set to {role.mention}. The guild owner and bot owner remain approvers.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class PanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Set dashboard channel (Admin only)",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            custom_id="dragon_dashboard:panel_channel",
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        channel = self.values[0]
        me = interaction.guild.me
        perms = channel.permissions_for(me) if me else None
        if not perms or not perms.send_messages or not perms.embed_links:
            return await interaction.response.send_message(
                "The bot needs Send Messages and Embed Links in that channel.", ephemeral=True
            )
        await self.view.bot.ai_set_setting(interaction.guild.id, "ai_panel_channel_id", channel.id)
        await interaction.response.send_message(
            f"Dashboard channel set to {channel.mention}. Use /publish-ai-dashboard to post the panel.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DashboardActionButton(discord.ui.Button):
    def __init__(self, bot, action: str, label: str, emoji: str, row: int = 4):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"dragon_dashboard:{action}",
            row=row,
        )
        self.bot = bot
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _is_controller(self.bot, interaction):
            return await interaction.response.send_message(
                "Only the configured AI controller or a server administrator can use this action.",
                ephemeral=True,
            )
        if self.action == "ask":
            return await interaction.response.send_modal(AIPromptModal(self.bot, interaction.channel))
        if self.action == "refresh":
            return await interaction.response.edit_message(
                embed=_dashboard_embed(interaction.guild),
                view=self.view,
            )
        if self.action == "diagnostics":
            prompt = "Run read-only server diagnostics. Report missing bot permissions, role hierarchy issues, channel configuration problems, and security configuration. Do not change anything."
        elif self.action == "security":
            prompt = "Inspect the current anti-raid and security status, including recent incidents and privileged roles. Read-only; do not change anything."
        else:
            prompt = "Show the latest 10 AI action audit log entries for this server."
        allowed, retry_after = await self.bot.ai_consume_rate_limit(
            interaction.guild.id, interaction.user.id
        )
        if not allowed:
            return await interaction.response.send_message(
                f"AI rate limit reached. Try again in {retry_after} seconds.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        context = self.bot.ai_context_factory(
            interaction.guild, interaction.user, can_use_tools=True, channel=interaction.channel
        )
        try:
            result = await self.bot.ai_run(context, prompt)
        except Exception as exc:
            result = f"Dashboard action failed safely: {type(exc).__name__}."
        await interaction.followup.send(
            str(result)[:1900],
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DashboardView(discord.ui.View):
    def __init__(self, bot, timeout: Optional[float] = 900):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.add_item(DashboardPageSelect())
        self.add_item(ControllerRoleSelect())
        self.add_item(ApprovalRoleSelect())
        self.add_item(PanelChannelSelect())
        self.add_item(DashboardActionButton(bot, "ask", "Ask AI", "🤖"))
        self.add_item(DashboardActionButton(bot, "diagnostics", "Diagnostics", "🩺"))
        self.add_item(DashboardActionButton(bot, "security", "Security", "🛡️"))
        self.add_item(DashboardActionButton(bot, "audit", "AI Audit", "📜"))
        self.add_item(DashboardActionButton(bot, "refresh", "Refresh", "🔄", row=4))


async def setup(bot) -> None:
    @bot.tree.command(
        name="ai-dashboard",
        description="Open the Discord-native AI administration dashboard",
    )
    async def ai_dashboard(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        if not await _is_controller(bot, interaction):
            return await interaction.response.send_message(
                "Only the configured AI controller or a server administrator can open this dashboard.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=_dashboard_embed(interaction.guild),
            view=DashboardView(bot),
            ephemeral=True,
        )

    @bot.tree.command(
        name="publish-ai-dashboard",
        description="Publish the AI administration dashboard in its configured channel",
    )
    async def publish_ai_dashboard(interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        channel_id = await bot.ai_raw_setting(interaction.guild.id, "ai_panel_channel_id")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id and str(channel_id).isdigit() else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Set a valid dashboard channel first using the dashboard channel selector or /set-ai-panel-channel.",
                ephemeral=True,
            )
        me = interaction.guild.me
        perms = channel.permissions_for(me) if me else None
        if not perms or not perms.send_messages or not perms.embed_links:
            return await interaction.response.send_message(
                "The bot needs Send Messages and Embed Links in the configured dashboard channel.",
                ephemeral=True,
            )
        message = await channel.send(
            embed=_dashboard_embed(interaction.guild),
            view=DashboardView(bot, timeout=None),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        bot.add_view(DashboardView(bot, timeout=None), message_id=message.id)
        await interaction.response.send_message(
            f"Dashboard published in {channel.mention}.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def teardown(bot) -> None:
    bot.tree.remove_command("ai-dashboard")
    bot.tree.remove_command("publish-ai-dashboard")
