"""#7, #8, #9 and #10: what the bot shows to whom, and whom it can ping."""

import asyncio
import datetime
import json

import pytest

import bot

GUILD_A = 300000000000006000
GUILD_B = 300000000000007000
ALICE = (200000000000001101, "alice-only-in-a")
BOB = (200000000000001201, "bob-only-in-b")
ASKER = (200000000000001401, "asker")
ASKER_BIRTHDAY = datetime.date(1993, 7, 4)
HOSTILE_ID = 200000000000001402
CAROL = (200000000000001501, "carol-in-a")
CAROL_BIRTHDAY = datetime.date(1990, 3, 15)
DAVE = (200000000000001601, "dave-in-no-guild")
DAVE_BIRTHDAY = datetime.date(1985, 11, 2)
FIVE_MEMBERS = [(200000000000001701 + i, f"member-{i}-in-a") for i in range(5)]
FIVE_BIRTHDAYS = [datetime.date(1980 + i, i + 1, 11 + i) for i in range(5)]


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


def ask_in_guild(gateway, guild, author, text, others=()):
    """Send "@bot <text>" to on_message from author in the guild's #general, mentioning others too."""
    message = gateway.message(
        guild,
        author,
        f"<@{gateway.bot_user.id}> {text}",
        mentions=[gateway.mention_bot(), *(gateway.mention(*user) for user in others)],
    )
    asyncio.run(bot.on_message(message))
    return gateway.sent_to(message.channel)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#7: the monthly post is built from every guild's members and sent to every guild",
)
def test_monthly_post_lists_only_members_of_that_guild(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [BOB])
    fake_redis.set(f"user:{ALICE[0]}:birthday", "1990-10-15")
    fake_redis.set(f"user:{BOB[0]}:birthday", "1992-11-20")

    asyncio.run(bot.check_upcoming_birthdays())

    post_a = rendered(gateway.sent_to(gateway.general(guild_a)))
    post_b = rendered(gateway.sent_to(gateway.general(guild_b)))
    if ALICE[1] not in post_a or BOB[1] not in post_b:
        pytest.fail(f"setup: each guild's post should list its own member, got A={post_a} B={post_b}")
    assert ALICE[1] not in post_b
    assert str(ALICE[0]) not in post_b


def test_reply_echoing_a_display_name_cannot_ping_everyone(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER])
    bot_id = gateway.bot_user.id
    message = gateway.message(
        guild,
        ASKER,
        f"<@{bot_id}> when is <@{HOSTILE_ID}>'s birthday?",
        mentions=[gateway.mention_bot(), gateway.mention(HOSTILE_ID, "mallory", nick="@everyone")],
    )

    asyncio.run(bot.on_message(message))

    # The reply no longer echoes the display name (#8); the payload must still allow no pings.
    replies = gateway.sent_to(message.channel)
    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    # The request payload holds the client-wide allowed_mentions merged with the
    # call's own arguments. Without the field, Discord parses every mention.
    allowed = replies[0].get("allowed_mentions")
    assert allowed is not None
    assert "everyone" not in allowed.get("parse", [])
    assert "roles" not in allowed.get("parse", [])
    assert not allowed.get("roles")


@pytest.mark.parametrize(
    ("text", "others"),
    [
        pytest.param("my birthday is December 25 1990", [], id="set"),
        pytest.param("my birthday is a secret", [], id="set, no date found"),
        pytest.param("when is my birthday?", [], id="get"),
        pytest.param(f"when is <@{CAROL[0]}>'s birthday?", [CAROL], id="get_other"),
    ],
)
def test_chat_replies_ping_nobody(text, others, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())

    replies = ask_in_guild(gateway, guild, ASKER, text, others)

    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    assert_pings_nobody(replies[0])


def test_monthly_post_pings_nobody(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the task posts on the 1st
    guild = gateway.add_guild(GUILD_A, "Guild A", [ALICE])
    fake_redis.set(f"user:{ALICE[0]}:birthday", "1990-10-15")

    asyncio.run(bot.check_upcoming_birthdays())

    posts = gateway.sent_to(gateway.general(guild))
    if len(posts) != 1 or ALICE[1] not in (posts[0].get("content") or ""):
        pytest.fail(f"setup: expected one post listing {ALICE[1]}, got {posts!r}")
    assert_pings_nobody(posts[0])


def test_dm_asking_for_a_members_birthday_gets_no_reply(fake_redis, gateway):
    gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())
    message = gateway.dm_message(
        ASKER,
        f"<@{gateway.bot_user.id}> when is <@{CAROL[0]}>'s birthday?",
        mentions=[gateway.mention_bot(member=False), gateway.mention(*CAROL, member=False)],
    )

    asyncio.run(bot.on_message(message))

    assert gateway.sent == []


