import os
import redis.asyncio
import ssl
from discord.ext import commands, tasks
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime
import calendar
import asyncio
from urllib.parse import urlparse
import spacy  # Added spaCy for enhanced message extraction

# Load spaCy's English model
nlp = spacy.load("en_core_web_sm")

# Read from the environment by main(), so importing this module reads no config.
TOKEN = None
REDIS_URL = None

# Function to initialize Redis with timeouts. A rediss:// URL checks the server's
# certificate and hostname; REDIS_TLS_INSECURE=1 turns both checks off, for servers
# with self-signed certificates such as Heroku's:
# https://devcenter.heroku.com/articles/connecting-heroku-redis
def create_redis_client():
    tls_options = {}
    if urlparse(REDIS_URL).scheme == "rediss" and os.getenv("REDIS_TLS_INSECURE") == "1":
        tls_options["ssl_cert_reqs"] = ssl.CERT_NONE
    return redis.asyncio.from_url(
        REDIS_URL,
        decode_responses=True,  # Ensures Redis returns strings instead of bytes
        socket_timeout=10,
        socket_connect_timeout=10,
        retry_on_timeout=True,
        **tls_options
    )

# Created by main(); tests replace it with a fake.
redis_client = None

# Discord rejects messages longer than this.
MESSAGE_LIMIT = 2000

# /forecast_birthdays lists the birthdays from today to this many days ahead.
FORECAST_DAYS = 90

# A birth year may be at most this many years before the current year.
MAX_AGE_YEARS = 120

# Store a birthday and share it with the guild where it was set, with error handling
async def set_birthday_redis(guild_id, user_id, birthday):
    try:
        # One transaction, so the date, the guild's membership and the member's list
        # of guilds (which /forget_birthday reads) are saved together.
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.set(f"user:{user_id}:birthday", birthday)
            pipe.sadd(f"guild:{guild_id}:birthdays", user_id)
            pipe.sadd(f"user:{user_id}:guilds", guild_id)
            await pipe.execute()
    except redis.RedisError as e:
        print(f"❌ Error setting birthday for user {user_id}: {e}")

# Get a member's birthday if they shared it with this guild, with error handling
async def get_birthday_redis(guild_id, user_id):
    try:
        if await redis_client.sismember(f"guild:{guild_id}:birthdays", user_id):
            birthday = await redis_client.get(f"user:{user_id}:birthday")
            if birthday:
                return birthday
    except redis.RedisError as e:
        print(f"❌ Error getting birthday for user {user_id}: {e}")
    return None

# Get the (user_id, birthday) pairs shared with a guild, with error handling
async def get_guild_birthdays_redis(guild_id):
    birthdays = []
    try:
        user_ids = list(await redis_client.smembers(f"guild:{guild_id}:birthdays"))
        if user_ids:
            dates = await redis_client.mget([f"user:{user_id}:birthday" for user_id in user_ids])
            birthdays = [(user_id, birthday) for user_id, birthday in zip(user_ids, dates) if birthday]
    except redis.RedisError as e:
        print(f"❌ Error retrieving birthdays for guild {guild_id}: {e}")
    return birthdays

# Delete a member's birthday and remove them from every guild's set. Returns whether
# anything was stored. Redis errors are raised, so a failed delete is never reported as done.
async def forget_birthday_redis(user_id):
    guilds_key = f"user:{user_id}:guilds"

    async def delete_all(pipe):
        guild_ids = await pipe.smembers(guilds_key)
        pipe.multi()
        pipe.delete(f"user:{user_id}:birthday", guilds_key)
        for guild_id in guild_ids:
            pipe.srem(f"guild:{guild_id}:birthdays", user_id)

    # transaction() WATCHes the member's list of guilds: if a /set_birthday adds a guild
    # after the read, the delete is not applied and runs again with the new list.
    return any(await redis_client.transaction(delete_all, guilds_key))

# Stop sharing a member's birthday with one guild. When no other guild has it, no guild
# can see the date, so it is deleted along with the member's list of guilds.
async def remove_birthday_from_guild_redis(guild_id, user_id):
    guilds_key = f"user:{user_id}:guilds"

    async def remove(pipe):
        other_guild_ids = await pipe.smembers(guilds_key) - {str(guild_id)}
        pipe.multi()
        pipe.srem(f"guild:{guild_id}:birthdays", user_id)
        if other_guild_ids:
            pipe.srem(guilds_key, guild_id)
        else:
            pipe.delete(f"user:{user_id}:birthday", guilds_key)

    # WATCHed as in forget_birthday_redis, so a guild added meanwhile keeps the date.
    await redis_client.transaction(remove, guilds_key)

