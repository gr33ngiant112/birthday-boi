"""#74: a member who left a server is no longer shown there, and is deleted from it.

The bot runs without the members intent, so Discord sends it no event when a member leaves. Each
view asks Discord instead, just before it shows its birthdays: /birthday list, /birthday upcoming,
the monthly post and the daily greeting. It asks with Request Guild Members by user_ids, which
needs no privileged intent. A member missing from the answer is looked up with Get Guild Member,
and only Discord's 404 "Unknown Member" (code 10007) counts as having left: the member is hidden
and removed from that server, and their birthday is deleted outright when no other server has it.
Any other answer, or none, deletes nothing and shows the members as before.

conftest's gateway answers both requests from every member the test added, cached or not, and
gateway.leave() removes one, as when they leave the server.
"""

import asyncio
import datetime
import logging
import re
import types

import aiohttp
import discord
import discord.state
import pytest
import redis

import bot

UTC = datetime.UTC
GUILD_A = 300000000000030000
GUILD_B = 300000000000031000
ADMIN = (200000000000010001, "admin")
STAYER = (200000000000010002, "stayer")
OTHER_STAYER = (200000000000010003, "other-stayer")
LEAVER = (200000000000010004, "leaver")
# Members the bot has not cached, as most members of a real server (#6).
SEVEN_STAYERS = [(200000000000010101 + i, f"uncached-{i}") for i in range(7)]
MANY = [(200000000000011001 + i, f"member-{i}") for i in range(250)]
# The 1st at 15:00 UTC: the monthly post goes out, and a greeting set for 09:00 UTC is due.
NOW = datetime.datetime(2026, 10, 1, 15, tzinfo=UTC)
# Today, so every view shows it: the list, the next 30 days, the post's three months, the greeting.
BIRTHDAY = "1990-10-01"


def logged_errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def seed(store, guild_id, *users):
    for user_id, _ in users:
        store.seed_birthday(guild_id, user_id, BIRTHDAY)


def rows_of(store, user):
    """Every row that holds the member: their own keys with their values, and each server's list
    they are on."""
    user_id = str(user[0])
    rows = {}
    for key, value in store.contents().items():
        if key.startswith(f"user:{user_id}:"):
            rows[key] = value
        elif key.startswith("guild:") and isinstance(value, set) and user_id in value:
            rows[key] = "on the list"
    return rows


def shown(text):
    """The IDs of the members a message names, as <@id>."""
    return {int(user_id) for user_id in re.findall(r"<@(\d+)>", text)}


def prepare(view, gateway, guild):
    """For the greeting, turn on guild's greetings at 09:00 UTC in #general, run by ADMIN."""
    if view == "greeting":
        interaction = gateway.interaction(guild, guild.get_member(ADMIN[0]), discord.Permissions(manage_guild=True))
        asyncio.run(bot.birthday_config.callback(interaction, gateway.general(guild), "09:00", "UTC"))
        if not interaction.sent[0][0].startswith("✅ Every day at"):
            pytest.fail(f"setup: /birthday config should turn on the greetings, got {interaction.sent!r}")


def in_general(gateway, guild):
    return "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))


def run_view(view, gateway, guild):
    """Run a view and return the text it showed in guild. A slash command is run by ADMIN; the
    monthly post and the greeting run their task's body once, over every server."""
    if view == "monthly post":
        asyncio.run(bot.check_upcoming_birthdays())
        return in_general(gateway, guild)
    if view == "greeting":
        asyncio.run(bot.send_birthday_greetings())
        return in_general(gateway, guild)
    interaction = gateway.interaction(guild, guild.get_member(ADMIN[0]))
    asyncio.run(getattr(bot, view).callback(interaction))
    return "\n".join(interaction.texts())


@pytest.fixture
def short_member_wait(monkeypatch):
    """discord.py waits 30 s for an answer to a member request; here, 0.05 s. Only the asyncio
    name inside discord.state is replaced, as conftest's freeze fixtures replace datetime."""
    fast = types.ModuleType("asyncio")
    fast.__dict__.update(vars(asyncio))

    async def wait_for(awaitable, timeout):  # noqa: ASYNC109 - stands in for asyncio.wait_for
        return await asyncio.wait_for(awaitable, timeout=min(timeout, 0.05))

    fast.wait_for = wait_for
    monkeypatch.setattr(discord.state, "asyncio", fast)


def test_birthday_list_hides_and_deletes_a_member_who_left(fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, STAYER], uncached=[LEAVER])
    seed(fake_redis, GUILD_A, STAYER, LEAVER)
    gateway.leave(guild, LEAVER)
    stayer_rows = rows_of(fake_redis, STAYER)

    text = run_view("birthday_list", gateway, guild)

    assert shown(text) == {STAYER[0]}
    # No other server has the birthday, so it is deleted outright: date, show_age, list of servers.
    assert rows_of(fake_redis, LEAVER) == {}
    assert rows_of(fake_redis, STAYER) == stayer_rows
    # Only the member missing from Discord's answer is looked up.
    assert gateway.member_fetches == [(GUILD_A, LEAVER[0])]


