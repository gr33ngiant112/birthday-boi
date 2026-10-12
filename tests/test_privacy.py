"""#7, #8, #9 and #10: what the bot shows to whom, and whom it can ping."""

import asyncio
import datetime
import json
import re

import pytest

import bot

GUILD_A = 300000000000006000
GUILD_B = 300000000000007000
ALICE = (200000000000001101, "alice-only-in-a")
BOB = (200000000000001201, "bob-only-in-b")
ASKER = (200000000000001401, "asker")
CAROL = (200000000000001501, "carol-in-a")
CAROL_BIRTHDAY = datetime.date(1990, 3, 15)
SETTER = (200000000000001801, "setter-in-a-and-b")
HOSTILE_ROLE_ID = 300000000000006500
# Display names a member controls, with an @everyone ping, a role mention and code-block breakouts.
HOSTILE_MEMBERS = [
    (200000000000001901, "@everyone"),
    (200000000000001902, f"<@&{HOSTILE_ROLE_ID}>"),
    (200000000000001903, "```@here```"),
]
# On 2026-10-01 all three fall in the forecast (the next 90 days) and in the monthly post.
HOSTILE_BIRTHDAYS = ["1990-11-03", "1991-11-24", "1992-12-15"]
# Ages on the next birthday from 2026-10-01: 44, 52 and 67, none of which can be a day of the month.
MONTHLY_MEMBERS = [
    (200000000000002101, "member-turning-soon-1", datetime.date(1982, 10, 15)),
    (200000000000002102, "member-turning-soon-2", datetime.date(1974, 11, 20)),
    (200000000000002103, "member-turning-soon-3", datetime.date(1959, 12, 5)),
]


def rendered(payloads):
    """Everything in the request payloads except the random nonce, as one string."""
    return json.dumps([{k: v for k, v in p.items() if k != "nonce"} for p in payloads], ensure_ascii=False)


def leaks(payloads, birthday, name):
    """Which forms of the birth date, and the member's name, appear in the payloads.

    The forms are how bot.py writes a date: stored ISO, MM-DD-YYYY replies, month
    and day with or without the year, and the year on its own.
    """
    forms = [
        birthday.isoformat(),
        birthday.strftime("%m-%d-%Y"),
        birthday.strftime("%m-%d"),
        birthday.strftime("%B %d"),
        str(birthday.year),
        name,
    ]
    text = rendered(payloads).lower()
    return [form for form in forms if form.lower() in text]


def assert_pings_nobody(payload):
    allowed = payload.get("allowed_mentions")
    # Without the field, Discord parses every mention in a channel message.
    assert allowed is not None
    assert allowed.get("parse") == []
    assert not allowed.get("users")
    assert not allowed.get("roles")
    assert not allowed.get("replied_user")


def run_command(gateway, guild, user, command, *args):
    """Run a slash command as user, a (user_id, name) member of guild; return the FakeInteraction."""
    interaction = gateway.interaction(guild, guild.get_member(user[0]))
    asyncio.run(getattr(bot, command).callback(interaction, *args))
    return interaction


def text_of(interaction):
    return "\n".join(content or "" for content, _ in interaction.sent)


def test_monthly_post_lists_only_members_of_that_guild(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [BOB])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-15")
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1992-11-20")

    asyncio.run(bot.check_upcoming_birthdays())

    post_a = rendered(gateway.sent_to(gateway.general(guild_a)))
    post_b = rendered(gateway.sent_to(gateway.general(guild_b)))
    if f"<@{ALICE[0]}>" not in post_a or f"<@{BOB[0]}>" not in post_b:
        pytest.fail(f"setup: each guild's post should list its own member, got A={post_a} B={post_b}")
    assert ALICE[1] not in post_b
    assert str(ALICE[0]) not in post_b


def test_setting_a_birthday_shares_it_only_with_that_guild(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    # SETTER and CAROL are members of both guilds; CAROL shared her birthday with guild B.
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [SETTER, CAROL])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [SETTER, CAROL])
    fake_redis.seed_birthday(GUILD_B, CAROL[0], CAROL_BIRTHDAY.isoformat())
    run_command(gateway, guild_a, SETTER, "set_birthday", "12-25-1990")

    list_a = text_of(run_command(gateway, guild_a, CAROL, "list_birthdays"))
    list_b = text_of(run_command(gateway, guild_b, CAROL, "list_birthdays"))

    if f"<@{SETTER[0]}>" not in list_a or f"<@{CAROL[0]}>" not in list_b:
        pytest.fail(f"setup: each list should show the member who set a birthday there, got A={list_a!r} B={list_b!r}")
    assert str(SETTER[0]) not in list_b
    assert "12-25-1990" not in list_b


