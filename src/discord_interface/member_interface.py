from os import path, environ
from dotenv import load_dotenv

import asyncio

from discord_database.config import Config
from discord_database.team import Team
from discord_database.user import User
from discord_database.warn import Warn

import discord.ext.commands.errors

from github import Github, UnknownObjectException

from datetime import datetime, timezone, timedelta
import pytz

from discord_interface.common_functions import get_gen_name, check_team_existence, clear_messages_channel, \
    get_github_user_by_id

# Set up .env path
dotenv_path = path.join(path.dirname(__file__), '../../.env')
load_dotenv(dotenv_path)

# Used emojis
CHECK_MARK_EMOJI = '\U0001F973'
RESTART_EMOJI = '\U0001F504'
THUMBS_UP_EMOJI = '\N{THUMBS UP SIGN}'

# GitHub data
github_token = environ.get('GITHUB_TOKEN')
org_name = environ.get('ORG_NAME')

# Bot data
online_since_date = None
utc = pytz.UTC


# Setup function
def setup_member_interface(bot: discord.ext.commands.Bot):
    # -------------------------------- Extra admin commands --------------------------------

    # Used to manually start the leader voting for a team. The leader voting process isn't automatically started when
    # the team is manually created by create_new_team command, and it may need to be manually restarted when the project
    # leader leaves the project through remove_me command or leaves the server
    @bot.command(hidden=True)
    async def start_leader_voting(ctx, team_name=''):
        guild = ctx.guild
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send(ctx.author.mention + ", you can't use this command")

        if not team_name:
            return await ctx.send(ctx.author.mention + ", please input a team name")

        team: Team = Team.get(team_name)
        if not team:
            return await ctx.send("Invalid team name.")
        role = ctx.guild.get_role(team.role_id)
        if not role:
            return await ctx.send("Couldn't find the team role.")
        leader_role = ctx.guild.get_role(team.leader_role_id)
        if leader_role.members:
            return await ctx.send("There is already a leader for this team.")

        if team.voting_id != -1:
            return await ctx.send("There is a current leader voting process")

        category = discord.utils.get(guild.categories, id=team.category_id)
        if not category:
            return await ctx.send("An error has occurred.")
        await vote_for_leader(team.team_name, guild, category)
        await ctx.send(f'The leader voting process has been started for `{team_name}`')

    # Used to manually create teams. This could be used for the teams that existed before the bot was created.
    # NOTE: when using this with existing teams, the category name, the role name, the repo name and the team name must
    # be the same.
    @bot.command(hidden=True)
    async def create_new_team(ctx, team_name):
        if len(team_name) >= 45:
            return await ctx.send(ctx.author.mention + ", the idea name must be less than 45 characters long.")
        team_name = await get_gen_name(team_name)
        await create_team(ctx.guild, team_name)
        await ctx.send(f'Created new team `{team_name}`')

    # Used to check each member's activity by checking the difference between the current time and the
    # start of the week of his latest contribution (Sunday). If the difference is more than 13 days the user gets warned
    @bot.command(hidden=True, brief="Checks the members contribution activities")
    async def activity_check(ctx):
        if not ctx.author.guild_permissions.administrator:
            await ctx.message.delete()
            return await ctx.send(ctx.author.mention + ", you can't do that", delete_after=3.0)

        if datetime.now(tz=timezone.utc).strftime("%A") != "Saturday":  # If it isn't Saturday
            return await ctx.send(ctx.author.mention + ", you can only do activity checks on Saturdays (UTC)")

        await ctx.send("Please wait...")

        g = Github(github_token)
        users = User.get_teams()  # The users in the database
        bot_channel_id = int(Config.get('bot-channel'))
        bot_channel = bot.get_channel(bot_channel_id)

        for user in users:
            repo_id = user.team.repo_id
            gen_name = user.user_team
            status = "inactive"

            guild_user = ctx.guild.get_member(user.user_id)  # The user in the server
            role = ctx.guild.get_role(user.team.role_id)  # The team role
            repo = g.get_repo(repo_id)  # The team repository
            stats_contributors = repo.get_stats_contributors()
            if not repo or not role or not guild_user or not stats_contributors:
                continue

            for stat in stats_contributors:
                if stat.author.id != user.user_github_id:
                    continue
                for week in stat.weeks:
                    if week.c < 1:  # If there were no commits in this week
                        continue
                    current_utc = datetime.now(tz=timezone.utc)  # The current time in UTC
                    commit_start_week = utc.localize(week.w)  # The commit start of the week (Sunday)
                    time_difference = current_utc - commit_start_week
                    day_difference = time_difference.days  # The time difference in days
                    if day_difference < 16:  # If the user has committed in the past two weeks, continue
                        status = 'active'
            # Check the status
            if status == 'inactive':
                await warn_member(guild_user, f'Being inactive in the {gen_name} team')

            await bot_channel.send(f'Name: {guild_user.mention} | Team: **{gen_name}** | Status: **{status}**')

        await ctx.send("Done.")

    # -------------------------------- Getting info --------------------------------
    # Show channels
    @bot.command(brief="Shows all the channels that are related to the voting process")
    async def channels(ctx):
        chans = Config.channels()
        msgs = [f'The {name.replace("-", " ")} is <#{chans[name]}>' for name in chans.keys()]
        msg = '\n'.join(msgs)
        await ctx.send(msg)

    @bot.command(brief="Shows information about the voting process")
    async def voting_info(ctx):
        time_to_wait = int(Config.get('time-to-wait'))
        req_votes = Config.get('required-votes')
        github_sleep_time = int(Config.get('github-sleep-time'))

        voting_days = str(time_to_wait // (24 * 60 * 60))
        voting_hours, voting_minutes, voting_seconds = await format_seconds(time_to_wait)
        voting_hours, voting_minutes, voting_seconds = \
            str(round(voting_hours)), str(round(voting_minutes)), str(round(voting_seconds))

        github_days = str(github_sleep_time // (24 * 60 * 60))
        github_hours, github_minutes, github_seconds = await format_seconds(github_sleep_time)
        github_hours, github_minutes, github_seconds = \
            str(round(github_hours)), str(round(github_minutes)), str(round(github_seconds))

        await ctx.send(f'The current voting period is `{voting_days}` day(s) (`{voting_hours}` hour(s), '
                       f'`{voting_minutes}` '
                       f'minute(s) and `{voting_seconds}` second(s)).\n' +
                       f'The required votes for each idea are `{req_votes}` votes.\n' +
                       f'The team will get created if enough voters reply to the bot DM with their Github username\n' +
                       f'The voters are given `{github_days}` day(s) (`{github_hours}` hour(s), '
                       f'`{github_minutes}` minute(s) and `{github_seconds}` second(s)) '
                       f'to reply with their Github usernames.')

    @bot.command(brief="Shows all teams members and their GitHub usernames")
    async def list_members(ctx, team_name=''):
        if team_name:  # If the user provided a team name
            users = User.get_team(team_name)
            title_name = team_name
        else:  # If the user didn't provide a team name, show all teams
            if not ctx.author.guild_permissions.administrator:
                return await ctx.send(ctx.author.mention + ', only admins can list all the members, '
                                                           'please use `#!list_members "team_name"`')
            users = User.get_teams()
            title_name = "Current users in teams"
        await ctx.send("Please wait...")
        users_str = ''
        teams_str = ''
        githubs_str = ''
        embed = discord.Embed(title=title_name)

        if not users:  # Happens when get_teams() function returns None
            return await ctx.send("There are currently no members in teams.")

        for user in users:
            guild = ctx.guild
            guild_user = guild.get_member(user.user_id)
            if not guild_user or not user.team:  # If the user has left the server or the team is still in creation
                continue
            username = guild_user.name
            role = discord.utils.get(guild_user.roles, id=user.team.role_id)
            github_user = get_github_user_by_id(github_token, user.user_github_id)

            teams_str += "\n" + user.team.team_name
            github_username = github_user.name or github_user.login
            users_str += "\n" + username
            githubs_str += "\n" + github_username
            if not role:
                githubs_str += " [LEFT]"

        users_str = "N/A" if not users_str else users_str
        githubs_str = "N/A" if not githubs_str else githubs_str
        teams_str = "N/A" if not teams_str else teams_str
        embed.add_field(name="Username", value=users_str)
        embed.add_field(name="Team", value=teams_str)
        embed.add_field(name="Github username", value=githubs_str)
        await ctx.send(embed=embed)

    @bot.command(brief="Shows a list of teams that you can join")
    async def list_teams(ctx):
        await ctx.send("Please wait...")
        g = Github(github_token)
        org = g.get_organization(org_name)
        teams = org.get_teams()
        embed = discord.Embed(title="Use the any of the following commands to add yourself to a specific team")
        for github_team in teams:
            github_id = github_team.id
            team: Team = Team.get(github_id=github_id)
            if not team:  # If the team does not exist in the teams table
                continue
            embed.add_field(name=github_team.name, value=f'#!add_me "your github username" "{team.team_name}"')
        if not embed.fields:
            embed.title = "There are no teams available"

        await ctx.send(embed=embed)

    # -------------------------------- Supporting functions --------------------------------
    # A mathematical function that is used to change seconds into hours, minutes, seconds format
    async def format_seconds(seconds):
        hours_decimal = seconds / 3600
        hours, minutes_in_hours = divmod(hours_decimal, 1)
        minutes_decimal = minutes_in_hours * 60
        minutes, seconds_in_minutes = divmod(minutes_decimal, 1)
        seconds = seconds_in_minutes * 60
        return hours, minutes, seconds

    # Get the time to wait for either the voting process or the GitHub-usernames-gathering process by adding the
    # appropriate waiting time config value to the message edit/creation date and subtracting the current
    # time from it. This is also used whenever the bot boots up to check how many seconds are remaining for each process
    async def get_time_to_wait(message, voting: bool):
        # Set the message creation time to the creation date if it hasn't been edited, otherwise set it to the edit date
        if not message.edited_at:
            message_creation_time: datetime = utc.localize(message.created_at)
        else:
            message_creation_time: datetime = utc.localize(message.edited_at)
        time_to_wait = int(Config.get('time-to-wait')) if voting else int(Config.get('github-sleep-time'))
        time_to_wait_addition = timedelta(seconds=time_to_wait)  # Seconds in timedelta to be able to add it to the
        # message creation date
        idea_end_date = message_creation_time + time_to_wait_addition  # The date at which the idea will end
        final_time = idea_end_date - datetime.now(tz=timezone.utc)
        days = final_time.days
        seconds = final_time.seconds
        if days < 0:
            days = 0
            seconds = 0
        return days, seconds

    async def get_github_percentage(votes_number):
        if votes_number < 60:
            return (80 - votes_number) / 100
        else:
            return 20 / 100

    # Continue asking for Github usernames if the bot goes down
    async def continue_githubs(gen_name, participants_message):
        days, seconds = await get_time_to_wait(participants_message, voting=False)
        time_to_wait = days * 24 * 60 * 60 + seconds
        hours_show, minutes_show, seconds_show = await format_seconds(seconds)  # The variables that are shown in the
        # message
        overview_channel_id = int(Config.get('overview-channel'))
        overview_channel = bot.get_channel(overview_channel_id)
        users = participants_message.mentions
        required_percentage = await get_github_percentage(len(users))
        participants_list = []
        for user in users:
            if isinstance(user, discord.User):
                continue
            if not discord.utils.get(user.roles, name=gen_name):  # If the mentioned user doesn't have the role
                participants_list.append(user)
        current_percentage = (len(users) - len(participants_list)) / len(users)
        await overview_channel.send(f'Voters for `{gen_name}` have `{str(days)}` day(s), `{str(round(hours_show))}` '
                                    f'hour(s), `{str(round(minutes_show))}` minutes and `{str(round(seconds_show))}` '
                                    f'seconds to send their GitHub usernames\n'
                                    f'`{required_percentage * 100}%` of the voters must reply to my DM with their '
                                    f'Github username in order to create the team.\n'
                                    f'`{current_percentage * 100}%` of the voters have replied.')
        await get_all_githubs(participants_list, gen_name, participants_message, time_to_wait)

    # Continue voting if the bot goes down
    async def continue_voting(message, gen_name):
        trials_field = discord.utils.get(message.embeds[0].fields, name="Trials")
        trials = int(trials_field.value)
        await wait_for_votes(message.id, gen_name, trials)

    # Checks if an idea team is already created
    async def check_if_finished(gen_name):
        overview_channel_id = int(Config.get('overview-channel'))
        overview_channel = bot.get_channel(overview_channel_id)
        participants_messages = await overview_channel.history().flatten()
        idea_ended = True
        if not participants_messages:
            return idea_ended
        for message in participants_messages:
            if message.embeds and message.embeds[0].title == gen_name:
                # If it is a participants messages containing an Embed
                idea_ended = False
                break

        return idea_ended

    # Adds the team role to the user and his GitHub user name to the db
    async def add_github(guild, guild_user, github_user, gen_name):
        g = Github(github_token)  # Logs into GitHub
        try:
            github = g.get_user(github_user)  # Tries to find the user on GitHub
            team: Team = Team.get(gen_name)
            if team:
                role = guild.get_role(team.role_id)  # Finds the team role if the team exists in the database
            else:
                role = discord.utils.get(guild.roles, name=gen_name)
                # Finds the role created in get_all_githubs function
            if not role:
                try:
                    return "Failed"
                except discord.Forbidden:
                    return "Failed"
            await guild_user.add_roles(role)  # Adds the role
            User.set(guild_user.id, gen_name, github_user, github.id)
            return github
        except UnknownObjectException:  # If the user's GitHub was not found
            return None

    # Checks if the user has already submitted the same GitHub username for the same team
    async def check_submitted_validity(member, gen_name, github_username, channel):
        valid = True
        user: User = User.get(member.id, gen_name)
        if not user:
            return valid
        if user.team:
            role = discord.utils.get(member.roles, id=user.team.role_id)
        else:
            role = discord.utils.get(member.roles, name=gen_name)
        try:
            g = Github(github_token)
            github_user = g.get_user(github_username)
        except UnknownObjectException:
            return await channel.send("Invalid Github username.")
        if user.user_github_id == github_user.id:
            await channel.send(f'Your GitHub username `{github_username}` is already set for this team')
            valid = False if role else True  # If the role already exists, there is no need to add it again
        else:
            await channel.send("Replacing your GitHub username...")
        return valid

    async def check_user_in_server(guild, member_id):
        guild_user = guild.get_member(member_id)
        return guild_user if guild_user else None

    # Checks for any unfinished ideas that were stopped when the bot rebooted
    async def check_unfinished_ideas():
        print("Checking for any unfinished ideas...")
        for channel_name in ['idea-channel', 'overview-channel']:
            channel_id = int(Config.get(channel_name))
            channel = bot.get_channel(channel_id)
            channel_messages = await channel.history().flatten()

            for message in channel_messages:  # Loop the messages in the channel
                if message.embeds:  # If the message contains an Embed, add the restart emoji
                    print(f'Found an unfinished process in {channel_name}!')
                    await message.add_reaction(RESTART_EMOJI)

    async def get_idea_inputs(ctx, required):
        await ctx.send(ctx.author.mention + f", please type `p: [{required}]` without '[', ']'")

        def check(m: discord.Message):
            return 'p:' in m.content.lower() and m.channel == ctx.channel and m.author == ctx.author \
                   and m.content.lower().strip() != 'p:'

        try:
            message = await bot.wait_for('message', timeout=75.0, check=check)
            content = message.content[2:]
            name = content.strip()
            return name
        except asyncio.TimeoutError:
            await ctx.send(ctx.author.mention + " your idea has been cancelled for not responding.")
            return None

    async def show_idea_preview(ctx, embed):
        def check(m):
            return m.channel == ctx.channel and m.content.lower().replace(" ", "") == 'p:yes' and m.author == ctx.author

        preview_message = await ctx.send(ctx.author.mention +
                                         ", this is how your idea will look in the ideas channel, type `p: yes` to"
                                         " confirm.", embed=embed)
        try:
            await bot.wait_for('message', timeout=75.0, check=check)
            return preview_message
        except asyncio.TimeoutError:
            await preview_message.delete()
            await ctx.send(ctx.author.mention + " your idea has been cancelled for not responding to the bot")
            return None

    # -------------------------------- Team management --------------------------------

    async def manage_leader_voting(ctx, gen_name, add=True):
        team: Team = Team.get(gen_name)
        category: discord.CategoryChannel = discord.utils.get(ctx.guild.categories, id=team.category_id)

        # Check the team category
        if not category:
            return
        voting_channel_id = team.voting_id

        if voting_channel_id == -1:  # if the voting channel doesn't exist
            return
        voting_channel = ctx.guild.get_channel(voting_channel_id)
        if not voting_channel:
            return

        messages = await voting_channel.history().flatten()
        mention_message = None
        # Find if there is a message that mentions the user
        for message in messages:
            if message.mentions and message.author.bot and message.mentions[0] == ctx.author:
                mention_message = message
        # If there is a message that mentions the user, do not resend
        if add and mention_message:
            return
        # If there is not, send a message that mentions the user to be added to the leader voting process
        elif add and not mention_message:
            message = await voting_channel.send(ctx.author.mention)
            return await message.add_reaction(THUMBS_UP_EMOJI)
        # If the user wanted to be removed, remove the mention message
        elif not add:
            await mention_message.delete()

    @bot.command(brief="Adds you to a team of your choice")
    async def add_me(ctx, github_username="", team_name=""):
        if github_username == "":
            return await ctx.send(ctx.author.mention + ", please provide your GitHub username as a first argument.")
        if team_name == "":
            return await ctx.send(ctx.author.mention + ", please provide the team name as a second argument "
                                                       "and your GitHub username as a first argument.")
        role = await check_team_existence(ctx, team_name, ctx.guild.roles)
        if not role:
            return

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel \
                   and "a:yes" in m.content.lower().strip().replace(" ", "")

        if not await check_submitted_validity(ctx.author, team_name, github_username, ctx.channel):
            # Checks if the user has already inputted his GitHub and if he has the role
            # If the user has the role and has already inputted the same GitHub name
            await ctx.send("If you are not in the Github team and would like to be added type: `a: yes`")
            try:
                await bot.wait_for('message', check=check, timeout=20)
            except asyncio.TimeoutError:
                return await ctx.send(ctx.author.mention + ", no actions have been performed on you")

        github_user = await add_github(ctx.guild, ctx.author, github_username, team_name)
        if github_user and github_user != "Failed":
            await ctx.send(f'Thank you! I am giving you the `{team_name}` role...')
        elif github_user and github_user == "Failed":
            await ctx.send(f"Couldn't find the role for `{team_name}, please contact an administrator`")
        else:
            await ctx.send("Invalid GitHub username.")

        team: Team = Team.get(team_name)
        g = Github(github_token)
        org = g.get_organization(org_name)
        github_team = org.get_team(team.github_id)
        if not github_team:
            return await ctx.send(ctx.author.mention + ", an error has occurred while adding you to the team.")
        await add_membership(ctx.author, team_name, github_team)
        await ctx.send("Done")
        await manage_leader_voting(ctx, team_name)

    @bot.command(brief="Removes you from a team you are a member of")
    async def remove_me(ctx, team_name):
        role = await check_team_existence(ctx, team_name, ctx.author.roles)
        if not role:
            return
        team: Team = Team.get(team_name)
        await ctx.author.remove_roles(role)
        await ctx.send(ctx.author.mention + ", I have removed your role. Please wait...")
        leader_role = discord.utils.get(ctx.author.roles, id=team.leader_role_id)
        if leader_role:
            await ctx.author.remove_roles(leader_role)
            await ctx.send(ctx.author.mention + ", I have removed your leadership role")

        g = Github(github_token)
        org = g.get_organization(org_name)
        github_team = org.get_team(team.github_id)
        if not github_team:
            return await ctx.send(ctx.author.mention + ", couldn't find the team on GitHub")
        user: User = User.get(ctx.author.id, team_name)
        if not user:
            return await ctx.send(ctx.author.mention + ", couldn't find you in the database.")
        github_id = user.user_github_id
        try:
            github_user = get_github_user_by_id(github_token, github_id)
            github_team.remove_membership(github_user)

            await ctx.send(ctx.author.mention + ", I have removed you from the GitHub team")
        except UnknownObjectException:
            await ctx.send("There was a problem finding you on GitHub, perhaps you have changed your username?")
        await manage_leader_voting(ctx, team_name, add=False)

    # -------------------------------- Team creation --------------------------------
    async def vote_for_leader(gen_name, guild, category):
        team: Team = Team.get(gen_name)
        if not team:
            return
        role = guild.get_role(team.role_id)
        # Only the team role members will be able to view the channel
        overwrites = {role: discord.PermissionOverwrite(view_channel=True),
                      guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        voting_channel = discord.utils.get(guild.channels, name='leader-voting', category=category)
        if voting_channel:
            Team.set_voting_channel(team.team_name, voting_channel.id)
            return
        voting_channel = await guild.create_text_channel("leader-voting", overwrites=overwrites, category=category)
        Team.set_voting_channel(gen_name, voting_channel.id)
        await voting_channel.send("Vote for who you would like to be the project leader")
        for member in role.members:
            if member.bot:
                continue
            voting_message = await voting_channel.send(member.mention)
            await voting_message.add_reaction(THUMBS_UP_EMOJI)

    async def create_category_channels(guild, gen_name):
        team: Team = Team.get(gen_name)
        if team:
            role = guild.get_role(team.role_id)
            leader_role = guild.get_role(team.role_id)
        else:
            role = discord.utils.get(guild.roles, name=gen_name)
            leader_role = discord.utils.get(guild.roles, name="pl-" + gen_name)

        if not role:  # Creates the team role
            role = await guild.create_role(name=gen_name)

        # Remove the role from the bot
        if role.members:
            for member in role.members:
                if member.bot:
                    await member.remove_roles(role)

        if not leader_role:  # Creates the leader role
            leader_role = await guild.create_role(name="pl-" + gen_name, color=discord.Colour(16711680))

        await role.edit(hoist=True)  # Makes the team role show in the members list

        # Only the team role members will be able to view the channel
        overwrites = {role: discord.PermissionOverwrite(view_channel=True),
                      guild.default_role: discord.PermissionOverwrite(view_channel=False)}

        # Tries to see if a category already exists with the team name
        category = discord.utils.get(guild.categories, name=gen_name)
        if not category:  # Creates the team category
            category = await guild.create_category(gen_name, overwrites=overwrites)
        else:
            for channel in category.channels:
                if channel.name != "general":
                    continue
                return channel, category, role, leader_role
        text_channel = await guild.create_text_channel("general", overwrites=overwrites, category=category)
        await guild.create_voice_channel("Collab room", overwrites=overwrites, category=category)
        await text_channel.send(role.mention + " LET'S GO!!")
        if not role.members:
            return text_channel, category, role, leader_role
        return text_channel, category, role, leader_role

    # To add users to a GitHub team
    async def add_membership(member, gen_name, team):
        user: User = User.get(member.id, gen_name)
        if not user:
            return
        try:
            github_user = get_github_user_by_id(github_token, user.user_github_id)
            team.add_membership(github_user, role="member")
        except UnknownObjectException:
            try:
                await member.send(f'There has been a problem adding you to the GitHub team in the `{gen_name}` project,'
                                  f' perhaps you have changed your GitHub username?')
            except discord.Forbidden:
                return

    async def create_org_team(gen_name, team_members, org):
        teams = org.get_teams()
        if teams:  # If there are teams in the organization
            for team in teams:
                if team.name != gen_name:
                    continue
                # If there is a team with the same name
                return team

        team = org.create_team(gen_name, privacy="closed")
        # If there are no role members
        if not team_members:
            return team

        for member in team_members:
            await add_membership(member, gen_name, team)

        return team

    async def create_repo(org, team, gen_name):
        repos = org.get_repos()
        if repos:  # If there are repos in the organization
            for repo in repos:
                if repo.name != gen_name:
                    continue
                # If a repository already exists for this idea
                team.add_to_repos(repo)
                return repo

        repo = org.create_repo(gen_name, private=False)
        team.add_to_repos(repo)
        return repo

    async def notify_about_team(repo, team, text_channel: discord.TextChannel):
        running_channel_id = int(Config.get('running-channel'))
        running_channel = bot.get_channel(running_channel_id)
        embed = discord.Embed(title=team.name)
        await text_channel.send(f'https://github.com/orgs/{org_name}/teams/{team.name}')
        await text_channel.send(f'https://github.com/{org_name}/{repo.name}')
        await running_channel.send(f'A new team has been created!\n'
                                   f'https://github.com/{org_name}/{repo.name}\n'
                                   f'Please use `#!add_me "your Github username" "{team.name}"` to be added.',
                                   embed=embed)

    # The team creation process
    async def create_team(guild, gen_name):

        text_channel, category, role, leader_role = await create_category_channels(guild, gen_name)

        team_members = role.members

        g = Github(github_token)
        org = g.get_organization(org_name)
        github_team = await create_org_team(gen_name, team_members, org)

        repo = await create_repo(org, github_team, gen_name)
        Team.set(gen_name, role.id, leader_role.id, category.id, text_channel.id, github_team.id, repo.id)
        await notify_about_team(repo, github_team, text_channel)
        await vote_for_leader(gen_name, guild, category)

    async def kick_member(member, reason):
        guild = member.guild
        bot_channel_id = int(Config.get('bot-channel'))
        bot_channel = bot.get_channel(bot_channel_id)
        if member.guild_permissions.administrator:
            return await bot_channel.send(f'Could not kick {member.mention}')
        await guild.kick(member, reason=reason)
        await bot_channel.send(f'{member.mention} has been kicked.\nReason: `{reason}`')
        Warn.delete(member.id)
        try:
            await member.send(f'You have been kicked from our server.\nReason: `{reason}`')
        except discord.Forbidden:
            return

    async def warn_member(member, reason):
        Warn.warn(member.id)
        bot_channel_id = int(Config.get('bot-channel'))
        bot_channel = bot.get_channel(bot_channel_id)
        await bot_channel.send(f'{member.mention} has been warned.\nReason: `{reason}`')
        if Warn.warnings(member.id) >= 3:
            await kick_member(member, "Reaching 3 or more warnings")
        try:
            await member.send(f'You have been warned.\nReason: `{reason}`')
        except discord.Forbidden:
            return

    # -------------------------------- Voting logic --------------------------------
    # Proposes a new idea to idea channel
    @bot.command(brief="Adds a new idea to the ideas channel")
    @discord.ext.commands.cooldown(1, 300, discord.ext.commands.BucketType.user)
    async def new_idea(ctx: discord.ext.commands.Context):
        # Get channel
        chanid = Config.get('idea-channel')
        chanid = int(chanid)
        chan = bot.get_channel(chanid)
        overview_id = int(Config.get('overview-channel'))
        overview_channel = bot.get_channel(overview_id)
        if not chanid:
            return await ctx.send('Idea channel is not available!')

        # Get the name of the idea
        idea_name = await get_idea_inputs(ctx, "the proposed idea name")
        if not idea_name:
            return
        gen_name = await get_gen_name(idea_name)

        if not gen_name:
            await ctx.send(ctx.author.mention + ", the idea name length must be less that 95 characters long")
            return await new_idea(ctx)

        if Team.get(gen_name):
            await ctx.send(ctx.author.mention + ", this idea name already exists.")
            return await new_idea(ctx)

        # Check if there is currently a proposed idea with the same title
        messages = await chan.history().flatten()
        for message in messages:
            if message.embeds and message.embeds[0].title == gen_name:
                await ctx.send(ctx.author.mention + ", this idea name already exists.")
                return await new_idea(ctx)

        # Get the idea explanation
        idea_explanation = await get_idea_inputs(ctx, "the idea explanation")
        if not idea_explanation:
            return

        # Get the idea language
        lang = await get_idea_inputs(ctx, "the programming language of idea")
        if not lang:
            return

        # Generate a name from idea
        for item in ['`', '"', '*', '_', '@', '#']:  # Filter out unwanted characters
            lang = lang.replace(item, '')
            idea_explanation = idea_explanation.replace(item, '')
            gen_name = gen_name.replace(item, '')

        # Check if the same name exists in the database and if so delete (there would be no current idea with this
        # name anyway, it would be an outdated finished idea)
        User.delete_team(gen_name)

        try:
            # Notify with embed
            embed = discord.Embed(title=gen_name, color=0x00ff00)
            embed.add_field(name="Idea Explanation", value=idea_explanation)
            embed.add_field(name='Programming Language', value=lang, inline=False)
            preview_message = await show_idea_preview(ctx, embed)
            if not preview_message:
                return
            await preview_message.delete()

            embed.insert_field_at(2, name="Trials", value="0")
            msg = await chan.send(f'{ctx.author.mention} proposed an idea, '
                                  f'@everyone please vote using a thumbs up reaction:',
                                  embed=embed)
            await msg.add_reaction('👍')

            # Watch it
            await wait_for_votes(msg.id, gen_name, 0)
        except discord.HTTPException:
            await overview_channel.send(ctx.author.mention +
                                        ", an error has occurred while processing one of your ideas")

    # Asks user for github
    async def get_github(voter, gen_name, forbidden=False):
        embed = discord.Embed(title=gen_name)
        embed.add_field(name="Idea", value=gen_name)
        embed.add_field(name="Guild ID", value=voter.guild.id)
        send_channel = voter
        message_content = f'Hello {voter.mention}!\nWe noticed that you have ' \
                          f'voted for the following idea:\n' + \
                          'Please send me your GitHub username so I can add you to the team.\n'
        if forbidden:
            print(f'Could not send messages to {voter.name}. Sending on failed messages channel')
            messages_channel_id = int(Config.get('messages-channel'))
            messages_channel = bot.get_channel(messages_channel_id)
            message_content = f"Hello {voter.mention}! We have noticed that you have voted for the following idea:\n" \
                              f"However we were not able to message you privately; please send your Github username " \
                              f"here so I can add you to the team."
            send_channel = messages_channel
        try:
            message = await send_channel.send(message_content +
                                              "\nIf you receive no reply, then the bot is down or the idea team "
                                              "has already been created.\n" +
                                              "If you accidentally send someone else's username, "
                                              "simply re-send your username",
                                              embed=embed)
            dm_channel = message.channel
            messages = await dm_channel.history().flatten()
            # Clean up Bot messages sent before it was rebooted
            for message in messages:
                if utc.localize(message.created_at) < online_since_date and message.author.bot and message.embeds:
                    # If it was a message sent before the bot rebooted, delete it
                    await message.delete()
        except discord.Forbidden:
            return await get_github(voter, gen_name, forbidden=True)

    # Notifies the participants about the idea processing results
    async def notify_voters(participants_message, gen_name):
        overview_id = int(Config.get('overview-channel'))
        message = None
        for member in participants_message.mentions:
            if member.bot:
                continue
            try:
                message = await member.send(f'Processing the `{gen_name}` idea has ended. '
                                            f'Please check <#{overview_id}>')
            except discord.Forbidden:
                continue
        if not message:
            return
        dm_channel = message.channel
        messages = await dm_channel.history().flatten()
        for message in messages:
            if message.embeds and message.author.bot and message.embeds[0].title == gen_name:
                await message.delete()

    async def warn_inactives(participants_message, gen_name):
        voters = participants_message.mentions
        for voter in voters:
            if isinstance(voter, discord.User):
                continue
            role = discord.utils.get(voter.roles, name=gen_name)
            if role:
                continue
            await warn_member(voter, "Failing to reply with their GitHub username after voting for an idea.")

    # Used after an idea gets enough votes
    async def get_all_githubs(participants, gen_name, message, waiting_time):
        guild = message.guild
        role = discord.utils.get(guild.roles, name=gen_name)  # Tries to find if a role already exists
        # This happens when the bot is turned off during the GitHub gathering process
        if not role:
            role = await guild.create_role(name=gen_name)  # Creates a role for the team

        for user in participants:
            if not user.bot:
                await get_github(user, gen_name)  # Asks each user for their Github
            else:
                await user.add_roles(role)  # Adds the role to the bot

        all_participants = message.mentions

        github_required_percentage = await get_github_percentage(len(all_participants))

        await asyncio.sleep(waiting_time)
        overview_id = int(Config.get('overview-channel'))
        overview_channel = bot.get_channel(overview_id)
        if not role.members:
            await overview_channel.send(f"An error has occurred while processing the `{gen_name}` idea. "
                                        f"The bot could not find the team role, idea cancelled.")
            return await message.delete()

        await warn_inactives(message, gen_name)
        # If the required percentage or more replied with their GitHub accounts and got their roles added
        if len(role.members) >= github_required_percentage * len(message.mentions):
            await overview_channel.send(f'More than {str(github_required_percentage * 100)}% ' +
                                        f'of the participants in `{gen_name}` ' +
                                        'replied with their GitHub usernames, idea approved!')
            await message.delete()
            await create_team(message.guild, gen_name)

        else:
            await overview_channel.send(
                f'Less than {str(github_required_percentage * 100)}% of the participants in `{gen_name}` '
                + "replied with their GitHub usernames, idea cancelled.")
            await role.delete()
            User.delete_team(gen_name)
            await message.delete()

        await notify_voters(message, gen_name)
        await clear_messages_channel(bot, gen_name)

    # Watches a vote for 14 days
    async def wait_for_votes(message_id, gen_name, trials):

        # Get channels
        overview_chan = Config.get('overview-channel')
        overview_chan = int(overview_chan)
        overview_chan = bot.get_channel(overview_chan)
        idea_id = int(Config.get('idea-channel'))
        idea_channel = bot.get_channel(idea_id)
        guild = idea_channel.guild
        msg = None

        # Trial count
        while trials <= 3:
            # Wait for 14 days
            msg = await idea_channel.fetch_message(message_id)
            days, seconds = await get_time_to_wait(msg, voting=True)
            hours_show, minutes_show, seconds_show = await format_seconds(seconds)
            time_to_wait = days * 24 * 60 * 60 + seconds
            await overview_chan.send(f'`{str(days)}` day(s), `{str(round(hours_show))}` hour(s), '
                                     f'`{str(round(minutes_show))}` minute(s) '
                                     f'and `{str(round(seconds_show))}` second(s) are '
                                     f'remaining till voting ends on the `{gen_name}` idea.')
            await asyncio.sleep(time_to_wait)
            msg = await idea_channel.fetch_message(message_id)
            voters_number = 0

            if msg.mentions and guild.get_member(msg.mentions[0].id):
                owner = msg.mentions[0]
                participants = owner.mention  # Add the idea owner as an initial participant
                participants_list = [owner]  # A list to contain Members

            else:
                owner = None
                participants = ''
                participants_list = []

            # Get the votes
            for reaction in msg.reactions:
                if reaction.emoji != THUMBS_UP_EMOJI:
                    continue
                users = await reaction.users().flatten()  # Find the users of this reaction
                voters_number = len(users)
                for user in users:
                    if user == owner:  # If the user is the owner of the idea, continue
                        voters_number -= 1
                        continue
                    if isinstance(user, discord.User):  # If the user has left the server, continue
                        voters_number -= 1
                        continue
                    participants += "\n" + user.mention
                    participants_list.append(user)

            req_votes = int(Config.get('required-votes'))
            # Check votes (-1 the bot)
            if voters_number > req_votes:
                await msg.delete()
                embed = discord.Embed(title=gen_name)
                participants_message = await overview_chan.send(
                    f'''
                    {CHECK_MARK_EMOJI * voters_number}\n\n''' +
                    f'''Voting for {gen_name} has ended, **approved**!\n'''
                    f'Participants:\n{participants}\nPlease check your messages, the team will be created '
                    f'if a sufficient number of voters reply to the DM with their GitHub usernames\n'
                    f'Check `#!voting_info` to know how much time you have to reply with your GitHub usernames',
                    embed=embed)
                github_wait = int(Config.get('github-sleep-time'))
                return await get_all_githubs(participants_list, gen_name, participants_message, github_wait)

            # If the votes aren't enough
            await overview_chan.send(
                f'Votes for `{gen_name}` were not enough, waiting for more votes...'
            )
            embed = msg.embeds[0].set_field_at(2, name="Trials", value=str(trials + 1))
            await msg.edit(embed=embed)
            trials += 1
            continue  # Wait 14 days more

        # Trials end here
        await overview_chan.send(
            f'The `{gen_name}` idea has been cancelled due to lack of interest :('
        )

        # Delete the message
        await msg.delete()

    # -------------------------------- Bot Events --------------------------------
    # Startup
    @bot.event
    async def on_ready():
        print('I\'m alive, my dear human :)')
        global online_since_date
        online_since_date = datetime.now(tz=timezone.utc)
        await check_unfinished_ideas()
        print("Done.")

    # Watch for reaction add
    @bot.event
    async def on_raw_reaction_add(reaction):
        idea_id = int(Config.get('idea-channel'))
        idea_channel = bot.get_channel(idea_id)
        overview_id = int(Config.get('overview-channel'))
        overview_channel = bot.get_channel(overview_id)

        if reaction.channel_id != idea_id and reaction.channel_id != overview_id:
            # Makes sure the reaction added is in the ideas channel or the overview channel
            return
        if reaction.channel_id == idea_id:
            message = await idea_channel.fetch_message(reaction.message_id)
        else:
            message = await overview_channel.fetch_message(reaction.message_id)
        if not message.author.bot or not message.embeds:
            # If the message reacted to is not by the bot and doesn't contain an embed. ie: it is not an idea message
            return
        embed = message.embeds[0]

        if reaction.emoji.name == THUMBS_UP_EMOJI:
            return

        elif reaction.emoji.name == RESTART_EMOJI and reaction.member.bot:
            # if it is a restart emoji put by the bot, restart the voting period
            idea_name = embed.title
            # We remove the reaction in case the voting period gets restarted again
            await message.remove_reaction(reaction.emoji, reaction.member)
            if reaction.channel_id == idea_id:
                await continue_voting(message, idea_name)
            else:
                await continue_githubs(idea_name, message)

        else:  # If it is another emoji, remove the reaction
            await message.remove_reaction(reaction.emoji, reaction.member)

    # Watch messages addition to check for sent GitHub accounts
    @bot.event
    async def on_message(message):
        channel = message.channel
        messages_channel_id = int(Config.get("messages-channel"))
        messages_channel = bot.get_channel(messages_channel_id)
        if channel.type != discord.ChannelType.private and channel != messages_channel:
            # If it is not a DM channel nor the failed messages channel
            return await bot.process_commands(message)  # Process the commands normally

        if message.author.bot:
            return

        messages = await channel.history().flatten()  # Get last 100 messages in DM channel

        # Variables initial declaration
        guild_id = 0
        checked_ideas = 0
        # The username
        username_message = messages[0]  # Gets the last message in DM (the one containing the username)
        github_user = username_message.content

        await channel.send("Hey there, please wait...")
        for m in messages:
            if not m.embeds or message.author not in m.mentions:
                continue
            embed = m.embeds[0]
            gen_name = embed.title  # Gets the idea name from the message sent to user

            for field in embed.fields:
                if field.name == "Guild ID":
                    guild_id = int(field.value)

            guild = bot.get_guild(guild_id)  # Gets the server

            # Checking if team creation process was done
            if await check_if_finished(gen_name):
                continue

            # Checking if the user is in the server
            guild_user = await check_user_in_server(guild, username_message.author.id)

            if not guild_user:
                return await channel.send("I can't find you in our server.")

            # Checking if the user has already submitted his username
            if not await check_submitted_validity(guild_user, gen_name, github_user, channel):
                continue

            # Add the user's GitHub name to the GitHub channel and give the team role
            user = await add_github(guild, guild_user, github_user, gen_name)
            if user and user != "Failed":
                await channel.send(f'Thank you! I am currently giving you the `{gen_name}` role...')
                checked_ideas += 1
            elif user and user == "Failed":
                return await channel.send(f"Could not find the role for `{gen_name}`, please contact an administrator")
            else:
                await channel.send("This username is not a valid Github username")
                return
        if checked_ideas > 0:
            await channel.send("Done")
            return await channel.send("The team will be created if a sufficient number of voters "
                                      "reply with their GitHub usernames")
        else:
            return await channel.send("Nothing to do here :)")

    # Command error handling as cool-down
    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, discord.ext.commands.CommandOnCooldown):
            await ctx.send(f'You can only use this command again after `{str(round(error.retry_after))}` seconds.')
        elif isinstance(error, discord.ext.commands.CommandNotFound):
            await ctx.send(f'Unknown command: `{ctx.message.content}`')
        elif isinstance(error, discord.ext.commands.ExpectedClosingQuoteError):
            await ctx.send("Please close all the quotation marks")
        elif isinstance(error, discord.ext.commands.MissingRequiredArgument):
            await ctx.send("Missing arguments. Please use `#!help command_name`")
        else:
            raise error
