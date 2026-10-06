"""#14: the monthly post goes out at 15:00 UTC on the 1st, never before READY, and once per
guild and month, however often the bot restarts.

The task is discord.py's own Loop, started by the client's setup_hook as at login, with
Client.tree.sync faked so that nothing reaches Discord. READY is what discord.py's
ConnectionState does once the guilds have arrived: it calls the client's "ready" handler,
which sets the event Client.wait_until_ready() waits for.

The clock is frozen (conftest.freeze_now): bot.py and the task loop read the moment the test
sets, and only the test moves it. The loop's timer still waits real time, for the fraction of
a second between the frozen moment and 15:00. Each asyncio.run() of a start is one process,
with a Redis client of its own on the shared fakeredis server, as when the bot restarts.
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

UTC = datetime.timezone.utc
# At 15:00 UTC a host at UTC+10 is already on the next day.
UTC_PLUS_10 = datetime.timezone(datetime.timedelta(hours=10))
GUILD_A = 300000000000017000
GUILD_B = 300000000000018000
ALICE = (200000000000007001, "alice-in-a")
BOB = (200000000000007002, "bob-in-b")
# Both fall in the post of Oct 1, which covers October to December.
ALICE_BIRTHDAY = datetime.date(1990, 10, 15)
BOB_BIRTHDAY = datetime.date(1992, 11, 20)
# How a date could reach the log: the birth dates as bot.py writes them, and the post's month.
DATE_FORMS = [
    form
    for birthday in (ALICE_BIRTHDAY, BOB_BIRTHDAY)
    for form in (
        birthday.isoformat(),
        birthday.strftime("%m-%d-%Y"),
        birthday.strftime("%m-%d"),
        birthday.strftime("%B %d"),
        str(birthday.year),
    )
] + ["2026-10"]


def utc(*fields):
    return datetime.datetime(*fields, tzinfo=UTC)


def server_of(store):
    return store.connection_pool.connection_kwargs["server"]


def posts(gateway, guild):
    """The text of each message the bot sent to the guild's #general."""
    return [payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild))]


def logged_errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def two_guilds(gateway, store):
    """Guild A, where ALICE shared her birthday, then guild B, where BOB shared his."""
    guilds = [gateway.add_guild(GUILD_A, "Guild A", [ALICE]), gateway.add_guild(GUILD_B, "Guild B", [BOB])]
    store.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())
    store.seed_birthday(GUILD_B, BOB[0], BOB_BIRTHDAY.isoformat())
    return guilds


def new_process(monkeypatch, store):
    """bot.redis_client becomes a new client on the store's fake server, as in a restarted bot."""
    client = fakeredis.FakeAsyncRedis(server=server_of(store), decode_responses=True)
    monkeypatch.setattr(bot, "redis_client", client)
    return client


@pytest.fixture
def runs(monkeypatch):
    """Each run of the task's body, from the loop or at startup: whether the client was
    ready when the run began. A run is added when it ends."""
    task = bot.check_upcoming_birthdays
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


