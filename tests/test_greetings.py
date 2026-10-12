"""#34: the daily birthday greetings, which a server turns on with /birthday config.

/birthday config (Manage Server only) sets a server's channel, time of day and time zone, or turns
the greetings off. The task send_birthday_greetings runs every 5 minutes. Once a server's local
time reaches its greeting time, it posts one "Happy birthday" line per member who shared their
birthday with that server and has it on that local date, pinging those members and nobody else,
once per server and local date: a Redis marker per server and date holds across runs, restarts
and processes.

The clock is frozen (conftest.freeze_now): bot.py and discord.py's task loop read the moment the
test sets. Most tests run the task's body at chosen moments, as the loop does every 5 minutes;
the restart tests start the bot as at login, with setup_hook() and then READY. Each asyncio.run()
of a start is one process, with a Redis client of its own on the shared fakeredis server.
"""

import asyncio
import datetime
import functools
import logging
import types
from unittest import mock

import discord
import fakeredis
import pytest
import redis

import bot

UTC = datetime.UTC
FIVE_MINUTES = datetime.timedelta(minutes=5)
GUILD_A = 300000000000020000
GUILD_B = 300000000000021000
GUILD_C = 300000000000022000
ADMIN = (200000000000008001, "admin")
ALICE = (200000000000008002, "alice")
BOB = (200000000000008003, "bob")
CAROL = (200000000000008004, "carol")
# A display name a member controls, which must never reach a message or ping anyone.
HOSTILE = (200000000000008005, "@everyone")
MANAGE_SERVER = discord.Permissions(manage_guild=True)
# A moderator's permissions, without Manage Server.
MODERATOR = discord.Permissions(
    view_channel=True, send_messages=True, manage_messages=True, manage_channels=True, manage_roles=True
)


def utc(*fields):
    return datetime.datetime(*fields, tzinfo=UTC)


def greeting(*users):
    """The greeting for these (user_id, name) members, as one message."""
    return "\n".join(f"🎂 Happy birthday, <@{user_id}>!" for user_id, _ in sorted(users))


def server_of(store):
    return store.connection_pool.connection_kwargs["server"]


def logged_errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def run_config(gateway, guild, user, *args, permissions=MANAGE_SERVER, off=False):
    """Run /birthday config as user, a (user_id, name) member of guild with these permissions.
    args are channel, time and timezone. Returns the FakeInteraction."""
    interaction = gateway.interaction(guild, guild.get_member(user[0]), permissions)
    asyncio.run(bot.birthday_config.callback(interaction, *args, off=off))
    return interaction


def reply_of(interaction):
    """The one reply to an interaction. Fails unless it was deferred and answered ephemerally,
    so that only the member who ran the command sees it."""
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True]
    return interaction.sent[0][0]


def configure(gateway, guild, time_of_day, zone_name):
    """Turn on guild's greetings in its text channel with /birthday config, run by ADMIN, who
    must be a member of guild. Returns the channel."""
    channel = guild.text_channels[0]
    reply = reply_of(run_config(gateway, guild, ADMIN, channel, time_of_day, zone_name))
    if not reply.startswith("✅ Every day at"):
        pytest.fail(f"setup: /birthday config should turn on the greetings, got {reply!r}")
    return channel


def greetings_in(gateway, channel):
    """The text of each message the bot sent to channel."""
    return [payload.get("content") or "" for payload in gateway.sent_to(channel)]


async def every_5_minutes(clock, gateway, start, end):
    """Run the task's body every 5 minutes from start until end, as the loop does. Returns
    (moment, channel id, text) for each message sent, in order."""
    sent = []
    moment = start
    while moment < end:
        clock.set(moment)
        before = len(gateway.sent)
        await bot.send_birthday_greetings()
        sent += [(moment, channel_id, payload.get("content")) for channel_id, payload in gateway.sent[before:]]
        moment += FIVE_MINUTES
    return sent


