"""Shared fixtures for the bot.py tests. Nothing here opens a network connection.

- fake_redis: an empty fakeredis client in place of bot.redis_client, which main()
  creates in production.
- freeze_today: fixes the date bot.py gets from datetime.date.today().
- gateway: real discord.py guilds, channels, members and messages, built from
  gateway-shaped payloads on the bot client's own ConnectionState. Channel messages
  are captured at HTTPClient.send_message, after discord.py has built the final
  request payload (including the effective allowed_mentions) and before any HTTP.
  gateway.interaction() returns a FakeInteraction for slash commands, whose
  responses would go through Discord's webhook endpoints instead.
"""

import copy
import datetime
import itertools
import types
import weakref
from unittest import mock

import discord
import fakeredis
import pytest

# Importing bot must not start the client (test_smoke.py checks it). Refuse
# Client.run here too, so a regression fails the run instead of logging in with
# whatever token the environment or a local .env holds.
with mock.patch.object(discord.Client, "run", side_effect=RuntimeError("client.run() was called during import")):
    import bot

BOT_ID = 100000000000000001
BOT_NAME = "Birthday Boi"
TIMESTAMP = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr(bot, "redis_client", client)
    return client


def frozen_datetime_module(day):
    """A copy of the datetime module whose date.today() returns day."""

    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)

    frozen = types.ModuleType("datetime")
    frozen.__dict__.update(vars(datetime))
    frozen.date = FrozenDate
    return frozen


@pytest.fixture
def freeze_today(monkeypatch):
    """Return freeze(date): bot.py's datetime.date.today() returns that date afterwards.

    Only the datetime name inside bot.py is replaced; the real module is untouched.
    """

    def freeze(day):
        monkeypatch.setattr(bot, "datetime", frozen_datetime_module(day))

    return freeze


def user_payload(user_id, name, *, is_bot=False):
    return {
        "id": str(user_id),
        "username": name,
        "discriminator": "0",
        "global_name": None,
        "avatar": None,
        "bot": is_bot,
    }


def member_payload(user_id, name, *, nick=None, is_bot=False):
    return {
        "user": user_payload(user_id, name, is_bot=is_bot),
        "nick": nick,
        "roles": [],
        "joined_at": TIMESTAMP,
        "flags": 0,
        "deaf": False,
        "mute": False,
    }


