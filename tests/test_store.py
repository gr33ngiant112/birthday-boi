"""#25 and #6: birthdays are stored per guild, read and deleted without KEYS or SCAN,
and listed without the member cache.

Under Intents.default() the bot caches only members in a voice channel, so a view
that looks names up in the cache shows almost nobody (#6). Rows render as <@id>,
which Discord shows as the member's name with no cache and, under the client-wide
AllowedMentions.none(), pings nobody.
"""

import asyncio
import datetime
from unittest import mock

import discord
import fakeredis
import pytest
import redis.asyncio

import bot

GUILD_ID = 300000000000008000
OTHER_GUILD_ID = 300000000000009000
ASKER = (200000000000003001, "asker")
MEMBER = (200000000000003002, "member")
UNCACHED = [(200000000000003101 + i, f"uncached-{i}") for i in range(3)]
# On 2026-10-01 each falls in all three views: /birthday upcoming with 90 days covers
# the next 90 days, to Dec 30, and the monthly post October to December.
UNCACHED_BIRTHDAYS = ["1990-11-03", "1991-11-24", "1992-12-15"]


def run_view(view, gateway, guild, asker, *args):
    """Run a view that lists a guild's birthdays and return the text it sent there."""
    if view == "monthly post":
        asyncio.run(bot.check_upcoming_birthdays())
        return "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    interaction = gateway.interaction(guild, guild.get_member(asker[0]))
    asyncio.run(getattr(bot, view).callback(interaction, *args))
    return "\n".join(interaction.texts())


@pytest.mark.parametrize("view", ["birthday_list", "birthday_upcoming", "monthly post"])
def test_view_lists_members_the_bot_has_not_cached(view, fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER], uncached=UNCACHED)
    for (user_id, _), birthday in zip(UNCACHED, UNCACHED_BIRTHDAYS, strict=True):
        fake_redis.seed_birthday(GUILD_ID, user_id, birthday)
    if any(guild.get_member(user_id) for user_id, _ in UNCACHED):
        pytest.fail("setup: these members should not be in the member cache")

    text = run_view(view, gateway, guild, ASKER, *((90,) if view == "birthday_upcoming" else ()))

    assert [user_id for user_id, _ in UNCACHED if f"<@{user_id}>" not in text] == []


@pytest.mark.parametrize(
    ("view", "no_birthdays"),
    [
        ("birthday_list", "❌ No birthdays have been set yet."),
        ("birthday_upcoming", "❌ No birthdays in the next 30 days."),
    ],
    ids=["birthday_list", "birthday_upcoming"],
)
def test_guild_with_no_stored_birthdays_gets_the_no_birthdays_message(
    view, no_birthdays, fake_redis, freeze_today, gateway
):
    freeze_today(datetime.date(2026, 10, 1))
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER])
    # Stored for another guild only, so this guild has no rows.
    fake_redis.seed_birthday(OTHER_GUILD_ID, MEMBER[0], "1990-11-03")
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    asyncio.run(getattr(bot, view).callback(interaction))

    assert [content for content, _ in interaction.sent] == [no_birthdays]