def new_process(monkeypatch, store):
    """bot.redis_client becomes a new client on the store's fake server, as in a restarted bot."""
    client = fakeredis.FakeAsyncRedis(server=server_of(store), decode_responses=True)
    monkeypatch.setattr(bot, "redis_client", client)
    return client


@pytest.fixture
def runs(monkeypatch):
    """Each run of the greeting task's body, from the loop or a direct call: whether the client
    was ready when it began. A run is added when it ends."""
    task = bot.send_birthday_greetings
    body = task.coro
    ended = []

    @functools.wraps(body)
    async def recorded(*args, **kwargs):
        was_ready = bot.client.is_ready()
        try:
            return await body(*args, **kwargs)
        finally:
            ended.append(was_ready)

    monkeypatch.setattr(task, "coro", recorded)
    return ended


async def eventually(condition, timeout=5):  # noqa: ASYNC109 - returns False at the deadline instead of raising
    """Wait, in real time, until condition() is true; return whether it was within timeout seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def start_bot(monkeypatch, store):
    """Start a bot process up to READY, as main() and Client.login() do: a Redis client of its
    own, the event that wait_until_ready() waits for, then setup_hook(), which starts the task
    loops. CommandTree.sync is faked, so nothing reaches Discord."""
    new_process(monkeypatch, store)
    monkeypatch.setattr(bot.client, "_ready", asyncio.Event())
    monkeypatch.setattr(bot.client.tree, "sync", mock.AsyncMock(return_value=[]))
    await bot.client.setup_hook()


async def become_ready(runs):
    """READY. Returns once the greeting task's first run has ended."""
    count = len(runs)
    bot.client._connection.call_handlers("ready")
    if not await eventually(lambda: len(runs) > count):
        pytest.fail("the greeting task did not run after READY")


async def stop_bot():
    """Cancel the task loops setup_hook() started, as when the process stops."""
    loops = [bot.send_birthday_greetings, bot.check_upcoming_birthdays, bot.heartbeat]
    tasks = [loop.get_task() for loop in loops if loop.get_task() is not None]
    for loop in loops:
        loop.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# The greeting task


def test_the_task_waits_for_ready_then_runs_at_once_and_every_5_minutes(
    runs, fake_redis, freeze_now, gateway, monkeypatch
):
    # 09:30 in Berlin (UTC+2), after the 09:00 greeting time, on ALICE's birthday.
    freeze_now(utc(2026, 10, 14, 7, 30))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    channel = configure(gateway, guild, "09:00", "Europe/Berlin")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")

    async def scenario():
        await start_bot(monkeypatch, fake_redis)
        await asyncio.sleep(0.5)  # time for the task to run, if it does not wait for READY
        before_ready = (list(runs), greetings_in(gateway, channel))
        await become_ready(runs)
        after_ready = (list(runs), greetings_in(gateway, channel), bot.send_birthday_greetings.next_iteration)
        await stop_bot()
        return before_ready, after_ready

    before_ready, after_ready = asyncio.run(scenario())

    # Before READY the bot may not have its guilds yet, so a run could greet nowhere.
    assert before_ready == ([], [])
    # Once ready, it runs at once, greets ALICE, and runs again 5 minutes later.
    assert after_ready == ([True], [greeting(ALICE)], utc(2026, 10, 14, 7, 35))


