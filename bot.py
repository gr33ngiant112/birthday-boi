import os
import redis
import ssl
from discord.ext import commands, tasks
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime
import asyncio

# If running on Heroku, DYNO will be set; otherwise load .env for local testing.
if os.getenv("DYNO"):
    TOKEN = os.getenv("DISCORD_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL")
else:
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL")

# Function to initialize Redis with SSL handling and timeouts
def create_redis_client():
    return redis.from_url(
        REDIS_URL,
        decode_responses=True,  # Ensures Redis returns strings instead of bytes
        ssl_cert_reqs=ssl.CERT_NONE,  # Correctly handles SSL for Heroku Redis
        socket_timeout=10,
        socket_connect_timeout=10,
        retry_on_timeout=True
    )

# Initialize Redis client
redis_client = create_redis_client()

# Function to set a birthday in Redis with error handling
def set_birthday_redis(user_id, birthday):
    try:
        redis_client.set(f"user:{user_id}:birthday", birthday)
    except redis.RedisError as e:
        print(f"❌ Error setting birthday for user {user_id}: {e}")

# Function to get a birthday from Redis with error handling
def get_birthday_redis(user_id):
    try:
        birthday = redis_client.get(f"user:{user_id}:birthday")
        if birthday:
            return birthday
    except redis.RedisError as e:
        print(f"❌ Error getting birthday for user {user_id}: {e}")
    return None

# Function to get all birthdays from Redis with error handling
def get_all_birthdays_redis():
    birthdays = []
    try:
        keys = redis_client.keys("user:*:birthday")
        for key in keys:
            user_id = key.split(":")[1]
            try:
                birthday = redis_client.get(key)
                if birthday:
                    birthdays.append((user_id, birthday))
            except redis.RedisError as e:
                print(f"❌ Error decoding birthday for key {key}: {e}")
    except redis.RedisError as e:
        print(f"❌ Error retrieving keys: {e}")
    return birthdays

# Background task to check for upcoming birthdays on the first day of each month
@tasks.loop(hours=24)
async def check_upcoming_birthdays():
    today = datetime.date.today()
    # Only run on the first day of the month.
    if today.day != 1:
        return

    birthdays = get_all_birthdays_redis()
    upcoming_birthdays = []
    next_month = (today.month % 12) + 1
    month_after = ((today.month + 1) % 12) + 1

    for user_id, birthday_str in birthdays:
        try:
            bd = datetime.date.fromisoformat(birthday_str)
            upcoming_bd = datetime.date(today.year, bd.month, bd.day)
            if upcoming_bd < today:
                upcoming_bd = datetime.date(today.year + 1, bd.month, bd.day)
            if upcoming_bd.month in [today.month, next_month, month_after]:
                # Calculate age on the next birthday
                age = upcoming_bd.year - bd.year
                upcoming_birthdays.append((user_id, upcoming_bd, age))
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if upcoming_birthdays:
        # Prepare the table header
        table_header = f"{'Who'.ljust(25)}{'Turning'.ljust(10)}{'When'.ljust(25)}\n"
        table_header += "-" * 60 + "\n"

        # Prepare the table rows
        table_rows = []
        for user_id, upcoming_bd, age in upcoming_birthdays:
            user = discord.utils.get(client.get_all_members(), id=int(user_id))
            if user:
                who = f"@{user.display_name}".ljust(25)
                turning = f"{age}".ljust(10)
                when = upcoming_bd.strftime("%A, %B %d %Y (%m-%d-%Y)").ljust(25)
                table_rows.append(f"{who}{turning}{when}")

        # Combine the header and rows
        table = table_header + "\n".join(table_rows)

        # Send the table as a message
        for guild in client.guilds:
            channel = discord.utils.get(guild.text_channels, name="general")
            if channel:
                try:
                    await channel.send(f"🎉 **Upcoming Birthdays:**\n```\n{table}\n```")
                except Exception as e:
                    print(f"❌ Error sending upcoming birthdays message in {guild.name}: {e}")

# Subclassing Client to use app commands (slash commands)
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
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

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if the bot is mentioned
    if client.user.mentioned_in(message):
        content = message.content.lower()

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
            # Extract the date from the message content
            try:
                # Remove the mention of the bot and extract the date part
                date_part = content.replace(f"@{client.user.name.lower()}", "").replace("my birthday is", "").replace("set my birthday to", "").strip()

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

                if birthday_date:
                    # Store the birthday in Redis
                    set_birthday_redis(message.author.id, birthday_date.isoformat())
                    await message.reply(
                        f"✅ Your birthday has been updated to {birthday_date.strftime('%m-%d-%Y')}.",
                        mention_author=True
                    )
                else:
                    await message.reply(
                        "❌ I couldn't understand the date format. Please try again with a valid date.",
                        mention_author=True
                    )
            except Exception as e:
                print(f"❌ Error processing birthday: {e}")
                await message.reply("❌ An error occurred while setting your birthday. Please try again.", mention_author=True)

        elif intent == "get":
            birthday_str = get_birthday_redis(message.author.id)
            if birthday_str:
                birthday_date = datetime.date.fromisoformat(birthday_str)
                response = f"🎂 Your birthday is on {birthday_date.strftime('%m-%d-%Y')}."
            else:
                response = "❌ You haven't set your birthday yet."
            await message.reply(response, mention_author=True)

        elif intent == "get_other":
            mentioned_users = message.mentions
            if mentioned_users:
                for user in mentioned_users:
                    if user.id != client.user.id:
                        birthday_str = get_birthday_redis(user.id)
                        if birthday_str:
                            birthday_date = datetime.date.fromisoformat(birthday_str)
                            response = f"🎂 {user.display_name}'s birthday is on {birthday_date.strftime('%m-%d-%Y')}."
                        else:
                            response = f"❌ {user.display_name} hasn't set their birthday yet."
                        await message.reply(response, mention_author=True)
            else:
                await message.reply("❌ You didn't mention anyone. Please try again.", mention_author=True)

