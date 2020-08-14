config = {
    'idea-channel': '742718894690795550'
}


# Setup function
def setup_admin_interface(bot):
    # Yells at the member for not being an admin
    async def you_are_not_admin(ctx):
        await ctx.send(f'**You can\'t do that {ctx.author.mention}!**', delete_after=3.0)
        await ctx.message.delete()

    # Sets the current channel that is used for ideas
    @bot.command(brief='Sets the channel that is used for ideas', hidden=True)
    async def set_idea_channel(ctx, chanid):

        # Check admin
        if not ctx.author.guild_permissions.administrator:
            return await you_are_not_admin(ctx)

        # Get rid of '<#...>'
        try:
            chanid = int(chanid[2:-1])
            # Set it as write channel
            config['idea-channel'] = str(chanid)
            await ctx.send(f'Idea channel channel is now <#{chanid}>!')
        except ValueError:
            return await ctx.send(f'The specified channel was not found, did you add a # before the channel name?')

    # Purges ideas
    @bot.command(hidden=True)
    async def purge(ctx):

        # Check admin
        if not ctx.author.guild_permissions.administrator:
            return await you_are_not_admin(ctx)

        # Purge ideas
        await ctx.channel.purge()
