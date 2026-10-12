import argparse
import os
import redis.asyncio
import ssl
from discord.ext import tasks
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime
import calendar
import asyncio
import logging
import sys
from urllib.parse import urlparse

import core

# Read from the environment by main(), so importing this module reads no config.
TOKEN = None
REDIS_URL = None
# DEV_GUILD_ID: a test server, where the commands are synced on every start and show up at once.
DEV_GUILD_ID = None
# Set by --sync: replace the bot's commands in every server with this build's at startup.
SYNC_COMMANDS = False


# Function to initialize Redis with timeouts. A rediss:// URL checks the server's
# certificate and hostname; REDIS_TLS_INSECURE=1 turns both checks off, for servers
# with self-signed certificates such as Heroku's:
# https://devcenter.heroku.com/articles/connecting-heroku-redis
def create_redis_client():
    tls_options = {}
    if urlparse(REDIS_URL).scheme == "rediss" and os.getenv("REDIS_TLS_INSECURE") == "1":
        tls_options["ssl_cert_reqs"] = ssl.CERT_NONE  # noqa: TID251 - the REDIS_TLS_INSECURE=1 opt-in
    return redis.asyncio.from_url(
        REDIS_URL,
        decode_responses=True,  # Ensures Redis returns strings instead of bytes
        socket_timeout=10,
        socket_connect_timeout=10,
        retry_on_timeout=True,
        **tls_options,
    )


# Created by main(); tests replace it with a fake.
redis_client = None

# /birthday upcoming lists the birthdays from today to this many days ahead, unless the member
# picks another number of days, from 1 to UPCOMING_MAX_DAYS.
UPCOMING_DAYS = 30
UPCOMING_MAX_DAYS = 366

# The newest year /birthday set offers. Discord reads it when the command is defined, so the
# handler also refuses a date after today.
LATEST_BIRTH_YEAR = datetime.date.today().year  # noqa: DTZ011 - the host's date

# What a member is told when the month has no such day, or the year no Feb 29.
NO_SUCH_DATE = "❌ That date does not exist. Please check it and try again."

# The monthly post goes out at this time on the 1st. There is no timezone setting, so UTC.
MONTHLY_POST_TIME = datetime.time(hour=15, tzinfo=datetime.UTC)

# A guild's monthly post is marked in Redis for this long: past the end of its month.
MONTHLY_POST_MARKER_TTL = datetime.timedelta(days=40)

# Errors are logged here. A log line never includes a birth date.
log = logging.getLogger(__name__)

# What a member is told when Redis fails.
SAVE_FAILED = "⚠️ Storage is unavailable, so your birthday was not saved. Please try again later."
READ_FAILED = "⚠️ Storage is unavailable. Please try again later."


# Raised by the storage helpers below when Redis fails, so that callers can tell an outage
# from "nothing stored". The redis.RedisError is its __cause__. Its message is only the
# error's type: callers log it, and the error of a failed transaction quotes its commands,
# birth date included.
class StoreUnavailable(Exception):
    pass


# Store a birthday and share it with the guild where it was set: "YYYY-MM-DD", or ISO 8601's
# year-less "--MM-DD". show_age is the member's opt-in to show the year and age, stored as
# user:<id>:show_age = "1" and deleted when off. Raises StoreUnavailable.
async def set_birthday_redis(guild_id, user_id, birthday, show_age=False):
    try:
        # One transaction, so the date, the opt-in, the guild's membership and the member's
        # list of guilds (which /birthday forget reads) are saved together.
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.set(f"user:{user_id}:birthday", birthday)
            if show_age:
                pipe.set(f"user:{user_id}:show_age", "1")
            else:
                pipe.delete(f"user:{user_id}:show_age")
            pipe.sadd(f"guild:{guild_id}:birthdays", user_id)
            pipe.sadd(f"user:{user_id}:guilds", guild_id)
            await pipe.execute()
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# Get a member's birthday and show_age opt-in, (birthday, show_age), if they shared the
# birthday with this guild; otherwise (None, False). Raises StoreUnavailable.
async def get_birthday_redis(guild_id, user_id):
    try:
        if await redis_client.sismember(f"guild:{guild_id}:birthdays", user_id):
            birthday, show_age = await redis_client.mget(f"user:{user_id}:birthday", f"user:{user_id}:show_age")
            if birthday:
                return birthday, show_age == "1"
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e
    return None, False