def test_get_birthday_of_a_member_who_shared_it_only_with_another_guild_shows_no_date(
    fake_redis, freeze_today, gateway
):
    freeze_today(datetime.date(2026, 9, 29))
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [ASKER, CAROL])
    fake_redis.seed_birthday(GUILD_A, CAROL[0], CAROL_BIRTHDAY.isoformat())

    in_a = run_command(gateway, guild_a, ASKER, "get_birthday", guild_a.get_member(CAROL[0]))
    in_b = run_command(gateway, guild_b, ASKER, "get_birthday", guild_b.get_member(CAROL[0]))

    if CAROL_BIRTHDAY.strftime("%m-%d-%Y") not in text_of(in_a):
        pytest.fail(f"setup: guild A's lookup should show the birthday, got {in_a.sent!r}")
    [(reply, _)] = in_b.sent
    assert "has not set their birthday" in reply
    assert leaks(in_b.payloads, CAROL_BIRTHDAY, CAROL[1]) == []


def test_hostile_display_names_are_never_shown_and_ping_nobody(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, *HOSTILE_MEMBERS])
    for (user_id, _), birthday in zip(HOSTILE_MEMBERS, HOSTILE_BIRTHDAYS, strict=True):
        fake_redis.seed_birthday(GUILD_A, user_id, birthday)
    all_ids = [user_id for user_id, _ in HOSTILE_MEMBERS]

    # (view, the members it should list, the request payloads it sent)
    views = [
        ("list", all_ids, run_command(gateway, guild, ASKER, "list_birthdays").payloads),
        ("forecast", all_ids, run_command(gateway, guild, ASKER, "forecast_birthdays").payloads),
    ]
    for user_id in all_ids:
        interaction = run_command(gateway, guild, ASKER, "get_birthday", guild.get_member(user_id))
        views.append((f"get {user_id}", [user_id], interaction.payloads))
    asyncio.run(bot.check_upcoming_birthdays())
    views.append(("monthly post", all_ids, gateway.sent_to(gateway.general(guild))))

    for view, expected_ids, payloads in views:
        text = "\n".join(payload.get("content") or "" for payload in payloads)
        if not all(f"<@{user_id}>" in text for user_id in expected_ids):
            pytest.fail(f"setup: the {view} should list {expected_ids}, got {text!r}")
        for _, name in HOSTILE_MEMBERS:
            assert name not in text, view
        for payload in payloads:
            assert_pings_nobody(payload)


def test_monthly_post_shows_no_birth_year_or_age(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the post covers October to December
    guild = gateway.add_guild(GUILD_A, "Guild A", [(user_id, name) for user_id, name, _ in MONTHLY_MEMBERS])
    for user_id, _, birthday in MONTHLY_MEMBERS:
        fake_redis.seed_birthday(GUILD_A, user_id, birthday.isoformat())

    asyncio.run(bot.check_upcoming_birthdays())

    post = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    if not all(
        f"<@{user_id}>" in post and birthday.strftime("%B %d") in post for user_id, _, birthday in MONTHLY_MEMBERS
    ):
        pytest.fail(f"setup: the post should list each member with the month and day, got {post!r}")
    for _, _, birthday in MONTHLY_MEMBERS:
        age = 2026 - birthday.year
        assert str(birthday.year) not in post
        assert re.search(rf"\b{age}\b", post) is None


def test_monthly_post_pings_nobody(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st
    guild = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-15")

    asyncio.run(bot.check_upcoming_birthdays())

    posts = gateway.sent_to(gateway.general(guild))
    if len(posts) != 1 or f"<@{ALICE[0]}>" not in (posts[0].get("content") or ""):
        pytest.fail(f"setup: expected one post listing {ALICE[1]}, got {posts!r}")
    assert_pings_nobody(posts[0])


@pytest.mark.parametrize("upcoming", [True, False], ids=["a birthday upcoming", "none upcoming"])
def test_forecast_birthdays_answers_only_the_caller(upcoming, fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 2, 5))  # the forecast covers the next 90 days, to May 6
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    if upcoming:
        fake_redis.seed_birthday(GUILD_A, CAROL[0], CAROL_BIRTHDAY.isoformat())
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    asyncio.run(bot.forecast_birthdays.callback(interaction))

    sent = [content or "" for content, _ in interaction.sent]
    if len(sent) != 1 or (f"<@{CAROL[0]}>" in sent[0]) != upcoming:
        pytest.fail(f"setup: expected one follow-up, listing {CAROL[1]} only if upcoming, got {sent!r}")
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True]