# Split a header and lines into messages of at most MESSAGE_LIMIT characters,
# never splitting a line. Every line is a mention and a date, far below the limit.
def chunk_messages(header, lines):
    chunks = [header]
    for line in lines:
        if len(chunks[-1]) + 1 + len(line) > MESSAGE_LIMIT:
            chunks.append(line)
        else:
            chunks[-1] += "\n" + line
    return chunks

# The first date on or after today with this month and day. A Feb 29 birthday falls
# on Feb 28 in years without Feb 29, so it still comes once a year.
def next_occurrence(month, day, today):
    # This year's date if it has not passed yet, otherwise next year's.
    year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
    if (month, day) == (2, 29) and not calendar.isleap(year):
        day = 28
    return datetime.date(year, month, day)

# Why a parsed date cannot be a birth date, or None if it can. A chat reply is public,
# so the message never repeats the date.
def birth_date_error(birthday_date, today):
    if birthday_date > today:
        return "❌ That date is in the future. Please check the year and try again."
    if birthday_date.year < today.year - MAX_AGE_YEARS:
        return f"❌ That date is more than {MAX_AGE_YEARS} years ago. Please check the year and try again."
    return None

# Background task to check for upcoming birthdays on the first day of each month
@tasks.loop(hours=24)
async def check_upcoming_birthdays():
    today = datetime.date.today()
    # Only run on the first day of the month.
    if today.day != 1:
        return

    next_month = (today.month % 12) + 1
    month_after = ((today.month + 1) % 12) + 1

    # Each guild's post lists only the members who shared their birthday with that guild.
    for guild in client.guilds:
        channel = discord.utils.get(guild.text_channels, name="general")
        if not channel:
            continue

        lines = []
        for user_id, birthday_str in await get_guild_birthdays_redis(guild.id):
            try:
                bd = datetime.date.fromisoformat(birthday_str)
                upcoming_bd = next_occurrence(bd.month, bd.day, today)
                if upcoming_bd.month in [today.month, next_month, month_after]:
                    # A public post: the month and day, never the birth year or age.
                    lines.append(f"<@{user_id}> - {upcoming_bd.strftime('%A, %B %d')}")
            except Exception as e:
                print(f"❌ Error processing birthday for user {user_id}: {e}")

        if not lines:
            continue

        try:
            for chunk in chunk_messages("🎉 **Upcoming Birthdays:**", lines):
                await channel.send(chunk)
        except Exception as e:
            print(f"❌ Error sending upcoming birthdays message in {guild.name}: {e}")

# Subclassing Client to use app commands (slash commands)
class MyClient(discord.Client):
    def __init__(self):
        # Messages echo text that members control, such as display names: never let them ping anyone.
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Slash commands synced globally.")
        # Start the upcoming birthdays task after commands are synced.
        check_upcoming_birthdays.start()

# Instantiate the client
client = MyClient()

# Event listener for when the bot has connected
@client.event
async def on_ready():
    print(f'✅ Logged in as {client.user}')