def test_one_greeting_per_server_and_local_day_across_runs_and_restarts(
    runs, fake_redis, freeze_now, gateway, monkeypatch
):
    # Berlin is at UTC+2 in October. ALICE's birthday is Oct 14, and BOB's Oct 15, given without a year.
    clock = freeze_now(utc(2026, 10, 14, 6, 58))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB])
    channel = configure(gateway, guild, "09:00", "Europe/Berlin")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")
    fake_redis.seed_birthday(GUILD_A, BOB[0], "--10-15")
    counts = []  # (moment, greetings sent so far) after each run

    async def process(started, *later):
        """A bot process: up and ready at `started`, when the task runs at once; the task's body
        then runs at each later moment, as the loop does every 5 minutes."""
        clock.set(started)
        await start_bot(monkeypatch, fake_redis)
        await become_ready(runs)
        counts.append((started, len(greetings_in(gateway, channel))))
        for moment in later:
            clock.set(moment)
            await bot.send_birthday_greetings()
            counts.append((moment, len(greetings_in(gateway, channel))))
        await stop_bot()

    # Times in Berlin. Up at 08:58, before the greeting time, and running at 09:00 and 09:05.
    asyncio.run(process(utc(2026, 10, 14, 6, 58), utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 5)))
    # Restarted at 13:00 and running until 23:55; restarted again at 23:58.
    asyncio.run(process(utc(2026, 10, 14, 11, 0), utc(2026, 10, 14, 11, 5), utc(2026, 10, 14, 21, 55)))
    asyncio.run(process(utc(2026, 10, 14, 21, 58)))
    # Restarted at 10:00 the next day, and running until 23:55.
    asyncio.run(process(utc(2026, 10, 15, 8, 0), utc(2026, 10, 15, 8, 5), utc(2026, 10, 15, 21, 55)))

    if len(runs) != len(counts):
        pytest.fail(f"setup: every run should be counted, got {len(runs)} runs and {counts!r}")
    assert counts == [
        (utc(2026, 10, 14, 6, 58), 0),
        (utc(2026, 10, 14, 7, 0), 1),
        (utc(2026, 10, 14, 7, 5), 1),
        (utc(2026, 10, 14, 11, 0), 1),
        (utc(2026, 10, 14, 11, 5), 1),
        (utc(2026, 10, 14, 21, 55), 1),
        (utc(2026, 10, 14, 21, 58), 1),
        (utc(2026, 10, 15, 8, 0), 2),
        (utc(2026, 10, 15, 8, 5), 2),
        (utc(2026, 10, 15, 21, 55), 2),
    ]
    assert greetings_in(gateway, channel) == [greeting(ALICE), greeting(BOB)]


# (time zone, the local date, the greeting time, when the greeting goes out in UTC). New York
# springs forward on Mar 8, 2026 (02:00 EST becomes 03:00 EDT, so 02:30 never shows) and falls
# back on Nov 1 (02:00 EDT becomes 01:00 EST, so 01:30 shows twice). Berlin springs forward on
# Mar 29 (02:00 CET becomes 03:00 CEST) and falls back on Oct 25 (03:00 CEST becomes 02:00 CET).
DST_DAYS = [
    pytest.param(
        "America/New_York", datetime.date(2026, 3, 8), "02:30", utc(2026, 3, 8, 7), id="New York, 02:30 skipped"
    ),
    pytest.param(
        "America/New_York", datetime.date(2026, 3, 8), "09:00", utc(2026, 3, 8, 13), id="New York, spring forward"
    ),
    pytest.param(
        "America/New_York", datetime.date(2026, 11, 1), "01:30", utc(2026, 11, 1, 5, 30), id="New York, 01:30 twice"
    ),
    pytest.param(
        "America/New_York", datetime.date(2026, 11, 1), "09:00", utc(2026, 11, 1, 14), id="New York, fall back"
    ),
    pytest.param("Europe/Berlin", datetime.date(2026, 3, 29), "02:30", utc(2026, 3, 29, 1), id="Berlin, 02:30 skipped"),
    pytest.param(
        "Europe/Berlin", datetime.date(2026, 10, 25), "02:30", utc(2026, 10, 25, 0, 30), id="Berlin, 02:30 twice"
    ),
]


@pytest.mark.parametrize(("zone_name", "day", "greeting_time", "greeted_at"), DST_DAYS)
def test_one_greeting_at_the_local_time_on_the_days_the_clocks_change(
    zone_name, day, greeting_time, greeted_at, fake_redis, freeze_now, gateway
):
    clock = freeze_now(utc(day.year, day.month, day.day))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    channel = configure(gateway, guild, greeting_time, zone_name)
    fake_redis.seed_birthday(GUILD_A, ALICE[0], f"1990-{day:%m-%d}")
    # From noon UTC the day before to noon UTC the day after: the whole local day and more.
    start = utc(day.year, day.month, day.day) - datetime.timedelta(hours=12)

    sent = asyncio.run(every_5_minutes(clock, gateway, start, start + datetime.timedelta(hours=48)))

    assert sent == [(greeted_at, channel.id, greeting(ALICE))]


