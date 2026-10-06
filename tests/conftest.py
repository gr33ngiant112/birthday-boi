"""Shared fixtures for the bot.py tests. Nothing here opens a network connection.

- fake_redis: bot.redis_client becomes an empty fakeredis.FakeAsyncRedis, in place of
  the redis.asyncio client main() creates in production. The fixture returns a
  synchronous FakeStore on the same fake server, for seeding and checks.
- freeze_today: fixes the date bot.py gets from datetime.date.today(), and the time it
  gets from datetime.datetime.now(): 15:00 UTC on that date, when the monthly post goes out.
- freeze_now: a frozen clock for bot.py and for discord.py's task loop, which the test
  moves with set(). It also sets the zone of the host, which date.today() reads.
- gateway: real discord.py guilds, channels, members and messages, built from
  gateway-shaped payloads on the bot client's own ConnectionState. Channel messages
  are captured at HTTPClient.send_message, after discord.py has built the final
  request payload (including the effective allowed_mentions) and before any HTTP.
  gateway.interaction() returns a FakeInteraction for slash commands, whose
  responses would go through Discord's webhook endpoints instead; it records the
  payload discord.py would build for each.
"""

import copy
import datetime
import itertools
import types
import weakref
from unittest import mock

import discord
import discord.ext.tasks
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
# When the monthly post goes out on the 1st (#14). bot.py has no timezone setting, so UTC.
POST_TIME = datetime.time(15, 0, tzinfo=datetime.timezone.utc)


class FakeStore(fakeredis.FakeRedis):
    """A synchronous client on the fake server behind bot.redis_client, for seeding and checks."""

    def seed_birthday(self, guild_id, user_id, birthday):
        """Store birthday (an ISO date) as /set_birthday run in that guild does.

        That is the user's date, the user's id in the guild's set (each guild's views
        list only the members in its own set), and the guild's id in the user's list
        of guilds (which /forget_birthday reads).
        """
        self.set(f"user:{user_id}:birthday", birthday)
        self.sadd(f"guild:{guild_id}:birthdays", user_id)
        self.sadd(f"user:{user_id}:guilds", guild_id)

    def contents(self):
        """Every key on the fake server and its value: a string, or a set of strings."""
        return {key: self.smembers(key) if self.type(key) == "set" else self.get(key) for key in self.keys()}

    def clear_monthly_post_markers(self):
        """Delete the markers of the monthly posts made so far, so the task posts as if it had not (#14)."""
        for key in self.keys("announce:*"):
            self.delete(key)