# Called when the bot is removed from a guild: kicked, banned or left, or the guild was
# deleted. Nobody there can see the guild's birthdays any more.
@client.event
async def on_guild_remove(guild):
    # Removing every member empties the guild's set, and Redis deletes an empty set.
    for user_id in await redis_client.smembers(f"guild:{guild.id}:birthdays"):
        await remove_birthday_from_guild_redis(guild.id, user_id)

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Answer only a direct mention of the bot in a server: not DMs, and not
    # @everyone or @here, which client.user.mentioned_in() also counts.
    if message.guild is not None and not message.mention_everyone and client.user in message.mentions:
        content = message.content.lower()

        # Use spaCy to process the message
        doc = nlp(content)

        # Extract potential date entities using spaCy
        extracted_date = None
        for ent in doc.ents:
            if ent.label_ == "DATE":
                extracted_date = ent.text
                break

        # Infer intent using expanded keyword matching
        intent = None
        if any(phrase in content for phrase in [
            "my birthday is", "my bday is", "set my birthday", "set my birthday to", "update my birthday"
        ]):
            intent = "set"
        elif any(phrase in content for phrase in [
            "what is my", "when is my", "when my", "what my", "my birthday",
            "when is my fucking birthday", "when do i get older", "next birthday"
        ]):
            intent = "get"
        elif any(phrase in content for phrase in [
            "what is", "when is", "next birthday", "birthday", "bday",
            "when is @everyone's birthday", "what birthdates are coming up", "what bdays are coming up"
        ]):
            intent = "get_other"

        # Handle the inferred intent
        if intent == "set":
            # Use the extracted date if available
            date_part = extracted_date if extracted_date else content.replace(f"@{client.user.name.lower()}", "").replace("my birthday is", "").replace("set my birthday to", "").strip()

            # Ensure the extracted date part is clean
            date_part = date_part.replace(",", "").replace("th", "").replace("st", "").replace("nd", "").replace("rd", "")

            birthday_date = None

            # Try parsing the date in various formats
            for fmt in ["%m%d%Y", "%m-%d-%Y", "%m/%d/%Y", "%Y%m%d", "%B %d %Y", "%B %d, %Y"]:
                try:
                    birthday_date = datetime.datetime.strptime(date_part, fmt).date()
                    break
                except ValueError:
                    continue

            # A date no one could be born on, such as 2099 typed for 1999, is refused, not stored.
            error = birth_date_error(birthday_date, datetime.date.today()) if birthday_date else None

            if error:
                await message.reply(error, mention_author=False)
            elif birthday_date:
                # Store the birthday and share it with this server
                await set_birthday_redis(message.guild.id, message.author.id, birthday_date.isoformat())
                await message.reply(
                    f"✅ Your birthday has been updated to {birthday_date.strftime('%m-%d-%Y')}.",
                    mention_author=False
                )
            else:
                await message.reply(
                    "❌ I couldn't understand the date format. Please try again with a valid date.",
                    mention_author=False
                )

        # Lookups get one public reply with no date or name in it; /get_birthday
        # shows a birthday only to the member who asks.
        elif intent == "get":
            await message.reply(
                "🔒 Use /get_birthday and pick yourself to see your birthday. Only you will see the answer.",
                mention_author=False
            )

        elif intent == "get_other":
            await message.reply(
                "🔒 Use /get_birthday and pick the member to see their birthday. Only you will see the answer.",
                mention_author=False
            )

# Slash command to set a birthday
@client.tree.command(name="set_birthday", description="Set your birthday and share it with this server (format: MM-DD-YYYY or YYYY-MM-DD)")
@app_commands.describe(date="The date of your birthday (MM-DD-YYYY or YYYY-MM-DD)")
@app_commands.guild_only()
async def set_birthday(interaction: discord.Interaction, date: str):
    await interaction.response.defer(ephemeral=True)  # Prevent Discord timeout
    user_id = interaction.user.id
    try:
        # Try MM-DD-YYYY first, then fallback to YYYY-MM-DD
        try:
            birthday_date = datetime.datetime.strptime(date, "%m-%d-%Y").date()
        except ValueError:
            birthday_date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        # A date no one could be born on is refused, not stored.
        error = birth_date_error(birthday_date, datetime.date.today())
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        # Store in Redis in ISO format (YYYY-MM-DD), shared with this server
        await set_birthday_redis(interaction.guild_id, user_id, birthday_date.isoformat())
        # Respond with birthday formatted as MM-DD-YYYY
        await interaction.followup.send(
            f"✅ Your birthday has been set to {birthday_date.strftime('%m-%d-%Y')}.", ephemeral=True
        )
    except ValueError:
        await interaction.followup.send(
            "❌ Invalid date format! Use MM-DD-YYYY or YYYY-MM-DD.", ephemeral=True
        )

# Command to query birthday
@client.tree.command(name="get_birthday", description="Get a user's birthday")
@app_commands.describe(user="The user whose birthday you want to look up")
@app_commands.guild_only()
async def get_birthday(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)  # Answer before reading Redis
    user_id = user.id
    birthday_str = await get_birthday_redis(interaction.guild_id, user_id)
    if birthday_str:
        try:
            # Parse the stored birthday
            birthdate = datetime.date.fromisoformat(birthday_str)
            # Compute the next birthday, today or later
            today = datetime.date.today()
            next_birthday = next_occurrence(birthdate.month, birthdate.day, today)

            await interaction.followup.send(
                f"🎂 **{user.mention}'s Birthday:**\n"
                f"{birthdate.strftime('%m-%d-%Y')} - next birthday {next_birthday.strftime('%A, %B %d %Y')}",
                ephemeral=True
            )
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")
            await interaction.followup.send("❌ An error occurred while retrieving the birthday.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {user.mention} has not set their birthday yet.", ephemeral=True)

