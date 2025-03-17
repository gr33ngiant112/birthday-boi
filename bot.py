#!/usr/local/bin/python3
import os
import redis
import ssl
from discord.ext import commands
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime

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

# Subclassing Client to use app commands (slash commands)
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

# Instantiate the client
client = MyClient()

# Event listener for when the bot has connected
@client.event
async def on_ready():
    print(f'✅ Logged in as {client.user}')
    try:
        await client.tree.sync()
        print("✅ Slash commands synced successfully!")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

# Command to set birthday (fixes unknown interaction error)
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
        await interaction.followup.send(f"✅ Your birthday has been set to {birthday_date.strftime('%m-%d-%Y')}.", ephemeral=True)
    except ValueError:
        await interaction.followup.send("❌ Invalid date format! Use MM-DD-YYYY or YYYY-MM-DD.", ephemeral=True)

# Command to query birthday
@client.tree.command(name="get_birthday", description="Get a user's birthday")
@app_commands.describe(user="The user whose birthday you want to query")
async def get_birthday(interaction: discord.Interaction, user: discord.Member):
    # If the queried user is the bot itself, return the special message.
    if user.id == client.user.id:
        await interaction.response.send_message(f"<@{interaction.user.id}> Foolish mop. I have no beginning, and I have no end.")
        return

    user_id = user.id
    birthday_str = get_birthday_redis(user_id)  # Retrieve from Redis

    if birthday_str:
        try:
            birthday_date = datetime.date.fromisoformat(birthday_str)
            formatted_date = birthday_date.strftime("%m-%d-%Y")
        except Exception:
            formatted_date = birthday_str
        await interaction.response.send_message(f"🎂 {user.display_name}'s birthday is on {formatted_date}.")
    else:
        await interaction.response.send_message(f"❌ {user.display_name} has not set their birthday yet.")

# Command to list all birthdays
@client.tree.command(name="list_birthdays", description="List all birthdays on the server")
async def list_birthdays(interaction: discord.Interaction):
    await interaction.response.defer()  # Prevent timeout while fetching data

    birthdays = get_all_birthdays_redis()  # Retrieve all birthdays from Redis
    if birthdays:
        birthday_list = []
        for user_id, date_str in birthdays:
            try:
                birthday_date = datetime.date.fromisoformat(date_str)
                formatted_date = birthday_date.strftime("%m-%d-%Y")
            except Exception:
                formatted_date = date_str
            birthday_list.append(f"<@{user_id}>: {formatted_date}")
        guild_name = interaction.guild.name if interaction.guild else "Server"
        message = f"🎉 **{guild_name} Birthdays:**\n" + "\n".join(birthday_list)
        await interaction.followup.send(message)
    else:
        await interaction.followup.send("❌ No birthdays have been set yet.")

# Run the bot
client.run(TOKEN)