import argparse
import os
import redis.asyncio
import ssl
from discord.ext import tasks
from discord import app_commands
import discord
import aiohttp
from dotenv import load_dotenv
import datetime
import calendar
import asyncio
import logging
import pathlib
import sys
import time
from urllib.parse import urlparse

import core

# Read from the environment by main(), so importing this module reads no config.
TOKEN = None
REDIS_URL = None
# DEV_GUILD_ID: a test server, where the commands are synced on every start and show up at once.
DEV_GUILD_ID = None
# Set by --sync: replace the bot's commands in every server with this build's at startup.
SYNC_COMMANDS = False
# The file the heartbeat task touches, and `--healthcheck` reads: HEARTBEAT_FILE, or
# DEFAULT_HEARTBEAT_FILE when that is unset or empty.
HEARTBEAT_FILE = None


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

# A guild's daily greeting is marked in Redis for this long: past the end of its date in every
# time zone, from UTC+14 to UTC-12, even if the guild changes its time zone that day.
GREETING_MARKER_TTL = datetime.timedelta(days=3)

# The bot's log, by a name of its own: run as `python bot.py`, __name__ would be "__main__". A log
# line names members and servers by ID and errors by type, never with a birth date or a name.
log = logging.getLogger("birthday_boi")

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


# Discord answers a Request Guild Members by user_ids with at most this many members.
MEMBER_QUERY_LIMIT = 100
# Discord's JSON error code "Unknown Member", which Get Guild Member's 404 carries for a member
# who is not in the server. Others, such as 10004 "Unknown Guild", say nothing about the member.
UNKNOWN_MEMBER = 10007


# Which of these stored IDs belong to members who left the guild (#74), as a set of strings.
# Each one is removed from the guild with remove_birthday_from_guild_redis, which deletes the
# birthday outright when no other guild has it (owner's decision, 2026-10-20). Without the members
# intent the bot gets no event when a member leaves, so a view calls this just before it shows the
# IDs. Request Guild Members by user_ids needs no privileged intent. An ID missing from its answer
# proves nothing: a READY after a reconnect answers a pending request with no members. So each
# missing ID is looked up with Get Guild Member, and only Discord's 404 "Unknown Member" counts
# as having left. Any other error stops the check: the IDs not yet checked count as members, and
# nothing more is deleted. Raises nothing, so it never ends a task loop.
async def departed_members(guild, user_ids):
    departed = set()
    user_ids = [str(user_id) for user_id in user_ids]
    if guild is None or not user_ids:
        return departed
    try:
        for start in range(0, len(user_ids), MEMBER_QUERY_LIMIT):
            batch = user_ids[start : start + MEMBER_QUERY_LIMIT]
            # discord.py asks for 5 members unless given a limit. cache=False: without the members
            # intent, a cached member is never removed when they leave.
            found = await guild.query_members(
                user_ids=[int(user_id) for user_id in batch], limit=len(batch), cache=False
            )
            found_ids = {str(member.id) for member in found}
            for user_id in batch:
                if user_id in found_ids:
                    continue
                try:
                    await guild.fetch_member(int(user_id))
                except discord.NotFound as e:
                    if e.code != UNKNOWN_MEMBER:
                        raise
                else:
                    continue  # still a member
                departed.add(user_id)
                try:
                    await remove_birthday_from_guild_redis(guild.id, user_id)
                except redis.RedisError as e:
                    # Hidden all the same; the next check tries the delete again.
                    log.error("Failed to delete user %s, who left guild %s (%s)", user_id, guild.id, type(e).__name__)
    # The request timed out (TimeoutError), the bot has no websocket (RuntimeError) or it is
    # closed (ConnectionClosed); or the lookup failed (HTTPException, or a connection error).
    except (
        TimeoutError,
        RuntimeError,
        discord.ConnectionClosed,
        discord.HTTPException,
        aiohttp.ClientError,
        OSError,
    ) as e:
        log.error("Member check stopped in guild %s; unchecked members are shown (%s)", guild.id, type(e).__name__)
    return departed


