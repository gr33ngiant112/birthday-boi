"""#15: a Redis failure is reported as one, never as a success or as "nothing stored".

The storage helpers raise bot.StoreUnavailable, with the redis.RedisError as its cause.
The slash commands then tell the member, and only them, that storage is unavailable; the
chat path answers in public, so its reply carries no date; the monthly post skips the
guild it could not read and goes on with the others. Each failure is logged with the
logging module, and no birth date reaches the log.

Two failures are emulated on the fakeredis server:
- Redis down: every command raises redis.ConnectionError (FakeServer.connected = False).
- write refused: Redis refuses the SET of a birth date when the transaction queues it, and
  discards the transaction. Redis refuses a queued write the same way when it is out of
  memory (OOM) or a read-only replica (READONLY); fakeredis emulates neither, so an ACL
  that leaves out the birth-date keys stands in. redis-py's error then quotes the refused
  command, birth date included.
"""

import asyncio
import datetime
import logging

import pytest
import redis

import bot

GUILD_ID = 300000000000015000
OTHER_GUILD_ID = 300000000000016000
SETTER = (200000000000006001, "setter")
MEMBER = (200000000000006002, "member")
BIRTHDAY = datetime.date(1990, 12, 25)
# How bot.py writes a date: stored ISO, MM-DD-YYYY replies, month and day, and the year.
DATE_FORMS = [
    BIRTHDAY.isoformat(),
    BIRTHDAY.strftime("%m-%d-%Y"),
    BIRTHDAY.strftime("%m-%d"),
    BIRTHDAY.strftime("%B %d"),
    str(BIRTHDAY.year),
]
SAVE_FAILED = "⚠️ Storage is unavailable, so your birthday was not saved. Please try again later."
READ_FAILED = "⚠️ Storage is unavailable. Please try again later."


def server_of(store):
    return store.connection_pool.connection_kwargs["server"]


def take_redis_down(store):
    server_of(store).connected = False


def bring_redis_back(store):
    server_of(store).connected = True


def refuse_writes_of_birth_dates(store):
    # The default user, which bot.redis_client uses, keeps every key but user:<id>:birthday.
    store.acl_setuser(
        "default", enabled=True, nopass=True, reset_keys=True, keys=["guild:*", "user:*:guilds"], commands=["+@all"]
    )


def allow_every_key(store):
    store.acl_setuser("default", enabled=True, nopass=True, reset_keys=True, keys=["*"], commands=["+@all"])


# Each failure, and how to undo it so that the test can look at what was stored.
FAILURES = {
    "redis down": (take_redis_down, bring_redis_back),
    "write refused": (refuse_writes_of_birth_dates, allow_every_key),
}


def set_birthday_through(path, gateway, guild):
    """SETTER sets BIRTHDAY in guild through path. Returns the replies as (content, ephemeral)."""
    if path == "/set_birthday":
        interaction = gateway.interaction(guild, guild.get_member(SETTER[0]))
        asyncio.run(bot.set_birthday.callback(interaction, BIRTHDAY.strftime("%m-%d-%Y")))
        return [(content, kwargs.get("ephemeral")) for content, kwargs in interaction.sent]
    message = gateway.message(
        guild, SETTER, f"<@{gateway.bot_user.id}> my birthday is December 25 1990", mentions=[gateway.mention_bot()]
    )
    asyncio.run(bot.on_message(message))
    # A chat reply is a message in the channel: everyone who can read the channel sees it.
    return [(payload.get("content"), None) for payload in gateway.sent_to(gateway.general(guild))]


def logged_errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("helper", "args"),
    [
        ("set_birthday_redis", (GUILD_ID, SETTER[0], BIRTHDAY.isoformat())),
        ("get_birthday_redis", (GUILD_ID, SETTER[0])),
        ("get_guild_birthdays_redis", (GUILD_ID,)),
    ],
)
def test_storage_helper_raises_store_unavailable_with_the_redis_error_as_its_cause(helper, args, fake_redis):
    take_redis_down(fake_redis)

    with pytest.raises(bot.StoreUnavailable) as raised:
        asyncio.run(getattr(bot, helper)(*args))

    assert isinstance(raised.value.__cause__, redis.ConnectionError)