def test_dec_31_and_jan_1_are_greeted_on_their_local_dates_east_and_west_of_utc(fake_redis, freeze_now, gateway):
    # Auckland is at UTC+13 in summer: its Dec 31, 09:00 is Dec 30, 20:00 UTC, and its Jan 1,
    # 09:00 is Dec 31, 20:00 UTC. Los Angeles is at UTC-8: its Dec 31, 09:00 is 17:00 UTC, and
    # its Dec 31 lasts until Jan 1, 08:00 UTC.
    clock = freeze_now(utc(2026, 12, 30))
    auckland = gateway.add_guild(GUILD_A, "Auckland", [ADMIN, ALICE, BOB])
    los_angeles = gateway.add_guild(GUILD_B, "Los Angeles", [ADMIN, ALICE, BOB])
    auckland_channel = configure(gateway, auckland, "09:00", "Pacific/Auckland")
    los_angeles_channel = configure(gateway, los_angeles, "09:00", "America/Los_Angeles")
    for guild_id in (GUILD_A, GUILD_B):
        fake_redis.seed_birthday(guild_id, ALICE[0], "1990-12-31")
        fake_redis.seed_birthday(guild_id, BOB[0], "1992-01-01")

    sent = asyncio.run(every_5_minutes(clock, gateway, utc(2026, 12, 30), utc(2027, 1, 2, 12)))

    assert sent == [
        (utc(2026, 12, 30, 20), auckland_channel.id, greeting(ALICE)),
        (utc(2026, 12, 31, 17), los_angeles_channel.id, greeting(ALICE)),
        (utc(2026, 12, 31, 20), auckland_channel.id, greeting(BOB)),
        (utc(2027, 1, 1, 17), los_angeles_channel.id, greeting(BOB)),
    ]
    assert sorted(fake_redis.keys("greeted:*")) == [
        f"greeted:{GUILD_A}:2026-12-31",
        f"greeted:{GUILD_A}:2027-01-01",
        f"greeted:{GUILD_B}:2026-12-31",
        f"greeted:{GUILD_B}:2027-01-01",
    ]


@pytest.mark.parametrize(
    ("day", "greeted"),
    [
        pytest.param(datetime.date(2027, 2, 28), True, id="Feb 28, 2027"),
        pytest.param(datetime.date(2027, 3, 1), False, id="Mar 1, 2027"),
        pytest.param(datetime.date(2028, 2, 28), False, id="Feb 28, 2028"),
        pytest.param(datetime.date(2028, 2, 29), True, id="Feb 29, 2028"),
    ],
)
def test_a_feb_29_birthday_is_greeted_on_feb_28_in_a_common_year(day, greeted, fake_redis, freeze_now, gateway):
    # ALICE gave the year 2000, BOB no year; both were born on Feb 29.
    freeze_now(utc(day.year, day.month, day.day, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB])
    channel = configure(gateway, guild, "09:00", "UTC")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "2000-02-29")
    fake_redis.seed_birthday(GUILD_A, BOB[0], "--02-29")

    asyncio.run(bot.send_birthday_greetings())

    assert greetings_in(gateway, channel) == ([greeting(ALICE, BOB)] if greeted else [])


def test_a_birthday_without_a_year_is_greeted_and_no_year_or_age_shows(fake_redis, freeze_now, gateway):
    freeze_now(utc(2026, 10, 14, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB, CAROL])
    channel = configure(gateway, guild, "09:00", "UTC")
    # ALICE gave no year. BOB gave 1990 and turned on show_age, so he turns 36 today, but the
    # greeting shows neither. CAROL's birthday is tomorrow.
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "--10-14")
    fake_redis.seed_birthday(GUILD_A, BOB[0], "1990-10-14", show_age=True)
    fake_redis.seed_birthday(GUILD_A, CAROL[0], "1990-10-15", show_age=True)

    asyncio.run(bot.send_birthday_greetings())

    [text] = greetings_in(gateway, channel)
    assert text == greeting(ALICE, BOB)
    assert "1990" not in text
    assert "36" not in text