# Command to list all birthdays
@client.tree.command(name="list_birthdays", description="List all birthdays in the server")
@app_commands.guild_only()
async def list_birthdays(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Prevent timeout while fetching data
    birthdays = await get_guild_birthdays_redis(interaction.guild_id)

    # One line per member. <@id> shows the member's name without the member cache.
    lines = []
    today = datetime.date.today()
    for user_id, birthday_str in birthdays:
        try:
            # Parse the stored birthday
            birthdate = datetime.date.fromisoformat(birthday_str)
            # Compute the next birthday, today or later
            next_birthday = next_occurrence(birthdate.month, birthdate.day, today)

            lines.append(f"<@{user_id}> - {birthdate.strftime('%m-%d-%Y')} - next birthday {next_birthday.strftime('%A, %B %d %Y')}")
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if lines:
        for chunk in chunk_messages("🎉 **Server Birthdays:**", lines):
            await interaction.followup.send(chunk, ephemeral=True)
    else:
        await interaction.followup.send("❌ No birthdays have been set yet.", ephemeral=True)

# Command to forecast upcoming birthdays
@client.tree.command(name="forecast_birthdays", description=f"Show birthdays in the next {FORECAST_DAYS} days")
@app_commands.guild_only()
async def forecast_birthdays(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Prevent timeout while fetching data
    today = datetime.date.today()
    birthdays = await get_guild_birthdays_redis(interaction.guild_id)
    upcoming_birthdays = []

    for user_id, birthday_str in birthdays:
        try:
            bd = datetime.date.fromisoformat(birthday_str)
            upcoming_bd = next_occurrence(bd.month, bd.day, today)
            # next_occurrence() is today or later, so this keeps 0 to FORECAST_DAYS days away.
            if (upcoming_bd - today).days <= FORECAST_DAYS:
                # Calculate age on the next birthday
                age = upcoming_bd.year - bd.year
                upcoming_birthdays.append((user_id, upcoming_bd, age))
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if upcoming_birthdays:
        # One line per member. <@id> shows the member's name without the member cache.
        lines = [
            f"<@{user_id}> - {upcoming_bd.strftime('%A, %B %d')} - turning {age}"
            for user_id, upcoming_bd, age in upcoming_birthdays
        ]
        for chunk in chunk_messages("🎉 **Upcoming Birthdays:**", lines):
            await interaction.followup.send(chunk, ephemeral=True)
    else:
        await interaction.followup.send(f"❌ No birthdays in the next {FORECAST_DAYS} days.", ephemeral=True)

# Command to delete your birthday from every server
@client.tree.command(name="forget_birthday", description="Delete your birthday from every server")
@app_commands.guild_only()
async def forget_birthday(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Answer before reading Redis
    try:
        forgotten = await forget_birthday_redis(interaction.user.id)
    except redis.RedisError as e:
        print(f"❌ Error deleting a birthday: {e}")
        await interaction.followup.send("❌ Your birthday could not be deleted. Please try again later.", ephemeral=True)
        return
    if forgotten:
        await interaction.followup.send("✅ Your birthday has been deleted from every server.", ephemeral=True)
    else:
        await interaction.followup.send("✅ You had no birthday stored, so there was nothing to delete.", ephemeral=True)

def main():
    global TOKEN, REDIS_URL, redis_client

    # If running on Heroku, DYNO will be set; otherwise load .env for local testing.
    if os.getenv("DYNO"):
        TOKEN = os.getenv("DISCORD_TOKEN")
        REDIS_URL = os.getenv("REDIS_URL")
    else:
        load_dotenv()
        TOKEN = os.getenv("DISCORD_TOKEN")
        REDIS_URL = os.getenv("REDIS_URL")

    # Initialize Redis client
    redis_client = create_redis_client()

    # Run the bot
    client.run(TOKEN)


if __name__ == "__main__":
    main()
