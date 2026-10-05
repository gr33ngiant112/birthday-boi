"""#13: Feb 29 birthdays in the next-birthday calculation.

check_upcoming_birthdays, get_birthday, list_birthdays and forecast_birthdays each
build the next birthday with datetime.date(year, month, day) from the stored month
and day. For Feb 29 that raises ValueError whenever the year is not a leap year,
and every caller swallows it. These expectations hold whether a fix maps Feb 29 to
Feb 28 or to Mar 1 in non-leap years.
"""

import asyncio
import datetime

import pytest

import bot

LEAP_DAY_BIRTHDAY = "2000-02-29"
LEAPLING = (200000000000001001, "leapling")
CONTROL = (200000000000001002, "control-user")
GUILD_ID = 300000000000005000


def xfail_13(why):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"#13: {why}")


@pytest.mark.parametrize(
    ("today", "next_birthday_year"),
    [
        pytest.param(datetime.date(2026, 9, 29), 2027, marks=xfail_13("date(2026, 2, 29) raises"), id="2026-09-29"),
        pytest.param(datetime.date(2027, 2, 28), 2027, marks=xfail_13("date(2027, 2, 29) raises"), id="2027-02-28"),
        pytest.param(datetime.date(2028, 2, 29), 2028, id="2028-02-29"),
        pytest.param(
            datetime.date(2028, 3, 1),
            2029,
            marks=xfail_13("the year + 1 branch raises on date(2029, 2, 29)"),
            id="2028-03-01",
        ),
    ],
)
def test_get_birthday_shows_a_leap_day_birthday(today, next_birthday_year, fake_redis, freeze_today, gateway):
    freeze_today(today)
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING])
    fake_redis.set(f"user:{LEAPLING[0]}:birthday", LEAP_DAY_BIRTHDAY)
    member = guild.get_member(LEAPLING[0])
    interaction = gateway.interaction(guild, member)

    asyncio.run(bot.get_birthday.callback(interaction, member))

    [(content, _)] = interaction.sent
    # The birthday table, not "An error occurred while retrieving the birthday."
    assert "02-29-2000" in content
    assert str(next_birthday_year) in content


@xfail_13("date(2026, 2, 29) raises and the row is skipped")
def test_list_birthdays_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.set(f"user:{LEAPLING[0]}:birthday", LEAP_DAY_BIRTHDAY)
    fake_redis.set(f"user:{CONTROL[0]}:birthday", "1990-08-21")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.list_birthdays.callback(interaction))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if CONTROL[1] not in text:
        pytest.fail(f"setup: the control user should be listed, got {text!r}")
    assert LEAPLING[1] in text


@xfail_13("date(2027, 2, 29) raises and the user is skipped")
def test_forecast_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    # From 2027-01-10, both Feb 28 and Mar 1 2027 fall inside the window, whether it is
    # the next two calendar months (today's code) or 60 or 90 days.
    freeze_today(datetime.date(2027, 1, 10))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.set(f"user:{LEAPLING[0]}:birthday", LEAP_DAY_BIRTHDAY)
    fake_redis.set(f"user:{CONTROL[0]}:birthday", "1990-02-10")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.forecast_birthdays.callback(interaction))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if CONTROL[1] not in text:
        pytest.fail(f"setup: the control user should be in the forecast, got {text!r}")
    assert LEAPLING[1] in text


@xfail_13("date(2027, 2, 29) raises and the user is skipped")
def test_monthly_post_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    # The task posts on the 1st; on 2027-02-01 its window is February to April.
    freeze_today(datetime.date(2027, 2, 1))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.set(f"user:{LEAPLING[0]}:birthday", LEAP_DAY_BIRTHDAY)
    fake_redis.set(f"user:{CONTROL[0]}:birthday", "1985-03-15")

    asyncio.run(bot.check_upcoming_birthdays())

    text = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    if CONTROL[1] not in text:
        pytest.fail(f"setup: the control user should be in the post, got {text!r}")
    assert LEAPLING[1] in text