def test_the_greeting_pings_only_the_birthday_members(fake_redis, freeze_now, gateway):
    freeze_now(utc(2026, 10, 14, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB, CAROL, HOSTILE])
    channel = configure(gateway, guild, "09:00", "UTC")
    for user, birthday in [(ALICE, "1990-10-14"), (BOB, "--10-14"), (CAROL, "1990-10-15"), (HOSTILE, "1991-10-14")]:
        fake_redis.seed_birthday(GUILD_A, user[0], birthday)

    asyncio.run(bot.send_birthday_greetings())

    # The request payload discord.py built: the mentions of the three birthday members ping them,
    # and nothing else pings anyone: no @everyone, @here or role, whatever the content holds.
    [payload] = gateway.sent_to(channel)
    if payload.get("content") != greeting(ALICE, BOB, HOSTILE):
        pytest.fail(f"setup: expected one greeting for ALICE, BOB and HOSTILE, got {payload!r}")
    assert payload["allowed_mentions"] == {"parse": [], "users": sorted([ALICE[0], BOB[0], HOSTILE[0]])}
    assert HOSTILE[1] not in payload["content"]


def test_a_greeting_for_many_members_is_split_and_each_message_pings_the_members_it_names(
    fake_redis, freeze_now, gateway
):
    freeze_now(utc(2026, 10, 14, 12))
    members = [(200000000000009000 + i, f"member-{i:03d}") for i in range(150)]
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN], uncached=members)
    channel = configure(gateway, guild, "09:00", "UTC")
    for user_id, _ in members:
        fake_redis.seed_birthday(GUILD_A, user_id, "--10-14")

    asyncio.run(bot.send_birthday_greetings())

    payloads = gateway.sent_to(channel)
    if len(payloads) < 2:
        pytest.fail(f"setup: 150 lines should take more than one message, got {len(payloads)}")
    named = [[user_id for user_id, _ in members if f"<@{user_id}>" in payload["content"]] for payload in payloads]
    assert max(len(payload["content"]) for payload in payloads) <= 2000
    assert sorted(user_id for message in named for user_id in message) == [user_id for user_id, _ in members]
    # Discord takes at most 100 users in allowed_mentions.
    assert [payload["allowed_mentions"] for payload in payloads] == [
        {"parse": [], "users": message} for message in named
    ]
    assert max(len(message) for message in named) <= 100


def test_a_member_is_greeted_only_in_the_servers_that_have_their_birthday(fake_redis, freeze_now, gateway):
    freeze_now(utc(2026, 10, 14, 12))
    # ALICE is a member of both servers and shared her birthday with server A only.
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, ALICE, BOB])
    channel_a = configure(gateway, guild_a, "09:00", "UTC")
    channel_b = configure(gateway, guild_b, "09:00", "UTC")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1992-10-14")

    asyncio.run(bot.send_birthday_greetings())

    assert greetings_in(gateway, channel_a) == [greeting(ALICE)]
    assert greetings_in(gateway, channel_b) == [greeting(BOB)]