class Gateway:
    """Feeds fake gateway payloads to the bot client's ConnectionState and records sends."""

    def __init__(self, state):
        self.state = state
        self.sent = []  # (channel_id, request payload), in send order
        self._ids = itertools.count(1)
        self.bot_user = discord.ClientUser(state=state, data=user_payload(BOT_ID, BOT_NAME, is_bot=True))

    @classmethod
    def install(cls, monkeypatch, client):
        state = client._connection
        # Start from empty caches; monkeypatch puts the originals back afterwards.
        monkeypatch.setattr(state, "_guilds", {})
        monkeypatch.setattr(state, "_users", weakref.WeakValueDictionary())
        gateway = cls(state)
        monkeypatch.setattr(state, "user", gateway.bot_user)  # what READY sets
        monkeypatch.setattr(state.http, "send_message", gateway.send_message)
        return gateway

    def add_guild(self, guild_id, name, members):
        """Add a guild as GUILD_CREATE would, with a #general text channel and a voice channel.

        members is a list of (user_id, name). The bot runs with Intents.default(), which
        has no members intent, so discord.py caches only members who are in a voice
        channel (Guild._from_data). Each member is put in voice so the bot can see them.
        """
        general_id, voice_id = guild_id + 1, guild_id + 2
        channel_base = {"guild_id": str(guild_id), "permission_overwrites": [], "nsfw": False, "parent_id": None}
        payload = {
            "id": str(guild_id),
            "name": name,
            "member_count": len(members) + 1,
            "roles": [
                {
                    "id": str(guild_id),
                    "name": "@everyone",
                    "permissions": "0",
                    "position": 0,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "mentionable": False,
                    "flags": 0,
                }
            ],
            "channels": [
                {**channel_base, "id": str(general_id), "type": 0, "name": "general", "position": 0},
                {
                    **channel_base,
                    "id": str(voice_id),
                    "type": 2,
                    "name": "Lounge",
                    "position": 1,
                    "bitrate": 64000,
                    "user_limit": 0,
                },
            ],
            "members": [member_payload(BOT_ID, BOT_NAME, is_bot=True)]
            + [member_payload(user_id, user_name) for user_id, user_name in members],
            "voice_states": [
                {
                    "user_id": str(user_id),
                    "channel_id": str(voice_id),
                    "session_id": f"session-{user_id}",
                    "deaf": False,
                    "mute": False,
                    "self_deaf": False,
                    "self_mute": False,
                    "self_video": False,
                    "suppress": False,
                }
                for user_id, _ in members
            ],
        }
        return self.state._add_guild_from_data(payload)

    @staticmethod
    def general(guild):
        return discord.utils.get(guild.text_channels, name="general")

    @staticmethod
    def mention(user_id, name, *, nick=None):
        """A user in a MESSAGE_CREATE's mentions list, with the partial member Discord adds in guilds."""
        data = user_payload(user_id, name)
        data["member"] = {"nick": nick, "roles": [], "joined_at": TIMESTAMP, "flags": 0, "deaf": False, "mute": False}
        return data

    def mention_bot(self):
        return self.mention(BOT_ID, BOT_NAME)

    def message(self, guild, author, content, *, mentions=()):
        """A MESSAGE_CREATE in the guild's #general. author is (user_id, name)."""
        channel = self.general(guild)
        author_id, author_name = author
        payload = {
            "id": str(next(self._ids)),
            "channel_id": str(channel.id),
            "guild_id": str(guild.id),
            "type": 0,
            "content": content,
            "author": user_payload(author_id, author_name),
            "member": {"roles": [], "joined_at": TIMESTAMP, "flags": 0, "deaf": False, "mute": False},
            "mentions": list(mentions),
            "mention_roles": [],
            "mention_everyone": False,
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "tts": False,
            "timestamp": TIMESTAMP,
            "edited_timestamp": None,
            "flags": 0,
        }
        return discord.Message(state=self.state, channel=channel, data=payload)

    async def send_message(self, channel_id, *, params):
        payload = copy.deepcopy(params.payload)
        self.sent.append((int(channel_id), payload))
        return {
            "id": str(next(self._ids)),
            "channel_id": str(channel_id),
            "type": 0,
            "content": payload.get("content") or "",
            "author": user_payload(BOT_ID, BOT_NAME, is_bot=True),
            "attachments": [],
            "embeds": payload.get("embeds", []),
            "mentions": [],
            "mention_roles": [],
            "mention_everyone": False,
            "pinned": False,
            "tts": False,
            "timestamp": TIMESTAMP,
            "edited_timestamp": None,
            "flags": 0,
        }

    def sent_to(self, channel):
        return [payload for channel_id, payload in self.sent if channel_id == channel.id]

    @staticmethod
    def interaction(guild, user):
        return FakeInteraction(guild, user)


@pytest.fixture
def gateway(monkeypatch):
    return Gateway.install(monkeypatch, bot.client)


class FakeInteraction:
    """The parts of discord.Interaction that bot.py's slash commands use.

    sent collects (content, kwargs) for every response and follow-up, in order.
    """

    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.sent = []
        self.response = FakeInteractionResponse(self)
        self.followup = FakeFollowup(self)


class FakeInteractionResponse:
    def __init__(self, interaction):
        self._interaction = interaction
        self._done = False

    def is_done(self):
        return self._done

    def _respond(self):
        # Discord accepts one initial response per interaction.
        if self._done:
            raise discord.InteractionResponded(self._interaction)
        self._done = True

    async def defer(self, **kwargs):
        self._respond()

    async def send_message(self, content=None, **kwargs):
        self._respond()
        self._interaction.sent.append((content, kwargs))


class FakeFollowup:
    def __init__(self, interaction):
        self._interaction = interaction

    async def send(self, content=None, **kwargs):
        if not self._interaction.response.is_done():
            raise RuntimeError("follow-up sent before the interaction was answered")
        self._interaction.sent.append((content, kwargs))