def test_store_unavailable_does_not_repeat_the_refused_command(fake_redis):
    refuse_writes_of_birth_dates(fake_redis)

    with pytest.raises(bot.StoreUnavailable) as raised:
        asyncio.run(bot.set_birthday_redis(GUILD_ID, SETTER[0], BIRTHDAY.isoformat()))

    if BIRTHDAY.isoformat() not in str(raised.value.__cause__):
        pytest.fail(f"setup: redis-py's error should quote the refused SET, got {raised.value.__cause__!r}")
    # Callers log the StoreUnavailable, so it must not carry the date.
    assert [form for form in DATE_FORMS if form in str(raised.value)] == []


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("path", ["/set_birthday", "chat"])
def test_a_failed_save_is_never_reported_as_saved(path, failure, fake_redis, freeze_today, gateway, caplog, capsys):
    freeze_today(datetime.date(2026, 10, 5))
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])
    break_redis, undo = FAILURES[failure]
    break_redis(fake_redis)

    replies = set_birthday_through(path, gateway, guild)

    undo(fake_redis)
    if fake_redis.contents():
        pytest.fail(f"setup: the store should have refused the write, got {fake_redis.contents()!r}")
    # One reply, which says the birthday was not saved: only to the member for the slash
    # command, and without the date in the public chat reply.
    assert replies == [(SAVE_FAILED, True if path == "/set_birthday" else None)]
    assert logged_errors(caplog) != []
    printed = capsys.readouterr()
    assert [form for form in DATE_FORMS if form in caplog.text + printed.out + printed.err] == []


@pytest.mark.parametrize("command", ["get_birthday", "list_birthdays", "forecast_birthdays"])
def test_a_failed_read_is_never_shown_as_nothing_stored(command, fake_redis, freeze_today, gateway, caplog):
    freeze_today(datetime.date(2026, 10, 5))  # BIRTHDAY is 81 days away, inside the forecast
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER, MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], BIRTHDAY.isoformat())
    args = (guild.get_member(MEMBER[0]),) if command == "get_birthday" else ()

    def run_command():
        interaction = gateway.interaction(guild, guild.get_member(SETTER[0]))
        asyncio.run(getattr(bot, command).callback(interaction, *args))
        return [(content, kwargs.get("ephemeral")) for content, kwargs in interaction.sent]

    while_up = run_command()
    take_redis_down(fake_redis)
    while_down = run_command()

    if not any(f"<@{MEMBER[0]}>" in (content or "") for content, _ in while_up):
        pytest.fail(f"setup: with Redis up, /{command} should show MEMBER, got {while_up!r}")
    # Not "has not set their birthday yet", "No birthdays have been set yet." or "No
    # birthdays in the next 90 days.": the store could not be read, so the bot cannot know.
    assert while_down == [(READ_FAILED, True)]
    assert logged_errors(caplog) != []


def test_monthly_post_skips_only_the_guild_whose_birthdays_it_cannot_read(
    fake_redis, freeze_today, gateway, caplog, monkeypatch
):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st; BIRTHDAY is in December
    unreadable = gateway.add_guild(GUILD_ID, "Unreadable", [SETTER])
    readable = gateway.add_guild(OTHER_GUILD_ID, "Readable", [MEMBER])
    fake_redis.seed_birthday(GUILD_ID, SETTER[0], BIRTHDAY.isoformat())
    fake_redis.seed_birthday(OTHER_GUILD_ID, MEMBER[0], BIRTHDAY.isoformat())
    # Reading the first guild's members fails, as when the connection drops; the next read works.
    smembers = bot.redis_client.smembers

    async def smembers_failing_for_the_first_guild(key):
        if key == f"guild:{GUILD_ID}:birthdays":
            raise redis.ConnectionError("Connection reset by peer")
        return await smembers(key)

    monkeypatch.setattr(bot.redis_client, "smembers", smembers_failing_for_the_first_guild)
    if [guild.id for guild in bot.client.guilds] != [GUILD_ID, OTHER_GUILD_ID]:
        pytest.fail("setup: the task should reach the unreadable guild first")

    # An exception here would stop the task's loop for good: discord.ext.tasks retries only
    # network errors such as OSError, and redis.RedisError is not one.
    asyncio.run(bot.check_upcoming_birthdays())

    posted = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(readable)))
    assert f"<@{MEMBER[0]}>" in posted
    assert gateway.sent_to(gateway.general(unreadable)) == []
    assert [message for message in logged_errors(caplog) if str(GUILD_ID) in message] != []
    assert [form for form in DATE_FORMS if form in caplog.text] == []
