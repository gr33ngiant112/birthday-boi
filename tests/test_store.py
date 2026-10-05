"""#25 and #6: birthdays are stored per guild, read without KEYS or SCAN, and listed
without the member cache.

Under Intents.default() the bot caches only members in a voice channel, so a view
that looks names up in the cache shows almost nobody (#6). Rows render as <@id>,
which Discord shows as the member's name with no cache and, under the client-wide
AllowedMentions.none(), pings nobody.
"""

import asyncio
import datetime

import pytest

import bot

GUILD_ID = 300000000000008000
OTHER_GUILD_ID = 300000000000009000
ASKER = (200000000000003001, "asker")
MEMBER = (200000000000003002, "member")
UNCACHED = [(200000000000003101 + i, f"uncached-{i}") for i in range(3)]
# On 2026-10-01 each falls in all three views: the forecast covers November and
# December, the monthly post October to December.
UNCACHED_BIRTHDAYS = ["1990-11-03", "1991-11-24", "1992-12-15"]


def run_view(view, gateway, guild, asker):
    """Run a view that lists a guild's birthdays and return the text it sent there."""
    if view == "monthly post":
        asyncio.run(bot.check_upcoming_birthdays())
        return "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    interaction = gateway.interaction(guild, guild.get_member(asker[0]))
    asyncio.run(getattr(bot, view).callback(interaction))
    return "\n".join(content or "" for content, _ in interaction.sent)


@pytest.mark.parametrize("view", ["list_birthdays", "forecast_birthdays", "monthly post"])
def test_view_lists_members_the_bot_has_not_cached(view, fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER], uncached=UNCACHED)
    for (user_id, _), birthday in zip(UNCACHED, UNCACHED_BIRTHDAYS):
        fake_redis.seed_birthday(GUILD_ID, user_id, birthday)
    if any(guild.get_member(user_id) for user_id, _ in UNCACHED):
        pytest.fail("setup: these members should not be in the member cache")

    text = run_view(view, gateway, guild, ASKER)

    assert [user_id for user_id, _ in UNCACHED if f"<@{user_id}>" not in text] == []


@pytest.mark.parametrize(
    ("view", "no_birthdays"),
    [
        ("list_birthdays", "❌ No birthdays have been set yet."),
        ("forecast_birthdays", "❌ No upcoming birthdays in the next 60 or 90 days."),
    ],
    ids=["list_birthdays", "forecast_birthdays"],
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

    if f"<@{MEMBER[0]}>" not in "".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(other))):
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

    for name in ("keys", "scan", "scan_iter"):
        monkeypatch.setattr(bot.redis_client, name, refuse(name))

    member = guild.get_member(MEMBER[0])
    texts = {}
    for command, args in [
        ("set_birthday", ("12-25-1991",)),
        ("get_birthday", (member,)),
        ("list_birthdays", ()),
        ("forecast_birthdays", ()),
    ]:
        interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))
        asyncio.run(getattr(bot, command).callback(interaction, *args))
        texts[command] = "\n".join(content or "" for content, _ in interaction.sent)
    texts["monthly post"] = run_view("monthly post", gateway, guild, ASKER)
    message = gateway.message(
        guild, ASKER, f"<@{gateway.bot_user.id}> my birthday is December 25 1990", mentions=[gateway.mention_bot()]
    )
    asyncio.run(bot.on_message(message))

    expected = {
        "get_birthday": "11-03-1990",
        "list_birthdays": f"<@{MEMBER[0]}>",
        "forecast_birthdays": f"<@{MEMBER[0]}>",
        "monthly post": f"<@{MEMBER[0]}>",
    }
    if any(shown not in texts[view] for view, shown in expected.items()):
        pytest.fail(f"setup: each view should show the stored member, got {texts!r}")
    assert refused == []