@pytest.mark.parametrize("view", ["birthday_upcoming", "monthly post", "greeting"])
def test_view_hides_and_deletes_a_member_who_left(view, fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, STAYER, LEAVER])
    prepare(view, gateway, guild)
    seed(fake_redis, GUILD_A, STAYER, LEAVER)
    gateway.leave(guild, LEAVER)
    stayer_rows = rows_of(fake_redis, STAYER)

    text = run_view(view, gateway, guild)

    assert shown(text) == {STAYER[0]}
    assert rows_of(fake_redis, LEAVER) == {}
    assert rows_of(fake_redis, STAYER) == stayer_rows


@pytest.mark.parametrize("view", ["monthly post", "greeting"])
def test_nothing_is_sent_when_the_only_member_left_and_the_next_server_still_gets_its_own(
    view, fake_redis, freeze_now, gateway, caplog
):
    freeze_now(NOW)
    emptied = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, LEAVER])
    other = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, STAYER])
    for guild in (emptied, other):
        prepare(view, gateway, guild)
    seed(fake_redis, GUILD_A, LEAVER)
    seed(fake_redis, GUILD_B, STAYER)
    gateway.leave(emptied, LEAVER)
    if [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should reach the emptied server first")

    # An error here would end the task for good: discord.ext.tasks does not retry an IndexError.
    run_view(view, gateway, emptied)

    # Not a bare "Upcoming Birthdays" header, and no empty greeting.
    assert gateway.sent_to(gateway.general(emptied)) == []
    assert rows_of(fake_redis, LEAVER) == {}
    assert shown(in_general(gateway, other)) == {STAYER[0]}
    assert logged_errors(caplog) == []


def test_a_birthday_another_server_has_is_kept_for_that_server(fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    left = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, LEAVER])
    kept = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, LEAVER])
    seed(fake_redis, GUILD_A, LEAVER)
    seed(fake_redis, GUILD_B, LEAVER)
    gateway.leave(left, LEAVER)

    in_left = run_view("birthday_list", gateway, left)
    in_kept = run_view("birthday_list", gateway, kept)

    assert (shown(in_left), shown(in_kept)) == (set(), {LEAVER[0]})
    # Removed from guild A only: the date stays, and guild B is the only server on their list.
    assert rows_of(fake_redis, LEAVER) == {
        f"user:{LEAVER[0]}:birthday": BIRTHDAY,
        f"user:{LEAVER[0]}:guilds": {str(GUILD_B)},
        f"guild:{GUILD_B}:birthdays": "on the list",
    }


def test_a_ready_during_the_check_deletes_nobody(fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN], uncached=SEVEN_STAYERS[:3])
    seed(fake_redis, GUILD_A, *SEVEN_STAYERS[:3])
    before = fake_redis.contents()
    # A reconnect with a new session answers the pending request with no members at all.
    gateway.websocket_mode = "ready"

    text = run_view("birthday_list", gateway, guild)

    # An empty answer proves nothing: each member is looked up, and each is still there.
    assert shown(text) == {user_id for user_id, _ in SEVEN_STAYERS[:3]}
    assert fake_redis.contents() == before
    assert sorted(user_id for _, user_id in gateway.member_fetches) == [user_id for user_id, _ in SEVEN_STAYERS[:3]]


def test_seven_uncached_members_who_stayed_are_shown_with_no_lookup(fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN], uncached=SEVEN_STAYERS)
    seed(fake_redis, GUILD_A, *SEVEN_STAYERS)
    before = fake_redis.contents()
    if any(guild.get_member(user_id) for user_id, _ in SEVEN_STAYERS):
        pytest.fail("setup: these members should not be in the member cache")

    text = run_view("birthday_list", gateway, guild)

    assert shown(text) == {user_id for user_id, _ in SEVEN_STAYERS}
    assert fake_redis.contents() == before
    # discord.py asks for 5 members unless told otherwise, and Discord then answers with at most 5.
    # A member the bot has not cached is still a member.
    assert gateway.member_fetches == []
    assert [request["limit"] for request in gateway.member_requests] == [7]
    # The answer is not cached: without the members intent, the bot would never learn they left.
    assert [user_id for user_id, _ in SEVEN_STAYERS if guild.get_member(user_id)] == []


