#!/usr/local/bin/python3
import os
import redis
from discord.ext import commands
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Connect to Redis using the Heroku Redis URL
REDIS_URL = os.getenv('REDIS_URL')

# Initialize Redis client
redis_client = redis.from_url(REDIS_URL)

# Function to set a birthday in Redis with error handling
def set_birthday_redis(user_id, birthday):
    try:
        redis_client.set(f"user:{user_id}:birthday", birthday)
    except redis.RedisError as e:
        print(f"Error setting birthday for user {user_id}: {e}")

# Function to get a birthday from Redis with error handling
def get_birthday_redis(user_id):
    try:
        birthday = redis_client.get(f"user:{user_id}:birthday")
        if birthday:
            return birthday.decode('utf-8')
    except redis.RedisError as e:
        print(f"Error getting birthday for user {user_id}: {e}")
    return None

# Function to get all birthdays from Redis with error handling
def get_all_birthdays_redis():
    birthdays = []
    try:
        keys = redis_client.keys("user:*:birthday")
        for key in keys:
            user_id = key.decode('utf-8').split(":")[1]
            try:
                birthday = redis_client.get(key).decode('utf-8')
                birthdays.append((user_id, birthday))
            except redis.RedisError as e:
                print(f"Error decoding birthday for key {key}: {e}")
    except redis.RedisError as e:
        print(f"Error retrieving keys: {e}")
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
    print(f'Logged in as {client.user}')

# Command to set birthday
@client.tree.command(name="set_birthday", description="Set your birthday (format: YYYY-MM-DD)")
@app_commands.describe(date="The date of your birthday (YYYY-MM-DD)")
async def set_birthday(interaction: discord.Interaction, date: str):
    user_id = interaction.user.id
    try:
        # Validate date
        birthday_date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        set_birthday_redis(user_id, birthday_date.isoformat())  # Store the birthday in Redis
        await interaction.response.send_message(f"Your birthday has been set to {birthday_date}.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid date format! Please use YYYY-MM-DD.", ephemeral=True)

# Command to query birthday
@client.tree.command(name="get_birthday", description="Get a user's birthday")
@app_commands.describe(user="The user whose birthday you want to query")
async def get_birthday(interaction: discord.Interaction, user: discord.Member):
    user_id = user.id
    birthday_date = get_birthday_redis(user_id)  # Retrieve the birthday from Redis
    if birthday_date:
        await interaction.response.send_message(f"{user.display_name}'s birthday is on {birthday_date}.")
    else:
        await interaction.response.send_message(f"{user.display_name} has not set their birthday yet.")

# Command to list all birthdays
@client.tree.command(name="list_birthdays", description="List all birthdays on the server")
async def list_birthdays(interaction: discord.Interaction):
    birthdays = get_all_birthdays_redis()  # Retrieve all birthdays from Redis
    if birthdays:
        birthday_list = [f"<@{user_id}>: {date}" for user_id, date in birthdays]
        message = "Here are the birthdays of server members:\n" + "\n".join(birthday_list)
        await interaction.response.send_message(message)
    else:
        await interaction.response.send_message("No birthdays have been set yet.")

# Run the bot
client.run(TOKEN)