@pytest.fixture
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(bot, "redis_client", fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    return FakeStore(server=server, decode_responses=True)


class FrozenClock:
    """A clock that reads the same moment, an aware datetime, until set() moves it.

    host_zone is the zone of the machine that runs the bot: date.today() and a naive
    datetime.now() read the moment there. CI and Heroku run in UTC.
    """

    def __init__(self, moment, host_zone=datetime.timezone.utc):
        self.moment = moment
        self.host_zone = host_zone

    def set(self, moment):
        self.moment = moment


def frozen_datetime_module(clock):
    """A copy of the datetime module whose date.today() and datetime.now() read clock."""

    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            day = clock.moment.astimezone(clock.host_zone).date()
            return cls(day.year, day.month, day.day)

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:  # the host's local time, without a zone, as datetime.now() returns it
                return clock.moment.astimezone(clock.host_zone).replace(tzinfo=None)
            return clock.moment.astimezone(tz)

    frozen = types.ModuleType("datetime")
    frozen.__dict__.update(vars(datetime))
    frozen.date = FrozenDate
    frozen.datetime = FrozenDateTime
    return frozen


@pytest.fixture
def freeze_today(monkeypatch):
    """Return freeze(date): bot.py's datetime.date.today() returns that date afterwards, and its
    datetime.datetime.now() returns 15:00 UTC on that date, when the monthly post goes out.

    Only the datetime name inside bot.py is replaced; the real module is untouched.
    """

    def freeze(day):
        clock = FrozenClock(datetime.datetime.combine(day, POST_TIME))
        monkeypatch.setattr(bot, "datetime", frozen_datetime_module(clock))

    return freeze


@pytest.fixture
def freeze_now(monkeypatch):
    """Return freeze(moment, host_zone=UTC), which freezes the clock at moment, an aware datetime,
    and returns the FrozenClock; clock.set() moves it.

    bot.py reads it, and so does discord.py's task loop: discord.ext.tasks, which picks the time
    of each run, and discord.utils, whose compute_timedelta() gives the real seconds the loop
    sleeps until then. Only the datetime name inside those modules is replaced.
    """

    def freeze(moment, host_zone=datetime.timezone.utc):
        clock = FrozenClock(moment, host_zone)
        frozen = frozen_datetime_module(clock)
        for module in (bot, discord.ext.tasks, discord.utils):
            monkeypatch.setattr(module, "datetime", frozen)
        return clock

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

    def add_guild(self, guild_id, name, members, *, uncached=(), text_channel="general"):
        """Add a guild as GUILD_CREATE would, with a text channel and a voice channel.

        The text channel is #general unless text_channel gives it another name.

        members and uncached are lists of (user_id, name). The bot runs with
        Intents.default(), which has no members intent, so discord.py caches only members
        who are in a voice channel (Guild._from_data). Each of members is put in voice so
        the bot can see them. uncached members are in the guild but not in voice, so
        guild.get_member() returns None for them, as for most members of a real guild (#6).
        """
        general_id, voice_id = guild_id + 1, guild_id + 2
        channel_base = {"guild_id": str(guild_id), "permission_overwrites": [], "nsfw": False, "parent_id": None}
        payload = {
            "id": str(guild_id),
            "name": name,
            "member_count": len(members) + len(uncached) + 1,
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
                {**channel_base, "id": str(general_id), "type": 0, "name": text_channel, "position": 0},
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
            + [member_payload(user_id, user_name) for user_id, user_name in [*members, *uncached]],
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
    def mention(user_id, name, *, nick=None, member=True):
        """A user in a MESSAGE_CREATE's mentions list, with the partial member Discord adds in guilds.

        member=False leaves the partial member out, as for a user who is not in the
        guild or a mention in a DM.
        """
        data = user_payload(user_id, name)
        if member:
            data["member"] = {
                "nick": nick,
                "roles": [],
                "joined_at": TIMESTAMP,
                "flags": 0,
                "deaf": False,
                "mute": False,
            }
        return data

    def mention_bot(self, *, member=True):
        return self.mention(BOT_ID, BOT_NAME, member=member)

    def message(self, guild, author, content, *, mentions=(), mention_everyone=False):
        """A MESSAGE_CREATE in the guild's #general. author is (user_id, name).

        mention_everyone is the flag Discord sets on a message that pings @everyone or @here.
        """
        channel = self.general(guild)
        payload = self._message_payload(channel, author, content, mentions, mention_everyone)
        payload["guild_id"] = str(guild.id)
        payload["member"] = {"roles": [], "joined_at": TIMESTAMP, "flags": 0, "deaf": False, "mute": False}
        return discord.Message(state=self.state, channel=channel, data=payload)

    def dm_message(self, author, content, *, mentions=()):
        """A MESSAGE_CREATE in a DM between author, (user_id, name), and the bot.

        A DM has no guild_id or member data: build its mentions with member=False.
        """
        author_id, author_name = author
        channel_data = {"id": str(next(self._ids)), "type": 1, "recipients": [user_payload(author_id, author_name)]}
        channel = discord.DMChannel(me=self.bot_user, state=self.state, data=channel_data)
        payload = self._message_payload(channel, author, content, mentions, False)
        return discord.Message(state=self.state, channel=channel, data=payload)

    def _message_payload(self, channel, author, content, mentions, mention_everyone):
        author_id, author_name = author
        return {
            "id": str(next(self._ids)),
            "channel_id": str(channel.id),
            "type": 0,
            "content": content,
            "author": user_payload(author_id, author_name),
            "mentions": list(mentions),
            "mention_roles": [],
            "mention_everyone": mention_everyone,
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "tts": False,
            "timestamp": TIMESTAMP,
            "edited_timestamp": None,
            "flags": 0,
        }

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

    def interaction(self, guild, user):
        return FakeInteraction(guild, user, self.state)


@pytest.fixture
def gateway(monkeypatch):
    return Gateway.install(monkeypatch, bot.client)


class FakeInteraction:
    """The parts of discord.Interaction that bot.py's slash commands use.

    sent collects (content, kwargs) for every response and follow-up, in order.
    payloads holds, in the same order, the payload discord.py builds for each one with
    handle_message_parameters(): its content, and the call's allowed_mentions merged
    over the client-wide default, as Webhook.send does for a follow-up.
    deferred holds the keyword arguments of response.defer(), or None if it was not called.
    """

    def __init__(self, guild, user, state):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.sent = []
        self.payloads = []
        self.deferred = None
        self.response = FakeInteractionResponse(self)
        self.followup = FakeFollowup(self)
        self._state = state

    def record(self, content, kwargs):
        self.sent.append((content, kwargs))
        with discord.http.handle_message_parameters(
            content=discord.utils.MISSING if content is None else content,
            allowed_mentions=kwargs.get("allowed_mentions", discord.utils.MISSING),
            previous_allowed_mentions=self._state.allowed_mentions,
        ) as params:
            self.payloads.append(params.payload)


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
        self._interaction.deferred = kwargs

    async def send_message(self, content=None, **kwargs):
        self._respond()
        self._interaction.record(content, kwargs)


class FakeFollowup:
    def __init__(self, interaction):
        self._interaction = interaction

    async def send(self, content=None, **kwargs):
        if not self._interaction.response.is_done():
            raise RuntimeError("follow-up sent before the interaction was answered")
        self._interaction.record(content, kwargs)