# Mark a guild's post for a month ("YYYY-MM") as sent. Returns False if it was marked
# already, by an earlier run or another bot process. Raises StoreUnavailable.
async def mark_monthly_post_redis(guild_id, month):
    try:
        return bool(await redis_client.set(f"announce:{guild_id}:{month}", 1, nx=True, ex=MONTHLY_POST_MARKER_TTL))
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# Turn on a guild's daily greetings, or change them: the hash guild:<id>:greetings holds the
# channel's ID, the time of day ("HH:MM") and the IANA time zone. Raises StoreUnavailable.
async def set_greeting_settings_redis(guild_id, channel_id, time_of_day, zone_name):
    try:
        await redis_client.hset(
            f"guild:{guild_id}:greetings", mapping={"channel": channel_id, "time": time_of_day, "timezone": zone_name}
        )
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# A guild's daily greeting settings, {"channel": ..., "time": ..., "timezone": ...}, or None while
# its greetings are off. Raises StoreUnavailable.
async def get_greeting_settings_redis(guild_id):
    try:
        return await redis_client.hgetall(f"guild:{guild_id}:greetings") or None
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# Turn off a guild's daily greetings: delete its settings. Raises StoreUnavailable.
async def delete_greeting_settings_redis(guild_id):
    try:
        await redis_client.delete(f"guild:{guild_id}:greetings")
    except redis.RedisError as e:
        raise StoreUnavailable(type(e).__name__) from e


# Mark a guild's daily greeting for a date, the guild's local date, as sent. Returns False if it
# was marked already, by an earlier run, before a restart or by another bot process. Raises
# StoreUnavailable.
async def mark_greeting_redis(guild_id, date):
    try:
        key = f"greeted:{guild_id}:{core.day_key(date)}"
        return bool(await redis_client.set(key, 1, nx=True, ex=GREETING_MARKER_TTL))
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
            except ValueError as e:  # a stored date that cannot be read; it must not stop the post
                log.error(
                    "Skipped user %s in the monthly post of guild %s: unreadable birthday (%s)",
                    user_id,
                    guild.id,
                    type(e).__name__,
                )

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

        # Members who left are not shown, and deleted (#74). Checked after the marker, so once a month.
        departed = await departed_members(guild, [user_id for _, user_id in upcoming])
        upcoming = [(upcoming_bd, user_id) for upcoming_bd, user_id in upcoming if user_id not in departed]
        if not upcoming:  # everyone in range left: no bare header
            continue

        # Soonest first. A public post: the month and day, never the birth year or age.
        lines = [f"<@{user_id}> - {core.format_occurrence(upcoming_bd)}" for upcoming_bd, user_id in sorted(upcoming)]
        try:
            for chunk in core.chunk_messages("🎉 **Upcoming Birthdays:**", lines):
                await channel.send(chunk)
        # Discord refused the post (HTTPException, such as Forbidden without permission to write in
        # #general), or the connection failed (an aiohttp error or an OSError, which discord.py's HTTP
        # client raises as they come, after a few retries for a reset connection). The other guilds
        # still get their posts.
        except (discord.HTTPException, aiohttp.ClientError, OSError) as e:
            log.error("The monthly post failed to send in guild %s (%s)", guild.id, type(e).__name__)


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


# Background task: the daily greetings (#34), in the servers that turned them on with
# /birthday config. Each server's greeting goes out at the first run at or after its time of day
# in its time zone, so up to 5 minutes late, or later that day after a restart.
@tasks.loop(minutes=5)
async def send_birthday_greetings():
    now = datetime.datetime.now(datetime.UTC)
    for guild in client.guilds:
        await greet_birthdays(guild, now)


# Before READY the bot has no guilds, so a run would greet nowhere. The first run is at READY.
@send_birthday_greetings.before_loop
async def before_send_birthday_greetings():
    await client.wait_until_ready()