def test_monthly_task_skips_a_guild_with_no_stored_birthdays(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st
    empty = gateway.add_guild(GUILD_ID, "Empty", [ASKER, MEMBER])
    other = gateway.add_guild(OTHER_GUILD_ID, "Other", [MEMBER])
    fake_redis.seed_birthday(OTHER_GUILD_ID, MEMBER[0], "1990-11-03")

    asyncio.run(bot.check_upcoming_birthdays())

    if f"<@{MEMBER[0]}>" not in "".join(
        payload.get("content") or "" for payload in gateway.sent_to(gateway.general(other))
    ):
        pytest.fail(f"setup: the other guild's post should list its member, got {gateway.sent!r}")
    assert gateway.sent_to(gateway.general(empty)) == []


def test_no_view_reads_with_keys_or_scan(fake_redis, freeze_today, gateway, monkeypatch):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER, MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-11-03")
    refused = []

    def refuse(name):
        def call(*args, **kwargs):
            refused.append(name)
            raise AssertionError(f"{name}() walks every key in the database")

        return call

    # On the client class, so that pipelines refuse them too: /birthday forget and
    # the cleanup when the bot leaves a guild read through a pipeline.
    for name in ("keys", "scan", "scan_iter"):
        monkeypatch.setattr(redis.asyncio.Redis, name, refuse(name))

    member = guild.get_member(MEMBER[0])
    texts = {}
    for command, args in [
        ("birthday_set", (12, 25, 1991)),
        ("birthday_show", (member,)),
        ("birthday_list", ()),
        ("birthday_upcoming", (90,)),
    ]:
        interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))
        asyncio.run(getattr(bot, command).callback(interaction, *args))
        texts[command] = "\n".join(interaction.texts())
    texts["monthly post"] = run_view("monthly post", gateway, guild, ASKER)
    # Then the paths that delete: ASKER forgets their birthday, and the bot leaves the guild.
    asyncio.run(bot.birthday_forget.callback(gateway.interaction(guild, guild.get_member(ASKER[0]))))
    asyncio.run(bot.client.on_guild_remove(guild))

    expected = {
        "birthday_show": "November 03",
        "birthday_list": f"<@{MEMBER[0]}>",
        "birthday_upcoming": f"<@{MEMBER[0]}>",
        "monthly post": f"<@{MEMBER[0]}>",
    }
    if any(shown not in texts[view] for view, shown in expected.items()):
        pytest.fail(f"setup: each view should show the stored member, got {texts!r}")
    # Only the marker of the month's post may stay (#14): it holds no member's data, and Redis deletes it.
    left = {key: value for key, value in fake_redis.contents().items() if not key.startswith("announce:")}
    if left:
        pytest.fail(f"setup: forgetting and leaving should delete every other key, got {left!r}")
    assert refused == []


def refuse_keys_and_scan(monkeypatch):
    """Make keys(), scan() and scan_iter() raise on every redis.asyncio client, as
    test_no_view_reads_with_keys_or_scan does: on the client class, so that pipelines and
    fakeredis clients refuse them too. Returns the names called, so that a call whose error
    an `except Exception` swallows still fails the test."""
    refused = []

    def refuse(name):
        def call(*args, **kwargs):
            refused.append(name)
            raise AssertionError(f"{name}() walks every key in the database")

        return call

    for name in ("keys", "scan", "scan_iter"):
        monkeypatch.setattr(redis.asyncio.Redis, name, refuse(name))
    return refused


def test_the_startup_ping_does_not_use_keys_or_scan(fake_redis, monkeypatch):
    # main() sends Redis a PING through a client of its own before it logs in (#15). Its
    # load_dotenv() is a no-op here, so it reads its config from the environment only.
    monkeypatch.setattr(bot, "load_dotenv", mock.Mock())
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("DEV_GUILD_ID", raising=False)
    # main() sets these module globals; monkeypatch puts them back afterwards.
    for name in ("TOKEN", "REDIS_URL", "DEV_GUILD_ID", "SYNC_COMMANDS", "HEARTBEAT_FILE", "redis_client"):
        monkeypatch.setattr(bot, name, getattr(bot, name))
    server = fake_redis.connection_pool.connection_kwargs["server"]
    monkeypatch.setattr(
        bot, "create_redis_client", lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    )
    pings = []
    ping = redis.asyncio.Redis.ping

    def noted_ping(client, **kwargs):
        pings.append(client)
        return ping(client, **kwargs)

    monkeypatch.setattr(redis.asyncio.Redis, "ping", noted_ping)
    refused = refuse_keys_and_scan(monkeypatch)

    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        bot.main()

    if not pings or not run.called:
        pytest.fail(f"setup: main() should PING Redis, then start the client; {len(pings)} PINGs, run {run.called}")
    assert refused == []


def test_the_monthly_post_at_startup_does_not_use_keys_or_scan(fake_redis, freeze_now, gateway, monkeypatch):
    # A bot that becomes ready after 15:00 UTC on the 1st posts the month's list at once (#14).
    freeze_now(datetime.datetime(2026, 10, 1, 15, 1, tzinfo=datetime.UTC))
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER, MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-11-03")
    ready = asyncio.Event()
    ready.set()  # as READY sets it, so wait_until_ready() returns at once
    monkeypatch.setattr(bot.client, "_ready", ready)
    refused = refuse_keys_and_scan(monkeypatch)

    asyncio.run(bot.before_check_upcoming_birthdays())

    post = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    if f"<@{MEMBER[0]}>" not in post:
        pytest.fail(f"setup: the startup run should post the month's list, got {gateway.sent!r}")
    assert refused == []