# Get the (user_id, birthday, show_age) of each member who shared a birthday with a guild.
# Raises StoreUnavailable.
async def get_guild_birthdays_redis(guild_id):
    birthdays = []
    try:
        user_ids = list(await redis_client.smembers(f"guild:{guild_id}:birthdays"))
        if user_ids:
            # MGET answers with one value per key, in order: each member's date, then each
            # member's opt-in.
            values = await redis_client.mget(
                [f"user:{user_id}:birthday" for user_id in user_ids]
                + [f"user:{user_id}:show_age" for user_id in user_ids]
            )
            dates, show_ages = values[: len(user_ids)], values[len(user_ids) :]
            birthdays = [
                (user_id, birthday, show_age == "1")
                for user_id, birthday, show_age in zip(user_ids, dates, show_ages, strict=True)
                if birthday
            ]
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e
    return birthdays


# Delete a member's birthday and show_age opt-in, and remove them from every guild's set.
# Returns whether anything was stored. Redis errors are raised, so a failed delete is never
# reported as done.
async def forget_birthday_redis(user_id):
    guilds_key = f"user:{user_id}:guilds"

    async def delete_all(pipe):
        guild_ids = await pipe.smembers(guilds_key)
        pipe.multi()
        pipe.delete(f"user:{user_id}:birthday", f"user:{user_id}:show_age", guilds_key)
        for guild_id in guild_ids:
            pipe.srem(f"guild:{guild_id}:birthdays", user_id)

    # transaction() WATCHes the member's list of guilds: if a /birthday set adds a guild
    # after the read, the delete is not applied and runs again with the new list.
    return any(await redis_client.transaction(delete_all, guilds_key))


# Stop sharing a member's birthday with one guild. When no other guild has it, no guild
# can see the date, so it is deleted along with the member's opt-in and list of guilds.
async def remove_birthday_from_guild_redis(guild_id, user_id):
    guilds_key = f"user:{user_id}:guilds"

    async def remove(pipe):
        other_guild_ids = await pipe.smembers(guilds_key) - {str(guild_id)}
        pipe.multi()
        pipe.srem(f"guild:{guild_id}:birthdays", user_id)
        if other_guild_ids:
            pipe.srem(guilds_key, guild_id)
        else:
            pipe.delete(f"user:{user_id}:birthday", f"user:{user_id}:show_age", guilds_key)

    # WATCHed as in forget_birthday_redis, so a guild added meanwhile keeps the date.
    await redis_client.transaction(remove, guilds_key)


# Mark a guild's post for a month ("YYYY-MM") as sent. Returns False if it was marked
# already, by an earlier run or another bot process. Raises StoreUnavailable.
async def mark_monthly_post_redis(guild_id, month):
    try:
        return bool(await redis_client.set(f"announce:{guild_id}:{month}", 1, nx=True, ex=MONTHLY_POST_MARKER_TTL))
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# Background task: every day at MONTHLY_POST_TIME, and on the 1st it posts the upcoming birthdays
@tasks.loop(time=MONTHLY_POST_TIME)
async def check_upcoming_birthdays():
    # The date in UTC, the zone of MONTHLY_POST_TIME, whatever the host's zone is.
    today = datetime.datetime.now(datetime.UTC).date()
    # Only run on the first day of the month.
    if not core.is_monthly_post_day(today):
        return

    # Each guild's post lists only the members who shared their birthday with that guild.
    for guild in client.guilds:
        channel = discord.utils.get(guild.text_channels, name="general")
        if not channel:
            continue

        try:
            birthdays = await get_guild_birthdays_redis(guild.id)
        except StoreUnavailable as e:
            # Skip this guild and go on with the others: an exception would stop the task.
            log.error("Storage unavailable in the monthly post; skipped guild %s (%s)", guild.id, e)
            continue

        upcoming = []
        # The post never shows a year or age, so it ignores the show_age opt-in.
        for user_id, birthday_str, _ in birthdays:
            try:
                month, day, _ = core.parse_birthday(birthday_str)
                upcoming_bd = core.monthly_post_occurrence(month, day, today)
                if upcoming_bd is not None:
                    upcoming.append((upcoming_bd, user_id))
            except Exception as e:  # noqa: BLE001 - one bad stored date must not stop the post; #37 narrows this
                print(f"❌ Error processing birthday for user {user_id}: {e}")

        if not upcoming:
            continue

        # Marked before sending, so a restart or a second process never posts again this month.
        # A send that fails is not retried.
        try:
            if not await mark_monthly_post_redis(guild.id, core.month_key(today)):
                continue
        except StoreUnavailable as e:
            log.error("Storage unavailable in the monthly post; skipped guild %s (%s)", guild.id, e)
            continue

        # Soonest first. A public post: the month and day, never the birth year or age.
        lines = [f"<@{user_id}> - {core.format_occurrence(upcoming_bd)}" for upcoming_bd, user_id in sorted(upcoming)]
        try:
            for chunk in core.chunk_messages("🎉 **Upcoming Birthdays:**", lines):
                await channel.send(chunk)
        except Exception as e:  # noqa: BLE001 - a failed send must not stop the other guilds' posts; #37 narrows this
            print(f"❌ Error sending upcoming birthdays message in {guild.name}: {e}")