# Post a guild's greeting for its local date at `now`, once that date's greeting time has come, if
# the guild turned greetings on and members who shared their birthday with it have it that day.
# Errors are logged and skip only this guild: an exception would end the task.
async def greet_birthdays(guild, now):
    try:
        settings = await get_greeting_settings_redis(guild.id)
    except StoreUnavailable as e:
        log.error("Storage unavailable in the daily greetings; skipped guild %s (%s)", guild.id, e)
        return
    if settings is None:  # greetings are off here
        return
    channel_id = settings.get("channel", "")
    greeting_time = core.parse_time_of_day(settings.get("time", ""))
    zone = core.time_zone(settings.get("timezone", ""))
    if not channel_id.isdigit() or greeting_time is None or zone is None:
        # Such as a time zone that an update of the host's time zone database removed.
        log.error("Unusable greeting settings in guild %s; it gets no greeting until /birthday config", guild.id)
        return
    date = core.greeting_date(now, greeting_time, zone)
    if date is None:  # its greeting time has not come yet today
        return

    try:
        birthdays = await get_guild_birthdays_redis(guild.id)
    except StoreUnavailable as e:
        log.error("Storage unavailable in the daily greetings; skipped guild %s (%s)", guild.id, e)
        return
    # The greeting never shows a year or age, so it ignores the show_age opt-in.
    user_ids = []
    for user_id, birthday, _ in birthdays:
        try:
            month, day, _ = core.parse_birthday(birthday)
        except ValueError:
            log.error("Skipped user %s in the daily greeting of guild %s: unreadable birthday", user_id, guild.id)
            continue
        if core.falls_on(month, day, date):
            user_ids.append(int(user_id))
    if not user_ids:
        return

    # Marked before sending, as the monthly post is: a restart or a second process never greets
    # the date again, and a send that fails is not retried.
    try:
        if not await mark_greeting_redis(guild.id, date):
            return
    except StoreUnavailable as e:
        log.error("Storage unavailable in the daily greetings; skipped guild %s (%s)", guild.id, e)
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        log.error("The greeting channel of guild %s is gone; no greeting today", guild.id)
        return
    # Members who left are not greeted, and deleted (#74). Checked after the marker, so once a day.
    departed = await departed_members(guild, user_ids)
    user_ids = [user_id for user_id in user_ids if str(user_id) not in departed]
    if not user_ids:  # everyone with a birthday today left: no greeting
        return
    # One line per member. Each message pings the members it names and nobody else: the client's
    # AllowedMentions.none() stays in force for everything else.
    lines = [f"🎂 Happy birthday, <@{user_id}>!" for user_id in sorted(user_ids)]
    try:
        for chunk in core.chunk_messages(lines[0], lines[1:]):
            named = [discord.Object(id=user_id) for user_id in sorted(user_ids) if f"<@{user_id}>" in chunk]
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions(users=named))
    except discord.HTTPException as e:
        log.error("The daily greeting in guild %s failed to send (%s)", guild.id, type(e).__name__)


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
            log.info("Slash commands synced to the development server %s", DEV_GUILD_ID)
        # A global sync replaces the commands in every server, so a build that is missing a
        # command deletes it everywhere: it runs only when asked.
        if SYNC_COMMANDS:
            await self.tree.sync()
            log.info("Slash commands synced globally")
        # Start the task loops after commands are synced.
        check_upcoming_birthdays.start()
        heartbeat.start()
        send_birthday_greetings.start()


# Instantiate the client
client = MyClient()


# Event listener for when the bot has connected
@client.event
async def on_ready():
    log.info("Logged in as %s (ID %s)", client.user, client.user.id)


# Called when the bot is removed from a guild: kicked, banned or left, or the guild was
# deleted. Nobody there can see the guild's birthdays any more, and it gets no more greetings.
@client.event
async def on_guild_remove(guild):
    await delete_greeting_settings_redis(guild.id)
    # Removing every member empties the guild's set, and Redis deletes an empty set.
    for user_id in await redis_client.smembers(f"guild:{guild.id}:birthdays"):
        await remove_birthday_from_guild_redis(guild.id, user_id)


# The /birthday commands. They replaced /set_birthday, /get_birthday, /list_birthdays,
# /forecast_birthdays and /forget_birthday. A group's subcommands cannot have settings of their
# own, so the group's apply to all six: Discord offers them in servers only (contexts, and
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
        except ValueError as e:  # a stored date that cannot be read; the member gets an error reply, not silence
            log.error(
                "Unreadable birthday of user %s in /birthday show in guild %s (%s)",
                user_id,
                interaction.guild_id,
                type(e).__name__,
            )
            await interaction.followup.send("❌ An error occurred while retrieving the birthday.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {member.mention} has not set their birthday yet.", ephemeral=True)


