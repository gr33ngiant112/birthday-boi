"""#7 and #10: what the bot shows to whom, and whom it can ping."""

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
HOSTILE_ID = 200000000000001402


def rendered(payloads):
    """Everything in the request payloads except the random nonce, as one string."""
    return json.dumps([{k: v for k, v in p.items() if k != "nonce"} for p in payloads], ensure_ascii=False)


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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#10: the client sets no allowed_mentions and replies pass mention_author=True, "
    "so discord.py sends AllowedMentions() with everyone and roles allowed",
)
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

    # Today the reply is "❌ @everyone hasn't set their birthday yet."
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