# Before READY the bot has no guilds, so a run would post nowhere. A bot that becomes ready
# after MONTHLY_POST_TIME has missed that day's run, so it runs once now: on the 1st that is
# the month's post, and the guilds that already had it are marked and get no second one.
@check_upcoming_birthdays.before_loop
async def before_check_upcoming_birthdays():
    await client.wait_until_ready()
    if datetime.datetime.now(datetime.UTC).timetz() > MONTHLY_POST_TIME:
        try:
            await check_upcoming_birthdays()
        except Exception as e:  # noqa: BLE001 - any error would end the task; see the comment below
            # Raised here, the error would end the task before its first scheduled run.
            log.error("The monthly post at startup failed (%s)", type(e).__name__)


# Subclassing Client to use app commands (slash commands)
class MyClient(discord.Client):
    def __init__(self):
        # Messages echo text that members control, such as display names: never let them ping anyone.
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if DEV_GUILD_ID:
            dev_guild = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=dev_guild)
            await self.tree.sync(guild=dev_guild)
            print(f"✅ Slash commands synced to the development server {DEV_GUILD_ID}.")
        # A global sync replaces the commands in every server, so a build that is missing a
        # command deletes it everywhere: it runs only when asked.
        if SYNC_COMMANDS:
            await self.tree.sync()
            print("✅ Slash commands synced globally.")
        # Start the upcoming birthdays task after commands are synced.
        check_upcoming_birthdays.start()


# Instantiate the client
client = MyClient()


# Event listener for when the bot has connected
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")


# Called when the bot is removed from a guild: kicked, banned or left, or the guild was
# deleted. Nobody there can see the guild's birthdays any more.
@client.event
async def on_guild_remove(guild):
    # Removing every member empties the guild's set, and Redis deletes an empty set.
    for user_id in await redis_client.smembers(f"guild:{guild.id}:birthdays"):
        await remove_birthday_from_guild_redis(guild.id, user_id)


# The /birthday commands. They replaced /set_birthday, /get_birthday, /list_birthdays,
# /forecast_birthdays and /forget_birthday. A group's subcommands cannot have settings of their
# own, so the group's apply to all five: Discord offers them in servers only (contexts, and
# dm_permission for older clients), never in DMs. Every reply is ephemeral: only the member
# who runs a command sees it.
birthday_group = app_commands.Group(
    name="birthday",
    description="Set, show and list the birthdays shared with this server",
    guild_only=True,
    allowed_contexts=app_commands.AppCommandContext(guild=True),
)

# The 12 months /birthday set offers: Discord shows the name and sends the month's number.
MONTH_CHOICES = [app_commands.Choice(name=calendar.month_name[month], value=month) for month in range(1, 13)]


# Set a birthday. Discord accepts only these months, days 1 to 31 and years 1900 to
# LATEST_BIRTH_YEAR, so a date such as Feb 30, Apr 31 or a future date is refused here.
@birthday_group.command(name="set", description="Set your birthday and share it with this server")
@app_commands.describe(
    month="The month of your birthday",
    day="The day of the month",
    year="The year you were born, if you want to give it. Others see it only with show_age",
    show_age="Let others see your birth year and age, if you give the year. Off by default",
)
@app_commands.choices(month=MONTH_CHOICES)
async def birthday_set(
    interaction: discord.Interaction,
    month: int,
    day: app_commands.Range[int, 1, 31],
    year: app_commands.Range[int, 1900, LATEST_BIRTH_YEAR] | None = None,
    show_age: bool = False,
):
    await interaction.response.defer(ephemeral=True)  # Prevent Discord timeout
    try:
        # Without a year, core.LEAP_YEAR stands in, so Feb 29 is a date.
        birthday_date = core.birthday_as_date(month, day, year)
    except ValueError:
        await interaction.followup.send(NO_SUCH_DATE, ephemeral=True)
        return
    if year is not None:
        # A date no one could be born on is refused, not stored.
        error = core.birth_date_error(birthday_date, datetime.date.today())  # noqa: DTZ011 - the host's date
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
    # "YYYY-MM-DD", or ISO 8601's date without a year, "--MM-DD"
    birthday = core.stored_birthday(birthday_date, with_year=year is not None)
    try:
        # Stored and shared with this server
        await set_birthday_redis(interaction.guild_id, interaction.user.id, birthday, show_age)
    except StoreUnavailable as e:
        log.error("Storage unavailable in /birthday set (%s)", e)
        await interaction.followup.send(SAVE_FAILED, ephemeral=True)
        return
    if year is not None and show_age:
        shown = "Others see the month and day, and your birth year and age."
    else:
        shown = "Others see only the month and day."
    await interaction.followup.send(
        f"✅ Your birthday has been set to {core.format_birthday(month, day, year)}. {shown}", ephemeral=True
    )


