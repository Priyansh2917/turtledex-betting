from typing import TYPE_CHECKING, AsyncIterator, List, Set

import discord
from discord.ui import Button, View

from ballsdex.core.models import BallInstance
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.packages.balls.countryballs_paginator import CountryballsSource
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from .cog import BettingCog

class BetBulkSelector(Pages):
    def __init__(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        balls: List[int],
        cog: "BettingCog",
    ):
        self.bot = interaction.client
        self.interaction = interaction
        source = CountryballsSource(balls)
        super().__init__(source, interaction=interaction)
        self.add_item(self.select_ball_menu)
        self.add_item(self.confirm_button)
        self.add_item(self.select_all_button)
        self.add_item(self.clear_button)
        self.balls_selected: Set[BallInstance] = set()
        self.cog = cog

    async def set_options(self, balls: AsyncIterator[BallInstance]):
        options: List[discord.SelectOption] = []
        async for ball in balls:
            if ball.is_tradeable is False:
                continue
            emoji = self.bot.get_emoji(int(ball.countryball.emoji_id))
            favorite = f"{settings.favorited_collectible_emoji} " if ball.favorite else ""
            special = ball.special_emoji(self.bot, True)
            options.append(
                discord.SelectOption(
                    label=f"{favorite}{special}#{ball.pk:0X} {ball.countryball.country}",
                    description=f"ATK: {ball.attack_bonus:+d}% • HP: {ball.health_bonus:+d}% • "
                    f"Caught on {ball.catch_date.strftime('%d/%m/%y %H:%M')}",
                    emoji=emoji,
                    value=f"{ball.pk}",
                    default=ball in self.balls_selected,
                )
            )
        self.select_ball_menu.options = options
        self.select_ball_menu.max_values = len(options)

    @discord.ui.select(min_values=1, max_values=25)
    async def select_ball_menu(
        self, interaction: discord.Interaction["BallsDexBot"], item: discord.ui.Select
    ):
        for value in item.values:
            ball_instance = await BallInstance.get(id=int(value)).prefetch_related(
                "ball", "player"
            )
            self.balls_selected.add(ball_instance)
        await interaction.response.defer()

    @discord.ui.button(label="Select Page", style=discord.ButtonStyle.secondary)
    async def select_all_button(
        self, interaction: discord.Interaction["BallsDexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        count = 0
        for ball in self.select_ball_menu.options:
            ball_instance = await BallInstance.get(id=int(ball.value)).prefetch_related(
                "ball", "player"
            )
            if ball_instance not in self.balls_selected:
                self.balls_selected.add(ball_instance)
                count += 1
        await interaction.followup.send(
            (
                f"Selected {count} {settings.plural_collectible_name} on this page.\n"
                "Note that the menu may not reflect this change until you change page."
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm_button(
        self, interaction: discord.Interaction["BallsDexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel_id = interaction.channel.id if interaction.channel else 0
        if channel_id not in self.cog.active_bets:
            return await interaction.followup.send("No active bet in this channel.", ephemeral=True)

        bet_view = self.cog.active_bets[channel_id]
        if bet_view.state != "OPEN":
            return await interaction.followup.send("The bet is no longer open for additions.", ephemeral=True)

        user_id = interaction.user.id
        if user_id not in bet_view.participants:
            bet_view.participants.append(user_id)
        if user_id not in bet_view.holdings:
            bet_view.holdings[user_id] = []

        if len(self.balls_selected) == 0:
            return await interaction.followup.send(
                f"You have not selected any {settings.plural_collectible_name} to add.",
                ephemeral=True,
            )

        for ball in self.balls_selected:
            if ball.is_tradeable is False:
                return await interaction.followup.send(
                    f"{settings.collectible_name.title()} #{ball.pk:0X} is not tradeable.",
                    ephemeral=True,
                )
            if await ball.is_locked():
                return await interaction.followup.send(
                    f"{settings.collectible_name.title()} #{ball.pk:0X} is locked for trade/donation.",
                    ephemeral=True,
                )
            view = ConfirmChoiceView(interaction)
            if ball.favorite:
                await interaction.followup.send(
                    f"One or more of the {settings.plural_collectible_name} is favorited, "
                    "are you sure you want to add it to the bet?",
                    view=view,
                    ephemeral=True,
                )
                await view.wait()
                if not view.value:
                    return

            bet_view.holdings[user_id].append(ball)
            await ball.lock_for_trade()

        grammar = (
            f"{settings.collectible_name}"
            if len(self.balls_selected) == 1
            else f"{settings.plural_collectible_name}"
        )
        await interaction.followup.send(
            f"{len(self.balls_selected)} {grammar} added to your bet pool.", ephemeral=True
        )
        
        await bet_view.update_message()
        self.balls_selected.clear()

    @discord.ui.button(label="Clear selected", style=discord.ButtonStyle.danger)
    async def clear_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        self.balls_selected.clear()
        await interaction.followup.send(
            f"You have cleared all currently selected {settings.plural_collectible_name}.",
            ephemeral=True,
        )
