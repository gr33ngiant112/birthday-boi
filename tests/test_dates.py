"""#13 and #22: the next birthday, the forecast window and the birth-year range.

#13: check_upcoming_birthdays, get_birthday, list_birthdays and forecast_birthdays
each built the next birthday with datetime.date(year, month, day) from the stored
month and day. For Feb 29 that raised ValueError whenever the year was not a leap
year, and every caller swallowed it, so leap-day members went missing. The view
tests hold whether a fix maps Feb 29 to Feb 28 or to Mar 1 in non-leap years; the
next_occurrence() tests pin Feb 28.

#22: both set paths stored any year, so a typo such as 2099 for 1999 showed as a
nonsense age, and /forecast_birthdays said "the next 60 and 90 days" but listed the
next two calendar months.
"""

import asyncio
import datetime

import pytest

import bot

LEAP_DAY_BIRTHDAY = "2000-02-29"
LEAPLING = (200000000000001001, "leapling")
CONTROL = (200000000000001002, "control-user")
MEMBER = (200000000000001003, "member")
SETTER = (200000000000001004, "setter")
GUILD_ID = 300000000000005000
# The year-range tests run on this day: the oldest birth year accepted is 1906.
SET_TODAY = datetime.date(2026, 10, 5)


def run_command(gateway, guild, user, command, *args):
    """Run a slash command as user, a (user_id, name) member of guild; return the FakeInteraction."""
    interaction = gateway.interaction(guild, guild.get_member(user[0]))
    asyncio.run(getattr(bot, command).callback(interaction, *args))
    return interaction


def text_of(interaction):
    return "\n".join(content or "" for content, _ in interaction.sent)


@pytest.mark.parametrize(
    ("today", "next_birthday_year"),
    [
        pytest.param(datetime.date(2026, 9, 29), 2027, id="2026-09-29"),
        pytest.param(datetime.date(2027, 2, 28), 2027, id="2027-02-28"),
        pytest.param(datetime.date(2028, 2, 29), 2028, id="2028-02-29"),
        pytest.param(datetime.date(2028, 3, 1), 2029, id="2028-03-01"),
    ],
)
def test_get_birthday_shows_a_leap_day_birthday(today, next_birthday_year, fake_redis, freeze_today, gateway):
    freeze_today(today)
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    member = guild.get_member(LEAPLING[0])
    interaction = gateway.interaction(guild, member)

    asyncio.run(bot.get_birthday.callback(interaction, member))

    [(content, _)] = interaction.sent
    # The birthday, not "An error occurred while retrieving the birthday."
    assert "02-29-2000" in content
    assert str(next_birthday_year) in content


def test_list_birthdays_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1990-08-21")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.list_birthdays.callback(interaction))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be listed, got {text!r}")
    assert f"<@{LEAPLING[0]}>" in text


def test_forecast_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    # From 2027-01-10, Feb 28 and Mar 1 2027 are 49 and 50 days away, inside the
    # forecast's 90 days (and inside the next two calendar months, its old window).
    freeze_today(datetime.date(2027, 1, 10))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1990-02-10")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.forecast_birthdays.callback(interaction))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the forecast, got {text!r}")
    assert f"<@{LEAPLING[0]}>" in text


def test_monthly_post_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    # The task posts on the 1st; on 2027-02-01 its window is February to April.
    freeze_today(datetime.date(2027, 2, 1))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1985-03-15")

    asyncio.run(bot.check_upcoming_birthdays())

    text = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the post, got {text!r}")
    assert f"<@{LEAPLING[0]}>" in text


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        pytest.param(datetime.date(2026, 9, 29), datetime.date(2027, 2, 28), id="2026-09-29"),
        pytest.param(datetime.date(2027, 2, 28), datetime.date(2027, 2, 28), id="2027-02-28"),
        pytest.param(datetime.date(2027, 3, 1), datetime.date(2028, 2, 29), id="2027-03-01"),
        pytest.param(datetime.date(2028, 2, 29), datetime.date(2028, 2, 29), id="2028-02-29"),
        pytest.param(datetime.date(2028, 3, 1), datetime.date(2029, 2, 28), id="2028-03-01"),
    ],
)
def test_next_occurrence_of_a_leap_day_birthday(today, expected):
    # In a year without Feb 29, the birthday falls on Feb 28.
    assert bot.next_occurrence(2, 29, today) == expected


def test_next_occurrence_of_every_month_and_day_from_every_day_of_four_years():
    # The expected dates come from a walk back through the calendar, starting a year
    # after the last today so that every month and day has been seen: for each day,
    # the nearest day on or after it with each month and day, where the last day of
    # February also stands for Feb 29. From 2026 to 2029, today falls in the leap
    # year 2028 and in common years followed by a common year and by a leap year.
    one_day = datetime.timedelta(days=1)
    first_today, last_today = datetime.date(2026, 1, 1), datetime.date(2029, 12, 31)
    days_of_a_leap_year = (datetime.date(2028, 1, 1) + n * one_day for n in range(366))
    every_month_and_day = [(day.month, day.day) for day in days_of_a_leap_year]
    nearest = {}
    wrong = []
    day = datetime.date(2030, 12, 31)
    while day >= first_today:
        nearest[(day.month, day.day)] = day
        if day.month == 2 and (day + one_day).month == 3:
            nearest[(2, 29)] = day
        if day <= last_today:
            for month, day_of_month in every_month_and_day:
                got = bot.next_occurrence(month, day_of_month, day)
                if got != nearest[(month, day_of_month)]:
                    wrong.append(((month, day_of_month), day, got, nearest[(month, day_of_month)]))
        day -= one_day

    # (month and day, today, next_occurrence's answer, the expected date)
    assert not wrong, f"{len(wrong)} wrong, the first: {wrong[:5]}"