# Show a member's birthday
@birthday_group.command(name="show", description="Show a member's birthday, if they shared it with this server")
@app_commands.describe(member="The member whose birthday you want to see")
async def birthday_show(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)  # Answer before reading Redis
    user_id = member.id
    try:
        birthday_str, show_age = await get_birthday_redis(interaction.guild_id, user_id)
    except StoreUnavailable as e:
        log.error("Storage unavailable in /birthday show (%s)", e)
        await interaction.followup.send(READ_FAILED, ephemeral=True)
        return
    if birthday_str:
        try:
            # Parse the stored birthday. The year and age show only if the member opted in.
            month, day, year = core.parse_birthday(birthday_str)
            shown_year = year if show_age else None
            # Compute the next birthday, today or later
            today = datetime.date.today()  # noqa: DTZ011 - the host's date
            next_birthday = core.next_occurrence(month, day, today)
            age = "" if shown_year is None else f", turning {core.age_on(shown_year, next_birthday)}"

            await interaction.followup.send(
                f"🎂 **{member.mention}'s Birthday:**\n"
                f"{core.format_birthday(month, day, shown_year)} - "
                f"next birthday {core.format_occurrence(next_birthday, with_year=True)}{age}",
                ephemeral=True,
            )
        except Exception as e:  # noqa: BLE001 - the member gets an error reply, not silence; #37 narrows this
            print(f"❌ Error processing birthday for user {user_id}: {e}")
            await interaction.followup.send("❌ An error occurred while retrieving the birthday.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {member.mention} has not set their birthday yet.", ephemeral=True)


# List this server's birthdays
@birthday_group.command(name="list", description="List the birthdays shared with this server")
async def birthday_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Prevent timeout while fetching data
    try:
        birthdays = await get_guild_birthdays_redis(interaction.guild_id)
    except StoreUnavailable as e:
        log.error("Storage unavailable in /birthday list (%s)", e)
        await interaction.followup.send(READ_FAILED, ephemeral=True)
        return

    # One line per member. <@id> shows the member's name without the member cache.
    lines = []
    today = datetime.date.today()  # noqa: DTZ011 - the host's date
    for user_id, birthday_str, show_age in birthdays:
        try:
            # Parse the stored birthday. The year shows only if the member opted in.
            month, day, year = core.parse_birthday(birthday_str)
            # Compute the next birthday, today or later
            next_birthday = core.next_occurrence(month, day, today)

            lines.append(
                f"<@{user_id}> - {core.format_birthday(month, day, year if show_age else None)} - "
                f"next birthday {core.format_occurrence(next_birthday, with_year=True)}"
            )
        except Exception as e:  # noqa: BLE001 - one bad stored date must not stop the list; #37 narrows this
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if lines:
        for chunk in core.chunk_messages("🎉 **Server Birthdays:**", lines):
            await interaction.followup.send(chunk, ephemeral=True)
    else:
        await interaction.followup.send("❌ No birthdays have been set yet.", ephemeral=True)