async def eventually(condition, timeout=5):
    """Wait, in real time, until condition() is true; return whether it was within timeout seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def start_bot(monkeypatch, store):
    """Start a bot process up to the gateway's READY. Returns its Redis client.

    As main() and Client.login() do: a Redis client of its own, the event that
    wait_until_ready() waits for, then setup_hook(), which starts the task.
    """
    client = new_process(monkeypatch, store)
    monkeypatch.setattr(bot.client, "_ready", asyncio.Event())  # as Client._async_setup_hook() makes it
    monkeypatch.setattr(bot.client.tree, "sync", mock.AsyncMock(return_value=[]))  # no request to Discord
    # The Loop keeps the previous process's next run until its first one is scheduled.
    monkeypatch.setattr(bot.check_upcoming_birthdays, "_next_iteration", None)
    await bot.client.setup_hook()
    return client


async def become_ready():
    """READY. Returns once the task has scheduled its next run (after any post at startup), or has ended."""
    bot.client._connection.call_handlers("ready")
    task = bot.check_upcoming_birthdays
    if not await eventually(lambda: task.next_iteration is not None or task.get_task().done()):
        pytest.fail("the task neither scheduled its next run nor ended")


async def stop_bot():
    task = bot.check_upcoming_birthdays.get_task()
    bot.check_upcoming_birthdays.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def run_at(clock, moment, runs):
    """Move the clock to moment, just after the task's next run is due, and wait for that run to end.

    The loop's timer was set from the frozen time, a fraction of a second before the run.
    """
    count = len(runs)
    clock.set(moment)
    if not await eventually(lambda: len(runs) > count):
        pytest.fail(f"the task did not run; its next run is {bot.check_upcoming_birthdays.next_iteration}")


def test_the_task_does_not_run_before_ready(runs, fake_redis, freeze_now, gateway, monkeypatch):
    # After 15:00 UTC on the 1st, so the task posts as soon as it may.
    freeze_now(utc(2026, 10, 1, 15, 1))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())

    async def scenario():
        await start_bot(monkeypatch, fake_redis)
        await asyncio.sleep(0.5)  # time for the task to run, if it does not wait for READY
        before_ready = (list(runs), posts(gateway, guild))
        await become_ready()
        after_ready = (list(runs), posts(gateway, guild), bot.check_upcoming_birthdays.next_iteration)
        await stop_bot()
        return before_ready, after_ready

    before_ready, (runs_after_ready, posted, next_run) = asyncio.run(scenario())

    # Before READY the bot has no guilds in production: a run then would post nowhere.
    assert before_ready == ([], [])
    # Once ready, it runs once and posts this month's list, then waits for the 2nd.
    assert runs_after_ready == [True]
    assert len(posted) == 1 and f"<@{ALICE[0]}>" in posted[0]
    assert next_run == utc(2026, 10, 2, 15)


@pytest.mark.parametrize(
    ("ready_at", "posts_at_once", "next_run"),
    [
        pytest.param(utc(2026, 10, 1, 0, 30), False, utc(2026, 10, 1, 15), id="the 1st, 00:30"),
        pytest.param(utc(2026, 10, 1, 14, 59), False, utc(2026, 10, 1, 15), id="the 1st, 14:59"),
        pytest.param(utc(2026, 10, 1, 15, 1), True, utc(2026, 10, 2, 15), id="the 1st, 15:01"),
        pytest.param(utc(2026, 10, 1, 23, 59), True, utc(2026, 10, 2, 15), id="the 1st, 23:59"),
        pytest.param(utc(2026, 10, 2, 15, 1), False, utc(2026, 10, 3, 15), id="the 2nd, 15:01"),
        pytest.param(utc(2026, 9, 30, 23, 59), False, utc(2026, 10, 1, 15), id="Sep 30, 23:59"),
        pytest.param(utc(2026, 12, 31, 16), False, utc(2027, 1, 1, 15), id="Dec 31, 16:00"),
    ],
)
def test_after_ready_the_task_posts_at_once_only_on_the_1st_after_15_utc(
    ready_at, posts_at_once, next_run, fake_redis, freeze_now, gateway, monkeypatch
):
    freeze_now(ready_at)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())

    async def scenario():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        result = (len(posts(gateway, guild)), bot.check_upcoming_birthdays.next_iteration)
        await stop_bot()
        return result

    posted, scheduled = asyncio.run(scenario())

    # discord.py schedules the first run at the next 15:00 UTC. On the 1st after 15:00 that is
    # the 2nd, so the bot posts once it is ready, or the month would have no post.
    assert posted == (1 if posts_at_once else 0)
    assert scheduled == next_run


@pytest.mark.parametrize(
    ("runs_at", "host_zone", "posts_then"),
    [
        pytest.param(utc(2026, 10, 1, 15, 0, 0, 500000), UTC, True, id="Oct 1"),
        pytest.param(utc(2026, 10, 2, 15, 0, 0, 500000), UTC, False, id="Oct 2"),
        pytest.param(utc(2026, 10, 1, 15, 0, 0, 500000), UTC_PLUS_10, True, id="Oct 1, host at UTC+10"),
        pytest.param(utc(2026, 9, 30, 15, 0, 0, 500000), UTC_PLUS_10, False, id="Sep 30, host at UTC+10"),
    ],
)
def test_the_task_runs_at_15_utc_and_posts_on_the_1st_in_utc(
    runs_at, host_zone, posts_then, runs, fake_redis, freeze_now, gateway, monkeypatch
):
    # The bot is ready 0.2 s before the run is due.
    clock = freeze_now(runs_at - datetime.timedelta(seconds=0.7), host_zone)
    guilds = two_guilds(gateway, fake_redis)

    async def scenario():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        at_ready = (bot.check_upcoming_birthdays.next_iteration, list(runs), list(gateway.sent))
        await run_at(clock, runs_at, runs)
        after = (list(runs), [len(posts(gateway, guild)) for guild in guilds], bot.check_upcoming_birthdays.next_iteration)
        await stop_bot()
        return at_ready, after

    at_ready, after = asyncio.run(scenario())

    due = runs_at.replace(microsecond=0)
    assert at_ready == (due, [], [])
    assert after == ([True], [1, 1] if posts_then else [0, 0], due + datetime.timedelta(days=1))


def test_a_restart_after_the_post_does_not_post_it_again(runs, fake_redis, freeze_now, gateway, monkeypatch):
    clock = freeze_now(utc(2026, 10, 1, 14, 59, 59, 800000))
    guilds = two_guilds(gateway, fake_redis)

    async def up_at_15():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        await run_at(clock, utc(2026, 10, 1, 15, 0, 0, 500000), runs)
        await stop_bot()

    async def restarted():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        await stop_bot()

    asyncio.run(up_at_15())
    if [len(posts(gateway, guild)) for guild in guilds] != [1, 1]:
        pytest.fail(f"setup: the 15:00 run should post once in each guild, got {gateway.sent!r}")
    clock.set(utc(2026, 10, 1, 15, 1))  # a deploy restarts the bot a minute later
    asyncio.run(restarted())

    # The restarted bot ran the task once it was ready, and posted nothing more.
    assert runs == [True, True]
    assert [len(posts(gateway, guild)) for guild in guilds] == [1, 1]


def test_a_bot_started_after_15_utc_on_the_1st_posts_once(runs, fake_redis, freeze_now, gateway, monkeypatch):
    clock = freeze_now(utc(2026, 10, 1, 15, 1))  # not running at 15:00, as during a deploy

    async def one_process():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        await stop_bot()

    guilds = two_guilds(gateway, fake_redis)
    asyncio.run(one_process())
    first_start = [len(posts(gateway, guild)) for guild in guilds]
    clock.set(utc(2026, 10, 1, 15, 5))  # and restarted again
    asyncio.run(one_process())

    assert first_start == [1, 1]
    assert runs == [True, True]
    assert [len(posts(gateway, guild)) for guild in guilds] == [1, 1]


def test_a_restart_posts_only_in_the_guilds_the_first_run_skipped(
    runs, fake_redis, freeze_now, gateway, monkeypatch, caplog
):
    clock = freeze_now(utc(2026, 10, 1, 14, 59, 59, 800000))
    posted, skipped = two_guilds(gateway, fake_redis)
    # Guild A, which the first run posts in, comes first: the restarted bot must go on to guild B.
    if [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should reach guild A first")

    async def up_at_15():
        client = await start_bot(monkeypatch, fake_redis)
        smembers = client.smembers

        # At 15:00 reading guild B's members fails, as when the connection drops.
        async def smembers_failing_for_guild_b(key):
            if key == f"guild:{GUILD_B}:birthdays":
                raise redis.ConnectionError("Connection reset by peer")
            return await smembers(key)

        monkeypatch.setattr(client, "smembers", smembers_failing_for_guild_b)
        await become_ready()
        await run_at(clock, utc(2026, 10, 1, 15, 0, 0, 500000), runs)
        await stop_bot()

    async def restarted():
        await start_bot(monkeypatch, fake_redis)
        await become_ready()
        await stop_bot()

    asyncio.run(up_at_15())
    if (len(posts(gateway, posted)), len(posts(gateway, skipped))) != (1, 0):
        pytest.fail(f"setup: the 15:00 run should post only in guild A, got {gateway.sent!r}")
    if [message for message in logged_errors(caplog) if str(GUILD_B) in message] == []:
        pytest.fail("setup: the 15:00 run should log that it skipped guild B")
    clock.set(utc(2026, 10, 1, 15, 30))
    asyncio.run(restarted())

    assert (len(posts(gateway, posted)), len(posts(gateway, skipped))) == (1, 1)
    assert f"<@{BOB[0]}>" in posts(gateway, skipped)[0]


def test_two_runs_at_once_post_once_in_each_guild(fake_redis, freeze_today, gateway):
    # As when a new process starts before the old one has stopped: both read the
    # birthdays, and only the one that sets the guild's marker first posts there.
    freeze_today(datetime.date(2026, 10, 1))
    guilds = two_guilds(gateway, fake_redis)

    async def two_runs():
        await asyncio.gather(bot.check_upcoming_birthdays(), bot.check_upcoming_birthdays())

    asyncio.run(two_runs())

    assert [len(posts(gateway, guild)) for guild in guilds] == [1, 1]


def test_a_guild_whose_post_cannot_be_marked_is_skipped_and_logged(fake_redis, freeze_today, gateway, caplog, capsys):
    freeze_today(datetime.date(2026, 10, 1))
    unmarked, marked = two_guilds(gateway, fake_redis)
    # Redis refuses the write of guild A's marker, and only that write, as it refuses writes
    # when it is out of memory; fakeredis does not emulate that, so an ACL stands in.
    fake_redis.acl_setuser(
        "default", enabled=True, nopass=True, reset_keys=True,
        keys=["guild:*", "user:*", f"announce:{GUILD_B}:*"], commands=["+@all"],
    )
    if [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should reach guild A first")

    asyncio.run(bot.check_upcoming_birthdays())

    fake_redis.acl_setuser("default", enabled=True, nopass=True, reset_keys=True, keys=["*"], commands=["+@all"])
    # Without its marker the post could go out again: guild A gets none, and guild B gets its own.
    assert posts(gateway, unmarked) == []
    assert len(posts(gateway, marked)) == 1
    assert [message for message in logged_errors(caplog) if str(GUILD_A) in message and "NoPermissionError" in message] != []
    printed = capsys.readouterr()
    assert [form for form in DATE_FORMS if form in caplog.text + printed.out + printed.err] == []
    # Nothing marks guild A as posted, so a restart that day posts there.
    assert fake_redis.keys("announce:*") == [f"announce:{GUILD_B}:2026-10"]


def test_a_post_that_fails_to_send_is_not_sent_again(fake_redis, freeze_today, gateway, monkeypatch):
    freeze_today(datetime.date(2026, 10, 1))
    refused, other = two_guilds(gateway, fake_redis)
    attempts = []
    send_message = gateway.send_message

    # Discord refuses the post in guild A, as when the bot may not write in its #general.
    async def refuse_in_guild_a(channel_id, *, params):
        attempts.append(int(channel_id))
        if int(channel_id) == gateway.general(refused).id:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"})
        return await send_message(channel_id, params=params)

    monkeypatch.setattr(bot.client._connection.http, "send_message", refuse_in_guild_a)

    for _ in ("the run at 15:00", "a restart later that day"):
        new_process(monkeypatch, fake_redis)
        asyncio.run(bot.check_upcoming_birthdays())

    # The marker is set before sending, so a failed post is not retried: at most one post a month.
    assert sorted(attempts) == sorted([gateway.general(refused).id, gateway.general(other).id])
    assert len(posts(gateway, other)) == 1


def test_the_post_lists_birthdays_soonest_first(fake_redis, freeze_today, gateway):
    # On Dec 1 the post covers December, January and February: next year's dates come last.
    freeze_today(datetime.date(2026, 12, 1))
    # (birth date, its date in this post), seeded in this order. Redis returns a set's members
    # in no fixed order, so with ten of them an unsorted post is almost never in date order.
    birthdays = [
        ("1990-02-27", datetime.date(2027, 2, 27)),
        ("1985-12-01", datetime.date(2026, 12, 1)),
        ("1979-01-31", datetime.date(2027, 1, 31)),
        ("2001-12-31", datetime.date(2026, 12, 31)),
        ("1995-01-01", datetime.date(2027, 1, 1)),
        ("1988-02-01", datetime.date(2027, 2, 1)),
        ("1992-12-15", datetime.date(2026, 12, 15)),
        ("1999-01-15", datetime.date(2027, 1, 15)),
        ("1983-02-14", datetime.date(2027, 2, 14)),
        ("1970-12-24", datetime.date(2026, 12, 24)),
    ]
    members = [(200000000000007100 + i, f"member-{i}") for i in range(len(birthdays))]
    guild = gateway.add_guild(GUILD_A, "Guild A", members)
    for (user_id, _), (birthday, _) in zip(members, birthdays):
        fake_redis.seed_birthday(GUILD_A, user_id, birthday)

    asyncio.run(bot.check_upcoming_birthdays())

    rows = "\n".join(posts(gateway, guild)).splitlines()[1:]  # after the header
    by_date = sorted(zip(members, birthdays), key=lambda pair: pair[1][1])
    expected = [f"<@{user_id}> - {in_post.strftime('%A, %B %d')}" for (user_id, _), (_, in_post) in by_date]
    if sorted(rows) != sorted(expected):
        pytest.fail(f"setup: the post should list every member once, got {rows!r}")
    assert rows == expected


def test_a_guild_without_general_is_skipped_and_the_others_get_their_post(
    fake_redis, freeze_today, gateway, caplog, capsys
):
    freeze_today(datetime.date(2026, 10, 1))
    without_general = gateway.add_guild(GUILD_A, "No General", [ALICE], text_channel="chat")
    with_general = gateway.add_guild(GUILD_B, "Guild B", [BOB])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())
    fake_redis.seed_birthday(GUILD_B, BOB[0], BOB_BIRTHDAY.isoformat())
    if gateway.general(without_general) is not None or [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should first reach a guild with no #general")

    asyncio.run(bot.check_upcoming_birthdays())

    assert [channel_id for channel_id, _ in gateway.sent] == [gateway.general(with_general).id]
    assert logged_errors(caplog) == []
    assert "❌" not in capsys.readouterr().out
    assert fake_redis.keys("announce:*") == [f"announce:{GUILD_B}:2026-10"]


def test_a_post_stores_only_a_marker_that_redis_deletes_after_40_days(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))
    guild = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())
    before = fake_redis.contents()

    asyncio.run(bot.check_upcoming_birthdays())

    if len(posts(gateway, guild)) != 1:
        pytest.fail(f"setup: the task should post once, got {gateway.sent!r}")
    marker = f"announce:{GUILD_A}:2026-10"
    # The guild's id and the month, and no member's data.
    assert fake_redis.contents() == {**before, marker: "1"}
    assert 40 * 86400 - 60 <= fake_redis.ttl(marker) <= 40 * 86400


def test_a_failed_post_at_startup_does_not_stop_the_task(runs, fake_redis, freeze_now, gateway, monkeypatch, caplog):
    freeze_now(utc(2026, 10, 1, 15, 1))  # the bot posts once it is ready
    gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], ALICE_BIRTHDAY.isoformat())

    async def scenario():
        client = await start_bot(monkeypatch, fake_redis)

        # An error the task's body does not handle: redis.asyncio raises it for a
        # connection opened in another event loop.
        async def fail(*args, **kwargs):
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(client, "smembers", fail)
        await become_ready()
        state = (bot.check_upcoming_birthdays.get_task().done(), bot.check_upcoming_birthdays.next_iteration)
        await stop_bot()
        return state

    ended, next_run = asyncio.run(scenario())

    if runs != [True]:
        pytest.fail(f"setup: the task should have run once after READY, got {runs!r}")
    # Raised at startup, the error would end the task before its first scheduled run.
    assert (ended, next_run) == (False, utc(2026, 10, 2, 15))
    assert [message for message in logged_errors(caplog) if "RuntimeError" in message] != []