def test_greetings_are_off_until_a_server_turns_them_on_and_after_it_turns_them_off(fake_redis, freeze_now, gateway):
    clock = freeze_now(utc(2026, 10, 14, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")
    fake_redis.seed_birthday(GUILD_A, BOB[0], "1990-10-15")
    stored = fake_redis.contents()

    asyncio.run(bot.send_birthday_greetings())
    never_turned_on = (list(gateway.sent), fake_redis.contents())
    channel = configure(gateway, guild, "09:00", "UTC")
    asyncio.run(bot.send_birthday_greetings())
    turned_on = greetings_in(gateway, channel)
    reply = reply_of(run_config(gateway, guild, ADMIN, off=True))
    clock.set(utc(2026, 10, 15, 12))  # BOB's birthday
    asyncio.run(bot.send_birthday_greetings())

    if turned_on != [greeting(ALICE)]:
        pytest.fail(f"setup: once turned on, the server should get ALICE's greeting, got {turned_on!r}")
    # Nothing is sent, and nothing is stored, for a server that never turned them on.
    assert never_turned_on == ([], stored)
    assert reply == bot.CONFIG_OFF
    assert greetings_in(gateway, channel) == [greeting(ALICE)]
    assert fake_redis.exists(f"guild:{GUILD_A}:greetings") == 0


def test_two_runs_at_once_greet_once(fake_redis, freeze_now, gateway):
    # As when a new process starts before the old one has stopped: both read the birthdays, and
    # only the one that sets the server's marker for the date first greets there.
    freeze_now(utc(2026, 10, 14, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    channel = configure(gateway, guild, "09:00", "UTC")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")

    async def two_runs():
        await asyncio.gather(bot.send_birthday_greetings(), bot.send_birthday_greetings())

    asyncio.run(two_runs())

    assert greetings_in(gateway, channel) == [greeting(ALICE)]


def test_a_greeting_stores_only_a_marker_that_redis_deletes_after_3_days(fake_redis, freeze_now, gateway):
    # 21:00 UTC on Oct 14 is 10:00 on Oct 15 in Auckland, at UTC+13.
    freeze_now(utc(2026, 10, 14, 21))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    channel = configure(gateway, guild, "09:00", "Pacific/Auckland")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-15")
    before = fake_redis.contents()

    asyncio.run(bot.send_birthday_greetings())

    if greetings_in(gateway, channel) != [greeting(ALICE)]:
        pytest.fail(f"setup: the task should greet ALICE, got {gateway.sent!r}")
    marker = f"greeted:{GUILD_A}:2026-10-15"
    # The server's ID and its local date, and no member's data.
    assert fake_redis.contents() == {**before, marker: "1"}
    assert 3 * 86400 - 60 <= fake_redis.ttl(marker) <= 3 * 86400


# What goes wrong


def test_a_greeting_that_fails_to_send_is_logged_and_not_sent_again(
    fake_redis, freeze_now, gateway, monkeypatch, caplog
):
    freeze_now(utc(2026, 10, 14, 12))
    refused = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    other = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, BOB])
    refused_channel = configure(gateway, refused, "09:00", "UTC")
    other_channel = configure(gateway, other, "09:00", "UTC")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1990-10-14")
    attempts = []
    send_message = gateway.send_message

    # Discord refuses the greeting in server A, as when the bot may not write in its channel.
    async def refuse_in_guild_a(channel_id, *, params):
        attempts.append(int(channel_id))
        if int(channel_id) == refused_channel.id:
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"}
            )
        return await send_message(channel_id, params=params)

    monkeypatch.setattr(bot.client._connection.http, "send_message", refuse_in_guild_a)

    for _ in ("the run at 12:00", "a restart later that day"):
        new_process(monkeypatch, fake_redis)
        asyncio.run(bot.send_birthday_greetings())

    # The marker is set before sending, so a failed greeting is not retried: at most one a day.
    assert sorted(attempts) == sorted([refused_channel.id, other_channel.id])
    assert greetings_in(gateway, other_channel) == [greeting(BOB)]
    assert [message for message in logged_errors(caplog) if str(GUILD_A) in message and "Forbidden" in message] != []


def test_a_server_whose_settings_cannot_be_read_is_skipped_and_greeted_once_storage_is_back(
    fake_redis, freeze_now, gateway, monkeypatch, caplog
):
    freeze_now(utc(2026, 10, 14, 12))
    unreadable = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    readable = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, BOB])
    unreadable_channel = configure(gateway, unreadable, "09:00", "UTC")
    readable_channel = configure(gateway, readable, "09:00", "UTC")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1990-10-14")
    if [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should reach server A first")
    hgetall = bot.redis_client.hgetall

    # Reading server A's settings fails, as when the connection drops; the next read works.
    async def hgetall_failing_for_guild_a(key):
        if key == f"guild:{GUILD_A}:greetings":
            raise redis.ConnectionError("Connection reset by peer")
        return await hgetall(key)

    monkeypatch.setattr(bot.redis_client, "hgetall", hgetall_failing_for_guild_a)
    # An exception here would end the task: discord.ext.tasks retries only network errors such
    # as OSError, and redis.RedisError is not one.
    asyncio.run(bot.send_birthday_greetings())
    while_failing = (greetings_in(gateway, unreadable_channel), greetings_in(gateway, readable_channel))
    monkeypatch.setattr(bot.redis_client, "hgetall", hgetall)
    asyncio.run(bot.send_birthday_greetings())

    assert while_failing == ([], [greeting(BOB)])
    assert [message for message in logged_errors(caplog) if str(GUILD_A) in message] != []
    # Server A was not marked, so the next run greets it, and server B is not greeted again.
    assert greetings_in(gateway, unreadable_channel) == [greeting(ALICE)]
    assert greetings_in(gateway, readable_channel) == [greeting(BOB)]


def test_unusable_settings_or_a_deleted_channel_skip_only_that_server(fake_redis, freeze_now, gateway, caplog):
    freeze_now(utc(2026, 10, 14, 12))
    unknown_zone = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    deleted_channel = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, ALICE])
    working = gateway.add_guild(GUILD_C, "Guild C", [ADMIN, ALICE])
    for guild in (unknown_zone, deleted_channel, working):
        configure(gateway, guild, "09:00", "UTC")
        fake_redis.seed_birthday(guild.id, ALICE[0], "1990-10-14")
    # A time zone this host does not know, as after an update of its time zone database.
    fake_redis.hset(f"guild:{GUILD_A}:greetings", "timezone", "Mars/Olympus_Mons")
    deleted_channel._remove_channel(deleted_channel.text_channels[0])

    for _ in range(2):
        asyncio.run(bot.send_birthday_greetings())

    assert [channel_id for channel_id, _ in gateway.sent] == [working.text_channels[0].id]
    errors = logged_errors(caplog)
    # Unusable settings are logged at every run, a deleted channel once: the date is marked first.
    assert len([message for message in errors if str(GUILD_A) in message]) == 2
    assert len([message for message in errors if str(GUILD_B) in message]) == 1
    assert sorted(fake_redis.keys("greeted:*")) == [f"greeted:{GUILD_B}:2026-10-14", f"greeted:{GUILD_C}:2026-10-14"]