# Show the upcoming birthdays
@birthday_group.command(
    name="upcoming", description=f"Show the birthdays in the next {UPCOMING_DAYS} days, or in the days you choose"
)
@app_commands.describe(
    days=f"How many days ahead to look, from 1 to {UPCOMING_MAX_DAYS} ({UPCOMING_DAYS} if you leave it out)"
)
async def birthday_upcoming(
    interaction: discord.Interaction, days: app_commands.Range[int, 1, UPCOMING_MAX_DAYS] = UPCOMING_DAYS
):
    await interaction.response.defer(ephemeral=True)  # Prevent timeout while fetching data
    today = datetime.date.today()  # noqa: DTZ011 - the host's date
    try:
        birthdays = await get_guild_birthdays_redis(interaction.guild_id)
    except StoreUnavailable as e:
        log.error("Storage unavailable in /birthday upcoming (%s)", e)
        await interaction.followup.send(READ_FAILED, ephemeral=True)
        return
    upcoming_birthdays = []

    for user_id, birthday_str, show_age in birthdays:
        try:
            month, day, year = core.parse_birthday(birthday_str)
            # The next birthday, if it is 0 to `days` days away
            upcoming_bd = core.upcoming_occurrence(month, day, today, days)
            if upcoming_bd is not None:
                # The age on the next birthday, only if the member opted in and gave a year
                age = core.age_on(year, upcoming_bd) if show_age and year is not None else None
                upcoming_birthdays.append((user_id, upcoming_bd, age))
        except Exception as e:  # noqa: BLE001 - one bad stored date must not stop the list; #37 narrows this
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if upcoming_birthdays:
        # One line per member. <@id> shows the member's name without the member cache.
        lines = [
            f"<@{user_id}> - {core.format_occurrence(upcoming_bd)}" + ("" if age is None else f" - turning {age}")
            for user_id, upcoming_bd, age in upcoming_birthdays
        ]
        for chunk in core.chunk_messages("🎉 **Upcoming Birthdays:**", lines):
            await interaction.followup.send(chunk, ephemeral=True)
    else:
        await interaction.followup.send(
            f"❌ No birthdays in the next {days} {'day' if days == 1 else 'days'}.", ephemeral=True
        )


# Delete your birthday from every server
@birthday_group.command(name="forget", description="Delete your birthday from every server")
async def birthday_forget(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Answer before reading Redis
    try:
        forgotten = await forget_birthday_redis(interaction.user.id)
    except redis.RedisError as e:
        print(f"❌ Error deleting a birthday: {e}")
        await interaction.followup.send(
            "❌ Your birthday could not be deleted. Please try again later.", ephemeral=True
        )
        return
    if forgotten:
        await interaction.followup.send("✅ Your birthday has been deleted from every server.", ephemeral=True)
    else:
        await interaction.followup.send(
            "✅ You had no birthday stored, so there was nothing to delete.", ephemeral=True
        )


# The group and its five subcommands are the bot's only slash commands.
client.tree.add_command(birthday_group)


# Send Redis a PING at startup. It goes through a client of its own, whose connections are
# closed here, because redis.asyncio connections belong to the event loop that opened them,
# and client.run() then starts a new loop for the bot.
async def ping_redis():
    startup_client = create_redis_client()
    try:
        await startup_client.ping()
    finally:
        await startup_client.aclose(close_connection_pool=True)


def main(argv=()):
    global TOKEN, REDIS_URL, DEV_GUILD_ID, SYNC_COMMANDS, redis_client

    parser = argparse.ArgumentParser(description="Run the birthday-boi Discord bot.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="replace the slash commands in every server with this build's; run once after a release that changes them",
    )
    SYNC_COMMANDS = parser.parse_args(argv).sync

    # If running on Heroku, DYNO will be set; otherwise load .env for local testing.
    if os.getenv("DYNO"):
        TOKEN = os.getenv("DISCORD_TOKEN")
        REDIS_URL = os.getenv("REDIS_URL")
    else:
        load_dotenv()
        TOKEN = os.getenv("DISCORD_TOKEN")
        REDIS_URL = os.getenv("REDIS_URL")

    # Stop before anything is contacted. An empty value counts as missing: a .env copied from
    # .env.example has DISCORD_TOKEN= until the token is filled in.
    missing = [name for name, value in (("DISCORD_TOKEN", TOKEN), ("REDIS_URL", REDIS_URL)) if not value]
    if missing:
        names = " and ".join(missing)
        sys.exit(f"Cannot start: {names} must be set, in .env or in the environment (see .env.example).")

    DEV_GUILD_ID = None
    if os.getenv("DEV_GUILD_ID"):
        try:
            DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID"))
        except ValueError:
            sys.exit("Cannot start: DEV_GUILD_ID must be a server ID, a number.")

    # Initialize Redis client
    redis_client = create_redis_client()

    # Stop here, before logging in to Discord, if Redis does not answer.
    try:
        asyncio.run(ping_redis())
    except redis.RedisError as e:
        sys.exit(
            "Cannot start: Redis did not answer a PING. Check REDIS_URL and that Redis is running. "
            f"{type(e).__name__}: {e}"
        )

    # Run the bot
    client.run(TOKEN)


if __name__ == "__main__":
    main(sys.argv[1:])