# A list's lines as the embeds of its reply, one per message, under the title on the first
# (core.chunk_embeds keeps each within Discord's limits). The client's AllowedMentions.none() goes
# with every message, so the <@id> mentions ping nobody.
def embeds_of(title, lines):
    return [
        discord.Embed(title=embed_title, description=description)
        for embed_title, description in core.chunk_embeds(title, lines)
    ]


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

    # One line per member, as (user_id, line). <@id> shows the member's name without the member cache.
    rows = []
    today = datetime.date.today()  # noqa: DTZ011 - the host's date
    for user_id, birthday_str, show_age in birthdays:
        try:
            # Parse the stored birthday. The year shows only if the member opted in.
            month, day, year = core.parse_birthday(birthday_str)
            # Compute the next birthday, today or later
            next_birthday = core.next_occurrence(month, day, today)

            rows.append(
                (
                    user_id,
                    f"<@{user_id}> - {core.format_birthday(month, day, year if show_age else None)} - "
                    f"next birthday {core.format_occurrence(next_birthday, with_year=True)}",
                )
            )
        except ValueError as e:  # a stored date that cannot be read; it must not stop the list
            log.error(
                "Skipped user %s in /birthday list in guild %s: unreadable birthday (%s)",
                user_id,
                interaction.guild_id,
                type(e).__name__,
            )

    # Members who left are not shown, and deleted (#74).
    departed = await departed_members(interaction.guild, [user_id for user_id, _ in rows])
    lines = [line for user_id, line in rows if user_id not in departed]
    if lines:
        for embed in embeds_of("🎉 Server Birthdays", lines):
            await interaction.followup.send(embed=embed, ephemeral=True)
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
        except ValueError as e:  # a stored date that cannot be read; it must not stop the list
            log.error(
                "Skipped user %s in /birthday upcoming in guild %s: unreadable birthday (%s)",
                user_id,
                interaction.guild_id,
                type(e).__name__,
            )

    # Members who left are not shown, and deleted (#74).
    departed = await departed_members(interaction.guild, [user_id for user_id, _, _ in upcoming_birthdays])
    upcoming_birthdays = [row for row in upcoming_birthdays if row[0] not in departed]
    if upcoming_birthdays:
        # One line per member. <@id> shows the member's name without the member cache.
        lines = [
            f"<@{user_id}> - {core.format_occurrence(upcoming_bd)}" + ("" if age is None else f" - turning {age}")
            for user_id, upcoming_bd, age in upcoming_birthdays
        ]
        for embed in embeds_of("🎉 Upcoming Birthdays", lines):
            await interaction.followup.send(embed=embed, ephemeral=True)
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
        log.error("Storage unavailable in /birthday forget (%s)", type(e).__name__)
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


# What /birthday config answers.
CONFIG_NOT_ALLOWED = "❌ Only members with the Manage Server permission can change the birthday greetings."
CONFIG_INCOMPLETE = (
    "❌ To turn on the daily birthday greetings, give the channel, the time and the time zone. "
    "To turn them off, set off to True."
)
CONFIG_BAD_TIME = "❌ The time must be 24-hour HH:MM, such as 09:00 or 18:30."
CONFIG_BAD_ZONE = "❌ Unknown time zone. Use a name from the tz database, such as Europe/Berlin or America/New_York."
CONFIG_OFF = "✅ The daily birthday greetings are off in this server."
CONFIG_FAILED = "⚠️ Storage is unavailable, so the greeting settings were not changed. Please try again later."


# Turn the daily greetings on, change them or turn them off. Discord takes default member
# permissions only for a whole top-level command, and discord.py ignores default_permissions on a
# subcommand, so every member sees this one and the handler refuses those without Manage Server.
# Discord sends the member's permissions in the channel with each interaction; an owner or an
# administrator has them all.
@birthday_group.command(
    name="config", description="Turn this server's daily birthday greetings on or off (Manage Server only)"
)
@app_commands.describe(
    channel="The channel for the greetings",
    time_of_day="The time to post them each day, 24-hour HH:MM, such as 09:00",
    zone_name="The time zone of that time, such as Europe/Berlin or America/New_York",
    off="Turn the greetings off",
)
@app_commands.rename(time_of_day="time", zone_name="timezone")
async def birthday_config(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    time_of_day: str | None = None,
    zone_name: str | None = None,
    off: bool = False,
):
    await interaction.response.defer(ephemeral=True)  # Answer before reading Redis
    if not interaction.permissions.manage_guild:
        await interaction.followup.send(CONFIG_NOT_ALLOWED, ephemeral=True)
        return
    if off:
        try:
            await delete_greeting_settings_redis(interaction.guild_id)
        except StoreUnavailable as e:
            log.error("Storage unavailable in /birthday config (%s)", e)
            await interaction.followup.send(CONFIG_FAILED, ephemeral=True)
            return
        await interaction.followup.send(CONFIG_OFF, ephemeral=True)
        return
    if channel is None or time_of_day is None or zone_name is None:
        await interaction.followup.send(CONFIG_INCOMPLETE, ephemeral=True)
        return
    greeting_time = core.parse_time_of_day(time_of_day)
    if greeting_time is None:
        await interaction.followup.send(CONFIG_BAD_TIME, ephemeral=True)
        return
    zone = core.time_zone(zone_name)
    if zone is None:
        await interaction.followup.send(CONFIG_BAD_ZONE, ephemeral=True)
        return
    shown_time = core.format_time_of_day(greeting_time)
    try:
        await set_greeting_settings_redis(interaction.guild_id, channel.id, shown_time, zone.key)
    except StoreUnavailable as e:
        log.error("Storage unavailable in /birthday config (%s)", e)
        await interaction.followup.send(CONFIG_FAILED, ephemeral=True)
        return
    await interaction.followup.send(
        f"✅ Every day at {shown_time} ({zone.key}), the bot wishes a happy birthday in {channel.mention} to the "
        "members who shared their birthday with this server and have it that day. It pings them, and shows no "
        "year or age.",
        ephemeral=True,
    )


