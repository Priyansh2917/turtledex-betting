import discord
from discord import app_commands
from discord.ext import commands
from typing import TYPE_CHECKING, Dict, List, Optional, cast, Set
import random
import asyncio

from ballsdex.core.models import BallInstance
from ballsdex.core.utils.transformers import BallEnabledTransform, BallInstanceTransform, SpecialEnabledTransform
from ballsdex.core.utils.sorting import SortingChoices, filter_balls, sort_balls
from ballsdex.settings import settings

from .bulk_selector import BetBulkSelector

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

class BetView(discord.ui.View):
    def __init__(self, cog: "BettingCog", channel_id: int, p1: discord.User, p2: discord.User):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        # OPEN, CONFIRMING, FINISHED
        self.state = "OPEN"
        self.p1 = p1
        self.p2 = p2
        self.winner: Optional[discord.User] = None
        self.participants: List[int] = [p1.id, p2.id]
        self.holdings: Dict[int, List[BallInstance]] = {p1.id: [], p2.id: []}
        self.locked_by: Set[int] = set()
        self.confirmed_by: Set[int] = set()
        self.message: Optional[discord.Message] = None

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        if isinstance(error, discord.NotFound):
            return
        await super().on_error(interaction, error, item)

    def build_buttons(self):
        self.clear_items()
        if self.state == "OPEN":
            lock_btn = discord.ui.Button(label="Lock Proposal", style=discord.ButtonStyle.primary, row=0)
            lock_btn.callback = self.lock_bet_button
            self.add_item(lock_btn)
            
            cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=0)
            cancel_btn.callback = self.cancel_bet_button
            self.add_item(cancel_btn)
        elif self.state == "CONFIRMING":
            confirm_btn = discord.ui.Button(style=discord.ButtonStyle.success, emoji="✅", row=0)
            confirm_btn.callback = self.confirm_bet_button
            self.add_item(confirm_btn)
            
            cancel_btn2 = discord.ui.Button(style=discord.ButtonStyle.danger, emoji="❌", row=0)
            cancel_btn2.callback = self.cancel_bet_button
            self.add_item(cancel_btn2)

    async def update_message(self):
        if not self.message:
            return
        
        embed = discord.Embed(title=f"{settings.bot_name} Betting", color=discord.Color.gold())
        
        if self.state == "OPEN":
            embed.description = "Propose your items using `/bet add`. Both users must lock their proposal."
        elif self.state == "CONFIRMING":
            embed.description = "Both users locked their propositions! Now confirm to conclude this bet."
        elif self.state == "FINISHED":
            if self.winner:
                embed.description = f"*The winner is **{self.winner.name}***"
            else:
                embed.description = "The bet has been cancelled."
            
        for player in [self.p1, self.p2]:
            pool = self.holdings.get(player.id, [])
            status_emoji = ""
            
            if self.state == "OPEN":
                status_emoji = "🔒 " if player.id in self.locked_by else ""
            elif self.state == "CONFIRMING":
                status_emoji = "✅ " if player.id in self.confirmed_by else "🔒 "
            elif self.state == "FINISHED":
                status_emoji = "✅ "
                
            name = f"{status_emoji}{player.name}"
            
            if not pool:
                value = "*Empty*"
            else:
                lines = []
                for ball in pool:
                    emoji = self.cog.bot.get_emoji(ball.countryball.emoji_id) or ""
                    lines.append(f"• {emoji} {ball.countryball.country}")
                value = "\n".join(lines)
                
                # Truncate if it exceeds embed field limits
                if len(value) > 1024:
                    value = value[:1000] + "\n*...and more*"
                    
            embed.add_field(name=name, value=value, inline=True)
            
        embed.set_footer(text="This message is updated every 15 seconds, but you can keep on editing your proposal.")
            
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def lock_bet_button(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
            
        if interaction.user.id not in self.participants:
            return await interaction.followup.send("You are not part of this bet.", ephemeral=True)
        if self.state != "OPEN":
            return await interaction.followup.send("Bet is not in the open state.", ephemeral=True)
            
        self.locked_by.add(interaction.user.id)
        if len(self.locked_by) == 2:
            self.state = "CONFIRMING"
            self.build_buttons()
            await interaction.followup.send("Both players have locked their bet. Please tick to confirm the bet.", ephemeral=True)
        else:
            await interaction.followup.send("Your proposal has been locked. You can wait for the other user to lock their proposal.", ephemeral=True)
        await self.update_message()

    async def confirm_bet_button(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
            
        if interaction.user.id not in self.participants:
            return await interaction.followup.send("You are not part of this bet.", ephemeral=True)
        if self.state != "CONFIRMING":
            return await interaction.followup.send("Bet is not in confirming state.", ephemeral=True)

        self.confirmed_by.add(interaction.user.id)
        if len(self.confirmed_by) == 2:
            self.state = "FINISHED"
            self.clear_items()
            
            # 50/50 RNG Winner
            winner_id = random.choice(self.participants)
            winner = self.p1 if winner_id == self.p1.id else self.p2
            self.winner = winner
            
            from ballsdex.core.models import Player
            winner_player, _ = await Player.get_or_create(discord_id=winner_id)
            
            for p_id, items in self.holdings.items():
                for ball in items:
                    ball.player = winner_player
                    ball.favorite = False
                    await ball.save()
                    await ball.unlock()
            
            if self.channel_id in self.cog.active_bets:
                del self.cog.active_bets[self.channel_id]
                
            await interaction.followup.send("The bet is now concluded.", ephemeral=True)
        else:
            await interaction.followup.send("You have confirmed the bet.", ephemeral=True)
            
        await self.update_message()

    async def cancel_bet_button(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)
        except discord.NotFound:
            return
            
        if interaction.user.id not in self.participants:
            return await interaction.followup.send("You are not part of this bet.", ephemeral=True)
            
        self.state = "FINISHED"
        self.clear_items()
        
        for p_id, items in self.holdings.items():
            for ball in items:
                await ball.unlock()
        if self.channel_id in self.cog.active_bets:
            del self.cog.active_bets[self.channel_id]
        
        await interaction.followup.send(f"The bet was cancelled by <@{interaction.user.id}> and items have been returned.", ephemeral=False)
        await self.update_message()

@app_commands.guild_only()
class BettingCog(commands.GroupCog, group_name="bet"):
    """
    Commands to bet countryballs in a betting pool
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.active_bets: Dict[int, BetView] = {}

    @app_commands.command()
    async def begin(self, interaction: discord.Interaction, user: discord.User):
        """Begin a new bet with another user"""
        await interaction.response.defer(ephemeral=False)
        channel_id = interaction.channel.id
        if channel_id in self.active_bets:
            return await interaction.followup.send("A bet is already active in this channel. Conclude or cancel it first.", ephemeral=True)
        if user.id == interaction.user.id:
            return await interaction.followup.send("You cannot bet against yourself.", ephemeral=True)
        if user.bot:
            return await interaction.followup.send("You cannot bet against a bot.", ephemeral=True)
            
        view = BetView(self, channel_id, interaction.user, user)
        view.build_buttons()
        self.active_bets[channel_id] = view
        
        msg = await interaction.followup.send(f"A new betting pool has started between {interaction.user.mention} and {user.mention}!\nUse `/bet add` or `/bet bulk_add` to add items to your side.", view=view, wait=True)
        view.message = msg
        await view.update_message()

    @app_commands.command()
    async def add(self, interaction: discord.Interaction, countryball: BallInstanceTransform):
        """Add a countryball to the current bet"""
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel.id
        if channel_id not in self.active_bets:
            return await interaction.followup.send("There is no active bet in this channel.", ephemeral=True)
        
        view = self.active_bets[channel_id]
        if interaction.user.id not in view.participants:
            return await interaction.followup.send("You are not part of this active bet.", ephemeral=True)
            
        if view.state != "OPEN":
            return await interaction.followup.send("The bet is no longer open for additions (items are locked).", ephemeral=True)

        if not countryball.is_tradeable:
            return await interaction.followup.send(f"You cannot bet this {settings.collectible_name}.", ephemeral=True)
            
        if await countryball.is_locked():
            return await interaction.followup.send(f"This {settings.collectible_name} is locked in another trade/bet.", ephemeral=True)

        user_id = interaction.user.id
        if countryball in view.holdings[user_id]:
            return await interaction.followup.send("You already added this item.", ephemeral=True)

        await countryball.lock_for_trade()
        view.holdings[user_id].append(countryball)
        await interaction.followup.send(f"Added {countryball.countryball.country} to the bet.", ephemeral=True)
        await view.update_message()

    @app_commands.command()
    async def bulk_add(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        sort: SortingChoices | None = None,
        special: SpecialEnabledTransform | None = None,
    ):
        """Bulk add countryballs to the bet"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = interaction.channel.id
        if channel_id not in self.active_bets:
            return await interaction.followup.send("There is no active bet in this channel.", ephemeral=True)
            
        view = self.active_bets[channel_id]
        if interaction.user.id not in view.participants:
            return await interaction.followup.send("You are not part of this active bet.", ephemeral=True)
            
        if view.state != "OPEN":
            return await interaction.followup.send("The bet is no longer open for additions (items are locked).", ephemeral=True)
        
        query = BallInstance.filter(player__discord_id=interaction.user.id).exclude(
            tradeable=False, ball__tradeable=False
        )
        if countryball:
            query = query.filter(ball=countryball)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)

        balls = cast(list[int], await query.values_list("id", flat=True))
        if not balls:
            return await interaction.followup.send(f"No {settings.plural_collectible_name} found matching criteria.", ephemeral=True)
            
        selector = BetBulkSelector(interaction, balls, self)
        await selector.start(
            content=f"Select the {settings.plural_collectible_name} you want to add "
            "to your bet pool. Note that the display will wipe on pagination however "
            f"the selected {settings.plural_collectible_name} will remain."
        )
