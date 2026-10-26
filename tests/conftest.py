"""Shared fixtures for the bot.py tests. Nothing here opens a network connection.

- fake_redis: bot.redis_client becomes an empty fakeredis.FakeAsyncRedis, in place of
  the redis.asyncio client main() creates in production. The fixture returns a
  synchronous FakeStore on the same fake server, for seeding and checks.
- freeze_today: fixes the date bot.py gets from datetime.date.today(), and the time it
  gets from datetime.datetime.now(): 15:00 UTC on that date, when the monthly post goes out.
- freeze_now: a frozen clock for bot.py and for discord.py's task loop, which the test
  moves with set(). It also sets the zone of the host, which date.today() reads.
- gateway: real discord.py guilds, channels and members, built from
  gateway-shaped payloads on the bot client's own ConnectionState. Channel messages
  are captured at HTTPClient.send_message, after discord.py has built the final
  request payload (including the effective allowed_mentions) and before any HTTP.
  gateway.interaction() returns a FakeInteraction for slash commands, whose
  responses would go through Discord's webhook endpoints instead; it records the
  payload discord.py would build for each, embeds included, and the text the member
  reads in it, and carries the member's permissions.
  The gateway also answers the member checks (#74): a fake websocket answers Request
  Guild Members from every member the test added, cached or not, through discord.py's
  own chunk parser, and a fake Get Guild Member answers with the member or Discord's 404.
  gateway.leave() removes a member, as when they leave the server.
"""

import asyncio
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
POST_TIME = datetime.time(15, 0, tzinfo=datetime.UTC)


class FakeStore(fakeredis.FakeRedis):
    """A synchronous client on the fake server behind bot.redis_client, for seeding and checks."""

    def seed_birthday(self, guild_id, user_id, birthday, show_age=False):
        """Store birthday ("YYYY-MM-DD", or "--MM-DD" without a year) as /birthday set run in
        that guild does.

        That is the user's date, the user's show_age opt-in ("1", or no key when off), the
        user's id in the guild's set (each guild's views list only the members in its own
        set), and the guild's id in the user's list of guilds (which /birthday forget reads).
        """
        self.set(f"user:{user_id}:birthday", birthday)
        if show_age:
            self.set(f"user:{user_id}:show_age", "1")
        self.sadd(f"guild:{guild_id}:birthdays", user_id)
        self.sadd(f"user:{user_id}:guilds", guild_id)

    def contents(self):
        """Every key on the fake server and its value: a string, a set of strings, or a dict for a hash."""
        read = {"set": self.smembers, "hash": self.hgetall}
        return {key: read.get(self.type(key), self.get)(key) for key in self.keys()}

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
    datetime.now() read the moment there. CI and the bot's image run in UTC.
    """

    def __init__(self, moment, host_zone=datetime.UTC):
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

    def freeze(moment, host_zone=datetime.UTC):
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


# Discord's JSON error code for a member who is not in the server.
UNKNOWN_MEMBER = 10007


class RunningLoop:
    """Stands in for ConnectionState.loop, which login sets and the tests never do: each
    asyncio.run() has a loop of its own, and discord.py's ChunkRequest makes its futures on
    this one."""

    def __getattr__(self, name):
        return getattr(asyncio.get_running_loop(), name)


class FakeWebSocket:
    """The gateway websocket, as far as Guild.query_members uses it.

    request_chunks() is Request Guild Members (op 8). Like Discord, the fake answers a request by
    user_ids with the guild's members among them, every member the test added, cached or not,
    and at most `limit` of them, in one Guild Members Chunk with the request's nonce. The answer
    arrives on the next turn of the event loop, through ConnectionState.parse_guild_members_chunk,
    as a gateway event would. More than 100 user_ids fails the test: Discord refuses them.

    gateway.websocket_mode picks another outcome:
    - "ready": a READY arrives instead, after a reconnect with a new session; discord.py's
      parse_ready calls clear_chunk_requests(None), which answers each pending request with the
      members received so far, none.
    - "silent": no answer, so discord.py's wait times out.
    - "closed": the socket is closed, so sending raises ConnectionClosed.
    """

    def __init__(self, gateway):
        self.gateway = gateway

    async def request_chunks(self, guild_id, query=None, *, limit, user_ids=None, presences=False, nonce=None):
        gateway = self.gateway
        gateway.member_requests.append({"guild_id": guild_id, "user_ids": list(user_ids or []), "limit": limit})
        if query is not None or not user_ids or presences:
            pytest.fail(f"the bot should ask for members by user_ids only, without presences; asked {query!r}")
        if len(user_ids) > 100:
            pytest.fail(f"Discord refuses a member request with more than 100 user_ids; got {len(user_ids)}")
        if gateway.websocket_mode == "closed":
            raise discord.ConnectionClosed(types.SimpleNamespace(close_code=1006), shard_id=None)
        loop = asyncio.get_running_loop()
        if gateway.websocket_mode == "ready":
            loop.call_soon(gateway.state.clear_chunk_requests, None)
            return
        if gateway.websocket_mode == "silent":
            return
        members = gateway.members.get(guild_id, {})
        found = [members[int(user_id)] for user_id in user_ids if int(user_id) in members]
        chunk = {
            "guild_id": str(guild_id),
            "members": [member_payload(user_id, name) for user_id, name in found[:limit]],
            "chunk_index": 0,
            "chunk_count": 1,
            "not_found": [str(user_id) for user_id in user_ids if int(user_id) not in members],
            "nonce": nonce,
        }
        loop.call_soon(gateway.state.parse_guild_members_chunk, chunk)


class Gateway:
    """Feeds fake gateway payloads to the bot client's ConnectionState and records sends."""

    def __init__(self, state):
        self.state = state
        self.sent = []  # (channel_id, request payload), in send order
        self._ids = itertools.count(1)
        self.bot_user = discord.ClientUser(state=state, data=user_payload(BOT_ID, BOT_NAME, is_bot=True))
        # guild id -> {user id: (user id, name)}: every member of each guild, cached or not.
        self.members = {}
        self.websocket = FakeWebSocket(self)
        self.connected = True  # False: no websocket, as before the bot connects
        self.websocket_mode = "answer"
        self.member_requests = []  # each Request Guild Members: guild_id, user_ids and limit
        self.member_fetches = []  # (guild id, user id) of each Get Guild Member, in order
        self.fetch_errors = {}  # user id -> the exception Get Guild Member raises for them

    @classmethod
    def install(cls, monkeypatch, client):
        state = client._connection
        # Start from empty caches; monkeypatch puts the originals back afterwards.
        monkeypatch.setattr(state, "_guilds", {})
        monkeypatch.setattr(state, "_users", weakref.WeakValueDictionary())
        monkeypatch.setattr(state, "_chunk_requests", {})
        gateway = cls(state)
        monkeypatch.setattr(state, "user", gateway.bot_user)  # what READY sets
        monkeypatch.setattr(state, "loop", RunningLoop())
        # Client._get_websocket returns client.ws. Replacing the state's hook leaves client.ws
        # None, so the heartbeat task still sees no connection.
        monkeypatch.setattr(state, "_get_websocket", gateway.get_websocket)
        monkeypatch.setattr(state.http, "send_message", gateway.send_message)
        monkeypatch.setattr(state.http, "get_member", gateway.get_member)
        return gateway

    def get_websocket(self, guild_id=None, *, shard_id=None):
        return self.websocket if self.connected else None

    async def get_member(self, guild_id, member_id):
        """Get Guild Member: the member's payload, or Discord's 404 "Unknown Member"."""
        self.member_fetches.append((int(guild_id), int(member_id)))
        if int(member_id) in self.fetch_errors:
            raise self.fetch_errors[int(member_id)]
        member = self.members.get(int(guild_id), {}).get(int(member_id))
        if member is None:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"),
                {"code": UNKNOWN_MEMBER, "message": "Unknown Member"},
            )
        return member_payload(*member)

    def leave(self, guild, user):
        """user, a (user_id, name) member, leaves guild. The bot, without the members intent, gets
        no event for it; a member in voice leaves the voice channel, and so the cache."""
        del self.members[guild.id][user[0]]
        member = guild.get_member(user[0])
        if member is not None:
            guild._remove_member(member)

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
        self.members[guild_id] = {
            user_id: (user_id, user_name) for user_id, user_name in [(BOT_ID, BOT_NAME), *members, *uncached]
        }
        return self.state._add_guild_from_data(payload)

    @staticmethod
    def general(guild):
        return discord.utils.get(guild.text_channels, name="general")

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

    def interaction(self, guild, user, permissions=None):
        """A slash command run by user in guild. permissions: the member's discord.Permissions in the
        channel, which Discord sends with the interaction; none unless given."""
        if permissions is None:
            permissions = discord.Permissions.none()
        return FakeInteraction(guild, user, self.state, permissions)