def test_more_than_100_stored_members_are_checked_in_batches_of_100(fake_redis, freeze_now, gateway):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN], uncached=MANY)
    seed(fake_redis, GUILD_A, *MANY)
    leaver = MANY[180]
    gateway.leave(guild, leaver)

    text = run_view("birthday_list", gateway, guild)

    requests = gateway.member_requests
    assert sorted(len(request["user_ids"]) for request in requests) == [50, 100, 100]
    assert [request["limit"] for request in requests] == [len(request["user_ids"]) for request in requests]
    assert sorted(int(user_id) for request in requests for user_id in request["user_ids"]) == [
        user_id for user_id, _ in MANY
    ]
    assert shown(text) == {user_id for user_id, _ in MANY} - {leaver[0]}
    assert rows_of(fake_redis, leaver) == {}
    assert gateway.member_fetches == [(GUILD_A, leaver[0])]


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [("silent", "TimeoutError"), ("no websocket", "RuntimeError"), ("closed", "ConnectionClosed")],
)
def test_no_answer_from_discord_deletes_nothing_and_shows_the_list_as_before(
    failure, error_type, fake_redis, freeze_now, gateway, short_member_wait, caplog
):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, STAYER, LEAVER])
    seed(fake_redis, GUILD_A, STAYER, LEAVER)
    gateway.leave(guild, LEAVER)
    before = fake_redis.contents()
    if failure == "no websocket":
        gateway.connected = False
    else:
        gateway.websocket_mode = failure

    text = run_view("birthday_list", gateway, guild)

    assert shown(text) == {STAYER[0], LEAVER[0]}
    assert fake_redis.contents() == before
    assert gateway.member_fetches == []
    errors = logged_errors(caplog)
    assert [message for message in errors if str(GUILD_A) in message and error_type in message] == errors
    assert len(errors) == 1


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (
            discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), {"code": 10013, "message": "x"}),
            "NotFound",
        ),
        (
            discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), {"code": 50001, "message": "x"}),
            "Forbidden",
        ),
        (
            discord.DiscordServerError(types.SimpleNamespace(status=503, reason="Unavailable"), {"code": 0}),
            "DiscordServerError",
        ),
        (aiohttp.ClientConnectionError(), "ClientConnectionError"),
        (ConnectionResetError(), "ConnectionResetError"),
    ],
    ids=["404 another code", "403", "503", "aiohttp error", "OSError"],
)
def test_a_failed_lookup_deletes_nothing_and_shows_the_member(
    error, error_type, fake_redis, freeze_now, gateway, caplog
):
    freeze_now(NOW)
    guild = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, STAYER, LEAVER])
    seed(fake_redis, GUILD_A, STAYER, LEAVER)
    gateway.leave(guild, LEAVER)
    gateway.fetch_errors[LEAVER[0]] = error
    before = fake_redis.contents()

    text = run_view("birthday_list", gateway, guild)

    # Only Discord's "Unknown Member" proves they left.
    assert shown(text) == {STAYER[0], LEAVER[0]}
    assert fake_redis.contents() == before
    errors = logged_errors(caplog)
    assert [message for message in errors if str(GUILD_A) in message and error_type in message] == errors
    assert len(errors) == 1


@pytest.mark.parametrize("view", ["monthly post", "greeting"])
def test_a_failed_delete_hides_the_member_and_the_task_goes_on(
    view, fake_redis, freeze_now, gateway, monkeypatch, caplog
):
    freeze_now(NOW)
    failing = gateway.add_guild(GUILD_A, "Guild A", [ADMIN, STAYER, LEAVER])
    other = gateway.add_guild(GUILD_B, "Guild B", [ADMIN, OTHER_STAYER])
    for guild in (failing, other):
        prepare(view, gateway, guild)
    seed(fake_redis, GUILD_A, STAYER, LEAVER)
    seed(fake_redis, GUILD_B, OTHER_STAYER)
    gateway.leave(failing, LEAVER)
    leaver_rows = rows_of(fake_redis, LEAVER)
    if [guild.id for guild in bot.client.guilds] != [GUILD_A, GUILD_B]:
        pytest.fail("setup: the task should reach the failing server first")
    transaction = bot.redis_client.transaction

    # The delete fails, as when the connection drops. remove_birthday_from_guild_redis raises
    # the redis.RedisError, which discord.ext.tasks does not retry.
    async def failing_transaction(*args, **kwargs):
        raise redis.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(bot.redis_client, "transaction", failing_transaction)

    run_view(view, gateway, failing)

    assert shown(in_general(gateway, failing)) == {STAYER[0]}
    assert shown(in_general(gateway, other)) == {OTHER_STAYER[0]}
    assert rows_of(fake_redis, LEAVER) == leaver_rows
    errors = logged_errors(caplog)
    assert [message for message in errors if str(LEAVER[0]) in message and "ConnectionError" in message] == errors
    assert len(errors) == 1

    # The next check tries the delete again.
    monkeypatch.setattr(bot.redis_client, "transaction", transaction)
    assert shown(run_view("birthday_list", gateway, failing)) == {STAYER[0]}
    assert rows_of(fake_redis, LEAVER) == {}