def test_an_unreadable_birthday_is_skipped_and_logged_without_it(fake_redis, freeze_now, gateway, caplog, capsys):
    freeze_now(utc(2026, 10, 14, 12))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE, BOB])
    channel = configure(gateway, guild, "09:00", "UTC")
    # ALICE's date in a form parse_birthday() does not read.
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "10-14-1990")
    fake_redis.seed_birthday(GUILD_A, BOB[0], "1990-10-14")

    asyncio.run(bot.send_birthday_greetings())

    assert greetings_in(gateway, channel) == [greeting(BOB)]
    assert [message for message in logged_errors(caplog) if str(ALICE[0]) in message] != []
    printed = capsys.readouterr()
    assert [form for form in ("10-14-1990", "1990", "10-14") if form in caplog.text + printed.out + printed.err] == []


# /birthday config


def test_birthday_config_stores_the_settings_and_answers_only_the_admin(fake_redis, gateway, monkeypatch):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN])
    channel = guild.text_channels[0]
    interaction = gateway.interaction(guild, guild.get_member(ADMIN[0]), MANAGE_SERVER)
    # Whether the interaction was answered each time the command took a Redis connection.
    answered = []
    pool = bot.redis_client.connection_pool
    get_connection = pool.get_connection

    async def get_connection_checking_the_answer(*args, **kwargs):
        answered.append(interaction.response.is_done())
        return await get_connection(*args, **kwargs)

    monkeypatch.setattr(pool, "get_connection", get_connection_checking_the_answer)

    # As typed, with spaces around the values and the hour in one digit.
    asyncio.run(bot.birthday_config.callback(interaction, channel, " 9:05 ", " Europe/Berlin "))

    assert reply_of(interaction) == (
        f"✅ Every day at 09:05 (Europe/Berlin), the bot wishes a happy birthday in <#{channel.id}> to the members "
        "who shared their birthday with this server and have it that day. It pings them, and shows no year or age."
    )
    assert fake_redis.contents() == {
        f"guild:{GUILD_A}:greetings": {"channel": str(channel.id), "time": "09:05", "timezone": "Europe/Berlin"}
    }
    assert answered != [] and all(answered)


