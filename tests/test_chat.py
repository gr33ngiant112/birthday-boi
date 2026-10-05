"""#16: setting a birthday by mentioning the bot in chat ("@bot my birthday is ...").

on_message strips "st", "nd", "rd" and "th" from the whole date text (so "august"
becomes "augu"), parses without removing the raw <@id> mention when spaCy finds no
DATE entity, and has no ISO format. The phrasings below are #16's acceptance list.
"""

import asyncio
import datetime

import pytest

import bot

SETTER = (200000000000001301, "setter")
GUILD_ID = 300000000000005000


def xfail_16(why):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"#16: {why}")


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        # Control: the same "%B %d %Y" shape as the August case, which parses today
        # because "december" contains no "st", "nd", "rd" or "th".
        pytest.param("December 25 1990", datetime.date(1990, 12, 25), id="December 25 1990"),
        pytest.param(
            "August 21st 1990",
            datetime.date(1990, 8, 21),
            marks=xfail_16("'august' loses its 'st' and becomes 'augu'"),
            id="August 21st 1990",
        ),
        pytest.param(
            "03-15-1990",
            datetime.date(1990, 3, 15),
            marks=xfail_16("not a spaCy DATE, and the fallback keeps the raw <@id> mention"),
            id="03-15-1990",
        ),
        pytest.param(
            "1990-03-15",
            datetime.date(1990, 3, 15),
            marks=xfail_16("no %Y-%m-%d format"),
            id="1990-03-15",
        ),
        pytest.param(
            "the 21st of March 1990",
            datetime.date(1990, 3, 21),
            marks=xfail_16("no format for day-first text like 'the 21st of March'"),
            id="the 21st of March 1990",
        ),
        pytest.param(
            "aug 5 1990",
            datetime.date(1990, 8, 5),
            marks=xfail_16("spaCy's DATE entity is '5 1990', without the month"),
            id="aug 5 1990",
        ),
        pytest.param(
            "5 August 1990",
            datetime.date(1990, 8, 5),
            marks=xfail_16("'august' becomes 'augu', and day-first has no format"),
            id="5 August 1990",
        ),
        pytest.param(
            "08/05/1990",
            datetime.date(1990, 8, 5),
            marks=xfail_16("not a spaCy DATE, and the fallback keeps the raw <@id> mention"),
            id="08_05_1990 slashes",
        ),
    ],
)
def test_chat_message_sets_birthday(phrase, expected, fake_redis, gateway):
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])
    bot_id = gateway.bot_user.id
    message = gateway.message(guild, SETTER, f"<@{bot_id}> my birthday is {phrase}", mentions=[gateway.mention_bot()])

    asyncio.run(bot.on_message(message))

    replies = [payload.get("content") or "" for payload in gateway.sent_to(message.channel)]
    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    stored = fake_redis.get(f"user:{SETTER[0]}:birthday")
    assert stored == expected.isoformat()
    assert replies[0].startswith("✅")
    assert expected.strftime("%m-%d-%Y") in replies[0]