@pytest.mark.parametrize("bot_mentioned", [False, True], ids=["bot not mentioned", "bot mentioned too"])
def test_message_pinging_everyone_gets_no_reply(bot_mentioned, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())
    content = f"@everyone when is <@{CAROL[0]}>'s birthday?"
    mentions = [gateway.mention(*CAROL)]
    if bot_mentioned:
        content = f"<@{gateway.bot_user.id}> {content}"
        mentions.insert(0, gateway.mention_bot())
    # Without the Message Content intent, Discord may deliver the first case with empty
    # content (#8). The content is set so that the test checks the mention check itself.
    message = gateway.message(guild, ASKER, content, mentions=mentions, mention_everyone=True)

    asyncio.run(bot.on_message(message))

    assert gateway.sent == []


def test_message_not_mentioning_the_bot_gets_no_reply(fake_redis, gateway):
    # Passes on develop too. It is the only test that fails if on_message stops
    # requiring a direct mention of the bot. As above, the content is set even
    # though Discord may deliver such a message without it.
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())
    message = gateway.message(guild, ASKER, f"when is <@{CAROL[0]}>'s birthday?", mentions=[gateway.mention(*CAROL)])

    asyncio.run(bot.on_message(message))

    assert gateway.sent == []


def test_asking_for_a_members_birthday_shows_no_date_or_name(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())

    replies = ask_in_guild(gateway, guild, ASKER, f"when is <@{CAROL[0]}>'s birthday?", [CAROL])

    assert len(replies) == 1
    assert leaks(replies, CAROL_BIRTHDAY, CAROL[1]) == []


def test_asking_for_a_non_members_birthday_shows_no_date(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER])
    fake_redis.set(f"user:{DAVE[0]}:birthday", DAVE_BIRTHDAY.isoformat())
    # DAVE is not in the guild, so his mention carries no member data.
    message = gateway.message(
        guild,
        ASKER,
        f"<@{gateway.bot_user.id}> when is <@{DAVE[0]}>'s birthday?",
        mentions=[gateway.mention_bot(), gateway.mention(*DAVE, member=False)],
    )

    asyncio.run(bot.on_message(message))

    replies = gateway.sent_to(message.channel)
    assert len(replies) == 1
    assert leaks(replies, DAVE_BIRTHDAY, DAVE[1]) == []


def test_asking_for_five_birthdays_sends_at_most_one_reply_without_dates(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, *FIVE_MEMBERS])
    for (user_id, _), birthday in zip(FIVE_MEMBERS, FIVE_BIRTHDAYS):
        fake_redis.set(f"user:{user_id}:birthday", birthday.isoformat())
    targets = " ".join(f"<@{user_id}>" for user_id, _ in FIVE_MEMBERS)

    replies = ask_in_guild(gateway, guild, ASKER, f"when is the birthday of {targets}?", FIVE_MEMBERS)

    assert len(replies) <= 1
    for (_, name), birthday in zip(FIVE_MEMBERS, FIVE_BIRTHDAYS):
        assert leaks(replies, birthday, name) == []


def test_asking_for_your_own_birthday_shows_no_date(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER])
    fake_redis.set(f"user:{ASKER[0]}:birthday", ASKER_BIRTHDAY.isoformat())

    replies = ask_in_guild(gateway, guild, ASKER, "when is my birthday?")

    assert len(replies) == 1
    assert leaks(replies, ASKER_BIRTHDAY, ASKER[1]) == []


@pytest.mark.parametrize("upcoming", [True, False], ids=["a birthday upcoming", "none upcoming"])
def test_forecast_birthdays_answers_only_the_caller(upcoming, fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 2, 5))  # the forecast covers March and April
    guild = gateway.add_guild(GUILD_A, "Guild A", [ASKER, CAROL])
    if upcoming:
        fake_redis.set(f"user:{CAROL[0]}:birthday", CAROL_BIRTHDAY.isoformat())
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    asyncio.run(bot.forecast_birthdays.callback(interaction))

    sent = [content or "" for content, _ in interaction.sent]
    if len(sent) != 1 or (CAROL[1] in sent[0]) != upcoming:
        pytest.fail(f"setup: expected one follow-up, listing {CAROL[1]} only if upcoming, got {sent!r}")
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True]
