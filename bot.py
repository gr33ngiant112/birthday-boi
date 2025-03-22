import os
import redis
import ssl
from discord.ext import commands, tasks
from discord import app_commands
import discord
from dotenv import load_dotenv
import datetime
import random
import asyncio
import spacy  # Add spaCy for natural language processing
import en_core_web_sm

# Load spaCy's English language model
nlp = en_core_web_sm.load()

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
        # Start the upcoming birthdays task after commands are synced.
        check_upcoming_birthdays.start()

# Instantiate the client
client = MyClient()

# Event listener for when the bot has connected
@client.event
async def on_ready():
    print(f'✅ Logged in as {client.user}')

# Natural conversation handler
@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if the bot is mentioned
    if client.user.mentioned_in(message):
        content = message.content.lower()
        doc = nlp(content)

        # Expanded intent detection
        intent = None

        # Intent: Get
        get_phrases = [
            "what is my", "when is my", "when my", "what my", "my birthday",
            "when is my fucking birthday", "when do i get older", "next birthday"
        ]
        if any(phrase in content for phrase in get_phrases):
            intent = "get"

        # Intent: Set
        set_phrases = [
            "my birthday is", "my bday is", "set my birthday", "set my birthday to", "update my birthday"
        ]
        if any(phrase in content for phrase in set_phrases):
            intent = "set"

        # Intent: Get Other
        if message.mentions:
            get_other_phrases = [
                "what is", "when is", "next birthday", "birthday", "bday"
            ]
            if any(phrase in content for phrase in get_other_phrases):
                intent = "get_other"

        # Intent: List
        list_phrases = ["list birthdays", "show birthdays", "when is @everyone's birthday", "what bdays are coming up"]
        if any(phrase in content for phrase in list_phrases):
            intent = "list"

        # Handle the inferred intent
        if intent == "set":
            await message.reply("Please provide your birthday in MM-DD-YYYY format.", mention_author=True)
            try:
                reply = await client.wait_for(
                    "message",
                    check=lambda m: m.author == message.author and m.channel == message.channel,
                    timeout=30.0
                )
                birthday_date = None
                for ent in nlp(reply.content).ents:
                    if ent.label_ == "DATE":
                        try:
                            birthday_date = datetime.datetime.strptime(ent.text, "%B %d %Y").date()
                        except ValueError:
                            try:
                                birthday_date = datetime.datetime.strptime(ent.text, "%m-%d-%Y").date()
                            except ValueError:
                                pass
                if birthday_date:
                    set_birthday_redis(message.author.id, birthday_date.isoformat())
                    await message.reply(
                        f"✅ Your birthday has been set to {birthday_date.strftime('%m-%d-%Y')}.",
                        mention_author=True
                    )
                else:
                    await message.reply("❌ I couldn't understand that date. Please try again.", mention_author=True)
            except asyncio.TimeoutError:
                await message.reply("❌ You took too long to respond. Please try again.", mention_author=True)

        elif intent == "get":
            birthday_str = get_birthday_redis(message.author.id)
            if birthday_str:
                birthday_date = datetime.date.fromisoformat(birthday_str)
                response = f"🎂 Your birthday is on {birthday_date.strftime('%m-%d-%Y')}."
            else:
                response = "❌ You haven't set your birthday yet."
            await message.reply(response, mention_author=True)
            await ask_to_broadcast(message, response)

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
                        await ask_to_broadcast(message, response)
            else:
                await message.reply("❌ You didn't mention anyone. Please try again.", mention_author=True)

        elif intent == "list":
            birthdays = get_all_birthdays_redis()
            if birthdays:
                birthday_list = "\n".join([f"<@{user_id}>: {date}" for user_id, date in birthdays])
                response = f"🎉 **Server Birthdays:**\n{birthday_list}"
            else:
                response = "❌ No birthdays have been set yet."
            await message.reply(response, mention_author=True)
            await ask_to_broadcast(message, response)

        else:
            await message.reply("❌ I couldn't understand your request. Please try again.", mention_author=True)


async def ask_to_broadcast(message, response):
    """Ask the user if they want to broadcast the response to the general channel."""
    await message.reply(
        "Do you want me to broadcast this to the general channel? (y/n)",
        mention_author=True
    )
    try:
        reply = await client.wait_for(
            "message",
            check=lambda m: m.author == message.author and m.channel == message.channel,
            timeout=30.0
        )
        if reply.content.lower() in ["y", "yes"]:
            general_channel = discord.utils.get(message.guild.text_channels, name="general")
            if general_channel:
                await general_channel.send(response)
            else:
                await message.reply("❌ Couldn't find a general channel to broadcast to.", mention_author=True)
        elif reply.content.lower() in ["n", "no"]:
            await message.reply("Alright, I won't broadcast it.", mention_author=True)
        else:
            await message.reply("❌ Invalid response. Please try again.", mention_author=True)
    except asyncio.TimeoutError:
        await message.reply("❌ You took too long to respond. No broadcast will be made.", mention_author=True)

# Run the bot
client.run(TOKEN)