@pytest.mark.parametrize(
    ("today", "birthday", "shown"),
    [
        pytest.param(datetime.date(2026, 9, 29), "1990-09-29", True, id="today"),
        pytest.param(datetime.date(2026, 9, 29), "1990-09-30", True, id="1 day away"),
        pytest.param(datetime.date(2026, 9, 29), "1990-12-28", True, id="90 days away"),
        pytest.param(datetime.date(2026, 9, 29), "1990-12-29", False, id="91 days away"),
        pytest.param(datetime.date(2026, 9, 29), "1990-09-28", False, id="yesterday, 364 days away"),
        pytest.param(datetime.date(2026, 12, 15), "1990-01-10", True, id="next year, 26 days away"),
        pytest.param(datetime.date(2026, 12, 15), "1990-03-15", True, id="next year, 90 days away"),
        pytest.param(datetime.date(2026, 12, 15), "1990-03-16", False, id="next year, 91 days away"),
    ],
)
def test_forecast_shows_birthdays_from_today_to_90_days_away(today, birthday, shown, fake_redis, freeze_today, gateway):
    freeze_today(today)
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], birthday)
    # 30 days away from either today, which the old two-calendar-month window showed too.
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], (today + datetime.timedelta(days=30)).replace(year=1985).isoformat())

    text = text_of(run_command(gateway, guild, CONTROL, "forecast_birthdays"))

    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the forecast, got {text!r}")
    assert (f"<@{MEMBER[0]}>" in text) is shown


def test_forecast_row_for_a_birthday_next_year(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 12, 15))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-01-10")

    interaction = run_command(gateway, guild, MEMBER, "forecast_birthdays")

    # Jan 10 2027 is a Sunday, and a member born in 1990 turns 37 that day. Only the asker sees it.
    row = f"<@{MEMBER[0]}> - Sunday, January 10 - turning 37"
    assert interaction.sent == [(f"🎉 **Upcoming Birthdays:**\n{row}", {"ephemeral": True})]


def test_forecast_description_and_empty_state_say_90_days(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-12-29")  # 91 days away

    interaction = run_command(gateway, guild, MEMBER, "forecast_birthdays")

    # The description Discord shows, from the payload tree.sync() sends.
    assert bot.forecast_birthdays.to_dict(bot.client.tree)["description"] == "Show birthdays in the next 90 days"
    assert [content for content, _ in interaction.sent] == ["❌ No birthdays in the next 90 days."]


def set_birthday(set_with, gateway, guild, birthday):
    """Set SETTER's birthday in guild with /set_birthday or a chat message; return the text of each reply."""
    if set_with == "chat":
        # The dates below use month names without "st", "nd", "rd" or "th", which
        # the chat parser strips from the date text (#16).
        phrase = f"{birthday.strftime('%B')} {birthday.day} {birthday.year}"
        message = gateway.message(
            guild, SETTER, f"<@{gateway.bot_user.id}> my birthday is {phrase}", mentions=[gateway.mention_bot()]
        )
        asyncio.run(bot.on_message(message))
        return [payload.get("content") or "" for payload in gateway.sent_to(message.channel)]
    interaction = run_command(gateway, guild, SETTER, "set_birthday", birthday.strftime("%m-%d-%Y"))
    return [content or "" for content, _ in interaction.sent]


@pytest.mark.parametrize("set_with", ["slash command", "chat"])
@pytest.mark.parametrize(
    ("birthday", "why"),
    [
        pytest.param(datetime.date(2026, 10, 6), "in the future", id="tomorrow"),
        pytest.param(datetime.date(2099, 12, 25), "in the future", id="2099"),
        pytest.param(datetime.date(1905, 12, 31), "more than 120 years ago", id="1905"),
    ],
)
def test_setting_an_impossible_birth_date_is_refused_and_not_stored(
    birthday, why, set_with, fake_redis, freeze_today, gateway
):
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(set_with, gateway, guild, birthday)

    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    [reply] = replies
    assert reply.startswith("❌")
    assert why in reply
    # The chat reply is public, so it never repeats the date in any form bot.py writes one.
    forms = [birthday.isoformat(), birthday.strftime("%m-%d-%Y"), birthday.strftime("%m-%d"), str(birthday.year)]
    assert [form for form in forms if form in reply] == []
    assert fake_redis.contents() == {}


@pytest.mark.parametrize("set_with", ["slash command", "chat"])
@pytest.mark.parametrize(
    "birthday",
    [
        pytest.param(SET_TODAY, id="today"),
        pytest.param(datetime.date(1906, 1, 1), id="1906"),
        pytest.param(datetime.date(1990, 12, 25), id="1990"),
    ],
)
def test_setting_a_birth_date_in_range_stores_it(birthday, set_with, fake_redis, freeze_today, gateway):
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(set_with, gateway, guild, birthday)

    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    assert replies[0].startswith("✅")
    assert fake_redis.get(f"user:{SETTER[0]}:birthday") == birthday.isoformat()