# Slash command to set a birthday
@client.tree.command(name="set_birthday", description="Set your birthday (format: MM-DD-YYYY or YYYY-MM-DD)")
@app_commands.describe(date="The date of your birthday (MM-DD-YYYY or YYYY-MM-DD)")
async def set_birthday(interaction: discord.Interaction, date: str):
    await interaction.response.defer(ephemeral=True)  # Prevent Discord timeout
    user_id = interaction.user.id
    try:
        # Try MM-DD-YYYY first, then fallback to YYYY-MM-DD
        try:
            birthday_date = datetime.datetime.strptime(date, "%m-%d-%Y").date()
        except ValueError:
            birthday_date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        # Store in Redis in ISO format (YYYY-MM-DD)
        set_birthday_redis(user_id, birthday_date.isoformat())
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
async def get_birthday(interaction: discord.Interaction, user: discord.Member):
    user_id = user.id
    birthday_str = get_birthday_redis(user_id)
    if birthday_str:
        try:
            # Parse the stored birthday
            birthdate = datetime.date.fromisoformat(birthday_str)
            # Compute the next birthday for the current year
            today = datetime.date.today()
            next_birthday = datetime.date(today.year, birthdate.month, birthdate.day)
            if next_birthday < today:
                next_birthday = datetime.date(today.year + 1, birthdate.month, birthdate.day)

            # Format the table
            table = f"{'Birthdate'.ljust(15)}{'Birthday'.ljust(25)}\n"
            table += "-" * 40 + "\n"
            table += f"{birthdate.strftime('%m-%d-%Y').ljust(15)}{next_birthday.strftime('%A, %B %d %Y').ljust(25)}"

            await interaction.response.send_message(f"🎂 **{user.display_name}'s Birthday:**\n```\n{table}\n```", ephemeral=True)
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")
            await interaction.response.send_message("❌ An error occurred while retrieving the birthday.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ {user.display_name} has not set their birthday yet.", ephemeral=True)

# Command to list all birthdays
@client.tree.command(name="list_birthdays", description="List all birthdays in the server")
async def list_birthdays(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Prevent timeout while fetching data
    birthdays = get_all_birthdays_redis()
    if birthdays:
        # Prepare the table header
        table = f"{'User'.ljust(25)}{'Birthdate'.ljust(15)}{'Birthday'.ljust(25)}\n"
        table += "-" * 65 + "\n"

        # Prepare the table rows
        today = datetime.date.today()
        for user_id, birthday_str in birthdays:
            try:
                # Parse the stored birthday
                birthdate = datetime.date.fromisoformat(birthday_str)
                # Compute the next birthday for the current year
                next_birthday = datetime.date(today.year, birthdate.month, birthdate.day)
                if next_birthday < today:
                    next_birthday = datetime.date(today.year + 1, birthdate.month, birthdate.day)

                # Get the user's display name
                user = interaction.guild.get_member(int(user_id))
                if user:
                    table += f"{user.display_name.ljust(25)}{birthdate.strftime('%m-%d-%Y').ljust(15)}{next_birthday.strftime('%A, %B %d %Y').ljust(25)}\n"
            except Exception as e:
                print(f"❌ Error processing birthday for user {user_id}: {e}")

        await interaction.followup.send(f"🎉 **Server Birthdays:**\n```\n{table}\n```", ephemeral=True)
    else:
        await interaction.followup.send("❌ No birthdays have been set yet.", ephemeral=True)

# Command to forecast upcoming birthdays
@client.tree.command(name="forecast_birthdays", description="Show upcoming birthdays in the next 60 and 90 days")
async def forecast_birthdays(interaction: discord.Interaction):
    await interaction.response.defer()  # Prevent timeout while fetching data
    today = datetime.date.today()
    birthdays = get_all_birthdays_redis()
    upcoming_birthdays = []
    next_month = (today.month % 12) + 1
    month_after = ((today.month + 1) % 12) + 1

    for user_id, birthday_str in birthdays:
        try:
            bd = datetime.date.fromisoformat(birthday_str)
            upcoming_bd = datetime.date(today.year, bd.month, bd.day)
            if upcoming_bd < today:
                upcoming_bd = datetime.date(today.year + 1, bd.month, bd.day)
            if upcoming_bd.month in [next_month, month_after]:
                # Calculate age on the next birthday
                age = upcoming_bd.year - bd.year
                upcoming_birthdays.append((user_id, upcoming_bd, age))
        except Exception as e:
            print(f"❌ Error processing birthday for user {user_id}: {e}")

    if upcoming_birthdays:
        # Prepare the table header
        table_header = f"{'Who'.ljust(25)}{'When'.ljust(25)}{'Turning'.ljust(10)}\n"
        table_header += "-" * 60 + "\n"

        # Prepare the table rows
        table_rows = []
        for user_id, upcoming_bd, age in upcoming_birthdays:
            user = interaction.guild.get_member(int(user_id))
            if user:
                who = f"@{user.display_name}".ljust(25)
                when = upcoming_bd.strftime("%A, %B %d").ljust(25)
                turning = f"{age}".ljust(10)
                table_rows.append(f"{who}{when}{turning}")

        # Combine the header and rows
        table = table_header + "\n".join(table_rows)

        # Send the table as a message
        await interaction.followup.send(f"🎉 **Upcoming Birthdays:**\n```\n{table}\n```")
    else:
        await interaction.followup.send("❌ No upcoming birthdays in the next 60 or 90 days.")

# Run the bot
client.run(TOKEN)