@pytest.mark.parametrize("off", [False, True], ids=["turn on", "turn off"])
def test_birthday_config_is_refused_without_manage_server(off, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    configure(gateway, guild, "09:00", "UTC")
    before = fake_redis.contents()
    args = () if off else (guild.text_channels[0], "18:00", "Europe/Berlin")

    interaction = run_config(gateway, guild, ALICE, *args, permissions=MODERATOR, off=off)

    assert reply_of(interaction) == bot.CONFIG_NOT_ALLOWED
    assert fake_redis.contents() == before


@pytest.mark.parametrize("time_of_day", ["24:00", "9", "09:60", "9am", "9:5", "09:00:00", "", "21h30", "٠٩:٣٠"])
def test_birthday_config_refuses_a_time_that_is_not_hh_mm(time_of_day, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN])

    interaction = run_config(gateway, guild, ADMIN, guild.text_channels[0], time_of_day, "Europe/Berlin")

    assert reply_of(interaction) == bot.CONFIG_BAD_TIME
    assert fake_redis.contents() == {}


@pytest.mark.parametrize(
    "zone_name", ["Mars/Olympus_Mons", "", "UTC+2", "America", "zone.tab", "../../etc/passwd", "/etc/localtime"]
)
def test_birthday_config_refuses_an_unknown_time_zone(zone_name, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN])

    interaction = run_config(gateway, guild, ADMIN, guild.text_channels[0], "09:00", zone_name)

    assert reply_of(interaction) == bot.CONFIG_BAD_ZONE
    assert fake_redis.contents() == {}


@pytest.mark.parametrize("missing", ["channel", "time", "timezone"])
def test_birthday_config_needs_the_channel_the_time_and_the_time_zone(missing, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN])
    args = {"channel": guild.text_channels[0], "time": "09:00", "timezone": "Europe/Berlin"}
    args[missing] = None

    interaction = run_config(gateway, guild, ADMIN, *args.values())

    assert reply_of(interaction) == bot.CONFIG_INCOMPLETE
    assert fake_redis.contents() == {}


@pytest.mark.parametrize("off", [False, True], ids=["turn on", "turn off"])
def test_birthday_config_says_so_when_storage_is_unavailable(off, fake_redis, gateway, caplog):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN])
    configure(gateway, guild, "09:00", "UTC")
    before = fake_redis.contents()
    args = () if off else (guild.text_channels[0], "18:00", "Europe/Berlin")
    server_of(fake_redis).connected = False  # every command raises redis.ConnectionError

    interaction = run_config(gateway, guild, ADMIN, *args, off=off)

    server_of(fake_redis).connected = True
    assert reply_of(interaction) == bot.CONFIG_FAILED
    assert fake_redis.contents() == before
    assert logged_errors(caplog) != []


def test_leaving_a_server_deletes_its_greeting_settings(fake_redis, gateway):
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, ALICE])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, BOB])
    configure(gateway, guild_b, "18:00", "America/New_York")
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1990-10-14")
    other_server = fake_redis.contents()
    configure(gateway, guild_a, "09:00", "Europe/Berlin")
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-14")

    # Through the client, where discord.py looks up the handler when Discord reports that the
    # bot was removed from a server (GUILD_DELETE).
    asyncio.run(bot.client.on_guild_remove(guild_a))

    # Server A's settings are gone with its birthdays; server B keeps its own.
    assert fake_redis.contents() == other_server