# The group and its six subcommands are the bot's only slash commands.
client.tree.add_command(birthday_group)

# What a member is told when a command fails with an error it does not handle.
COMMAND_FAILED = "❌ Something went wrong. Please try again later."


# discord.py calls this when a slash command raises an error, in place of its own handler, which
# logs the traceback: the messages there can quote a stored birthday (see StoreUnavailable). This
# logs one line, with the command, the server's ID and the error's type, and tells the member, in a
# reply only they see. A command that has already deferred or replied gets a follow-up.
@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original  # the error the command's code raised
    command = interaction.command.qualified_name if interaction.command is not None else None
    log.error("Command failed: command=%r guild=%s error=%s", command, interaction.guild_id, type(error).__name__)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(COMMAND_FAILED, ephemeral=True)
        else:
            await interaction.response.send_message(COMMAND_FAILED, ephemeral=True)
    except discord.HTTPException as e:  # such as NotFound once the interaction has expired
        log.error("Error reply failed: command=%r guild=%s error=%s", command, interaction.guild_id, type(e).__name__)


# The heartbeat, for the Docker HEALTHCHECK (#35): a gateway bot has no port to probe, so the
# heartbeat task touches HEARTBEAT_FILE every HEARTBEAT_INTERVAL while the bot is connected to
# Discord, and `python bot.py --healthcheck` fails once the file is HEARTBEAT_MAX_AGE old.
HEARTBEAT_INTERVAL = datetime.timedelta(seconds=60)
HEARTBEAT_MAX_AGE = 3 * HEARTBEAT_INTERVAL
# Under /tmp, which the image's unprivileged user can write.
DEFAULT_HEARTBEAT_FILE = "/tmp/birthday-boi.heartbeat"


@tasks.loop(seconds=HEARTBEAT_INTERVAL.total_seconds())
async def heartbeat():
    # Only while ready and connected: discord.py keeps is_ready() through a reconnect, so the
    # gateway's websocket must be open too. A bot that is not connected leaves the file to age.
    if client.is_ready() and client.ws is not None and client.ws.open:
        await asyncio.to_thread(pathlib.Path(HEARTBEAT_FILE).touch)


# How many seconds ago the heartbeat file was touched, or None if it cannot be read.
def heartbeat_age(path):
    try:
        return time.time() - pathlib.Path(path).stat().st_mtime
    except OSError:
        return None


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
    global TOKEN, REDIS_URL, DEV_GUILD_ID, SYNC_COMMANDS, HEARTBEAT_FILE, redis_client

    parser = argparse.ArgumentParser(description="Run the birthday-boi Discord bot.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="replace the slash commands in every server with this build's; run once after a release that changes them",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 if the heartbeat file is fresh, or 1 if it is missing or stale; the image's HEALTHCHECK",
    )
    args = parser.parse_args(argv)
    SYNC_COMMANDS = args.sync

    # Log lines go to stderr, this module's and discord.py's alike, in discord.py's format, from
    # INFO up. basicConfig() does nothing where logging is set up already, as under pytest, and
    # client.run() below is told not to add a handler of its own, which would repeat its lines.
    logging.basicConfig(
        level=logging.INFO,
        format="[{asctime}] [{levelname:<8}] {name}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{",
    )

    # The settings come from the environment, after load_dotenv() adds those in a .env file if
    # there is one. It never overrides a variable that is already set, so in a container the
    # env_file's values win; the image has no .env (.dockerignore).
    load_dotenv()
    HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE
    if args.healthcheck:
        age = heartbeat_age(HEARTBEAT_FILE)
        if age is None or age >= HEARTBEAT_MAX_AGE.total_seconds():
            limit = HEARTBEAT_MAX_AGE.total_seconds()
            state = "is missing or unreadable" if age is None else f"is {age:.0f} s old, over the {limit:.0f} s limit"
            log.error("Unhealthy: the heartbeat file %s %s.", HEARTBEAT_FILE, state)
            sys.exit(1)
        log.info("Healthy: the heartbeat file %s is %.0f s old.", HEARTBEAT_FILE, age)
        sys.exit(0)
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

    # Run the bot. Logging is set up above.
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main(sys.argv[1:])