@pytest.fixture
def gateway(monkeypatch):
    return Gateway.install(monkeypatch, bot.client)


class FakeInteraction:
    """The parts of discord.Interaction that bot.py's slash commands and its command error
    handler use.

    sent collects (content, kwargs) for every response and follow-up, in order.
    payloads holds, in the same order, the payload discord.py builds for each one with
    handle_message_parameters(): its content and embeds, and the call's allowed_mentions
    merged over the client-wide default, as Webhook.send does for a follow-up.
    texts() gives, in the same order, what the member reads in each one.
    deferred holds the keyword arguments of response.defer(), or None if it was not called.
    permissions is the member's discord.Permissions, as Interaction.permissions gives them.
    command is what Interaction.command gives the error handler: the slash command being run,
    which a test sets, or None for a command the bot does not know.
    """

    def __init__(self, guild, user, state, permissions):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.permissions = permissions
        self.command = None
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
            embed=kwargs.get("embed", discord.utils.MISSING),
            embeds=kwargs.get("embeds", discord.utils.MISSING),
            allowed_mentions=kwargs.get("allowed_mentions", discord.utils.MISSING),
            previous_allowed_mentions=self._state.allowed_mentions,
        ) as params:
            self.payloads.append(params.payload)

    def texts(self):
        """What the member reads in each response and follow-up, in order: its content, then the
        title and the description of each embed, one per line."""
        texts = []
        for content, kwargs in self.sent:
            embeds = [kwargs["embed"]] if kwargs.get("embed") is not None else list(kwargs.get("embeds", []))
            parts = [content, *(part for embed in embeds for part in (embed.title, embed.description))]
            texts.append("\n".join(part for part in parts if part))
        return texts


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
