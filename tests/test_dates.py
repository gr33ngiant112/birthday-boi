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

#33 and #9: /birthday set takes the month, the day and an optional year as typed options,
whose ranges Discord enforces, and refuses a date that does not exist. Without a year it
stores ISO 8601's "--MM-DD", which every view reads.
"""

import asyncio
import datetime

import pytest

import bot
import core

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
def test_birthday_show_shows_a_leap_day_birthday(today, next_birthday_year, fake_redis, freeze_today, gateway):
    freeze_today(today)
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING])
    # With the show_age opt-in, so the reply has the whole date.
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY, show_age=True)
    member = guild.get_member(LEAPLING[0])
    interaction = gateway.interaction(guild, member)

    asyncio.run(bot.birthday_show.callback(interaction, member))

    [(content, _)] = interaction.sent
    # The birthday, not "An error occurred while retrieving the birthday."
    assert "February 29, 2000" in content
    assert str(next_birthday_year) in content


def test_birthday_list_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1990-08-21")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.birthday_list.callback(interaction))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be listed, got {text!r}")
    assert f"<@{LEAPLING[0]}>" in text


def test_birthday_upcoming_includes_a_leap_day_birthday(fake_redis, freeze_today, gateway):
    # From 2027-01-10, Feb 28 and Mar 1 2027 are 49 and 50 days away, inside the next 90
    # days (and inside the next two calendar months, /forecast_birthdays' old window).
    freeze_today(datetime.date(2027, 1, 10))
    guild = gateway.add_guild(GUILD_ID, "Guild", [LEAPLING, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, LEAPLING[0], LEAP_DAY_BIRTHDAY)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1990-02-10")
    interaction = gateway.interaction(guild, guild.get_member(CONTROL[0]))

    asyncio.run(bot.birthday_upcoming.callback(interaction, 90))

    text = "\n".join(content or "" for content, _ in interaction.sent)
    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the list, got {text!r}")
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
        # 2100 is divisible by 4 but has no Feb 29.
        pytest.param(datetime.date(2099, 3, 1), datetime.date(2100, 2, 28), id="2099-03-01"),
    ],
)
def test_next_occurrence_of_a_leap_day_birthday(today, expected):
    # In a year without Feb 29, the birthday falls on Feb 28.
    assert core.next_occurrence(2, 29, today) == expected


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
                got = core.next_occurrence(month, day_of_month, day)
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
def test_birthday_upcoming_shows_birthdays_from_today_to_90_days_away(
    today, birthday, shown, fake_redis, freeze_today, gateway
):
    freeze_today(today)
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], birthday)
    # 30 days away from either today, which the old two-calendar-month window showed too.
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], (today + datetime.timedelta(days=30)).replace(year=1985).isoformat())

    text = text_of(run_command(gateway, guild, CONTROL, "birthday_upcoming", 90))

    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the list, got {text!r}")
    assert (f"<@{MEMBER[0]}>" in text) is shown


@pytest.mark.parametrize(
    ("birthday", "shown"),
    [
        pytest.param("1990-09-29", True, id="today"),
        pytest.param("1990-10-29", True, id="30 days away"),
        pytest.param("1990-10-30", False, id="31 days away"),
    ],
)
def test_birthday_upcoming_shows_the_next_30_days_by_default(birthday, shown, fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], birthday)
    fake_redis.seed_birthday(GUILD_ID, CONTROL[0], "1985-10-09")  # 10 days away

    text = text_of(run_command(gateway, guild, CONTROL, "birthday_upcoming"))

    if f"<@{CONTROL[0]}>" not in text:
        pytest.fail(f"setup: the control user should be in the list, got {text!r}")
    assert (f"<@{MEMBER[0]}>" in text) is shown


def test_birthday_upcoming_row_for_a_birthday_next_year(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 12, 15))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    # With the show_age opt-in, so the row has the age.
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-01-10", show_age=True)

    interaction = run_command(gateway, guild, MEMBER, "birthday_upcoming")

    # Jan 10 2027 is a Sunday, and a member born in 1990 turns 37 that day. Only the asker sees it.
    row = f"<@{MEMBER[0]}> - Sunday, January 10 - turning 37"
    assert interaction.sent == [(f"🎉 **Upcoming Birthdays:**\n{row}", {"ephemeral": True})]


@pytest.mark.parametrize(
    ("days", "birthday", "no_birthdays"),
    [
        pytest.param((), "1990-10-30", "❌ No birthdays in the next 30 days.", id="default, 31 days away"),
        pytest.param((90,), "1990-12-29", "❌ No birthdays in the next 90 days.", id="90 days, 91 days away"),
        pytest.param((1,), "1990-10-01", "❌ No birthdays in the next 1 day.", id="1 day, 2 days away"),
    ],
)
def test_birthday_upcoming_description_and_empty_state_say_how_many_days(
    days, birthday, no_birthdays, fake_redis, freeze_today, gateway
):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], birthday)

    interaction = run_command(gateway, guild, MEMBER, "birthday_upcoming", *days)

    # The description and option Discord shows, from the payload tree.sync() sends.
    payload = bot.birthday_upcoming.to_dict(bot.client.tree)
    assert payload["description"] == "Show the birthdays in the next 30 days, or in the days you choose"
    [option] = payload["options"]
    assert (option["name"], option["required"], option["min_value"], option["max_value"]) == ("days", False, 1, 366)
    assert [content for content, _ in interaction.sent] == [no_birthdays]


def set_birthday(gateway, guild, *args):
    """Run /birthday set with args (month, day and year) as SETTER in guild; return (content, ephemeral)
    for each reply."""
    interaction = run_command(gateway, guild, SETTER, "birthday_set", *args)
    return [(content or "", kwargs.get("ephemeral")) for content, kwargs in interaction.sent]


@pytest.mark.parametrize(
    ("birthday", "why"),
    [
        pytest.param(datetime.date(2026, 10, 6), "in the future", id="tomorrow"),
        pytest.param(datetime.date(2099, 12, 25), "in the future", id="2099"),
        pytest.param(datetime.date(2999, 1, 1), "in the future", id="2999"),  # #16's 01-01-2999
        pytest.param(datetime.date(1905, 12, 31), "more than 120 years ago", id="1905"),
    ],
)
def test_setting_an_impossible_birth_date_is_refused_and_not_stored(birthday, why, fake_redis, freeze_today, gateway):
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(gateway, guild, birthday.month, birthday.day, birthday.year)

    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    [(reply, _)] = replies
    assert reply.startswith("❌")
    assert why in reply
    # The reply never repeats the date in any form bot.py writes one.
    forms = [birthday.isoformat(), birthday.strftime("%m-%d-%Y"), birthday.strftime("%m-%d"), str(birthday.year)]
    assert [form for form in forms if form in reply] == []
    assert fake_redis.contents() == {}


@pytest.mark.parametrize(
    "birthday",
    [
        pytest.param(SET_TODAY, id="today"),
        pytest.param(datetime.date(1906, 1, 1), id="1906"),
        pytest.param(datetime.date(1990, 12, 25), id="1990"),
    ],
)
def test_setting_a_birth_date_in_range_stores_it(birthday, fake_redis, freeze_today, gateway):
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(gateway, guild, birthday.month, birthday.day, birthday.year)

    if len(replies) != 1:
        pytest.fail(f"setup: expected one reply, got {replies!r}")
    assert replies[0][0].startswith("✅")
    assert fake_redis.get(f"user:{SETTER[0]}:birthday") == birthday.isoformat()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param((2, 30), id="February 30"),
        pytest.param((4, 31, 1990), id="April 31"),
        pytest.param((2, 29, 2001), id="February 29 in 2001, not a leap year"),
    ],
)
def test_a_date_that_does_not_exist_is_refused_and_not_stored(args, fake_redis, freeze_today, gateway):
    # #33: Discord offers every month with days 1 to 31, so the handler refuses these.
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(gateway, guild, *args)

    assert replies == [(bot.NO_SUCH_DATE, True)]
    assert fake_redis.contents() == {}


@pytest.mark.parametrize(
    ("month", "day", "stored", "shown"),
    [
        pytest.param(12, 25, "--12-25", "December 25", id="December 25"),
        # LEAP_YEAR stands in for the missing year, so Feb 29 is a date.
        pytest.param(2, 29, "--02-29", "February 29", id="February 29"),
    ],
)
def test_a_birthday_without_a_year_is_stored_as_month_and_day(
    month, day, stored, shown, fake_redis, freeze_today, gateway
):
    # #9 and #33: the year is optional. Without one, the date is ISO 8601's "--MM-DD".
    freeze_today(SET_TODAY)
    guild = gateway.add_guild(GUILD_ID, "Guild", [SETTER])

    replies = set_birthday(gateway, guild, month, day)

    assert replies == [(f"✅ Your birthday has been set to {shown}. Others see only the month and day.", True)]
    assert fake_redis.contents() == {
        f"user:{SETTER[0]}:birthday": stored,
        f"guild:{GUILD_ID}:birthdays": {str(SETTER[0])},
        f"user:{SETTER[0]}:guilds": {str(GUILD_ID)},
    }


@pytest.mark.parametrize("view", ["birthday_show", "birthday_list", "birthday_upcoming", "monthly post"])
def test_every_view_reads_a_birthday_stored_without_a_year(view, fake_redis, freeze_today, gateway):
    # On Dec 1 the monthly task posts, and Dec 25, a Friday, is 24 days away: inside
    # /birthday upcoming's 30 days. The member opted in with show_age, but there is no year
    # and so no age to show.
    freeze_today(datetime.date(2026, 12, 1))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER, CONTROL])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "--12-25", show_age=True)

    if view == "monthly post":
        asyncio.run(bot.check_upcoming_birthdays())
        text = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    else:
        args = (guild.get_member(MEMBER[0]),) if view == "birthday_show" else ()
        text = text_of(run_command(gateway, guild, CONTROL, view, *args))

    next_birthday = "next birthday Friday, December 25 2026"
    expected = {
        "birthday_show": f"🎂 **<@{MEMBER[0]}>'s Birthday:**\nDecember 25 - {next_birthday}",
        "birthday_list": f"🎉 **Server Birthdays:**\n<@{MEMBER[0]}> - December 25 - {next_birthday}",
        "birthday_upcoming": f"🎉 **Upcoming Birthdays:**\n<@{MEMBER[0]}> - Friday, December 25",
        "monthly post": f"🎉 **Upcoming Birthdays:**\n<@{MEMBER[0]}> - Friday, December 25",
    }
    assert text == expected[view]
